"""Merging another DBC into the open one.

``analyse`` compares the incoming database with the open one, message by
message and value table by value table, and suggests what to do with each.
``apply`` carries out a plan (the user's choices) on the open database.

Messages are matched by name. A message the open DBC does not have is new; the
same name and frame ID with the same content is identical; the same name and
frame ID with other content differs; another message already owning the frame
ID, or the name living at another frame ID, is a clash the user has to settle
(skip, replace, or add under a new ID or name).

Message attributes are not copied across: attribute definitions differ between
DBCs (send types are indices into a list), so only the send type and cycle
time are carried over, translated to the open DBC's definitions. Anything else
is reported as dropped.
"""

from __future__ import annotations

import copy
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

import cantools
from cantools.database.can import Database, Message, Signal

from . import ops
from .ops import OpError

ACTIONS = ("skip", "add", "replace")
STANDARD_ATTRIBUTES = {"GenMsgCycleTime", "GenMsgSendType", "GenSigStartValue", "Baudrate"}


# ---------- reading the incoming file ----------

def parse(text: str) -> Database:
    if not isinstance(text, str) or not text.strip():
        raise OpError("The file to merge is empty")
    try:
        return cantools.database.load_string(text, database_format="dbc", strict=False)
    except Exception as exc:
        raise OpError(f"That file is not a DBC cantools can read: {exc}") from exc


# ---------- comparing ----------

def _signal_sig(s: Signal) -> Tuple:
    return (s.name, s.start, s.length, s.byte_order, s.is_signed, s.is_float, s.scale, s.offset,
            s.minimum, s.maximum, s.unit or "", tuple(s.receivers), s.comment or "",
            s.raw_initial, tuple(sorted((int(k), str(v)) for k, v in (s.choices or {}).items())),
            s.is_multiplexer, tuple(s.multiplexer_ids or ()), s.multiplexer_signal)


SIGNAL_FIELDS = ("name", "start", "length", "byte order", "signed", "float", "factor", "offset",
                 "min", "max", "unit", "receivers", "comment", "start value", "values",
                 "multiplexer", "multiplexer ids", "multiplexed by")


def _message_fields(m: Message) -> Dict[str, Any]:
    return {"frame ID": m.frame_id, "length": m.length, "extended": m.is_extended_frame,
            "CAN FD": m.is_fd, "senders": tuple(m.senders), "send type": m.send_type or "",
            "cycle time": m.cycle_time, "comment": m.comment or ""}


def describe_difference(mine: Message, theirs: Message) -> List[str]:
    out = []
    a, b = _message_fields(mine), _message_fields(theirs)
    for k in a:
        if a[k] != b[k]:
            out.append(f"{k}: {_show(a[k])} -> {_show(b[k])}")
    sa = {s.name: s for s in mine.signals}
    sb = {s.name: s for s in theirs.signals}
    for n in sb:
        if n not in sa:
            out.append(f"signal {n} added")
    for n in sa:
        if n not in sb:
            out.append(f"signal {n} removed")
    for n in sa:
        if n in sb and _signal_sig(sa[n]) != _signal_sig(sb[n]):
            fields = [f for f, x, y in zip(SIGNAL_FIELDS, _signal_sig(sa[n]), _signal_sig(sb[n])) if x != y]
            out.append(f"signal {n}: {', '.join(fields)} differ")
    return out


def _show(v: Any) -> str:
    if isinstance(v, tuple):
        return ", ".join(v) or "none"
    return str(v) if v != "" else "none"


def _table(entries) -> Dict[int, str]:
    return {int(k): str(v) for k, v in entries.items()}


def _tables(db: Database) -> Dict[str, Any]:
    return db.dbc.value_tables if db.dbc is not None and db.dbc.value_tables else {}


def _by_id(db: Database, fid: int) -> Optional[Message]:
    return next((m for m in db.messages if m.frame_id == fid), None)


def _by_name(db: Database, name: str) -> Optional[Message]:
    return next((m for m in db.messages if m.name == name), None)


