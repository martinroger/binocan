"""Edit operations on a cantools database.

Every operation is a plain dict (``{"op": "signal.set", ...}``) so the browser,
the tests and scripts all use the same entry points. The session applies an
operation to a copy of the database, writes it to DBC text, loads that text
back and checks the change really survived, so a change cantools cannot
represent is refused instead of silently lost.
"""

from __future__ import annotations

import copy
import re
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional, Tuple

from cantools.database.can import Database, Message, Node, Signal
from cantools.database.can.formats.dbc import DbcAttribute, DbcSpecifics

NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_STANDARD_ID = 0x7FF
MAX_EXTENDED_ID = 0x1FFFFFFF
BYTE_ORDERS = ("little_endian", "big_endian")


class OpError(Exception):
    """The operation was refused; the message is meant for the user."""


# ---------- value helpers ----------

def _name(value: Any, what: str) -> str:
    if not isinstance(value, str) or not NAME_RE.match(value):
        raise OpError(f"{what} must start with a letter or underscore and contain only "
                      f"letters, digits and underscores (got {value!r})")
    return value


def _int(value: Any, what: str, lo: Optional[int] = None, hi: Optional[int] = None) -> int:
    try:
        if isinstance(value, bool):
            raise ValueError
        if isinstance(value, str):
            value = int(value.strip(), 0)
        elif isinstance(value, float) and value.is_integer():
            value = int(value)
        elif not isinstance(value, int):
            raise ValueError
    except ValueError:
        raise OpError(f"{what} must be a whole number (got {value!r})") from None
    if lo is not None and value < lo:
        raise OpError(f"{what} must be at least {lo}")
    if hi is not None and value > hi:
        raise OpError(f"{what} must be at most {hi}")
    return value


def _num(value: Any, what: str, allow_none: bool = False) -> Optional[float]:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        if allow_none:
            return None
        raise OpError(f"{what} is required")
    try:
        if isinstance(value, bool):
            raise ValueError
        out = float(value)
    except (TypeError, ValueError):
        raise OpError(f"{what} must be a number (got {value!r})") from None
    if out != out or out in (float("inf"), float("-inf")):
        raise OpError(f"{what} must be a finite number")
    return int(out) if out.is_integer() and abs(out) < 2 ** 53 else out


def _bool(value: Any, what: str) -> bool:
    if isinstance(value, bool):
        return value
    raise OpError(f"{what} must be true or false")


