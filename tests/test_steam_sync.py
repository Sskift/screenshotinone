from dataclasses import dataclass, replace
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from screenshotinone.steam_sync import steam_queue_status, steam_sync_once


@dataclass
class FakeScreenshot:
    account_id: str
    str_game_id: str
    handle: int
    path: Path
    uploaded: bool = False
    ugc_id: str | None = None
    visibility: str | None = "private"
    published_file_id: str | None = None

    @property
    def game_id(self):
        return self.str_game_id


class FakeBridge:
    """In-memory Steam metadata only; these tests never contact Steam."""

    def __init__(self, account_id="123"):
        self.account_id = account_id
        self.screenshots = {}
        self.publish_calls = []
        self.doctor_error = None
        self.publish_error = None
        self.publish_result = True
        self.confirm_immediately = True
        self.on_doctor = None
        self.on_publish = None

    def doctor(self):
        if self.on_doctor:
            self.on_doctor()
        if self.doctor_error:
            raise self.doctor_error
        return {"ready": True, "supported": True, "account_id": self.account_id}

    def list_screenshots(self):
        return list(self.screenshots.values())

    def get_screenshot(self, game_id, handle):
        return self.screenshots[(str(game_id), handle)]

    def add(self, screenshot):
        self.screenshots[(screenshot.game_id, screenshot.handle)] = screenshot
        return screenshot

    def confirm(self, screenshot, *, visibility="private", ugc_id="12345", published_file_id=None):
        return self.add(replace(
            screenshot, uploaded=True, ugc_id=ugc_id, visibility=visibility,
            published_file_id=published_file_id,
        ))

    def publish(self, screenshot, visibility="private"):
        self.publish_calls.append((screenshot, visibility))
        if self.on_publish:
            self.on_publish(screenshot)
        if self.publish_error:
            raise self.publish_error
        if self.publish_result is True and self.confirm_immediately:
            self.confirm(screenshot, visibility=visibility)
        return self.publish_result