def _free_id(db: Database, start: int, taken: set) -> int:
    limit = 0x1FFFFFFF if start > ops.MAX_STANDARD_ID else ops.MAX_STANDARD_ID
    fid = start
    while fid <= limit and (_by_id(db, fid) is not None or fid in taken):
        fid += 1
    if fid > limit:
        raise OpError("No free frame ID above the incoming one")
    return fid


def _dropped_attributes(m: Message) -> List[str]:
    names = set((m.dbc.attributes if m.dbc else {}).keys())
    for s in m.signals:
        names |= set((s.dbc.attributes if s.dbc else {}).keys())
    return sorted(names - STANDARD_ATTRIBUTES)


# ---------- analysis ----------

def analyse(db: Database, incoming: Database) -> Dict[str, Any]:
    taken: set = set()
    messages = []
    for theirs in sorted(incoming.messages, key=lambda m: m.frame_id):
        same_name = _by_name(db, theirs.name)
        same_id = _by_id(db, theirs.frame_id)
        item: Dict[str, Any] = {
            "name": theirs.name, "frame_id": theirs.frame_id, "hex_id": f"0x{theirs.frame_id:03X}",
            "length": theirs.length, "signal_count": len(theirs.signals),
            "senders": list(theirs.senders), "clashes": [], "changes": [],
            "dropped_attributes": _dropped_attributes(theirs),
        }
        if same_name is None and same_id is None:
            item["status"], item["suggested"] = "new", {"action": "add"}
            taken.add(theirs.frame_id)
        elif same_name is not None and same_id is same_name:
            changes = describe_difference(same_name, theirs)
            item["status"] = "differs" if changes else "identical"
            item["changes"] = changes
            item["suggested"] = {"action": "skip"}
            item["clashes"] = [same_name.name]
        else:
            item["status"] = "clash"
            if same_id is not None:
                item["clashes"].append(f"{same_id.name} already uses 0x{theirs.frame_id:03X}")
            if same_name is not None:
                item["clashes"].append(f"{same_name.name} exists at 0x{same_name.frame_id:03X}")
            item["suggested"] = {"action": "skip"}
            if same_id is not None and same_name is None:
                try:
                    item["free_id"] = _free_id(db, theirs.frame_id, taken)
                except OpError:
                    pass
        messages.append(item)

    tables = []
    mine_tables = _tables(db)
    for name, entries in _tables(incoming).items():
        if name not in mine_tables:
            status, suggested = "new", "add"
        elif _table(mine_tables[name]) == _table(entries):
            status, suggested = "identical", "skip"
        else:
            status, suggested = "differs", "skip"
        tables.append({"name": name, "status": status, "suggested": {"action": suggested},
                       "entries": {str(int(k)): str(v) for k, v in entries.items()}})

    mine_nodes = {n.name for n in db.nodes}
    nodes = [{"name": n.name, "status": "existing" if n.name in mine_nodes else "new"}
             for n in incoming.nodes]
    return {"messages": messages, "tables": tables, "nodes": nodes,
            "summary": {k: sum(1 for m in messages if m["status"] == k)
                        for k in ("new", "identical", "differs", "clash")}}


# ---------- applying ----------

def _copy_message(db: Database, theirs: Message, name: str, fid: int) -> Message:
    """A copy of ``theirs`` fit for ``db``: attributes translated, not shared."""
    send_type = theirs.send_type
    cycle = theirs.cycle_time
    dup = copy.deepcopy(theirs)
    dup.dbc = ops._new_dbc()
    for s in dup.signals:
        s.dbc = ops._new_dbc()
    dup = ops._replace_message(ops._Shim(db, dup), dup, name=name, frame_id=fid,
                               is_extended_frame=theirs.is_extended_frame or fid > ops.MAX_STANDARD_ID,
                               cycle_time=cycle, send_type=send_type)
    d_def = ops._send_type_definition(db)
    if send_type and d_def is not None:
        if send_type not in d_def.choices:
            raise OpError(f"{theirs.name}: send type {send_type} does not exist in this DBC")
        ops._set_send_type_attribute(db, dup, send_type)
    cycle_def = db.dbc.attribute_definitions.get("GenMsgCycleTime") if db.dbc else None
    if cycle is not None and (cycle_def is None or cycle != ops.default_cycle_time(db)):
        dup.cycle_time = cycle
    return dup


