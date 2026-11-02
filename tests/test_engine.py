import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from screenshotinone.engine import sync_once
from screenshotinone.state import exclusive_state_lock


class RecordingTarget:
    def __init__(self, identity="recording-target"):
        self.identity = identity
        self.uploads = []
        self.fail = False
        self.on_upload = None

    def upload(self, screenshot, snapshot_path, digest):
        if self.fail:
            raise OSError("network is unavailable")
        if self.on_upload:
            self.on_upload()
        contents = snapshot_path.read_bytes()
        self.uploads.append((screenshot.relative_path, contents, digest))
        return f"receipt-{digest}"


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Windows CI may return a DOS short path (RUNNER~1) from tempfile;
        # discovery resolves it to the long name before our fault injection.
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "Steam"
        self.original = self.root / "userdata/123/760/remote/456/screenshots/one.png"
        self.original.parent.mkdir(parents=True)
        self.state = self.base / "state" / "sync.sqlite3"
        self.target = RecordingTarget()
        self.write_original(b"first-image")

    def write_original(self, contents):
        self.original.write_bytes(contents)
        old = time.time() - 60
        os.utime(self.original, (old, old))

    def sync(self, **kwargs):
        return sync_once([self.root], self.target, self.state, **kwargs)

    def test_uploaded_once_and_success_survives_new_engine_instance(self):
        first = self.sync()
        second = self.sync()
        self.assertEqual((first.scanned, first.uploaded, first.skipped, first.errors), (1, 1, 0, []))
        self.assertEqual((second.uploaded, second.skipped, second.errors), (0, 1, []))
        self.assertEqual(len(self.target.uploads), 1)
        self.assertEqual(self.target.uploads[0][2], hashlib.sha256(b"first-image").hexdigest())
        self.assertEqual(self.original.read_bytes(), b"first-image")

    def test_failed_upload_is_retried(self):
        self.target.fail = True
        first = self.sync()
        self.assertEqual(first.uploaded, 0)
        self.assertEqual(len(first.errors), 1)
        self.target.fail = False
        second = self.sync()
        self.assertEqual(second.uploaded, 1)
        self.assertEqual(second.errors, [])

    def test_missing_target_file_is_error_not_pending_source(self):
        with patch.object(self.target, "upload", side_effect=FileNotFoundError("missing destination")):
            result = self.sync()
        self.assertEqual(result.pending, 0)
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(self.sync().uploaded, 1)

    def test_changed_contents_at_same_name_are_uploaded_again(self):
        self.sync()
        self.write_original(b"changed-image")
        self.assertEqual(self.sync().uploaded, 1)
        self.assertEqual([call[1] for call in self.target.uploads], [b"first-image", b"changed-image"])

    def test_same_contents_are_sent_to_a_different_target(self):
        self.sync()
        self.target.identity = "a-different-cloud-folder"
        self.assertEqual(self.sync().uploaded, 1)

    def test_new_empty_and_future_timestamp_files_remain_pending(self):
        for contents, timestamp in (
            (b"still-saving", time.time()),
            (b"", time.time() - 60),
            (b"clock-skew", time.time() + 600),
        ):
            self.original.write_bytes(contents)
            os.utime(self.original, (timestamp, timestamp))
            result = self.sync()
            self.assertEqual((result.uploaded, result.pending, result.errors), (0, 1, []))
        self.assertEqual(self.target.uploads, [])

    def test_upload_reads_immutable_snapshot_when_source_changes(self):
        self.target.on_upload = lambda: self.write_original(b"later-image")
        first = self.sync()
        self.assertEqual(first.uploaded, 1)
        self.assertEqual(self.target.uploads[0][1], b"first-image")
        self.target.on_upload = None
        self.assertEqual(self.sync().uploaded, 1)
        self.assertEqual(self.target.uploads[1][1], b"later-image")

    def test_file_changed_during_snapshot_waits_for_next_pass(self):
        actual_hash = hashlib.sha256
        original = self.original

        class MutatingHash:
            def __init__(self):
                self.hash = actual_hash()

            def update(self, block):
                self.hash.update(block)
                original.write_bytes(b"new-longer-image")

            def hexdigest(self):
                return self.hash.hexdigest()

        with patch("screenshotinone.engine.hashlib.sha256", MutatingHash):
            result = self.sync()
        self.assertEqual((result.uploaded, result.pending, result.errors), (0, 1, []))
        self.write_original(b"now-finished")
        self.assertEqual(self.sync().uploaded, 1)

    def test_dry_run_creates_neither_state_directory_nor_uploads(self):
        result = self.sync(dry_run=True)
        self.assertEqual(result.planned, ["123/456/one.png"])
        self.assertEqual(result.uploaded, 0)
        self.assertFalse(self.state.parent.exists())
        self.assertEqual(self.target.uploads, [])

    def test_dry_run_reads_existing_receipts_without_changing_state(self):
        self.sync()
        before = self.state.read_bytes()
        timestamp = self.state.stat().st_mtime_ns
        result = self.sync(dry_run=True)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.planned, [])
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(self.state.stat().st_mtime_ns, timestamp)
        self.assertEqual(len(self.target.uploads), 1)

    def test_success_receipt_is_preserved_when_later_upload_fails(self):
        second = self.original.with_name("two.png")
        second.write_bytes(b"second")
        os.utime(second, (time.time() - 60, time.time() - 60))
        upload = self.target.upload

        def fail_second(screenshot, snapshot_path, digest):
            if screenshot.path.name == "two.png":
                raise OSError("temporary upload failure")
            return upload(screenshot, snapshot_path, digest)

        with patch.object(self.target, "upload", side_effect=fail_second):
            first = self.sync()
        self.assertEqual((first.uploaded, len(first.errors)), (1, 1))
        second_pass = self.sync()
        self.assertEqual((second_pass.skipped, second_pass.uploaded), (1, 1))

    def test_missing_root_is_reported(self):
        result = sync_once([self.root / "missing"], self.target, self.state)
        self.assertEqual(result.scanned, 0)
        self.assertEqual(len(result.errors), 1)

    def test_inaccessible_game_does_not_block_healthy_games(self):
        inaccessible = self.root / "userdata/123/760/remote/999/screenshots"
        inaccessible.mkdir(parents=True)
        original_iterdir = Path.iterdir

        def guarded_iterdir(path):
            if path == inaccessible:
                raise PermissionError("game folder is inaccessible")
            return original_iterdir(path)

        with patch.object(Path, "iterdir", guarded_iterdir):
            result = self.sync()
        self.assertEqual(result.uploaded, 1)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("game folder is inaccessible", result.errors[0])

    def test_invalid_settle_time_rejected(self):
        for value in (-1, float("inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.sync(settle_seconds=value)

    def test_concurrent_process_cannot_take_state_lock(self):
        script = (
            "import sys; from pathlib import Path; "
            "from screenshotinone.state import exclusive_state_lock; "
            "\nwith exclusive_state_lock(Path(sys.argv[1])): pass"
        )
        with exclusive_state_lock(self.state):
            process = subprocess.run(
                [sys.executable, "-c", script, str(self.state)],
                capture_output=True,
                text=True,
                timeout=15,
            )
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("Another sync is using this state database", process.stderr)
        self.assertEqual(self.sync().uploaded, 1)


if __name__ == "__main__":
    unittest.main()