class SteamSyncTests(unittest.TestCase):
    profile_screenshots_url = "https://steamcommunity.com/profiles/76561197960265851/screenshots/"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "Steam"
        self.state = self.base / "state" / "steam-state.sqlite3"
        self.bridge = FakeBridge()
        self.original = self.add_image("one.jpg", handle=1)

    def add_image(self, filename, *, account_id="123", game_id="456", handle=2):
        path = self.root / f"userdata/{account_id}/760/remote/{game_id}/screenshots/{filename}"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.write_image(path, b"original screenshot: " + filename.encode())
        self.bridge.add(FakeScreenshot(account_id, game_id, handle, path))
        return path

    @staticmethod
    def write_image(path, contents):
        path.write_bytes(contents)
        old = time.time() - 60
        os.utime(path, (old, old))

    def sync(self, **kwargs):
        options = {"account_id": "123", "confirm_timeout": 0}
        options.update(kwargs)
        return steam_sync_once([self.root], self.bridge, self.state, **options)

    def test_first_pass_baselines_existing_then_new_file_is_published(self):
        first = self.sync()
        self.assertEqual(first.scanned, 1)
        self.assertEqual(first.baseline, 1)
        self.assertEqual(first.uploaded, 0)
        self.assertEqual(self.bridge.publish_calls, [])
        self.assertTrue(steam_queue_status(self.state, account_id="123")["baseline_established"])

        new_file = self.add_image("new.jpg", handle=2)
        second = self.sync()
        self.assertEqual(second.uploaded, 1)
        self.assertEqual(second.errors, [])
        self.assertEqual(len(self.bridge.publish_calls), 1)
        self.assertEqual(self.bridge.publish_calls[0][0].path, new_file)

    def test_include_existing_publishes_and_restart_does_not_duplicate(self):
        first = self.sync(include_existing=True)
        self.assertEqual(first.uploaded, 1)
        self.assertEqual(first.errors, [])
        self.assertEqual(first.links, [self.profile_screenshots_url])
        second = self.sync(include_existing=True)
        self.assertEqual(second.uploaded, 0)
        self.assertEqual(len(self.bridge.publish_calls), 1)
        self.assertEqual(steam_queue_status(self.state, account_id="123")["counts"].get("confirmed"), 1)

    def test_changed_confirmed_original_preserves_receipt_without_republishing(self):
        self.assertEqual(self.sync(include_existing=True).uploaded, 1)
        self.write_image(self.original, b"replacement at the same screenshot path")
        result = self.sync(include_existing=True)
        self.assertEqual(result.uploaded, 0)
        self.assertTrue(result.errors)
        self.assertEqual(len(self.bridge.publish_calls), 1)
        status = steam_queue_status(self.state, account_id="123")
        self.assertEqual(status["counts"].get("confirmed"), 1)
        self.assertEqual(status["links"], [self.profile_screenshots_url])

    def test_unstable_initial_queue_still_establishes_baseline_before_any_probe(self):
        self.write_image(self.original, b"")

        def unexpected_probe():
            self.fail("An empty screenshot should be queued without probing Steam")

        self.bridge.on_doctor = unexpected_probe
        first = self.sync(include_existing=True)
        self.assertEqual(first.uploaded, 0)
        self.assertEqual(first.pending, 1)
        status = steam_queue_status(self.state, account_id="123")
        self.assertTrue(status["baseline_established"])
        self.assertEqual(status["counts"].get("queued"), 1)
        self.assertEqual(self.bridge.publish_calls, [])

        self.bridge.on_doctor = None
        self.write_image(self.original, b"initial screenshot finished saving")
        new_file = self.add_image("new-after-baseline.jpg", handle=2)
        second = self.sync()
        self.assertEqual(second.uploaded, 2)
        self.assertEqual(second.errors, [])
        self.assertEqual({call[0].path for call in self.bridge.publish_calls}, {self.original, new_file})

    def test_only_path_does_not_baseline_or_publish_other_files(self):
        self.add_image("other.jpg", handle=2)
        result = self.sync(only_path=self.original, include_existing=True, limit=1)
        self.assertEqual(result.uploaded, 1)
        self.assertEqual([call[0].path for call in self.bridge.publish_calls], [self.original])
        status = steam_queue_status(self.state, account_id="123")
        self.assertFalse(status["baseline_established"])

        self.sync()
        self.assertEqual(len(self.bridge.publish_calls), 1)
        self.assertTrue(steam_queue_status(self.state, account_id="123")["baseline_established"])

    def test_offline_discovery_persists_queue_before_bridge_probe(self):
        observed_queue_sizes = []
        self.bridge.on_doctor = lambda: observed_queue_sizes.append(
            steam_queue_status(self.state, account_id="123")["counts"].get("queued", 0)
        )
        self.bridge.doctor_error = OSError("Steam connection unavailable")
        first = self.sync(include_existing=True)
        self.assertEqual(first.uploaded, 0)
        self.assertTrue(first.errors)
        self.assertTrue(self.state.exists())
        self.assertEqual(steam_queue_status(self.state, account_id="123")["counts"].get("queued"), 1)
        self.assertEqual(observed_queue_sizes, [1])

        self.bridge.doctor_error = None
        self.bridge.on_doctor = None
        second = self.sync()
        self.assertEqual(second.uploaded, 1)
        self.assertEqual(len(self.bridge.publish_calls), 1)

    def test_unconfirmed_acceptance_is_never_blindly_retried(self):
        self.bridge.confirm_immediately = False
        first = self.sync(include_existing=True)
        self.assertEqual(first.uploaded, 0)
        self.assertEqual(first.uncertain, 1)
        self.assertEqual(len(self.bridge.publish_calls), 1)

        second = self.sync(include_existing=True)
        self.assertEqual(second.uploaded, 0)
        self.assertEqual(second.uncertain, 1)
        self.assertEqual(len(self.bridge.publish_calls), 1)

        native = self.bridge.get_screenshot("456", 1)
        self.bridge.confirm(native)
        third = self.sync(include_existing=True)
        self.assertEqual(third.uploaded, 1)
        self.assertEqual(third.uncertain, 0)
        self.assertEqual(len(self.bridge.publish_calls), 1)

    def test_deleted_uncertain_original_remains_visible_without_republication(self):
        self.bridge.confirm_immediately = False
        self.assertEqual(self.sync(include_existing=True).uncertain, 1)
        self.original.unlink()
        result = self.sync()
        self.assertEqual(result.uploaded, 0)
        self.assertEqual(result.uncertain, 1)
        self.assertEqual(len(self.bridge.publish_calls), 1)
        self.assertEqual(steam_queue_status(self.state, account_id="123")["counts"].get("uncertain"), 1)

    def test_exception_after_dispatch_remains_uncertain_across_restart(self):
        self.bridge.publish_error = OSError("Disconnected after dispatch")
        first = self.sync(include_existing=True)
        self.assertEqual(first.uploaded, 0)
        self.assertEqual(first.uncertain, 1)
        self.assertEqual(len(self.bridge.publish_calls), 1)
        self.bridge.publish_error = None
        second = self.sync(include_existing=True)
        self.assertEqual(second.uncertain, 1)
        self.assertEqual(len(self.bridge.publish_calls), 1)

    def test_declined_publication_uses_backoff_and_stops_after_three_attempts(self):
        self.bridge.publish_result = False
        now = time.time()
        with patch("screenshotinone.steam_sync.time.time", return_value=now) as clock:
            first = self.sync(include_existing=True)
            self.assertEqual(first.uncertain, 0)
            self.assertEqual(len(self.bridge.publish_calls), 1)

            delayed = self.sync(include_existing=True)
            self.assertEqual(delayed.pending, 1)
            self.assertEqual(len(self.bridge.publish_calls), 1)

            for day in (1, 2):
                clock.return_value = now + day * 86400
                self.sync(include_existing=True)
            self.assertEqual(len(self.bridge.publish_calls), 3)
            self.assertEqual(
                steam_queue_status(self.state, account_id="123")["counts"].get("failed"), 1,
            )

            clock.return_value = now + 3 * 86400
            exhausted = self.sync(include_existing=True)
            self.assertEqual(exhausted.uploaded, 0)
            self.assertTrue(exhausted.errors)
            self.assertEqual(len(self.bridge.publish_calls), 3)

    def test_process_interruption_after_dispatch_cannot_trigger_duplicate(self):
        self.bridge.publish_error = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.sync(include_existing=True)
        self.assertEqual(len(self.bridge.publish_calls), 1)
        self.bridge.publish_error = None
        result = self.sync(include_existing=True)
        self.assertEqual(result.uploaded, 0)
        self.assertEqual(result.uncertain, 1)
        self.assertEqual(len(self.bridge.publish_calls), 1)

    def test_account_mismatch_never_dispatches(self):
        self.bridge.account_id = "999"
        result = self.sync(include_existing=True)
        self.assertEqual(result.uploaded, 0)
        self.assertTrue(result.errors)
        self.assertEqual(self.bridge.publish_calls, [])

    def test_other_local_account_is_not_queued(self):
        self.add_image("another-account.jpg", account_id="999", handle=2)
        result = self.sync(include_existing=True)
        self.assertEqual(result.scanned, 1)
        self.assertEqual(result.uploaded, 1)
        self.assertEqual([call[0].account_id for call in self.bridge.publish_calls], ["123"])

    def test_native_account_mismatch_never_dispatches(self):
        native = self.bridge.get_screenshot("456", 1)
        self.bridge.add(replace(native, account_id="999"))
        result = self.sync(include_existing=True)
        self.assertEqual(result.uploaded, 0)
        self.assertEqual(self.bridge.publish_calls, [])

    def test_native_path_change_before_dispatch_never_uploads_another_file(self):
        native = self.bridge.get_screenshot("456", 1)
        unrelated = self.base / "another-file.jpg"
        self.write_image(unrelated, b"not the selected screenshot")
        self.bridge.get_screenshot = lambda game_id, handle: replace(native, path=unrelated)
        result = self.sync(include_existing=True)
        self.assertEqual(result.uploaded, 0)
        self.assertEqual(self.bridge.publish_calls, [])

    def test_existing_upload_with_wrong_privacy_is_not_republished(self):
        native = self.bridge.get_screenshot("456", 1)
        self.bridge.confirm(native, visibility="public")
        result = self.sync(include_existing=True, visibility="private")
        self.assertEqual(result.uploaded, 0)
        self.assertTrue(result.errors)
        self.assertEqual(self.bridge.publish_calls, [])
        self.assertEqual(result.links, [])

    def test_existing_upload_with_matching_privacy_is_reconciled(self):
        native = self.bridge.get_screenshot("456", 1)
        self.bridge.confirm(native, visibility="private")
        result = self.sync(include_existing=True, visibility="private")
        self.assertEqual(result.uploaded, 1)
        self.assertEqual(self.bridge.publish_calls, [])
        self.assertEqual(result.links, [self.profile_screenshots_url])

    def test_permalink_uses_published_file_id_not_ufs_content_handle(self):
        native = self.bridge.get_screenshot("456", 1)
        self.bridge.confirm(native, ugc_id="12345", published_file_id="999999")
        result = self.sync(include_existing=True, visibility="private")
        self.assertEqual(result.uploaded, 1)
        self.assertEqual(self.bridge.publish_calls, [])
        self.assertEqual(result.links, ["https://steamcommunity.com/sharedfiles/filedetails/?id=999999"])

    def test_malformed_ugc_id_never_counts_as_confirmed(self):
        for index, ugc_id in enumerate((None, "0", "not-a-number", True, -1)):
            with self.subTest(ugc_id=ugc_id):
                state = self.base / f"invalid-ugc-{index}.sqlite3"
                native = self.bridge.get_screenshot("456", 1)
                self.bridge.confirm(native, ugc_id=ugc_id)
                result = steam_sync_once(
                    [self.root], self.bridge, state,
                    account_id="123", include_existing=True, confirm_timeout=0,
                )
                self.assertEqual(result.uploaded, 0)
                self.assertEqual(result.links, [])
                self.assertEqual(self.bridge.publish_calls, [])

    def test_original_change_during_publish_prevents_success(self):
        self.bridge.on_publish = lambda shot: self.write_image(shot.path, b"changed during upload")
        result = self.sync(include_existing=True)
        self.assertEqual(result.uploaded, 0)
        self.assertEqual(result.links, [])
        self.assertTrue(result.errors or result.uncertain)
        self.sync(include_existing=True)
        self.assertEqual(len(self.bridge.publish_calls), 1)

    def test_dry_run_creates_no_state_and_never_publishes(self):
        result = self.sync(include_existing=True, dry_run=True)
        self.assertEqual(result.uploaded, 0)
        self.assertEqual(self.bridge.publish_calls, [])
        self.assertFalse(self.state.parent.exists())

    def test_dry_run_does_not_change_existing_baseline_or_database(self):
        self.sync()
        before = self.state.read_bytes()
        modified = self.state.stat().st_mtime_ns
        result = self.sync(include_existing=True, dry_run=True)
        self.assertEqual(result.uploaded, 0)
        self.assertEqual(self.bridge.publish_calls, [])
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(self.state.stat().st_mtime_ns, modified)
        self.assertEqual(steam_queue_status(self.state, account_id="123")["counts"].get("baseline"), 1)

    def test_status_for_absent_database_has_no_side_effects(self):
        status = steam_queue_status(self.state, account_id="123")
        self.assertFalse(status["baseline_established"])
        self.assertFalse(self.state.parent.exists())
        self.assertFalse(status["links"])


if __name__ == "__main__":
    unittest.main()