def _text(value: Any, what: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise OpError(f"{what} must be text")
    return value


def _choices(value: Any, what: str) -> "OrderedDict[int, str]":
    if value is None:
        return OrderedDict()
    if not isinstance(value, dict):
        raise OpError(f"{what} must map numbers to labels")
    out: "OrderedDict[int, str]" = OrderedDict()
    for k, v in value.items():
        key = _int(k, f"{what} value")
        if key in out:
            raise OpError(f"{what}: value {key} appears twice")
        label = _text(v, f"{what} label").strip()
        if not label:
            raise OpError(f"{what}: value {key} needs a label")
        if '"' in label:
            raise OpError(f"{what}: labels cannot contain double quotes")
        out[key] = label
    return OrderedDict(sorted(out.items()))


# ---------- lookups ----------

def _message(db: Database, frame_id: Any) -> Message:
    fid = _int(frame_id, "frame ID")
    for m in db.messages:
        if m.frame_id == fid:
            return m
    raise OpError(f"No message with frame ID 0x{fid:X}")


def _signal(msg: Message, name: Any) -> Signal:
    for s in msg.signals:
        if s.name == name:
            return s
    raise OpError(f"{msg.name} has no signal {name}")


def _node_names(db: Database) -> List[str]:
    return [n.name for n in db.nodes]


def _check_nodes(db: Database, names: Any, what: str) -> List[str]:
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise OpError(f"{what} must be a list of node names")
    known = set(_node_names(db))
    for n in names:
        if n not in known:
            raise OpError(f"{what}: {n} is not a node")
    return list(dict.fromkeys(names))


def _new_dbc() -> DbcSpecifics:
    return DbcSpecifics(attributes=OrderedDict(), attribute_definitions=OrderedDict())


def _ensure_dbc(msg: Message) -> DbcSpecifics:
    if msg.dbc is None:
        msg.dbc = _new_dbc()
    return msg.dbc


def _send_type_definition(db: Database):
    return db.dbc.attribute_definitions.get("GenMsgSendType") if db.dbc else None


def default_cycle_time(db: Database) -> int:
    d = db.dbc.attribute_definitions.get("GenMsgCycleTime") if db.dbc else None
    return int(d.default_value) if d is not None and d.default_value is not None else 100


# ---------- message rebuild ----------

def _replace_message(db: Database, old: Message, **override: Any) -> Message:
    """Builds a copy of ``old`` with some constructor fields changed and swaps it in.

    cantools has no setters for senders, send type and similar fields, so a
    changed message is rebuilt through the public constructor.
    """
    kw: Dict[str, Any] = dict(
        frame_id=old.frame_id,
        name=old.name,
        length=old.length,
        signals=old.signals,
        contained_messages=old.contained_messages,
        header_id=old.header_id,
        header_byte_order=old.header_byte_order,
        unused_bit_pattern=old.unused_bit_pattern,
        comment=old.comments if old.comments else None,
        senders=list(old.senders),
        send_type=old.send_type,
        cycle_time=old.cycle_time,
        dbc_specifics=old.dbc,
        autosar_specifics=old.autosar,
        is_extended_frame=old.is_extended_frame,
        is_fd=old.is_fd,
        bus_name=old.bus_name,
        signal_groups=old.signal_groups,
        strict=False,
        protocol=old.protocol,
    )
    kw.update(override)
    new = Message(**kw)
    idx = db.messages.index(old)
    db.messages[idx] = new
    return new


def _set_send_type_attribute(db: Database, msg: Message, send_type: Optional[str]) -> None:
    d = _send_type_definition(db)
    if d is None:
        raise OpError("This DBC has no GenMsgSendType attribute definition")
    attrs = _ensure_dbc(msg).attributes
    if send_type is None or send_type == d.default_value:
        attrs.pop("GenMsgSendType", None)
    else:
        attrs["GenMsgSendType"] = DbcAttribute(value=d.choices.index(send_type), definition=d)


# ---------- message operations ----------

def _message_set(db: Database, op: Dict[str, Any]):
    msg = _message(db, op.get("frame_id"))
    field, value = op.get("field"), op.get("value")
    select = {"frame_id": msg.frame_id}

    if field == "name":
        value = _name(value, "Message name")
        if any(m.name == value and m is not msg for m in db.messages):
            raise OpError(f"A message called {value} already exists")
        _replace_message(db, msg, name=value)
        check = lambda d: d.get_message_by_frame_id(msg.frame_id).name == value
    elif field == "frame_id":
        fid = _int(value, "Frame ID", 0, MAX_EXTENDED_ID)
        if any(m.frame_id == fid and m is not msg for m in db.messages):
            raise OpError(f"Frame ID 0x{fid:X} is already used by another message")
        extended = msg.is_extended_frame or fid > MAX_STANDARD_ID
        _replace_message(db, msg, frame_id=fid, is_extended_frame=extended)
        select = {"frame_id": fid}
        check = lambda d: d.get_message_by_name(msg.name).frame_id == fid
    elif field == "is_extended":
        ext = _bool(value, "Extended frame")
        if not ext and msg.frame_id > MAX_STANDARD_ID:
            raise OpError("A frame ID above 0x7FF needs the extended (29-bit) format")
        _replace_message(db, msg, is_extended_frame=ext)
        check = lambda d: d.get_message_by_frame_id(msg.frame_id).is_extended_frame == ext
    elif field == "is_fd":
        fd = _bool(value, "CAN FD")
        if not fd and msg.length > 8:
            raise OpError("Shorten the message to 8 bytes before turning CAN FD off")
        _replace_message(db, msg, is_fd=fd)
        check = lambda d: d.get_message_by_frame_id(msg.frame_id).is_fd == fd
    elif field == "length":
        n = _int(value, "Length", 0, 64 if msg.is_fd else 8)
        _replace_message(db, msg, length=n)
        check = lambda d: d.get_message_by_frame_id(msg.frame_id).length == n
    elif field == "senders":
        senders = _check_nodes(db, value, "Sender")
        _replace_message(db, msg, senders=senders)
        check = lambda d: list(d.get_message_by_frame_id(msg.frame_id).senders) == senders
    elif field == "send_type":
        d_def = _send_type_definition(db)
        if value is not None and (d_def is None or value not in d_def.choices):
            raise OpError(f"Unknown send type {value!r}")
        _set_send_type_attribute(db, msg, value)
        target = value or (d_def.default_value if d_def else None)
        check = lambda d: (d.get_message_by_frame_id(msg.frame_id).send_type or target) == target
    elif field == "cycle_time":
        ct = None if value in (None, "") else _int(value, "Cycle time", 1, 10_000_000)
        _ensure_dbc(msg)
        msg.cycle_time = ct if ct is not None else default_cycle_time(db)
        want = msg.cycle_time
        check = lambda d: d.get_message_by_frame_id(msg.frame_id).cycle_time == want
    elif field == "comment":
        text = _text(value, "Comment")
        msg.comment = text if text.strip() else None
        want = text if text.strip() else None
        check = lambda d: (d.get_message_by_frame_id(msg.frame_id).comment or None) == want
    else:
        raise OpError(f"Unknown message field {field!r}")
    return select, check


def _message_add(db: Database, op: Dict[str, Any]):
    name = _name(op.get("name"), "Message name")
    fid = _int(op.get("frame_id"), "Frame ID", 0, MAX_EXTENDED_ID)
    length = _int(op.get("length", 8), "Length", 0, 64)
    if any(m.name == name for m in db.messages):
        raise OpError(f"A message called {name} already exists")
    if any(m.frame_id == fid for m in db.messages):
        raise OpError(f"Frame ID 0x{fid:X} is already used")
    senders = _check_nodes(db, op.get("senders", []), "Sender")
    ct = _int(op.get("cycle_time", default_cycle_time(db)), "Cycle time", 1, 10_000_000)
    msg = Message(fid, name, length, [], senders=senders, cycle_time=ct,
                  dbc_specifics=_new_dbc(), is_extended_frame=fid > MAX_STANDARD_ID,
                  is_fd=length > 8, strict=False)
    db.messages.append(msg)
    return {"frame_id": fid}, lambda d: d.get_message_by_frame_id(fid).name == name


def _message_duplicate(db: Database, op: Dict[str, Any]):
    src = _message(db, op.get("frame_id"))
    name = _name(op.get("name"), "Message name")
    fid = _int(op.get("new_frame_id"), "Frame ID", 0, MAX_EXTENDED_ID)
    if any(m.name == name for m in db.messages):
        raise OpError(f"A message called {name} already exists")
    if any(m.frame_id == fid for m in db.messages):
        raise OpError(f"Frame ID 0x{fid:X} is already used")
    dup = copy.deepcopy(src)
    dup = _replace_message(_Shim(db, dup), dup, name=name, frame_id=fid,
                           is_extended_frame=src.is_extended_frame or fid > MAX_STANDARD_ID)
    db.messages.append(dup)
    return {"frame_id": fid}, lambda d: len(d.get_message_by_frame_id(fid).signals) == len(src.signals)


class _Shim:
    """Lets ``_replace_message`` rebuild a message that is not in the database yet."""

    def __init__(self, db: Database, msg: Message):
        self.dbc = db.dbc
        self.messages = [msg]


def _message_delete(db: Database, op: Dict[str, Any]):
    msg = _message(db, op.get("frame_id"))
    fid = msg.frame_id
    db.messages.remove(msg)

    def check(d: Database) -> bool:
        return all(m.frame_id != fid for m in d.messages)
    return None, check


# ---------- signal operations ----------

def _check_signal_name(msg: Message, sig: Optional[Signal], name: str) -> str:
    name = _name(name, "Signal name")
    if any(s.name == name and s is not sig for s in msg.signals):
        raise OpError(f"{msg.name} already has a signal called {name}")
    return name


def _signal_set(db: Database, op: Dict[str, Any]):
    msg = _message(db, op.get("frame_id"))
    sig = _signal(msg, op.get("name"))
    field, value = op.get("field"), op.get("value")
    fid, name = msg.frame_id, sig.name
    select = {"frame_id": fid, "signal": name}

    def getter(d: Database, n: str = name) -> Signal:
        return d.get_message_by_frame_id(fid).get_signal_by_name(n)

    if field == "name":
        new = _check_signal_name(msg, sig, value)
        for other in msg.signals:
            if other.multiplexer_signal == name:
                other.multiplexer_signal = new
        sig.name = new
        select["signal"] = new
        check = lambda d: getter(d, new) is not None
    elif field == "start":
        v = _int(value, "Start bit", 0, msg.length * 8 - 1 if msg.length else 0)
        sig.start = v
        check = lambda d: getter(d).start == v
    elif field == "length":
        v = _int(value, "Length", 1, 64)
        sig.length = v
        check = lambda d: getter(d).length == v
    elif field == "byte_order":
        if value not in BYTE_ORDERS:
            raise OpError("Byte order must be little_endian (Intel) or big_endian (Motorola)")
        sig.byte_order = value
        check = lambda d: getter(d).byte_order == value
    elif field == "is_signed":
        v = _bool(value, "Signed")
        sig.is_signed = v
        check = lambda d: getter(d).is_signed == v
    elif field == "is_float":
        v = _bool(value, "Float")
        if v and sig.length not in (32, 64):
            raise OpError("A float signal must be 32 or 64 bits long; change the length first")
        _set_float(sig, v)
        check = lambda d: getter(d).is_float == v
    elif field == "scale":
        v = _num(value, "Factor")
        if v == 0:
            raise OpError("Factor cannot be 0")
        sig.scale = v
        check = lambda d: getter(d).scale == v
    elif field == "offset":
        v = _num(value, "Offset")
        sig.offset = v
        check = lambda d: getter(d).offset == v
    elif field in ("minimum", "maximum"):
        v = _num(value, field.capitalize(), allow_none=True)
        setattr(sig, field, v)
        check = lambda d: getattr(getter(d), field) == v
    elif field == "unit":
        v = _text(value, "Unit")
        sig.unit = v or None
        check = lambda d: (getter(d).unit or "") == v
    elif field == "comment":
        v = _text(value, "Comment")
        sig.comment = v if v.strip() else None
        check = lambda d: (getter(d).comment or "") == (v if v.strip() else "")
    elif field == "receivers":
        v = _check_nodes(db, value, "Receiver")
        sig.receivers = v
        check = lambda d: list(getter(d).receivers) == v
    elif field == "initial":
        v = _num(value, "Start value", allow_none=True)
        sig.raw_initial = v
        check = lambda d: getter(d).raw_initial == v
    elif field == "is_multiplexer":
        v = _bool(value, "Multiplexer")
        if v:
            other = next((x for x in msg.signals if x.is_multiplexer and x is not sig), None)
            if other is not None:
                raise OpError(f"{msg.name} already has a multiplexer signal ({other.name}); "
                              f"a DBC message can have only one")
            if sig.multiplexer_ids:
                raise OpError(f"{name} is multiplexed; take it out of multiplexing first")
        else:
            users = [x.name for x in msg.signals if x.multiplexer_signal == name]
            if users:
                raise OpError(f"{', '.join(users)} depend on {name}; make them plain signals first")
        sig.is_multiplexer = v
        check = lambda d: getter(d).is_multiplexer == v
    elif field == "multiplexer_ids":
        ids = _mux_ids(msg, sig, value)
        if ids:
            mux = next(x for x in msg.signals if x.is_multiplexer)
            sig.multiplexer_ids = ids
            sig.multiplexer_signal = mux.name
            check = lambda d: list(getter(d).multiplexer_ids or []) == ids \
                and getter(d).multiplexer_signal == mux.name
        else:
            sig.multiplexer_ids = None
            sig.multiplexer_signal = None
            check = lambda d: not getter(d).multiplexer_ids
    else:
        raise OpError(f"Unknown signal field {field!r}")
    return select, check


def _mux_ids(msg: Message, sig: Signal, value: Any) -> List[int]:
    """Validated multiplexer values for a signal, or [] to take it out of multiplexing."""
    if value in (None, "", []):
        return []
    if not isinstance(value, list):
        raise OpError("Multiplexer values must be a list of whole numbers")
    mux = next((x for x in msg.signals if x.is_multiplexer), None)
    if mux is None:
        raise OpError(f"{msg.name} has no multiplexer signal; mark one first")
    if sig is mux:
        raise OpError("The multiplexer signal cannot be multiplexed itself")
    top = 2 ** mux.length - 1
    ids = sorted({_int(v, "Multiplexer value", 0, top) for v in value})
    return ids


def _set_float(sig: Signal, is_float: bool) -> None:
    from cantools.database.conversion import BaseConversion
    sig.conversion = BaseConversion.factory(scale=sig.scale, offset=sig.offset,
                                            choices=sig.choices, is_float=is_float)


def _signal_add(db: Database, op: Dict[str, Any]):
    msg = _message(db, op.get("frame_id"))
    name = _check_signal_name(msg, None, op.get("name"))
    length = _int(op.get("length", 8), "Length", 1, 64)
    start = _int(op.get("start", 0), "Start bit", 0, max(msg.length * 8 - 1, 0))
    byte_order = op.get("byte_order", "little_endian")
    if byte_order not in BYTE_ORDERS:
        raise OpError("Byte order must be little_endian or big_endian")
    receivers = _check_nodes(db, op.get("receivers", []), "Receiver")
    sig = Signal(name, start, length, byte_order=byte_order, receivers=receivers,
                 dbc_specifics=_new_dbc())
    msg.signals.append(sig)
    fid = msg.frame_id
    return ({"frame_id": fid, "signal": name},
            lambda d: d.get_message_by_frame_id(fid).get_signal_by_name(name) is not None)


def _signal_delete(db: Database, op: Dict[str, Any]):
    msg = _message(db, op.get("frame_id"))
    sig = _signal(msg, op.get("name"))
    if any(o.multiplexer_signal == sig.name for o in msg.signals):
        raise OpError(f"{sig.name} is the multiplexer of other signals; remove those first")
    msg.signals.remove(sig)
    fid, name = msg.frame_id, sig.name
    return ({"frame_id": fid},
            lambda d: all(s.name != name for s in d.get_message_by_frame_id(fid).signals))


def _signal_set_choices(db: Database, op: Dict[str, Any]):
    msg = _message(db, op.get("frame_id"))
    sig = _signal(msg, op.get("name"))
    choices = _choices(op.get("choices"), "Value table")
    sig.choices = choices or None
    fid, name = msg.frame_id, sig.name
    want = {int(k): v for k, v in choices.items()}

    def check(d: Database) -> bool:
        got = d.get_message_by_frame_id(fid).get_signal_by_name(name).choices or {}
        return {int(k): str(v) for k, v in got.items()} == want
    return {"frame_id": fid, "signal": name}, check


def _signal_apply_table(db: Database, op: Dict[str, Any]):
    table = (db.dbc.value_tables if db.dbc else {}).get(op.get("table"))
    if table is None:
        raise OpError(f"No value table called {op.get('table')}")
    return _signal_set_choices(db, {"frame_id": op.get("frame_id"), "name": op.get("name"),
                                    "choices": {int(k): str(v) for k, v in table.items()}})


# ---------- node operations ----------

def _node_add(db: Database, op: Dict[str, Any]):
    name = _name(op.get("name"), "Node name")
    if name in _node_names(db):
        raise OpError(f"A node called {name} already exists")
    comment = _text(op.get("comment"), "Comment").strip() or None
    db.nodes.append(Node(name, comment, dbc_specifics=_new_dbc()))
    return None, lambda d: name in [n.name for n in d.nodes]


def _node_users(db: Database, name: str) -> Tuple[List[str], List[str]]:
    sends = [m.name for m in db.messages if name in m.senders]
    receives = [f"{m.name}.{s.name}" for m in db.messages for s in m.signals
                if name in s.receivers]
    return sends, receives


def _node_index(db: Database, name: Any) -> int:
    for i, n in enumerate(db.nodes):
        if n.name == name:
            return i
    raise OpError(f"No node called {name}")


def _node_rename(db: Database, op: Dict[str, Any]):
    i = _node_index(db, op.get("name"))
    old = db.nodes[i]
    new = _name(op.get("new_name"), "Node name")
    if new != old.name and new in _node_names(db):
        raise OpError(f"A node called {new} already exists")
    db.nodes[i] = Node(new, old.comment, dbc_specifics=old.dbc, autosar_specifics=old.autosar)
    for m in list(db.messages):
        if old.name in m.senders:
            _replace_message(db, m, senders=[new if s == old.name else s for s in m.senders])
    for m in db.messages:
        for s in m.signals:
            if old.name in s.receivers:
                s.receivers = [new if r == old.name else r for r in s.receivers]
    return None, lambda d: new in [n.name for n in d.nodes] and old.name not in [n.name for n in d.nodes]


def _node_set_comment(db: Database, op: Dict[str, Any]):
    i = _node_index(db, op.get("name"))
    old = db.nodes[i]
    comment = _text(op.get("comment"), "Comment").strip() or None
    db.nodes[i] = Node(old.name, comment, dbc_specifics=old.dbc, autosar_specifics=old.autosar)
    name = old.name
    return None, lambda d: ([n.comment for n in d.nodes if n.name == name] or [None])[0] == comment


def _node_delete(db: Database, op: Dict[str, Any]):
    i = _node_index(db, op.get("name"))
    name = db.nodes[i].name
    sends, receives = _node_users(db, name)
    if (sends or receives) and not op.get("force"):
        parts = []
        if sends:
            parts.append(f"sends {len(sends)} message(s) ({', '.join(sends[:3])}{'…' if len(sends) > 3 else ''})")
        if receives:
            parts.append(f"receives {len(receives)} signal(s)")
        raise OpError(f"{name} still " + " and ".join(parts) + ". Remove it from them first.")
    for m in list(db.messages):
        if name in m.senders:
            _replace_message(db, m, senders=[s for s in m.senders if s != name])
    for m in db.messages:
        for s in m.signals:
            if name in s.receivers:
                s.receivers = [r for r in s.receivers if r != name]
    del db.nodes[i]
    return None, lambda d: name not in [n.name for n in d.nodes]


def _message_set_node_role(db: Database, op: Dict[str, Any]):
    msg = _message(db, op.get("frame_id"))
    node = op.get("node")
    if node not in _node_names(db):
        raise OpError(f"{node} is not a node")
    role = op.get("role")
    if role not in ("tx", "rx", "none"):
        raise OpError("Role must be tx, rx or none")
    senders = [s for s in msg.senders if s != node]
    if role == "tx":
        senders.append(node)
    msg = _replace_message(db, msg, senders=senders)
    for s in msg.signals:
        rec = [r for r in s.receivers if r != node]
        if role == "rx":
            rec.append(node)
        s.receivers = rec
    fid = msg.frame_id

    def check(d: Database) -> bool:
        m = d.get_message_by_frame_id(fid)
        if role == "tx":
            return node in m.senders
        if role == "rx":
            return node not in m.senders and all(node in s.receivers for s in m.signals)
        return node not in m.senders and all(node not in s.receivers for s in m.signals)
    return {"frame_id": fid}, check


# ---------- global value tables ----------

def _tables(db: Database) -> "OrderedDict[str, Any]":
    if db.dbc is None:
        raise OpError("This database has no DBC specifics")
    return db.dbc.value_tables


def _table_add(db: Database, op: Dict[str, Any]):
    name = _name(op.get("name"), "Table name")
    tables = _tables(db)
    if name in tables:
        raise OpError(f"A value table called {name} already exists")
    tables[name] = _choices(op.get("entries"), "Value table")
    return None, lambda d: name in d.dbc.value_tables


def _table_set(db: Database, op: Dict[str, Any]):
    name = op.get("name")
    tables = _tables(db)
    if name not in tables:
        raise OpError(f"No value table called {name}")
    entries = _choices(op.get("entries"), f"Value table {name}")
    old = {int(k): str(v) for k, v in tables[name].items()}
    tables[name] = entries
    updated = 0
    if op.get("apply_to_signals", True):
        for m in db.messages:
            for s in m.signals:
                if s.choices and {int(k): str(v) for k, v in s.choices.items()} == old:
                    s.choices = OrderedDict(entries)
                    updated += 1
    want = {int(k): v for k, v in entries.items()}
    return None, lambda d: {int(k): str(v) for k, v in d.dbc.value_tables[name].items()} == want


def _table_rename(db: Database, op: Dict[str, Any]):
    old = op.get("name")
    new = _name(op.get("new_name"), "Table name")
    tables = _tables(db)
    if old not in tables:
        raise OpError(f"No value table called {old}")
    if new != old and new in tables:
        raise OpError(f"A value table called {new} already exists")
    renamed = OrderedDict((new if k == old else k, v) for k, v in tables.items())
    tables.clear()
    tables.update(renamed)
    return None, lambda d: new in d.dbc.value_tables and old not in d.dbc.value_tables


def _table_delete(db: Database, op: Dict[str, Any]):
    name = op.get("name")
    tables = _tables(db)
    if name not in tables:
        raise OpError(f"No value table called {name}")
    del tables[name]
    return None, lambda d: name not in d.dbc.value_tables


# ---------- attributes ----------

MANAGED_ATTRIBUTES = ("GenMsgCycleTime", "GenMsgSendType", "GenSigStartValue", "Baudrate")
ATTRIBUTE_KINDS = {"network": None, "node": "BU_", "message": "BO_", "signal": "SG_"}
ATTRIBUTE_TYPES = ("INT", "HEX", "FLOAT", "STRING", "ENUM")


def _definitions(db: Database):
    if db.dbc is None:
        raise OpError("This database has no DBC specifics")
    return db.dbc.attribute_definitions


def _definition(db: Database, name: Any, allow_managed: bool = False):
    defs = _definitions(db)
    if name not in defs:
        raise OpError(f"No attribute called {name}")
    if name in MANAGED_ATTRIBUTES and not allow_managed:
        raise OpError(f"{name} is edited through the message and signal fields, not here")
    return defs[name]


def _attribute_value(defn, value: Any, what: str):
    """Value as cantools stores it: numbers, text, or the index of an enum choice."""
    t = defn.type_name
    if t == "STRING":
        return _text(value, what)
    if t == "ENUM":
        if value not in defn.choices:
            raise OpError(f"{what}: {value!r} is not one of {', '.join(defn.choices)}")
        return defn.choices.index(value)
    if t in ("INT", "HEX"):
        v = _int(value, what)
    else:
        v = _num(value, what)
    if defn.minimum is not None and v < defn.minimum or defn.maximum is not None and v > defn.maximum:
        raise OpError(f"{what} must be between {defn.minimum} and {defn.maximum}")
    return v


def _default_value(defn, value: Any):
    """Default as cantools keeps it: for ENUM the choice text, otherwise the plain value."""
    if defn.type_name == "ENUM":
        if value not in defn.choices:
            raise OpError(f"Default {value!r} is not one of {', '.join(defn.choices)}")
        return value
    return _attribute_value(defn, value, "Default")


def _attribute_define(db: Database, op: Dict[str, Any]):
    from cantools.database.can.formats.dbc import DbcAttributeDefinition
    name = _name(op.get("name"), "Attribute name")
    defs = _definitions(db)
    if name in defs:
        raise OpError(f"An attribute called {name} already exists")
    scope = op.get("scope")
    if scope not in ATTRIBUTE_KINDS:
        raise OpError("Scope must be network, node, message or signal")
    typ = op.get("type")
    if typ not in ATTRIBUTE_TYPES:
        raise OpError(f"Type must be one of {', '.join(ATTRIBUTE_TYPES)}")
    choices = None
    lo = hi = None
    if typ == "ENUM":
        choices = op.get("choices")
        if not isinstance(choices, list) or not choices or not all(
                isinstance(c, str) and c and '"' not in c for c in choices):
            raise OpError("An enumeration needs a list of choices (no double quotes)")
        if len(set(choices)) != len(choices):
            raise OpError("Enumeration choices must be different")
    elif typ in ("INT", "HEX", "FLOAT"):
        conv = _int if typ != "FLOAT" else _num
        lo = conv(op.get("minimum", 0), "Minimum")
        hi = conv(op.get("maximum", 0), "Maximum")
        if lo > hi:
            raise OpError("Minimum is above maximum")
    defn = DbcAttributeDefinition(name, None, ATTRIBUTE_KINDS[scope], typ, lo, hi, choices)
    default = op.get("default")
    if default is None:
        default = choices[0] if typ == "ENUM" else ("" if typ == "STRING" else (lo if lo is not None else 0))
    defn.default_value = _default_value(defn, default)
    defs[name] = defn
    return None, lambda d: name in d.dbc.attribute_definitions \
        and d.dbc.attribute_definitions[name].type_name == typ


def _attribute_set_default(db: Database, op: Dict[str, Any]):
    defn = _definition(db, op.get("name"))
    value = _default_value(defn, op.get("value"))
    defn.default_value = value
    name = defn.name
    return None, lambda d: d.dbc.attribute_definitions[name].default_value == value


def _holders(db: Database, kind: Optional[str]):
    """Every object that can carry an attribute of this kind."""
    if kind is None:
        return [db]
    if kind == "BU_":
        return list(db.nodes)
    if kind == "BO_":
        return list(db.messages)
    return [s for m in db.messages for s in m.signals]


def _attribute_delete(db: Database, op: Dict[str, Any]):
    defn = _definition(db, op.get("name"))
    name = defn.name
    for holder in _holders(db, defn.kind):
        specifics = holder.dbc
        if specifics is not None:
            specifics.attributes.pop(name, None)
    del db.dbc.attribute_definitions[name]
    return None, lambda d: name not in d.dbc.attribute_definitions


def _attribute_holder(db: Database, defn, op: Dict[str, Any]):
    kind = defn.kind
    if kind is None:
        return db
    if kind == "BU_":
        node = next((n for n in db.nodes if n.name == op.get("node")), None)
        if node is None:
            raise OpError(f"No node called {op.get('node')}")
        return node
    msg = _message(db, op.get("frame_id"))
    return msg if kind == "BO_" else _signal(msg, op.get("signal"))


def _attribute_set(db: Database, op: Dict[str, Any]):
    defn = _definition(db, op.get("name"))
    holder = _attribute_holder(db, defn, op)
    if holder.dbc is None:
        holder.dbc = _new_dbc()
    name = defn.name
    value = op.get("value")
    if value is None:
        holder.dbc.attributes.pop(name, None)
        stored = None
    else:
        stored = _attribute_value(defn, value, name)
        holder.dbc.attributes[name] = DbcAttribute(value=stored, definition=defn)

    def read(d: Database):
        if defn.kind is None:
            target = d
        elif defn.kind == "BU_":
            target = next(n for n in d.nodes if n.name == op.get("node"))
        else:
            m = d.get_message_by_frame_id(op.get("frame_id"))
            target = m if defn.kind == "BO_" else m.get_signal_by_name(op.get("signal"))
        a = (target.dbc.attributes if target.dbc else {}).get(name)
        return None if a is None else a.value
    return {"frame_id": op["frame_id"]} if defn.kind in ("BO_", "SG_") else None, \
        lambda d: read(d) == stored


# ---------- registry ----------

OPS: Dict[str, Callable[[Database, Dict[str, Any]], Tuple[Optional[dict], Optional[Callable]]]] = {
    "message.set": _message_set,
    "message.add": _message_add,
    "message.duplicate": _message_duplicate,
    "message.delete": _message_delete,
    "message.set_node_role": _message_set_node_role,
    "signal.set": _signal_set,
    "signal.add": _signal_add,
    "signal.delete": _signal_delete,
    "signal.set_choices": _signal_set_choices,
    "signal.apply_table": _signal_apply_table,
    "node.add": _node_add,
    "node.rename": _node_rename,
    "node.set_comment": _node_set_comment,
    "node.delete": _node_delete,
    "table.add": _table_add,
    "table.set": _table_set,
    "table.rename": _table_rename,
    "table.delete": _table_delete,
    "attribute.define": _attribute_define,
    "attribute.set_default": _attribute_set_default,
    "attribute.delete": _attribute_delete,
    "attribute.set": _attribute_set,
}


def _merge_apply(db: Database, op: Dict[str, Any]):
    from . import merge   # merge builds on this module, so import it late
    return merge.apply(db, merge.parse(op.get("text")), op.get("plan"))


OPS["merge.apply"] = _merge_apply


def apply_op(db: Database, op: Dict[str, Any]) -> Tuple[Optional[dict], Optional[Callable]]:
    """Mutates ``db`` according to ``op``. Returns (selection hint, persistence check)."""
    if not isinstance(op, dict) or op.get("op") not in OPS:
        raise OpError(f"Unknown operation {op.get('op') if isinstance(op, dict) else op!r}")
    try:
        result = OPS[op["op"]](db, op)
    except OpError:
        raise
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise OpError(f"Could not apply {op['op']}: {exc}") from exc
    db.refresh()
    return result
