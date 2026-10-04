"""Tests for editing operations, sessions (undo, save) and the README/TOO.MD sync."""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
REPO = HERE.parents[2]

from binocan_dbc import docsync, model, ops  # noqa: E402
from binocan_dbc.session import Session  # noqa: E402

SLOW = 0x110  # ITF_slow_metrics


class SessionCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for name in ("binocan.dbc", "README.md", "TOO.MD"):
            shutil.copy(REPO / name, self.tmp / name)
        self.session = Session(self.tmp / "binocan.dbc", self.tmp / "src")

    def msg(self, fid=SLOW):
        return next(m for m in self.session.db.messages if m.frame_id == fid)

    def sig(self, name, fid=SLOW):
        return next(s for s in self.msg(fid).signals if s.name == name)


class OpTests(SessionCase):
    def test_message_fields(self):
        for field, value in [("cycle_time", 400), ("comment", "slow"), ("send_type", "CyclicIfActive"),
                             ("senders", ["LDB"]), ("length", 6), ("name", "ITF_slow2")]:
            with self.subTest(field=field):
                self.session.apply({"op": "message.set", "frame_id": SLOW, "field": field, "value": value})
        m = self.msg()
        self.assertEqual((m.cycle_time, m.comment, m.send_type, m.senders, m.length, m.name),
                         (400, "slow", "CyclicIfActive", ["LDB"], 6, "ITF_slow2"))

    def test_frame_id_change_and_clash(self):
        self.session.apply({"op": "message.set", "frame_id": SLOW, "field": "frame_id", "value": 0x115})
        self.assertEqual(self.msg(0x115).name, "ITF_slow_metrics")
        with self.assertRaises(ops.OpError):
            self.session.apply({"op": "message.set", "frame_id": 0x115, "field": "frame_id", "value": 0x100})

    def test_signal_fields_persist(self):
        base = {"op": "signal.set", "frame_id": SLOW, "name": "ITF_lv_voltage_v"}
        for field, value in [("scale", 0.5), ("offset", 1), ("minimum", 0), ("maximum", 50), ("unit", "V"),
                             ("comment", "c"), ("receivers", ["LDB"]), ("initial", 5)]:
            self.session.apply({**base, "field": field, "value": value})
        s = self.sig("ITF_lv_voltage_v")
        self.assertEqual((s.scale, s.offset, s.minimum, s.maximum, s.unit, s.comment, s.receivers, s.raw_initial),
                         (0.5, 1, 0, 50, "V", "c", ["LDB"], 5))

    def test_overlap_is_reported_but_blocks_save(self):
        res = self.session.apply({"op": "signal.set", "frame_id": SLOW, "name": "ITF_lv_voltage_v",
                                  "field": "start", "value": 0})
        self.assertTrue(any(i["level"] == "error" for i in res["issues"]))
        with self.assertRaises(model.SaveError):
            self.session.save()

    def test_refusals_leave_state_alone(self):
        before = self.session.text
        for op in [{"op": "nope"},
                   {"op": "message.set", "frame_id": 0x7FE, "field": "name", "value": "x"},
                   {"op": "message.set", "frame_id": SLOW, "field": "name", "value": "bad name"},
                   {"op": "message.set", "frame_id": SLOW, "field": "cycle_time", "value": -5},
                   {"op": "node.add", "name": "ITF"},
                   {"op": "node.delete", "name": "ITF"}]:
            with self.subTest(op=op), self.assertRaises(ops.OpError):
                self.session.apply(op)
        self.assertEqual(self.session.text, before)
        self.assertFalse(self.session.dirty)

    def test_add_duplicate_delete_message(self):
        self.session.apply({"op": "message.add", "name": "NEWMSG", "frame_id": 0x140, "length": 8,
                            "senders": ["ITF"], "cycle_time": 50})
        self.session.apply({"op": "signal.add", "frame_id": 0x140, "name": "a", "start": 0, "length": 8})
        self.session.apply({"op": "message.duplicate", "frame_id": 0x140, "name": "NEWMSG2", "new_frame_id": 0x141})
        self.assertEqual([s.name for s in self.msg(0x141).signals], ["a"])
        self.session.apply({"op": "message.delete", "frame_id": 0x141})
        self.assertNotIn(0x141, [m.frame_id for m in self.session.db.messages])

    def test_choices_and_tables(self):
        self.session.apply({"op": "table.add", "name": "T1", "entries": {"0": "off", "1": "on"}})
        self.session.apply({"op": "signal.apply_table", "frame_id": SLOW, "name": "ITF_lv_voltage_v", "table": "T1"})
        self.assertEqual(dict(self.sig("ITF_lv_voltage_v").choices), {0: "off", 1: "on"})
        self.session.apply({"op": "table.set", "name": "T1", "entries": {"0": "off", "1": "on", "2": "err"},
                            "apply_to_signals": True})
        self.assertEqual(dict(self.sig("ITF_lv_voltage_v").choices)[2], "err")
        self.session.apply({"op": "table.rename", "name": "T1", "new_name": "T2"})
        self.assertIn("T2", self.session.db.dbc.value_tables)

    def test_node_lifecycle(self):
        self.session.apply({"op": "node.add", "name": "TST", "comment": "tester"})
        self.session.apply({"op": "node.rename", "name": "TST", "new_name": "TESTER"})
        self.session.apply({"op": "message.set_node_role", "frame_id": SLOW, "node": "TESTER", "role": "rx"})
        self.assertIn("TESTER", self.sig("ITF_lv_voltage_v").receivers)
        with self.assertRaises(ops.OpError):
            self.session.apply({"op": "node.delete", "name": "TESTER"})
        self.session.apply({"op": "node.delete", "name": "TESTER", "force": True})
        self.assertNotIn("TESTER", [n.name for n in self.session.db.nodes])

    def test_undo_redo(self):
        original = self.session.text
        self.session.apply({"op": "message.set", "frame_id": SLOW, "field": "cycle_time", "value": 400})
        self.assertTrue(self.session.dirty)
        self.assertTrue(self.session.undo())
        self.assertEqual(self.session.text, original)
        self.assertEqual(self.msg().cycle_time, 500)
        self.assertTrue(self.session.redo())
        self.assertEqual(self.msg().cycle_time, 400)
        self.assertFalse(self.session.redo())


