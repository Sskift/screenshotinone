from contextlib import redirect_stderr, redirect_stdout
import io
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from screenshotinone.cli import main
from screenshotinone.config import Config


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "Steam"
        self.images = self.root / "userdata/123/760/remote/456/screenshots"
        self.images.mkdir(parents=True)
        (self.images / "capture.jpg").write_bytes(b"test image bytes")
        self.config_path = self.base / "config.json"
        self.config = {"steam_roots": [str(self.root)], "settle_seconds": 0,
                       "target": {"type": "folder", "path": "cloud"}}
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")

    def tearDown(self):
        logger = logging.getLogger("screenshotinone")
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)

    def run_cli(self, *args):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            result = main(["--config", str(self.config_path), *args])
        return result, output.getvalue(), error.getvalue()

    def test_cli_dry_run_is_nonmutating_then_real_sync_is_idempotent(self):
        code, output, error = self.run_cli("sync", "--dry-run")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output)["planned"], ["123/456/capture.jpg"])
        self.assertFalse((self.base / "cloud").exists())
        self.assertFalse((self.base / "state.sqlite3").exists())
        self.assertFalse((self.base / "sync.log").exists())
        code, output, _ = self.run_cli("sync")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["uploaded"], 1)
        code, output, _ = self.run_cli("sync")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["skipped"], 1)
        self.assertEqual(len(list((self.base / "cloud").rglob("*.jpg"))), 1)

    def test_scan_uses_configured_root(self):
        code, output, _ = self.run_cli("scan")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["count"], 1)

    def test_init_never_overwrites_configuration(self):
        before = self.config_path.read_bytes()
        code, _, _ = self.run_cli("init", "--target", "folder", "--destination", str(self.base / "other"))
        self.assertEqual(code, 1)
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_config_paths_are_relative_to_config_not_current_directory(self):
        config = Config.load(self.config_path)
        self.assertEqual(config.target_options["destination"], self.base / "cloud")
        self.assertEqual(config.state_path, self.base / "state.sqlite3")

    def test_bad_config_produces_actionable_error(self):
        self.config["poll_interval"] = -1
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        code, _, error = self.run_cli("sync")
        self.assertEqual(code, 1)
        self.assertIn("poll_interval", error)

    def test_credentials_in_url_are_rejected(self):
        self.config["target"] = {"type": "netclip", "url": "https://user:secret@example.com"}
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        code, _, error = self.run_cli("sync")
        self.assertEqual(code, 1)
        self.assertNotIn("secret", error)

    def test_explicit_config_expands_home_before_checking_existence(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"HOME": str(self.base), "USERPROFILE": str(self.base)}):
            with redirect_stdout(output):
                code = main(["--config", "~/config.json", "status"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["screenshots"], 1)

    def test_watch_bootstrap_error_is_logged_without_a_console(self):
        self.config["steam_roots"] = [str(self.base / "missing")]
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        with patch("sys.stderr", None), patch("sys.stdout", None):
            code = main(["--config", str(self.config_path), "watch"])
        self.assertEqual(code, 1)
        self.assertIn("Worker could not start", (self.base / "sync.log").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
