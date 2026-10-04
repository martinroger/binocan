"""Theoretical CAN busload of a DBC.

Same frame-bit formula as vxGauge's ``decoder/common/busload.py``. Messages are
excluded by send type rather than by ID: anything sent strictly on request
(``GenMsgSendType`` = ``SendOnRequest``) carries no periodic load.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from cantools.database.can import Database

DEFAULT_BAUDRATE = 500000
DEFAULT_STUFF_FACTOR = 0.20
EXCLUDED_SEND_TYPES = {"SendOnRequest", "SendOnRequestWithDelay"}


def frame_bits(dlc: int, is_extended: bool = False,
               stuff_factor: float = DEFAULT_STUFF_FACTOR) -> float:
    """Nominal CAN 2.0 frame length in bits, with average bit stuffing.

    11-bit ID: 47 + 8 * dlc bits, of which 34 + 8 * dlc are stuffable.
    29-bit ID: 67 + 8 * dlc bits, of which 54 + 8 * dlc are stuffable.
    """
    if is_extended:
        fixed, stuffable = 67 + 8 * dlc, 54 + 8 * dlc
    else:
        fixed, stuffable = 47 + 8 * dlc, 34 + 8 * dlc
    return fixed + stuffable * stuff_factor


def baudrate_of(db: Database, original_text: Optional[str] = None) -> int:
    """Bus bitrate from the DBC's Baudrate attribute, else 500 kbit/s."""
    if db.dbc is not None:
        attr = db.dbc.attributes.get("Baudrate")
        if attr is not None:
            return int(attr.value)
        definition = db.dbc.attribute_definitions.get("Baudrate")
        if definition is not None and definition.default_value is not None:
            return int(definition.default_value)
    if original_text:
        m = re.search(r'BA_DEF_DEF_\s+"Baudrate"\s+(\d+);', original_text)
        if m:
            return int(m.group(1))
    return DEFAULT_BAUDRATE


def default_cycle_time(db: Database) -> Optional[int]:
    if db.dbc is None:
        return None
    definition = db.dbc.attribute_definitions.get("GenMsgCycleTime")
    if definition is None or definition.default_value is None:
        return None
    return int(definition.default_value)


def compute(db: Database, baudrate: Optional[int] = None,
            overrides: Optional[Dict[int, int]] = None,
            original_text: Optional[str] = None) -> Dict[str, Any]:
    """Busload breakdown. ``overrides`` maps frame ID to a what-if cycle time in ms."""
    baud = baudrate or baudrate_of(db, original_text)
    overrides = overrides or {}
    default_ct = default_cycle_time(db)

    rows, excluded = [], []
    total_bps = 0.0
    for m in db.messages:
        send_type = m.send_type or "Cyclic"
        if send_type in EXCLUDED_SEND_TYPES and m.frame_id not in overrides:
            excluded.append({"id": m.frame_id, "name": m.name, "send_type": send_type})
            continue
        ct = overrides.get(m.frame_id, m.cycle_time or default_ct)
        if not ct or ct <= 0:
            excluded.append({"id": m.frame_id, "name": m.name, "send_type": send_type,
                             "reason": "no cycle time"})
            continue
        hz = 1000.0 / ct
        bits = frame_bits(m.length, m.is_extended_frame)
        bps = hz * bits
        total_bps += bps
        rows.append({
            "id": m.frame_id,
            "hex_id": f"0x{m.frame_id:03X}",
            "name": m.name,
            "dlc": m.length,
            "send_type": send_type,
            "cycle_time_ms": ct,
            "overridden": m.frame_id in overrides,
            "frequency_hz": hz,
            "frame_bits": bits,
            "bitrate_bps": bps,
            "busload_pct": bps / baud * 100.0,
        })
    rows.sort(key=lambda r: r["bitrate_bps"], reverse=True)
    return {
        "baudrate": baud,
        "message_count": len(rows),
        "total_fps": sum(r["frequency_hz"] for r in rows),
        "total_bps": total_bps,
        "total_busload_pct": total_bps / baud * 100.0,
        "messages": rows,
        "excluded": excluded,
    }


def format_table(result: Dict[str, Any], title: str = "") -> str:
    out = []
    line = "=" * 86
    out.append(line)
    out.append(f" CAN busload: {title} @ {result['baudrate']:,} bit/s")
    out.append(line)
    out.append(f" {'ID':<7} {'Message':<30} {'DLC':>3} {'Cycle ms':>9} {'Hz':>7} "
               f"{'bit/s':>9} {'Load %':>7}")
    out.append(" " + "-" * 84)
    for r in result["messages"]:
        mark = "*" if r["overridden"] else " "
        out.append(f" {r['hex_id']:<7} {r['name'][:30]:<30} {r['dlc']:>3} "
                   f"{r['cycle_time_ms']:>8}{mark} {r['frequency_hz']:>7.1f} "
                   f"{r['bitrate_bps']:>9.1f} {r['busload_pct']:>7.2f}")
    out.append(line)
    out.append(f" Counted messages  : {result['message_count']}")
    if result["excluded"]:
        names = ", ".join(f"0x{e['id']:03X}" for e in result["excluded"])
        out.append(f" Excluded          : {len(result['excluded'])} ({names})")
    out.append(f" Frame rate        : {result['total_fps']:.1f} frames/s")
    out.append(f" Throughput        : {result['total_bps']:,.0f} bit/s")
    out.append(f" Theoretical load  : {result['total_busload_pct']:.2f} %")
    out.append(line)
    return "\n".join(out)