def _choice(plan: Dict[str, Any], section: str, name: str, default: str) -> Dict[str, Any]:
    entry = (plan.get(section) or {}).get(name) or {"action": default}
    action = entry.get("action", default)
    if action not in ACTIONS:
        raise OpError(f"{name}: unknown action {action!r}")
    return {**entry, "action": action}


def apply(db: Database, incoming: Database, plan: Dict[str, Any]):
    """Runs the plan on ``db``. Returns (selection hint, persistence check)."""
    if not isinstance(plan, dict):
        raise OpError("The merge plan is missing")
    added: List[Tuple[str, int, int]] = []     # name, frame id, signal count
    added_tables: List[Tuple[str, Dict[int, str]]] = []
    needed_nodes: List[str] = []

    for theirs in sorted(incoming.messages, key=lambda m: m.frame_id):
        choice = _choice(plan, "messages", theirs.name, "skip")
        if choice["action"] == "skip":
            continue
        name = ops._name(choice.get("new_name") or theirs.name, "Message name")
        fid = ops._int(choice.get("new_id", theirs.frame_id), "Frame ID", 0, ops.MAX_EXTENDED_ID)
        if choice["action"] == "replace":
            for old in {id(m): m for m in (_by_name(db, theirs.name), _by_id(db, fid)) if m}.values():
                db.messages.remove(old)
        if _by_name(db, name) is not None:
            raise OpError(f"{name}: a message with that name already exists; choose replace or a new name")
        if _by_id(db, fid) is not None:
            raise OpError(f"{theirs.name}: frame ID 0x{fid:X} is already used; choose replace or a new ID")
        for n in list(theirs.senders) + [r for s in theirs.signals for r in s.receivers]:
            if n not in needed_nodes:
                needed_nodes.append(n)
        # Nodes must exist before the message is rebuilt against this database.
        for n in needed_nodes:
            if n not in ops._node_names(db):
                src = next((x for x in incoming.nodes if x.name == n), None)
                db.nodes.append(type(src)(n, src.comment if src else None, dbc_specifics=ops._new_dbc())
                                if src else _plain_node(n))
        dup = _copy_message(db, theirs, name, fid)
        db.messages.append(dup)
        added.append((name, fid, len(theirs.signals)))

    mine_tables = _tables(db)
    for theirs_name, entries in _tables(incoming).items():
        choice = _choice(plan, "tables", theirs_name, "skip")
        if choice["action"] == "skip":
            continue
        name = ops._name(choice.get("new_name") or theirs_name, "Table name")
        if name in mine_tables and choice["action"] != "replace":
            raise OpError(f"{name}: a value table with that name already exists; choose replace or a new name")
        if db.dbc is None:
            raise OpError("This DBC has no DBC-specific data to hold value tables")
        db.dbc.value_tables[name] = OrderedDict(sorted(_table(entries).items()))
        added_tables.append((name, _table(entries)))

    if not added and not added_tables:
        raise OpError("The plan changes nothing: choose at least one message or value table")

    def check(d: Database) -> bool:
        for name, fid, count in added:
            m = d.get_message_by_name(name)
            if m.frame_id != fid or len(m.signals) != count:
                return False
        return all(name in d.dbc.value_tables and _table(d.dbc.value_tables[name]) == entries
                   for name, entries in added_tables)

    return ({"frame_id": added[0][1]} if added else None), check


def _plain_node(name: str):
    from cantools.database.can import Node
    return Node(name, None, dbc_specifics=ops._new_dbc())
