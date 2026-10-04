"""Consistency checks on a cantools database."""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List

from cantools.database.can import Database, Message, Signal


def signal_bits(signal: Signal) -> List[int]:
    """Absolute bit indices (byte * 8 + bit, LSB = bit 0) a signal occupies."""
    if signal.byte_order == "little_endian":
        return list(range(signal.start, signal.start + signal.length))
    # Motorola: start is the MSB in DBC sawtooth numbering.
    bits, pos = [], signal.start
    for _ in range(signal.length):
        bits.append(pos)
        pos = pos + 15 if pos % 8 == 0 else pos - 1
    return bits


def raw_range(signal: Signal):
    if signal.is_float:
        return None
    if signal.is_signed:
        return -(1 << (signal.length - 1)), (1 << (signal.length - 1)) - 1
    return 0, (1 << signal.length) - 1


def _issue(level: str, where: str, text: str) -> Dict[str, str]:
    return {"level": level, "where": where, "text": text}


def _overlaps(message: Message) -> List[Dict[str, str]]:
    issues = []
    owners: Dict[int, List[Signal]] = defaultdict(list)
    for sig in message.signals:
        for bit in signal_bits(sig):
            owners[bit].append(sig)
    reported = set()
    for bit, sigs in owners.items():
        for i, a in enumerate(sigs):
            for b in sigs[i + 1:]:
                if _mutually_exclusive(a, b):
                    continue
                key = tuple(sorted((a.name, b.name)))
                if key not in reported:
                    reported.add(key)
                    issues.append(_issue("error", message.name,
                                         f"{a.name} and {b.name} overlap (bit {bit})"))
    return issues


def _mutually_exclusive(a: Signal, b: Signal) -> bool:
    """Signals under different values of the same multiplexer never coexist."""
    if not a.multiplexer_ids or not b.multiplexer_ids:
        return False
    if a.multiplexer_signal != b.multiplexer_signal:
        return False
    return not set(a.multiplexer_ids) & set(b.multiplexer_ids)


def check(db: Database) -> List[Dict[str, str]]:
    issues: List[Dict[str, str]] = []
    node_names = {n.name for n in db.nodes}

    seen_ids: Dict[int, str] = {}
    seen_names: Dict[str, int] = {}
    for m in db.messages:
        if m.frame_id in seen_ids:
            issues.append(_issue("error", m.name,
                                 f"frame ID 0x{m.frame_id:X} also used by {seen_ids[m.frame_id]}"))
        seen_ids[m.frame_id] = m.name
        if m.name in seen_names:
            issues.append(_issue("error", m.name, "duplicate message name"))
        seen_names[m.name] = m.frame_id

        for sender in m.senders:
            if sender not in node_names:
                issues.append(_issue("error", m.name, f"sender {sender} is not a node"))

        if m.send_type and "Cyclic" in m.send_type and not m.cycle_time:
            issues.append(_issue("warning", m.name, f"send type {m.send_type} but no cycle time"))

        sig_names = set()
        for s in m.signals:
            where = f"{m.name}.{s.name}"
            if s.name in sig_names:
                issues.append(_issue("error", where, "duplicate signal name in message"))
            sig_names.add(s.name)
            bits = signal_bits(s)
            if min(bits) < 0 or max(bits) >= m.length * 8:
                issues.append(_issue("error", where,
                                     f"bits {min(bits)}..{max(bits)} fall outside DLC {m.length}"))
            for r in s.receivers:
                if r not in node_names:
                    issues.append(_issue("error", where, f"receiver {r} is not a node"))
            rr = raw_range(s)
            if rr is not None:
                lo = rr[0] * s.scale + s.offset
                hi = rr[1] * s.scale + s.offset
                lo, hi = min(lo, hi), max(lo, hi)
                # DBC files print large limits with ~15 significant digits.
                eps = max(abs(lo), abs(hi)) * 1e-12 + abs(s.scale) * 1e-6
                if s.minimum is not None and s.minimum < lo - eps:
                    issues.append(_issue("warning", where,
                                         f"minimum {s.minimum:g} is below the encodable {lo:g}"))
                if s.maximum is not None and s.maximum > hi + eps:
                    issues.append(_issue("warning", where,
                                         f"maximum {s.maximum:g} is above the encodable {hi:g}"))
            if (s.minimum is not None and s.maximum is not None
                    and s.minimum > s.maximum):
                issues.append(_issue("error", where, "minimum is greater than maximum"))
        issues.extend(_overlaps(m))
    return issues
