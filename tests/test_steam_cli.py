from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
import io
import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from screenshotinone.cli import main
from screenshotinone.config import Config


@dataclass
class Report:
    uploaded: int = 0
    uncertain: int = 0
    errors: list = field(default_factory=list)


class SteamCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "Steam"
        self.shot = self.root / "userdata/123/760/remote/456/screenshots/a.jpg"
        self.shot.parent.mkdir(parents=True)
        self.shot.write_bytes(b"fixture")
        self.path = self.base / "config.json"
        self.data = {"target": {"type": "folder", "path": "cloud"},
                     "steam_roots": [str(self.root)], "settle_seconds": 0,
                     "steam": {"enabled": True, "account_id": "123", "visibility": "private"}}
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.data), encoding="utf-8")

    def tearDown(self):
        logger = logging.getLogger("screenshotinone")
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)

    def command(self, *args):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            result = main(["--config", str(self.path), *args])
        return result, json.loads(output.getvalue()) if output.getvalue() else None, error.getvalue()

    def test_old_config_keeps_steam_disabled(self):
        del self.data["steam"]
        self.save()
        self.assertIsNone(Config.load(self.path).steam)

    def test_config_defaults_to_private_without_historical_publication(self):
        self.data["steam"].pop("visibility")
        self.save()
        settings = Config.load(self.path).steam
        self.assertEqual(settings.visibility, "private")
        self.assertFalse(settings.include_existing)
        self.assertEqual(settings.state_path, self.base / "steam-state.sqlite3")

    def test_automatic_publication_requires_an_account_pin(self):
        del self.data["steam"]["account_id"]
        self.save()
        with self.assertRaisesRegex(ValueError, "account_id"):
            Config.load(self.path)

    def test_invalid_endpoint_and_visibility_are_rejected(self):
        for key, value in (("endpoint", "http://example.com:8080"),
                           ("endpoint", "http://user:secret@127.0.0.1:8080"),
                           ("endpoint", "http://127.0.0.1:0"),
                           ("visibility", 8), ("visibility", []),
                           ("enabled", "yes"), ("include_existing", 1)):
            with self.subTest(key=key, value=value):
                self.data["steam"] = {"enabled": True, "account_id": "123", key: value}
                self.save()
                with self.assertRaises(ValueError) as raised:
                    Config.load(self.path)
                self.assertNotIn("secret", str(raised.exception))

    def test_queue_requires_a_different_database(self):
        self.data["steam"]["state_path"] = "state.sqlite3"
        self.save()
        with self.assertRaisesRegex(ValueError, "separate"):
            Config.load(self.path)

    def test_steam_failure_does_not_prevent_cloud_sync(self):
        with patch("screenshotinone.cli._steam_pass", side_effect=RuntimeError("Steam is offline")):
            code, output, _ = self.command("sync")
        self.assertEqual(code, 1)
        self.assertEqual(output["uploaded"], 1)
        self.assertIn("offline", output["steam"]["errors"][0])
        self.assertEqual(len(list((self.base / "cloud").rglob("*.jpg"))), 1)

    def test_cloud_failure_does_not_prevent_steam_pass(self):
        with patch("screenshotinone.cli.sync_once", side_effect=OSError("Cloud disk missing")):
            with patch("screenshotinone.cli._steam_pass", return_value=Report(uploaded=1)) as steam:
                code, output, _ = self.command("sync")
        self.assertEqual(code, 1)
        steam.assert_called_once()
        self.assertEqual(output["steam"]["uploaded"], 1)

    def test_single_file_publish_is_always_limited_to_one(self):
        with patch("screenshotinone.cli._steam_pass", return_value=Report()) as steam:
            code, _, _ = self.command("steam", "publish", "--file", str(self.shot), "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(steam.call_args.kwargs["only_path"], self.shot)
        self.assertEqual(steam.call_args.kwargs["limit"], 1)
        self.assertTrue(steam.call_args.kwargs["dry_run"])
        self.assertTrue(steam.call_args.kwargs["include_existing"])
        self.assertFalse((self.base / "sync.log").exists())

    def test_uncertain_publication_is_not_reported_as_success(self):
        with patch("screenshotinone.cli._steam_pass", return_value=Report(uncertain=1)):
            code, output, _ = self.command("steam", "sync", "--dry-run")
        self.assertEqual(code, 1)
        self.assertEqual(output["uncertain"], 1)

    def test_doctor_reads_account_without_publishing_or_writing_state(self):
        with patch("screenshotinone.steam_bridge.SteamBridge") as bridge:
            bridge.return_value.doctor.return_value = {"ready": True, "account_id": "123"}
            code, output, _ = self.command("steam", "doctor")
        self.assertEqual(code, 0)
        self.assertTrue(output["account_matches"])
        bridge.return_value.publish.assert_not_called()
        self.assertFalse((self.base / "steam-state.sqlite3").exists())
        self.assertFalse((self.base / "sync.log").exists())

    def test_doctor_rejects_another_logged_in_account(self):
        with patch("screenshotinone.steam_bridge.SteamBridge") as bridge:
            bridge.return_value.doctor.return_value = {"ready": True, "account_id": "999"}
            code, output, _ = self.command("steam", "doctor")
        self.assertEqual(code, 1)
        self.assertFalse(output["account_matches"])

    def test_doctor_unavailable_is_actionable_and_read_only(self):
        with patch("screenshotinone.steam_bridge.SteamBridge") as bridge:
            bridge.return_value.doctor.side_effect = RuntimeError("CDP unavailable")
            code, output, _ = self.command("steam", "doctor")
        self.assertEqual(code, 1)
        self.assertFalse(output["available"])
        self.assertIn("-cef-enable-debugging", output["next"])
        bridge.return_value.close.assert_called_once()

    def test_doctor_requires_supported_online_client_even_for_matching_account(self):
        with patch("screenshotinone.steam_bridge.SteamBridge") as bridge:
            bridge.return_value.doctor.return_value = {"ready": False, "account_id": "123", "reason": "Offline"}
            code, output, _ = self.command("steam", "doctor")
        self.assertEqual(code, 1)
        self.assertFalse(output["ready"])


if __name__ == "__main__":
    unittest.main()
