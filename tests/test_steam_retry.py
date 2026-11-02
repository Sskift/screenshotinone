from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from screenshotinone.steam_sync import steam_retry_needed, steam_sync_once


class SteamRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.state = self.base / "state" / "steam-state.sqlite3"

    def create_queue(self, rows):
        self.state.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.state, isolation_level=None)) as connection:
            connection.execute("CREATE TABLE steam_queue (account_id TEXT, status TEXT, source_key TEXT NOT NULL DEFAULT '')")
            connection.executemany("INSERT INTO steam_queue (account_id, status) VALUES (?, ?)", rows)

    def test_missing_database_does_not_create_state_or_lock(self):
        self.assertFalse(steam_retry_needed(self.state))
        self.assertFalse(self.state.parent.exists())

    def test_active_and_terminal_states_are_distinguished(self):
        self.create_queue([])
        self.assertFalse(steam_retry_needed(self.state))
        for status in ("queued", "publishing", "uncertain", "baseline", "confirmed", "blocked", "failed"):
            with self.subTest(status=status):
                with closing(sqlite3.connect(self.state, isolation_level=None)) as connection:
                    connection.execute("DELETE FROM steam_queue")
                    connection.execute("INSERT INTO steam_queue (account_id, status) VALUES ('123', ?)", (status,))
                self.assertEqual(
                    steam_retry_needed(self.state),
                    status in {"queued", "publishing", "uncertain"},
                )

    def test_account_filter_ignores_another_accounts_pending_work(self):
        self.create_queue([("123", "confirmed"), ("456", "uncertain")])
        self.assertTrue(steam_retry_needed(self.state))
        self.assertFalse(steam_retry_needed(self.state, "123"))
        self.assertTrue(steam_retry_needed(self.state, "456"))
        self.assertFalse(steam_retry_needed(self.state, "123' OR 1=1 --"))

    def test_query_does_not_change_database_or_create_other_files(self):
        self.create_queue([("123", "queued")])
        before = self.state.read_bytes()
        modified = self.state.stat().st_mtime_ns
        files = set(self.state.parent.iterdir())
        self.assertTrue(steam_retry_needed(self.state, "123"))
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(self.state.stat().st_mtime_ns, modified)
        self.assertEqual(set(self.state.parent.iterdir()), files)

    def test_pending_work_for_an_old_root_is_ignored(self):
        self.create_queue([])
        old_root = self.base / "Old Steam"
        current_root = self.base / "Current Steam"
        source = old_root / "userdata/123/760/remote/456/screenshots/one.jpg"
        with closing(sqlite3.connect(self.state, isolation_level=None)) as connection:
            connection.execute(
                "INSERT INTO steam_queue VALUES ('123', 'uncertain', ?)",
                (os.path.normcase(str(source.resolve())),),
            )
        self.assertTrue(steam_retry_needed(self.state, "123"))
        self.assertFalse(steam_retry_needed(self.state, "123", roots=[current_root]))
        self.assertTrue(steam_retry_needed(self.state, "123", roots=[old_root]))
        self.assertTrue(steam_retry_needed(self.state, "123", roots=iter([current_root, old_root])))
        self.assertFalse(steam_retry_needed(self.state, "123", roots=[]))

    def test_root_scope_matches_literal_names_and_directory_boundaries(self):
        self.create_queue([])
        selected_root = self.base / "Steam_%"
        other_root = self.base / "Steam_AX"
        prefix_collision = selected_root / "userdata-old/123/760/remote/456/screenshots/one.jpg"
        other_source = other_root / "userdata/123/760/remote/456/screenshots/one.jpg"
        with closing(sqlite3.connect(self.state, isolation_level=None)) as connection:
            connection.executemany(
                "INSERT INTO steam_queue VALUES ('123', 'queued', ?)",
                [(os.path.normcase(str(source.resolve())),) for source in (prefix_collision, other_source)],
            )
        self.assertFalse(steam_retry_needed(self.state, "123", roots=[selected_root]))
        selected_source = selected_root / "userdata/123/760/remote/456/screenshots/one.jpg"
        with closing(sqlite3.connect(self.state, isolation_level=None)) as connection:
            connection.execute(
                "INSERT INTO steam_queue VALUES ('123', 'queued', ?)",
                (os.path.normcase(str(selected_source.resolve())),),
            )
        self.assertTrue(steam_retry_needed(self.state, "123", roots=[selected_root]))

    def test_incompatible_database_raises_instead_of_claiming_empty(self):
        self.state.parent.mkdir()
        with closing(sqlite3.connect(self.state, isolation_level=None)) as connection:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
        with self.assertRaises(sqlite3.OperationalError):
            steam_retry_needed(self.state)

    def test_scan_failure_is_explicit_even_without_queued_work(self):
        report = steam_sync_once(
            [self.base / "missing-steam-root"],
            object(),  # No bridge methods should be reached when discovery fails.
            self.state,
            account_id="123",
        )
        self.assertTrue(report.scan_failed)
        self.assertTrue(report.errors)
        self.assertFalse(steam_retry_needed(self.state, "123"))


if __name__ == "__main__":
    unittest.main()
