"""The open DBC as an editing session: working copy, undo and redo, and save.

The working state is DBC text. Every operation is applied to a copy of the
database, dumped, loaded back and checked, so what the browser shows is
always what a save would write. Undo and redo just swap texts.
"""

from __future__ import annotations

import secrets
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import cantools
from cantools.database.can import Database

from . import docsync, generate, model, ops, validate

HISTORY_LIMIT = 200


class Session:
    def __init__(self, dbc_path: Path, c_output_dir: Path, doc_paths: Optional[List[Path]] = None):
        self.path = Path(dbc_path).resolve()
        self.c_output_dir = Path(c_output_dir).resolve()
        self.doc_paths = [Path(p).resolve() for p in (doc_paths if doc_paths is not None
                                                      else self._default_docs())]
        self.token = secrets.token_urlsafe(24)
        self._lock = threading.RLock()
        self._load_from_disk()

    # ---------- state ----------

    def _default_docs(self) -> List[Path]:
        return [p for p in (self.path.parent / "README.md", self.path.parent / "TOO.MD")
                if p.is_file()]

    def _load_from_disk(self) -> None:
        text = self.path.read_text(encoding=model.ENCODING)
        self.disk_text = text
        self.original_text = text           # source of attribute lines cantools drops
        self.text = text
        self.saved_text = text
        self.db = self._parse(text)
        self.undo_stack: List[str] = []
        self.redo_stack: List[str] = []
        self._mtime = self.path.stat().st_mtime

    @staticmethod
    def _parse(text: str) -> Database:
        return cantools.database.load_string(text, database_format="dbc", strict=False)

    @property
    def dirty(self) -> bool:
        return self.text != self.saved_text

    def disk_changed(self) -> bool:
        try:
            return self.path.read_text(encoding=model.ENCODING) != self.disk_text
        except OSError:
            return True

    def current(self):
        """The working database and its text. Reloads silently when clean and the file moved."""
        with self._lock:
            if not self.dirty and self.disk_changed():
                self._load_from_disk()
            return self.db, self.text

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "dirty": self.dirty,
                "can_undo": bool(self.undo_stack),
                "can_redo": bool(self.redo_stack),
                "disk_changed": self.dirty and self.disk_changed(),
                "docs": [p.name for p in self.doc_paths],
            }

    # ---------- merging ----------

    def sibling_dbcs(self) -> List[Dict[str, Any]]:
        """Other .dbc files next to the open one, offered as merge sources."""
        return [{"name": f.name, "size": f.stat().st_size}
                for f in sorted(self.path.parent.glob("*.dbc"))
                if f.is_file() and f.resolve() != self.path]

    def read_sibling(self, name: str) -> str:
        target = (self.path.parent / str(name)).resolve()
        if target.parent != self.path.parent or target.suffix.lower() != ".dbc" \
                or target == self.path or not target.is_file():
            raise ops.OpError(f"{name} is not a DBC file next to {self.path.name}")
        return target.read_text(encoding=model.ENCODING)

    def merge_preview(self, text: str) -> Dict[str, Any]:
        from . import merge
        with self._lock:
            db, _ = self.current()
            return merge.analyse(db, merge.parse(text))

    # ---------- editing ----------

    def apply(self, op: Dict[str, Any]) -> Dict[str, Any]:
        """Applies one operation. Raises ops.OpError and leaves the state alone on refusal."""
        with self._lock:
            self.current()
            work = self._parse(self.text)
            select, check = ops.apply_op(work, op)
            new_text = model.dumps(work, self.original_text)
            try:
                reloaded = self._parse(new_text)
            except Exception as exc:  # cantools could not read what it wrote
                raise ops.OpError(f"That change produces a DBC cantools cannot read: {exc}") from exc
            if check is not None:
                try:
                    persisted = check(reloaded)
                except Exception:
                    persisted = False
                if not persisted:
                    raise ops.OpError("cantools did not keep that change when writing the DBC, "
                                      "so it was not applied")
            if new_text != self.text:
                self._push_undo()
                self.text = new_text
                self.db = reloaded
                self.redo_stack.clear()
            return {"select": select, "issues": validate.check(self.db)}

    def _push_undo(self) -> None:
        self.undo_stack.append(self.text)
        del self.undo_stack[:-HISTORY_LIMIT]

    def undo(self) -> bool:
        with self._lock:
            if not self.undo_stack:
                return False
            self.redo_stack.append(self.text)
            self.text = self.undo_stack.pop()
            self.db = self._parse(self.text)
            return True

    def redo(self) -> bool:
        with self._lock:
            if not self.redo_stack:
                return False
            self.undo_stack.append(self.text)
            self.text = self.redo_stack.pop()
            self.db = self._parse(self.text)
            return True

    def reload(self) -> None:
        """Throws the working copy away and reads the file again."""
        with self._lock:
            self._load_from_disk()

    # ---------- saving ----------

    def save(self, sync_docs: bool = True) -> Dict[str, Any]:
        with self._lock:
            errors = [i for i in validate.check(self.db) if i["level"] == "error"]
            if errors:
                raise model.SaveError(
                    f"{len(errors)} error(s) in the DBC; fix them before saving "
                    f"(first: {errors[0]['where']}: {errors[0]['text']})")
            written = model.save(self.db, self.path, original=self.original_text,
                                 expected_disk_text=self.disk_text)
            self.disk_text = written
            self.original_text = written
            self.text = written
            self.saved_text = written
            self.db = self._parse(written)
            self.undo_stack.clear()
            self.redo_stack.clear()
            result: Dict[str, Any] = {"saved": self.path.name, "docs": [], "warnings": []}
            if sync_docs and self.doc_paths:
                for r in docsync.sync(self.db, self.doc_paths):
                    if r.changed:
                        result["docs"].append(r.path.name)
                    result["warnings"].extend(r.warnings)
            try:
                gen = generate.generate(self.path, self.c_output_dir, dry_run=True)
                result["c_stale"] = [Path(p).name for p in gen["changed"] + gen["created"]]
            except Exception as exc:
                result["c_stale"] = []
                result["warnings"].append(f"Could not check the C sources: {exc}")
            return result
