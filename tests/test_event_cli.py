"""Event worker contracts use temporary sources and never contact Steam."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from screenshotinone.cli import main
from screenshotinone.steam_sync import SteamSyncReport


class EventCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "Steam"
        self.shot = self.root / "userdata/123/760/remote/456/screenshots/a.jpg"
        self.shot.parent.mkdir(parents=True)
        self.shot.write_bytes(b"fixture")
        self.config = self.base / "config.json"
        self.data = {"steam_roots": [str(self.root)], "settle_seconds": 0,
                     "target": {"type": "folder", "path": "cloud"}}
        self.save()

    def save(self):
        self.config.write_text(json.dumps(self.data), encoding="utf-8")

    def tearDown(self):
        logger = logging.getLogger("screenshotinone")
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)

    def command(self, name):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main(["--config", str(self.config), name])
        return code, json.loads(output.getvalue()) if output.getvalue() else None

    def enable_steam_only(self):
        self.data["target"]["enabled"] = False
        self.data["steam"] = {"enabled": True, "account_id": "123"}
        self.save()

    def test_watch_plan_is_read_only_and_does_not_scan_or_connect(self):
        self.data["settle_seconds"] = 3
        self.save()
        before = set(self.base.rglob("*"))
        with patch("screenshotinone.cli.scan_screenshots") as scan:
            with patch("screenshotinone.cli._target") as target:
                with patch("screenshotinone.cli._steam_pass") as steam:
                    code, plan = self.command("watch-plan")
        self.assertEqual(code, 0)
        self.assertEqual(plan, {"roots": [str(self.root / "userdata")],
                                "debounce_ms": 3250, "enabled": True})
        scan.assert_not_called()
        target.assert_not_called()
        steam.assert_not_called()
        self.assertEqual(set(self.base.rglob("*")), before)

    def test_idle_and_finished_upload_do_not_schedule_retry(self):
        code, report = self.command("event-sync")
        self.assertEqual(code, 0)
        self.assertEqual(report["uploaded"], 1)
        self.assertFalse(report["retry_needed"])
        code, report = self.command("event-sync")
        self.assertEqual(code, 0)
        self.assertEqual(report["skipped"], 1)

    def test_watch_plan_supports_long_settling_without_integer_overflow(self):
        self.data["settle_seconds"] = 60
        self.save()
        code, plan = self.command("watch-plan")
        self.assertEqual((code, plan["debounce_ms"]), (0, 60250))
        self.data["settle_seconds"] = 1e308
        self.save()
        code, plan = self.command("watch-plan")
        self.assertEqual((code, plan), (1, None))

    def test_file_still_being_saved_requests_retry_without_upload(self):
        self.shot.write_bytes(b"")
        code, report = self.command("event-sync")
        self.assertEqual(code, 10)
        self.assertEqual(report["pending"], 1)
        self.assertEqual(report["uploaded"], 0)

    def test_cloud_failure_requests_retry(self):
        with patch("screenshotinone.cli.sync_once", side_effect=OSError("Offline")):
            code, report = self.command("event-sync")
        self.assertEqual(code, 10)
        self.assertTrue(report["retry_needed"])

    def test_permanent_bootstrap_error_does_not_retry_and_is_logged(self):
        self.data["steam_roots"] = [str(self.base / "missing")]
        self.save()
        with patch("sys.stdout", None), patch("sys.stderr", None):
            code = main(["--config", str(self.config), "event-sync"])
        self.assertEqual(code, 1)
        self.assertIn("Worker could not start", (self.base / "sync.log").read_text())

    def test_steam_retry_depends_on_remaining_queue_not_processed_count(self):
        self.enable_steam_only()
        report = SteamSyncReport(queued=1, uploaded=1)
        with patch("screenshotinone.cli._steam_pass", return_value=report):
            with patch("screenshotinone.steam_sync.steam_retry_needed", return_value=False):
                code, output = self.command("event-sync")
        self.assertEqual(code, 0)
        self.assertFalse(output["retry_needed"])

    def test_steam_pending_retries_but_blocked_error_sleeps(self):
        self.enable_steam_only()
        for pending, expected in ((True, 10), (False, 1)):
            with self.subTest(pending=pending):
                report = SteamSyncReport(errors=["Steam unavailable" if pending else "Blocked"])
                with patch("screenshotinone.cli._steam_pass", return_value=report):
                    with patch("screenshotinone.steam_sync.steam_retry_needed", return_value=pending):
                        with patch("screenshotinone.cli._target") as target:
                            code, _ = self.command("event-sync")
                self.assertEqual(code, expected)
                target.assert_not_called()

    def test_steam_scan_failure_without_queued_files_retries(self):
        self.enable_steam_only()
        report = SteamSyncReport(scan_failed=True, errors=["Cannot scan source"])
        with patch("screenshotinone.cli._steam_pass", return_value=report):
            code, _ = self.command("event-sync")
        self.assertEqual(code, 10)


if __name__ == "__main__":
    unittest.main()