class SaveTests(SessionCase):
    def test_save_writes_backup_and_syncs_docs(self):
        self.session.apply({"op": "message.set", "frame_id": SLOW, "field": "cycle_time", "value": 400})
        res = self.session.save()
        self.assertEqual(res["saved"], "binocan.dbc")
        self.assertEqual(sorted(res["docs"]), ["README.md", "TOO.MD"])
        self.assertTrue((self.tmp / "binocan.dbc.bak").is_file())
        self.assertIn("400 ms (2.5 Hz)", (self.tmp / "README.md").read_text(encoding="utf-8"))
        self.assertFalse(self.session.dirty)
        self.assertFalse(self.session.undo_stack)
        self.assertEqual(res["warnings"], [])

    def test_save_refuses_when_file_changed_on_disk(self):
        self.session.apply({"op": "message.set", "frame_id": SLOW, "field": "comment", "value": "x"})
        path = self.tmp / "binocan.dbc"
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaises(model.SaveError):
            self.session.save()
        self.assertTrue(self.session.status()["disk_changed"])

    def test_saved_file_reloads_to_the_edited_database(self):
        self.session.apply({"op": "message.set", "frame_id": SLOW, "field": "comment", "value": "kept"})
        self.session.save()
        fresh = Session(self.tmp / "binocan.dbc", self.tmp / "src")
        self.assertEqual(next(m for m in fresh.db.messages if m.frame_id == SLOW).comment, "kept")


class DocSyncTests(SessionCase):
    def test_noop_on_repo_documents(self):
        results = docsync.sync(self.session.db, self.session.doc_paths, dry_run=True)
        self.assertEqual([r.changed for r in results], [False, False])
        self.assertEqual([w for r in results for w in r.warnings], [])

    def test_new_message_row_and_uncovered_warning(self):
        self.session.apply({"op": "message.add", "name": "NEWMSG", "frame_id": 0x400, "length": 8,
                            "senders": ["ITF"], "cycle_time": 50})
        results = docsync.sync(self.session.db, self.session.doc_paths, dry_run=True)
        warnings = [w for r in results for w in r.warnings]
        self.assertTrue(any("NEWMSG" in w and "no cycle-time table" in w for w in warnings))
        self.session.apply({"op": "message.set", "frame_id": 0x400, "field": "frame_id", "value": 0x150})
        results = docsync.sync(self.session.db, self.session.doc_paths, dry_run=True)
        self.assertTrue(any(r.changed for r in results))


if __name__ == "__main__":
    unittest.main()
