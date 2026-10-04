"""Comparing two DBCs (or the working copy with the saved file), read only."""

from __future__ import annotations

from typing import Any, Dict, List

from cantools.database.can import Database

from . import merge


def compare(a: Database, b: Database) -> Dict[str, Any]:
    """What differs going from ``a`` (the open DBC) to ``b`` (the other one)."""
    mine = {m.name: m for m in a.messages}
    theirs = {m.name: m for m in b.messages}
    only_a = [{"name": n, "hex_id": f"0x{m.frame_id:03X}", "signal_count": len(m.signals)}
              for n, m in sorted(mine.items(), key=lambda kv: kv[1].frame_id) if n not in theirs]
    only_b = [{"name": n, "hex_id": f"0x{m.frame_id:03X}", "signal_count": len(m.signals)}
              for n, m in sorted(theirs.items(), key=lambda kv: kv[1].frame_id) if n not in mine]
    changed: List[Dict[str, Any]] = []
    for n, m in sorted(mine.items(), key=lambda kv: kv[1].frame_id):
        if n in theirs:
            diff = merge.describe_difference(m, theirs[n])
            if diff:
                changed.append({"name": n, "hex_id": f"0x{m.frame_id:03X}", "changes": diff})

    def table_map(db: Database) -> Dict[str, Dict[int, str]]:
        return {k: merge._table(v) for k, v in merge._tables(db).items()}

    ta, tb = table_map(a), table_map(b)
    na, nb = {n.name for n in a.nodes}, {n.name for n in b.nodes}
    return {
        "messages_only_here": only_a,
        "messages_only_there": only_b,
        "messages_changed": changed,
        "tables_only_here": sorted(set(ta) - set(tb)),
        "tables_only_there": sorted(set(tb) - set(ta)),
        "tables_changed": sorted(k for k in ta if k in tb and ta[k] != tb[k]),
        "nodes_only_here": sorted(na - nb),
        "nodes_only_there": sorted(nb - na),
        "same": not (only_a or only_b or changed or set(ta) ^ set(tb)
                     or any(ta[k] != tb[k] for k in ta if k in tb) or na ^ nb),
    }
