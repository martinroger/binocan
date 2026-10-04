"""Loading, writing and comparing DBC databases through cantools.

cantools reloads its own DBC output without semantic loss, but it drops a few
network-level attribute definitions (e.g. ``BA_DEF_ "Baudrate"``) on dump.
``dumps`` re-adds those from the original text so a save never loses them.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cantools
from cantools.database.can import Database

ENCODING = "cp1252"  # cantools' default DBC encoding

MESSAGE_FIELDS = (
    "frame_id",
    "is_extended_frame",
    "is_fd",
    "length",
    "senders",
    "cycle_time",
    "send_type",
    "comment",
)
SIGNAL_FIELDS = (
    "start",
    "length",
    "byte_order",
    "is_signed",
    "is_float",
    "scale",
    "offset",
    "minimum",
    "maximum",
    "unit",
    "receivers",
    "comment",
    "is_multiplexer",
    "multiplexer_ids",
    "multiplexer_signal",
    "raw_initial",
    "choices",
)

# Line classes that must never shrink on a load and save without edits.
LINE_CLASSES = ("BO_ ", " SG_ ", "BU_:", "CM_ ", "BA_DEF_ ", "BA_DEF_DEF_ ", "BA_ ",
                "VAL_ ", "VAL_TABLE_ ", "SIG_VALTYPE_ ")

_ATTR_DEF_RE = re.compile(r'^BA_DEF_\s+(?:(BU_|BO_|SG_|EV_)\s+)?"([^"]+)"')
_ATTR_DEF_DEF_RE = re.compile(r'^BA_DEF_DEF_\s+"([^"]+)"')
_ATTR_NET_RE = re.compile(r'^BA_\s+"([^"]+)"\s+(?!BU_|BO_|SG_|EV_)')


def load(path: Path) -> Tuple[Database, str]:
    """Returns the database and the original file text."""
    path = Path(path)
    text = path.read_text(encoding=ENCODING)
    db = cantools.database.load_string(text, database_format="dbc", strict=False)
    return db, text


def _dropped_lines(original: str, dumped: str, known: Optional[set] = None) -> List[Tuple[str, str]]:
    """Attribute lines from ``original`` that cantools did not write back.

    Only definitions, their defaults and network-level values are restored.
    Per-object values follow the model, so a deleted message stays deleted.
    When ``known`` (the attribute names the model still defines) is given, lines
    for other names are not restored, so a deleted attribute stays deleted.
    """
    def keys(text: str) -> Dict[str, set]:
        out = {"def": set(), "defdef": set(), "net": set()}
        for line in text.splitlines():
            m = _ATTR_DEF_RE.match(line)
            if m:
                out["def"].add((m.group(1) or "", m.group(2)))
                continue
            m = _ATTR_DEF_DEF_RE.match(line)
            if m:
                out["defdef"].add(m.group(1))
                continue
            m = _ATTR_NET_RE.match(line)
            if m:
                out["net"].add(m.group(1))
        return out

    have = keys(dumped)
    alive = (lambda name: True) if known is None else (lambda name: name in known)
    missing = []
    for line in original.splitlines():
        m = _ATTR_DEF_RE.match(line)
        if m and (m.group(1) or "", m.group(2)) not in have["def"]:
            if alive(m.group(2)):
                missing.append(("def", line))
            continue
        m = _ATTR_DEF_DEF_RE.match(line)
        if m and m.group(1) not in have["defdef"]:
            if alive(m.group(1)):
                missing.append(("defdef", line))
            continue
        m = _ATTR_NET_RE.match(line)
        if m and m.group(1) not in have["net"]:
            if alive(m.group(1)):
                missing.append(("net", line))
    return missing


def _insert_after_last(lines: List[str], prefix: str, new: List[str],
                       fallback_prefix: Optional[str] = None) -> None:
    idx = max((i for i, l in enumerate(lines) if l.startswith(prefix)), default=None)
    if idx is None and fallback_prefix:
        idx = max((i for i, l in enumerate(lines) if l.startswith(fallback_prefix)),
                  default=None)
    if idx is None:
        lines.extend(new)
    else:
        lines[idx + 1:idx + 1] = new


def _canonical_order(db: Database) -> None:
    """Fixes signal order so repeated saves produce identical text.

    cantools sorts signals by start bit on load but keeps file order for ties
    (multiplexed signals sharing a start bit), and its dump order depends on
    that, so without this two saves can disagree. cantools writes signals in
    reverse model order, so sorting ties by descending multiplexer value puts
    them in ascending order in the file, and in the generated C structs.
    """
    for m in db.messages:
        m.signals.sort(key=lambda s: (s.start, tuple(-v for v in (s.multiplexer_ids or ())),
                                      s.name))
    db.refresh()


def dumps(db: Database, original: Optional[str] = None) -> str:
    """DBC text for ``db``, with dropped attribute lines restored from ``original``."""
    _canonical_order(db)
    text = db.as_dbc_string()
    if not original:
        return text
    known = set(db.dbc.attribute_definitions) if db.dbc is not None else None
    missing = _dropped_lines(original, text, known)
    if not missing:
        return text
    lines = text.splitlines()
    defs = [l for kind, l in missing if kind == "def"]
    defdefs = [l for kind, l in missing if kind == "defdef"]
    nets = [l for kind, l in missing if kind == "net"]
    if defs:
        _insert_after_last(lines, "BA_DEF_ ", defs, fallback_prefix="CM_ ")
    if defdefs:
        _insert_after_last(lines, "BA_DEF_DEF_ ", defdefs, fallback_prefix="BA_DEF_ ")
    if nets:
        _insert_after_last(lines, "BA_DEF_DEF_ ", nets, fallback_prefix="BA_DEF_ ")
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def line_class_counts(text: str) -> Dict[str, int]:
    counts = {}
    for cls in LINE_CLASSES:
        if cls == " SG_ ":
            counts[cls] = sum(1 for l in text.splitlines() if l.lstrip().startswith("SG_ "))
        else:
            counts[cls] = sum(1 for l in text.splitlines() if l.startswith(cls))
    return counts


def _norm(value):
    if isinstance(value, dict):
        return {k: str(v) for k, v in value.items()}
    if isinstance(value, list):
        return list(value)
    if hasattr(value, "name") and not isinstance(value, (str, int, float)):
        return value.name
    return value


def semantic_diff(a: Database, b: Database) -> List[str]:
    """Human-readable differences between two databases; empty when equal."""
    diffs = []
    names_a = {n.name: n.comment for n in a.nodes}
    names_b = {n.name: n.comment for n in b.nodes}
    if names_a != names_b:
        diffs.append(f"nodes: {names_a} != {names_b}")
    vt_a = {k: _norm(v) for k, v in (a.dbc.value_tables or {}).items()} if a.dbc else {}
    vt_b = {k: _norm(v) for k, v in (b.dbc.value_tables or {}).items()} if b.dbc else {}
    if vt_a != vt_b:
        diffs.append("value tables differ")
    ids_a = {m.frame_id for m in a.messages}
    ids_b = {m.frame_id for m in b.messages}
    for fid in sorted(ids_a ^ ids_b):
        diffs.append(f"message 0x{fid:X} only in one database")
    for fid in sorted(ids_a & ids_b):
        ma = a.get_message_by_frame_id(fid)
        mb = b.get_message_by_frame_id(fid)
        if ma.name != mb.name:
            diffs.append(f"0x{fid:X}: name {ma.name} != {mb.name}")
        for f in MESSAGE_FIELDS:
            va, vb = _norm(getattr(ma, f)), _norm(getattr(mb, f))
            if va != vb:
                diffs.append(f"{ma.name}.{f}: {va!r} != {vb!r}")
        sa = {s.name: s for s in ma.signals}
        sb = {s.name: s for s in mb.signals}
        for name in sorted(set(sa) ^ set(sb)):
            diffs.append(f"{ma.name}.{name}: signal only in one database")
        for name in sorted(set(sa) & set(sb)):
            for f in SIGNAL_FIELDS:
                va, vb = _norm(getattr(sa[name], f)), _norm(getattr(sb[name], f))
                if va != vb:
                    diffs.append(f"{ma.name}.{name}.{f}: {va!r} != {vb!r}")
    return diffs


class SaveError(Exception):
    pass


def save(db: Database, path: Path, original: Optional[str] = None,
         expected_disk_text: Optional[str] = None, backup: bool = True) -> str:
    """Validates, writes atomically, re-reads and compares. Returns the written text.

    ``expected_disk_text`` is what the file held when it was opened; the save is
    refused if someone else changed the file since.
    """
    path = Path(path)
    if expected_disk_text is not None and path.exists():
        if path.read_text(encoding=ENCODING) != expected_disk_text:
            raise SaveError(f"{path.name} changed on disk since it was opened; reload first.")

    text = dumps(db, original)
    reloaded = cantools.database.load_string(text, database_format="dbc", strict=False)
    diffs = semantic_diff(db, reloaded)
    if diffs:
        raise SaveError("written DBC does not reload identically: " + "; ".join(diffs[:5]))

    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding=ENCODING, newline="") as fh:
            fh.write(text)
        if backup and path.exists():
            shutil.copy2(path, str(path) + ".bak")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return text
