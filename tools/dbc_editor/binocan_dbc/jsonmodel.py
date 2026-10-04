"""JSON view of a cantools database for the browser UI."""

from __future__ import annotations

from typing import Any, Dict

from cantools.database.can import Database, Message, Signal

from .validate import raw_range, signal_bits


def _choices(signal: Signal):
    if not signal.choices:
        return None
    return {str(int(k)): str(v) for k, v in signal.choices.items()}


def _value_table_name(db: Database, signal: Signal):
    """Name of the global VAL_TABLE_ whose entries match this signal's, if any."""
    if not signal.choices or db.dbc is None or not db.dbc.value_tables:
        return None
    mine = {int(k): str(v) for k, v in signal.choices.items()}
    for name, table in db.dbc.value_tables.items():
        if {int(k): str(v) for k, v in table.items()} == mine:
            return name
    return None


def signal_json(db: Database, s: Signal) -> Dict[str, Any]:
    rr = raw_range(s)
    return {
        "name": s.name,
        "start": s.start,
        "length": s.length,
        "byte_order": s.byte_order,
        "is_signed": s.is_signed,
        "is_float": s.is_float,
        "scale": s.scale,
        "offset": s.offset,
        "minimum": s.minimum,
        "maximum": s.maximum,
        "unit": s.unit or "",
        "receivers": list(s.receivers),
        "comment": s.comment or "",
        "initial": s.raw_initial,
        "choices": _choices(s),
        "value_table": _value_table_name(db, s),
        "is_multiplexer": s.is_multiplexer,
        "multiplexer_ids": list(s.multiplexer_ids) if s.multiplexer_ids else None,
        "multiplexer_signal": s.multiplexer_signal,
        "raw_min": rr[0] if rr else None,
        "raw_max": rr[1] if rr else None,
        "bits": signal_bits(s),
    }


def message_json(db: Database, m: Message) -> Dict[str, Any]:
    return {
        "frame_id": m.frame_id,
        "hex_id": f"0x{m.frame_id:03X}",
        "name": m.name,
        "is_extended": m.is_extended_frame,
        "is_fd": m.is_fd,
        "length": m.length,
        "senders": list(m.senders),
        "cycle_time": m.cycle_time,
        "send_type": m.send_type,
        "comment": m.comment or "",
        "signals": sorted((signal_json(db, s) for s in m.signals),
                          key=lambda s: min(s["bits"]) if s["bits"] else 0),
    }


def database_json(db: Database) -> Dict[str, Any]:
    tables = {}
    if db.dbc is not None and db.dbc.value_tables:
        tables = {name: {str(int(k)): str(v) for k, v in t.items()}
                  for name, t in db.dbc.value_tables.items()}
    send_types = []
    if db.dbc is not None:
        d = db.dbc.attribute_definitions.get("GenMsgSendType")
        if d is not None and d.choices:
            send_types = list(d.choices)
    return {
        "version": db.version,
        "nodes": [{"name": n.name, "comment": n.comment or ""} for n in db.nodes],
        "value_tables": tables,
        "send_types": send_types,
        "messages": sorted((message_json(db, m) for m in db.messages),
                           key=lambda m: m["frame_id"]),
    }
