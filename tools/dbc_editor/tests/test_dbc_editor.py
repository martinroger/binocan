"""Tests for the binocan DBC tools. Run from tools/dbc_editor:

    python -m unittest discover -s tests
"""

import json
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
from unittest import mock
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
REPO = HERE.parents[2]
DBCS = [REPO / "binocan.dbc", REPO / "racebox_companion.dbc"]

import cantools  # noqa: E402

from binocan_dbc import busload, deps, generate, model, server, validate  # noqa: E402


class FidelityTests(unittest.TestCase):
    def test_round_trip_is_lossless(self):
        for path in DBCS:
            with self.subTest(dbc=path.name):
                db, text = model.load(path)
                out = model.dumps(db, text)
                reloaded = cantools.database.load_string(out, database_format="dbc", strict=False)
                self.assertEqual(model.semantic_diff(db, reloaded), [])

    def test_no_line_class_shrinks(self):
        for path in DBCS:
            with self.subTest(dbc=path.name):
                db, text = model.load(path)
                before = model.line_class_counts(text)
                after = model.line_class_counts(model.dumps(db, text))
                for cls, n in before.items():
                    self.assertGreaterEqual(after[cls], n, f"{cls!r} lines lost")

    def test_baudrate_definition_kept(self):
        db, text = model.load(REPO / "binocan.dbc")
        out = model.dumps(db, text)
        self.assertIn('BA_DEF_  "Baudrate" INT 0 1000000;', out)
        self.assertIn('BA_DEF_DEF_  "Baudrate" 500000;', out)

    def test_dumps_is_stable(self):
        db, text = model.load(REPO / "binocan.dbc")
        once = model.dumps(db, text)
        db2 = cantools.database.load_string(once, database_format="dbc", strict=False)
        self.assertEqual(model.dumps(db2, once), once)

    def test_save_writes_backup_and_refuses_stale_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "binocan.dbc"
            shutil.copyfile(REPO / "binocan.dbc", target)
            db, text = model.load(target)
            db.get_message_by_name("ITF_slow_metrics").cycle_time = 1000
            written = model.save(db, target, original=text, expected_disk_text=text)
            self.assertTrue((Path(tmp) / "binocan.dbc.bak").is_file())
            self.assertIn('BA_ "GenMsgCycleTime" BO_ 272 1000;', written)
            # The file on disk is now different from what was opened first.
            with self.assertRaises(model.SaveError):
                model.save(db, target, original=text, expected_disk_text=text)

    def test_edits_survive_save(self):
        db, text = model.load(REPO / "binocan.dbc")
        m = db.get_message_by_name("ITF_fast_metrics")
        m.cycle_time = 50
        m.comment = "edited"
        sig = m.get_signal_by_name("ITF_rpm")
        sig.maximum = 9000
        out = model.dumps(db, text)
        r = cantools.database.load_string(out, database_format="dbc", strict=False)
        rm = r.get_message_by_name("ITF_fast_metrics")
        self.assertEqual(rm.cycle_time, 50)
        self.assertEqual(rm.comment, "edited")
        self.assertEqual(rm.get_signal_by_name("ITF_rpm").maximum, 9000)


class ValidateTests(unittest.TestCase):
    def test_repo_dbcs_are_clean(self):
        for path in DBCS:
            with self.subTest(dbc=path.name):
                db, _ = model.load(path)
                self.assertEqual(validate.check(db), [])

    def test_motorola_bits(self):
        sig = cantools.database.can.Signal("s", start=7, length=12, byte_order="big_endian")
        self.assertEqual(validate.signal_bits(sig), [7, 6, 5, 4, 3, 2, 1, 0, 15, 14, 13, 12])

    def test_detects_overlap_and_dlc(self):
        db, _ = model.load(REPO / "binocan.dbc")
        m = db.get_message_by_name("ITF_slow_metrics")
        m.signals[0].start = m.signals[1].start
        m.length = 1
        texts = " ".join(i["text"] for i in validate.check(db) if i["where"].startswith(m.name))
        self.assertIn("overlap", texts)
        self.assertIn("outside DLC", texts)

    def test_mux_branches_do_not_overlap(self):
        db, _ = model.load(REPO / "binocan.dbc")
        m = db.get_message_by_name("ITF_board_version")
        self.assertEqual([i for i in validate.check(db) if i["where"] == m.name], [])


class BusloadTests(unittest.TestCase):
    def setUp(self):
        self.db, self.text = model.load(REPO / "binocan.dbc")

    def test_frame_bits_match_vxgauge(self):
        self.assertAlmostEqual(busload.frame_bits(8), 47 + 64 + (34 + 64) * 0.2)
        self.assertAlmostEqual(busload.frame_bits(8, True), 67 + 64 + (54 + 64) * 0.2)

    def test_request_only_frames_excluded(self):
        res = busload.compute(self.db, original_text=self.text)
        excluded = {e["id"] for e in res["excluded"]}
        on_request = {m.frame_id for m in self.db.messages if m.send_type == "SendOnRequest"}
        self.assertEqual(excluded, on_request)
        self.assertTrue(on_request)
        counted = {r["id"] for r in res["messages"]}
        self.assertFalse(counted & on_request)
        self.assertEqual(len(counted) + len(excluded), len(self.db.messages))

    def test_cyclic_and_on_request_is_counted(self):
        res = busload.compute(self.db, original_text=self.text)
        mixed = [m.frame_id for m in self.db.messages if m.send_type == "CyclicAndSendOnRequest"]
        counted = {r["id"] for r in res["messages"]}
        for fid in mixed:
            self.assertIn(fid, counted)

    def test_total_is_sum_of_rows(self):
        res = busload.compute(self.db, original_text=self.text)
        self.assertEqual(res["baudrate"], 500000)
        total = sum(r["bitrate_bps"] for r in res["messages"])
        self.assertAlmostEqual(res["total_busload_pct"], total / 500000 * 100)

    def test_override_and_baud(self):
        base = busload.compute(self.db, original_text=self.text)
        faster = busload.compute(self.db, overrides={0x110: 50}, original_text=self.text)
        self.assertGreater(faster["total_bps"], base["total_bps"])
        half = busload.compute(self.db, baudrate=250000, original_text=self.text)
        self.assertAlmostEqual(half["total_busload_pct"], base["total_busload_pct"] * 2)


