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


class MergeTests(SessionCase):
    """Merging a second DBC; the incoming file is made by editing a copy of binocan.dbc."""

    def incoming(self, *edits):
        other = Session(self.tmp / "binocan.dbc", self.tmp / "src", doc_paths=[])
        for op in edits:
            other.apply(op)
        return other.text

    def test_self_merge_is_all_identical(self):
        analysis = self.session.merge_preview(self.session.text)
        self.assertEqual(analysis["summary"]["identical"], len(self.session.db.messages))
        with self.assertRaises(ops.OpError):
            self.session.apply({"op": "merge.apply", "text": self.session.text, "plan": {}})

    def test_new_message_brings_nodes_and_tables(self):
        text = self.incoming(
            {"op": "node.add", "name": "NEWNODE", "comment": "from the other file"},
            {"op": "table.add", "name": "NewTable", "entries": {"0": "a", "1": "b"}},
            {"op": "message.add", "name": "NEWX", "frame_id": 0x1F0, "length": 4, "senders": ["NEWNODE"],
             "cycle_time": 40},
            {"op": "signal.add", "frame_id": 0x1F0, "name": "sig_x", "start": 0, "length": 8,
             "receivers": ["ITF"]},
            {"op": "message.set", "frame_id": 0x1F0, "field": "send_type", "value": "CyclicIfActive"})
        analysis = self.session.merge_preview(text)
        self.assertEqual([(m["name"], m["status"]) for m in analysis["messages"] if m["status"] != "identical"],
                         [("NEWX", "new")])
        self.assertEqual([t["status"] for t in analysis["tables"] if t["name"] == "NewTable"], ["new"])
        self.session.apply({"op": "merge.apply", "text": text, "plan": {
            "messages": {"NEWX": {"action": "add"}}, "tables": {"NewTable": {"action": "add"}}}})
        m = self.msg(0x1F0)
        self.assertEqual((m.name, m.cycle_time, m.send_type, m.senders), ("NEWX", 40, "CyclicIfActive", ["NEWNODE"]))
        self.assertIn("NEWNODE", [n.name for n in self.session.db.nodes])
        self.assertIn("NewTable", self.session.db.dbc.value_tables)
        self.assertTrue(self.session.undo())
        self.assertNotIn(0x1F0, [x.frame_id for x in self.session.db.messages])

    def test_changed_message_is_replaced_only_on_request(self):
        text = self.incoming({"op": "message.set", "frame_id": SLOW, "field": "cycle_time", "value": 400},
                             {"op": "signal.set", "frame_id": SLOW, "name": "ITF_lv_voltage_v",
                              "field": "unit", "value": "mV"})
        item = next(m for m in self.session.merge_preview(text)["messages"] if m["status"] == "differs")
        self.assertEqual(item["name"], "ITF_slow_metrics")
        self.assertTrue(any("cycle time" in c for c in item["changes"]))
        self.assertTrue(any("ITF_lv_voltage_v" in c and "unit" in c for c in item["changes"]))
        with self.assertRaises(ops.OpError):          # add would duplicate it
            self.session.apply({"op": "merge.apply", "text": text,
                                "plan": {"messages": {"ITF_slow_metrics": {"action": "add"}}}})
        self.session.apply({"op": "merge.apply", "text": text,
                            "plan": {"messages": {"ITF_slow_metrics": {"action": "replace"}}}})
        self.assertEqual(self.msg().cycle_time, 400)
        self.assertEqual(self.sig("ITF_lv_voltage_v").unit, "mV")

    def test_frame_id_clash_needs_a_decision(self):
        text = self.incoming({"op": "message.set", "frame_id": 0x100, "field": "name", "value": "OTHER_FAST"})
        item = next(m for m in self.session.merge_preview(text)["messages"] if m["name"] == "OTHER_FAST")
        self.assertEqual(item["status"], "clash")
        self.assertGreater(item["free_id"], 0x100)
        with self.assertRaises(ops.OpError):
            self.session.apply({"op": "merge.apply", "text": text,
                                "plan": {"messages": {"OTHER_FAST": {"action": "add"}}}})
        self.session.apply({"op": "merge.apply", "text": text, "plan": {
            "messages": {"OTHER_FAST": {"action": "add", "new_id": item["free_id"]}}}})
        self.assertEqual(self.msg(item["free_id"]).name, "OTHER_FAST")
        self.assertEqual(self.msg(0x100).name, "ITF_fast_metrics")

    def test_unreadable_file_is_refused(self):
        with self.assertRaises(ops.OpError):
            self.session.merge_preview("this is not a dbc")

    def test_only_neighbouring_dbc_files_can_be_read(self):
        shutil.copy(self.tmp / "binocan.dbc", self.tmp / "other.dbc")
        self.assertEqual([f["name"] for f in self.session.sibling_dbcs()], ["other.dbc"])
        self.session.read_sibling("other.dbc")
        for bad in ("../README.md", "README.md", "binocan.dbc", "nope.dbc"):
            with self.subTest(name=bad), self.assertRaises(ops.OpError):
                self.session.read_sibling(bad)


class MultiplexingTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.session.apply({"op": "signal.add", "frame_id": SLOW, "name": "selector", "start": 56, "length": 2})

    def set(self, name, field, value):
        return self.session.apply({"op": "signal.set", "frame_id": SLOW, "name": name,
                                   "field": field, "value": value})

    def test_multiplex_a_signal(self):
        self.set("selector", "is_multiplexer", True)
        self.set("ITF_lv_voltage_v", "multiplexer_ids", [1, 0, 1])
        s = self.sig("ITF_lv_voltage_v")
        self.assertEqual((s.multiplexer_ids, s.multiplexer_signal), ([0, 1], "selector"))
        self.assertIn(" SG_ selector M :", self.session.text)
        self.assertIn(" SG_ ITF_lv_voltage_v m0 :", self.session.text)
        self.assertIn("SG_MUL_VAL_ 272 ITF_lv_voltage_v selector 0-1;", self.session.text)
        self.assertTrue(self.sig("selector").is_multiplexer)

    def test_refusals(self):
        with self.assertRaises(ops.OpError):                      # no multiplexer yet
            self.set("ITF_lv_voltage_v", "multiplexer_ids", [0])
        self.set("selector", "is_multiplexer", True)
        with self.assertRaises(ops.OpError):                      # 2 bits hold values 0..3
            self.set("ITF_lv_voltage_v", "multiplexer_ids", [4])
        with self.assertRaises(ops.OpError):                      # one multiplexer per message
            self.set("ITF_coolant_temp", "is_multiplexer", True)
        self.set("ITF_lv_voltage_v", "multiplexer_ids", [0])
        with self.assertRaises(ops.OpError):                      # it still carries a signal
            self.set("selector", "is_multiplexer", False)

    def test_take_out_of_multiplexing(self):
        self.set("selector", "is_multiplexer", True)
        self.set("ITF_lv_voltage_v", "multiplexer_ids", [2])
        self.set("ITF_lv_voltage_v", "multiplexer_ids", None)
        self.assertFalse(self.sig("ITF_lv_voltage_v").multiplexer_ids)
        self.set("selector", "is_multiplexer", False)
        self.assertFalse(self.sig("selector").is_multiplexer)


class AttributeTests(SessionCase):
    def test_define_set_and_delete(self):
        define = lambda **kw: self.session.apply({"op": "attribute.define", **kw})
        define(scope="message", name="MyNote", type="STRING")
        define(scope="signal", name="MyKind", type="ENUM", choices=["A", "B"], default="B")
        define(scope="node", name="NodeId", type="INT", minimum=0, maximum=255, default=1)
        define(scope="network", name="NetName", type="STRING")
        apply = lambda **kw: self.session.apply({"op": "attribute.set", **kw})
        apply(name="MyNote", frame_id=SLOW, value="hello")
        apply(name="MyKind", frame_id=SLOW, signal="ITF_lv_voltage_v", value="A")
        apply(name="NodeId", node="ITF", value=7)
        apply(name="NetName", value="main")
        saved = Session(self.tmp / "binocan.dbc", self.tmp / "src", doc_paths=[])   # untouched on disk
        self.assertNotIn("MyNote", saved.db.dbc.attribute_definitions)
        self.session.save(sync_docs=False)
        again = Session(self.tmp / "binocan.dbc", self.tmp / "src", doc_paths=[])
        values = {(v["scope"], v["name"]): v["value"]
                  for v in __import__("binocan_dbc.jsonmodel", fromlist=["x"]).attributes_json(again.db)["values"]}
        self.assertEqual(values[("message", "MyNote")], "hello")
        self.assertEqual(values[("signal", "MyKind")], "A")
        self.assertEqual(values[("node", "NodeId")], 7)
        self.assertEqual(values[("network", "NetName")], "main")
        self.session.apply({"op": "attribute.delete", "name": "MyNote"})
        self.assertNotIn("MyNote", self.session.db.dbc.attribute_definitions)
        self.assertNotIn("MyNote", self.session.text)

    def test_refusals(self):
        self.session.apply({"op": "attribute.define", "scope": "node", "name": "NodeId", "type": "INT",
                            "minimum": 0, "maximum": 255})
        for op in [{"op": "attribute.set", "name": "NodeId", "node": "ITF", "value": 300},
                   {"op": "attribute.set", "name": "NodeId", "node": "NOPE", "value": 1},
                   {"op": "attribute.set", "name": "GenMsgCycleTime", "frame_id": SLOW, "value": 5},
                   {"op": "attribute.delete", "name": "GenMsgSendType"},
                   {"op": "attribute.define", "scope": "node", "name": "NodeId", "type": "INT"},
                   {"op": "attribute.define", "scope": "message", "name": "Bad", "type": "ENUM", "choices": []},
                   {"op": "attribute.set", "name": "Nothing", "value": 1}]:
            with self.subTest(op=op), self.assertRaises(ops.OpError):
                self.session.apply(op)


class CompareTests(SessionCase):
    def test_unsaved_changes_against_saved_file(self):
        self.assertTrue(self.session.compare_with(self.session.text, "saved")["same"])
        self.session.apply({"op": "message.set", "frame_id": SLOW, "field": "cycle_time", "value": 400})
        self.session.apply({"op": "node.add", "name": "TST"})
        r = self.session.compare_with(self.session.current()[1], "saved")
        self.assertFalse(r["same"])
        self.assertEqual(r["messages_changed"][0]["changes"], ["cycle time: 500 -> 400"])
        self.assertEqual(r["nodes_only_there"], ["TST"])

    def test_against_another_file(self):
        other = Session(self.tmp / "binocan.dbc", self.tmp / "src", doc_paths=[])
        other.apply({"op": "message.delete", "frame_id": SLOW})
        r = self.session.compare_with(other.text)
        self.assertEqual([m["name"] for m in r["messages_only_here"]], ["ITF_slow_metrics"])
