"""Keeps the cycle-time tables in README.md and TOO.MD in step with the DBC.

A table is generated when it sits between two marker comments:

    <!-- dbc:cycle-table ids=0x100-0x1FF -->
    | Message Name | CAN ID (Hex) | ... |
    ...
    <!-- /dbc:cycle-table -->

Rows are the DBC messages whose frame ID is in the range. Columns are
recognised by their header: message name, CAN ID (Hex/Dec), transmitter,
periodicity and send type come from the DBC; any other column (such as Key
Signals) keeps its hand-written text for messages already in the table. A
generated cell that already says the same thing in its own words
("Cyclic & On Request", "200 ms / Event", "Tester / External") is left alone, so
saving an unchanged DBC leaves the documents untouched.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

from cantools.database.can import Database, Message

BLOCK_RE = re.compile(
    r"(?P<open><!--\s*dbc:cycle-table\s+ids=(?P<lo>0x[0-9A-Fa-f]+)-(?P<hi>0x[0-9A-Fa-f]+)\s*-->[ \t]*\n)"
    r"(?P<body>.*?)"
    r"(?P<close><!--\s*/dbc:cycle-table\s*-->)",
    re.DOTALL,
)
NAME_IN_CELL_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")

SEND_TYPE_TEXT = {
    "Cyclic": "Cyclic",
    "CyclicAndSendOnRequest": "Cyclic & On Request",
    "SendOnRequest": "SendOnRequest",
    "CyclicIfActive": "Cyclic If Active",
    "SendOnRequestWithDelay": "On Request With Delay",
    "CyclicAndSendOnRequestWithDelay": "Cyclic & On Request With Delay",
    "IfActive": "If Active",
    "NoMsgSendType": "None",
}


class SyncResult(NamedTuple):
    path: Path
    changed: bool
    warnings: List[str]


def _send_type_norm(text: str) -> str:
    return re.sub(r"send", "", re.sub(r"[^a-z]", "", text.lower().replace("&", "and")))


def _is_cyclic(msg: Message) -> bool:
    return "Cyclic" in (msg.send_type or "Cyclic") or (msg.send_type or "") in ("IfActive",)


def _hz(cycle_ms: int) -> str:
    hz = 1000.0 / cycle_ms
    return str(int(hz)) if hz.is_integer() else f"{hz:.1f}".rstrip("0").rstrip(".")


def _split_row(line: str) -> List[str]:
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    cells, cur, in_code = [], "", False
    for ch in inner:
        if ch == "`":
            in_code = not in_code
        if ch == "|" and not in_code:
            cells.append(cur.strip())
            cur = ""
        else:
            cur += ch
    cells.append(cur.strip())
    return cells


def _join_row(cells: List[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _transmitter_cell(msg: Message, old: Optional[str]) -> str:
    senders = list(msg.senders)
    if old is not None:
        tokens = {t.lower() for t in re.findall(r"[A-Za-z0-9_]+", old)}
        if senders and all(s.lower() in tokens for s in senders):
            return old
    return ", ".join(senders) if senders else "—"


def _periodicity_cell(msg: Message, old: Optional[str]) -> str:
    cyclic = _is_cyclic(msg)
    if old is not None:
        m = re.search(r"(\d+)\s*ms", old)
        if cyclic and m and int(m.group(1)) == msg.cycle_time:
            return old
        if not cyclic and old.strip().lower().startswith("aperiodic"):
            return old
    if cyclic and msg.cycle_time:
        return f"{msg.cycle_time} ms ({_hz(msg.cycle_time)} Hz)"
    return "Aperiodic"


def _send_type_cell(msg: Message, old: Optional[str]) -> str:
    st = msg.send_type or "Cyclic"
    if old is not None and _send_type_norm(old) == _send_type_norm(st):
        return old
    return SEND_TYPE_TEXT.get(st, st)


def _fallback_cell(msg: Message) -> str:
    comment = " ".join((msg.comment or "").split())
    if comment:
        return comment
    return ", ".join(f"`{s.name}`" for s in msg.signals[:4])


def _cell(header: str, msg: Message, old: Optional[str]) -> str:
    h = re.sub(r"\s+", " ", header.strip().lower())
    if h == "message name":
        return f"**`{msg.name}`**"
    if h in ("can id (hex)", "id (hex)", "can id"):
        return f"`0x{msg.frame_id:03X}`"
    if h in ("can id (dec)", "id (dec)"):
        return str(msg.frame_id)
    if h in ("transmitter", "sender"):
        return _transmitter_cell(msg, old)
    if h in ("periodicity", "period", "cycle time"):
        return _periodicity_cell(msg, old)
    if h == "send type":
        return _send_type_cell(msg, old)
    return old if old not in (None, "") else _fallback_cell(msg)


def _render_table(old_body: str, messages: List[Message]) -> Optional[str]:
    lines = [l for l in old_body.splitlines() if l.strip().startswith("|")]
    if len(lines) < 2:
        return None
    header = _split_row(lines[0])
    separator = lines[1]
    old_rows: Dict[str, List[str]] = {}
    for line in lines[2:]:
        cells = _split_row(line)
        m = NAME_IN_CELL_RE.search(cells[0]) if cells else None
        if m:
            old_rows[m.group(1)] = cells
    out = [lines[0], separator]
    for msg in messages:
        old = old_rows.get(msg.name)
        cells = []
        for i, h in enumerate(header):
            prev = old[i] if old is not None and i < len(old) else None
            cells.append(_cell(h, msg, prev))
        out.append(_join_row(cells))
    return "\n".join(out) + "\n"


def sync_text(db: Database, text: str, source: str = "") -> Tuple[str, List[str], set]:
    """Returns the text with every marked table regenerated, warnings, and covered frame IDs."""
    warnings: List[str] = []
    covered: set = set()

    def replace(m: "re.Match[str]") -> str:
        lo, hi = int(m.group("lo"), 16), int(m.group("hi"), 16)
        messages = sorted((x for x in db.messages if lo <= x.frame_id <= hi),
                          key=lambda x: x.frame_id)
        table = _render_table(m.group("body"), messages)
        if table is None:
            warnings.append(f"{source}: table for ids {m.group('lo')}-{m.group('hi')} has no header")
            return m.group(0)
        covered.update(x.frame_id for x in messages)
        return m.group("open") + table + m.group("close")

    return BLOCK_RE.sub(replace, text), warnings, covered


def sync(db: Database, paths: List[Path], dry_run: bool = False) -> List[SyncResult]:
    results: List[SyncResult] = []
    covered: set = set()
    names: List[str] = []
    for path in paths:
        path = Path(path)
        if not path.is_file():
            continue
        text = path.read_bytes().decode("utf-8")
        if not BLOCK_RE.search(text):
            continue
        new_text, warnings, ids = sync_text(db, text, path.name)
        covered |= ids
        names.append(path.name)
        changed = new_text != text
        if changed and not dry_run:
            fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(new_text.encode("utf-8"))
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        results.append(SyncResult(path, changed, warnings))
    if results:
        missing = [f"{m.name} (0x{m.frame_id:X}) is in no cycle-time table of {', '.join(names)}"
                   for m in sorted(db.messages, key=lambda x: x.frame_id)
                   if m.frame_id not in covered]
        if missing:
            results[-1].warnings.extend(missing)
    return results