class DepsTests(unittest.TestCase):
    def test_parse_version(self):
        self.assertEqual(deps.parse_version("44.1.0"), (44, 1, 0))
        self.assertEqual(deps.parse_version("44.1.0rc1"), (44, 1, 0))
        self.assertLess(deps.parse_version("43.0.2"), deps.parse_version("43.0.10"))

    def test_installed_meets_minimum(self):
        st = deps.status(check_latest=False)
        self.assertIsNotNone(st["installed"])
        self.assertFalse(st["too_old"])


class VenvFallbackTests(unittest.TestCase):
    """cantools missing on an externally managed Python (PEP 668, e.g. Homebrew)."""

    missing = {"python": "3.14.0", "python_executable": "/opt/homebrew/bin/python3.14",
               "installed": None, "minimum": deps.MIN_VERSION, "latest": None,
               "too_old": False, "outdated": False}

    def setUp(self):
        self.env = mock.patch.dict("os.environ", {}, clear=False)
        self.env.start()
        deps.os.environ.pop(deps.REEXEC_ENV, None)
        for target, value in [
            ("status", self.missing), ("externally_managed", True),
            ("_in_our_venv", False), ("_venv_ready", False),
        ]:
            attr = mock.Mock(return_value=value)
            p = mock.patch.object(deps, target, attr)
            setattr(self, "m_" + target, p.start())
            self.addCleanup(p.stop)

    def tearDown(self):
        self.env.stop()

    def test_never_pip_installs_into_a_managed_python(self):
        with mock.patch.object(deps, "pip_install") as pip, \
                mock.patch.object(deps, "setup_venv", return_value=False) as setup:
            self.assertFalse(deps.ensure_cantools(assume_yes=True))
            pip.assert_not_called()
            setup.assert_called_once()

    def test_declined_venv_stops_cleanly(self):
        with mock.patch.object(deps, "pip_install") as pip, \
                mock.patch.object(deps, "setup_venv") as setup:
            self.assertFalse(deps.ensure_cantools(assume_yes=False))  # no tty in tests
            pip.assert_not_called()
            setup.assert_not_called()

    def test_existing_venv_is_reused_without_prompting(self):
        self.m__venv_ready.return_value = True
        with mock.patch.object(deps, "reexec_in_venv", side_effect=SystemExit(0)) as hop:
            with self.assertRaises(SystemExit):
                deps.ensure_cantools(assume_yes=False)
            hop.assert_called_once()

    def test_no_second_hop_from_inside_the_venv(self):
        deps.os.environ[deps.REEXEC_ENV] = "1"
        with mock.patch.object(deps, "reexec_in_venv") as hop, \
                mock.patch.object(deps, "setup_venv") as setup:
            self.assertFalse(deps.ensure_cantools(assume_yes=True))
            hop.assert_not_called()
            setup.assert_not_called()

    def test_venv_flag_hops_when_ready(self):
        self.m__venv_ready.return_value = True
        with mock.patch.object(deps, "reexec_in_venv", side_effect=SystemExit(0)):
            with self.assertRaises(SystemExit):
                deps.ensure_cantools(assume_yes=True, use_venv=True)

    def test_venv_python_path(self):
        self.assertTrue(str(deps.venv_python()).startswith(str(deps.VENV_DIR)))


class GenerateTests(unittest.TestCase):
    def test_committed_c_matches_dbc(self):
        res = generate.generate(REPO / "binocan.dbc", REPO / "src", dry_run=True)
        self.assertEqual(res["changed"], [], "src/ is out of date with binocan.dbc")
        self.assertEqual(res["created"], [])


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.session = server.Session(REPO / "binocan.dbc", REPO / "src")
        cls.httpd = server.make_server(cls.session, 0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _get(self, path, token=True):
        req = urllib.request.Request(self.base + path)
        if token:
            req.add_header("X-Token", self.session.token)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()

    def test_api_requires_token(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/api/db", token=False)
        self.assertEqual(ctx.exception.code, 403)

    def test_db_payload(self):
        status, body = self._get("/api/db")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(data["file"], "binocan.dbc")
        db, _ = model.load(REPO / "binocan.dbc")
        self.assertEqual(len(data["messages"]), len(db.messages))
        fast = next(m for m in data["messages"] if m["frame_id"] == 0x100)
        self.assertEqual(fast["cycle_time"], 25)
        self.assertTrue(all(s["bits"] for s in fast["signals"]))

    def test_busload_endpoint(self):
        _, body = self._get("/api/busload?o=0x100:50")
        data = json.loads(body)
        row = next(r for r in data["messages"] if r["id"] == 0x100)
        self.assertEqual(row["cycle_time_ms"], 50)
        self.assertTrue(row["overridden"])

    def test_static_index_and_traversal(self):
        status, body = self._get("/", token=False)
        self.assertEqual(status, 200)
        self.assertIn(b"Binocan DBC Editor", body)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/../model.py", token=False)
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
