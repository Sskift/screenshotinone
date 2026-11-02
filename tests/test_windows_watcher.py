"""Windows integration tests for the native event relay; no Steam is contacted."""

import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


FAKE_WORKER = r'''
import ctypes
import json
import os
from pathlib import Path
import sys
import time

config_path = Path(sys.argv[sys.argv.index("--config") + 1])
assert sys.argv[-1] == "event-sync", sys.argv
settings = json.loads(config_path.read_text(encoding="utf-8"))
events_path = config_path.parent / "events.jsonl"
previous = events_path.read_text(encoding="utf-8").splitlines() if events_path.exists() else []
index = sum(json.loads(line)["event"] == "start" for line in previous)
plans = settings.get("runs", [{"exit": 0}])
plan = plans[min(index, len(plans) - 1)]
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.GetProcessTimes.argtypes = [ctypes.c_void_p] * 5
kernel.GetProcessTimes.restype = ctypes.c_int
times = [ctypes.c_ulonglong() for _ in range(4)]
if not kernel.GetProcessTimes(ctypes.c_void_p(-1), *(ctypes.byref(value) for value in times)):
    raise ctypes.WinError(ctypes.get_last_error())

def event(kind, **extra):
    data = {"event": kind, "pid": os.getpid(), "creation": times[0].value,
            "run": index + 1, "time": time.monotonic(), **extra}
    with events_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(data) + "\n")
        stream.flush()
        os.fsync(stream.fileno())

event("start")
if plan.get("hold"):
    while not (config_path.parent / plan["hold"]).exists():
        time.sleep(0.025)
time.sleep(plan.get("sleep", 0))
code = plan.get("exit", 0)
event("end", code=code)
sys.exit(code)
'''


@unittest.skipUnless(os.name == "nt", "The native watcher runs only on Windows")
class WindowsWatcherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = Path(__file__).resolve().parents[1]
        cls.pythonw = Path(sys.executable).with_name("pythonw.exe")
        if not cls.pythonw.is_file():
            raise unittest.SkipTest("This Python installation does not provide pythonw.exe")
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if not powershell:
            raise unittest.SkipTest("PowerShell is required to compile the Windows watcher")
        cls.build_directory = tempfile.TemporaryDirectory(prefix="screenshotinone-watcher-build-")
        cls.addClassCleanup(cls.build_directory.cleanup)
        cls.hidden = subprocess.STARTUPINFO()
        cls.hidden.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        cls.hidden.wShowWindow = subprocess.SW_HIDE
        built = subprocess.run(
            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(cls.project / "scripts" / "build-watcher.ps1"),
             "-OutputDirectory", cls.build_directory.name],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=40,
            startupinfo=cls.hidden, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if built.returncode:
            raise RuntimeError(f"Native watcher build failed:\n{built.stdout}\n{built.stderr}")
        profile = b"screenshotinone-watcher-v1|netfx4|winexe|optimize|x86\n"
        digest = hashlib.sha256(profile + (cls.project / "windows" / "ScreenshotWatcher.cs").read_bytes()).hexdigest()[:16]
        cls.executable = Path(cls.build_directory.name) / f"ScreenshotWatcher-{digest}.exe"
        if not cls.executable.is_file():
            raise RuntimeError(f"Watcher executable missing after successful build:\n{built.stdout}")

        cls.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        cls.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        cls.kernel.OpenProcess.restype = wintypes.HANDLE
        cls.kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.c_void_p] * 4
        cls.kernel.GetProcessTimes.restype = wintypes.BOOL
        cls.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        cls.kernel.WaitForSingleObject.restype = wintypes.DWORD
        cls.kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        cls.kernel.TerminateProcess.restype = wintypes.BOOL
        cls.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        cls.kernel.CloseHandle.restype = wintypes.BOOL

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="screenshotinone-watcher-test-")
        self.root = Path(self.temporary.name)
        self.fake_project = self.root / "fake project"
        package = self.fake_project / "screenshotinone"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "__main__.py").write_text(FAKE_WORKER, encoding="utf-8")
        self.userdata = self.root / "userdata"
        self.screenshots = self.userdata / "12345" / "760" / "remote" / "570" / "screenshots"
        self.screenshots.mkdir(parents=True)
        self.config = self.root / "config with spaces.json"
        self.events_path = self.root / "events.jsonl"
        self.log_path = self.root / "watcher.log"
        self.processes = []
        self.child_handles = {}

    def tearDown(self):
        try:
            self.events()
            for process in self.processes:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)
            # If job cleanup is broken, clean only child identities recorded by
            # our fixture. Creation timestamps prevent a reused PID from matching.
            self.events()
            for handle in self.child_handles.values():
                if self.kernel.WaitForSingleObject(handle, 0) == 258:
                    self.kernel.TerminateProcess(handle, 99)
                    self.kernel.WaitForSingleObject(handle, 5000)
        finally:
            for handle in self.child_handles.values():
                self.kernel.CloseHandle(handle)
            self.temporary.cleanup()

    def events(self):
        if not self.events_path.exists():
            return []
        entries = []
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue  # A complete append will be visible on the next read.
            entries.append(entry)
            identity = entry["pid"], entry["creation"]
            if entry["event"] != "start" or identity in self.child_handles:
                continue
            # SYNCHRONIZE | QUERY_LIMITED_INFORMATION | TERMINATE.
            handle = self.kernel.OpenProcess(0x100000 | 0x1000 | 0x0001, False, entry["pid"])
            if not handle:
                continue
            times = [ctypes.c_ulonglong() for _ in range(4)]
            matches = self.kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times))
            if matches and times[0].value == entry["creation"]:
                self.child_handles[identity] = handle
            else:
                self.kernel.CloseHandle(handle)
        return entries

    def starts(self):
        return [entry for entry in self.events() if entry["event"] == "start"]

    def ends(self):
        return [entry for entry in self.events() if entry["event"] == "end"]

    def watcher_log(self):
        return self.log_path.read_text(encoding="utf-8-sig") if self.log_path.exists() else ""

    def wait_for(self, predicate, message, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.025)
        self.fail(f"{message}\nEvents: {self.events()}\nWatcher log:\n{self.watcher_log()}")

    def launch(self, runs=None):
        if not self.config.exists():
            self.config.write_text(json.dumps({"runs": runs or [{"exit": 0}]}), encoding="utf-8")
        process = subprocess.Popen(
            [str(self.executable), "--python", str(self.pythonw), "--project", str(self.fake_project),
             "--config", str(self.config), "--debounce-ms", "150", "--root", str(self.userdata)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            startupinfo=self.hidden, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.processes.append(process)
        return process

    def wait_for_idle(self, count):
        self.wait_for(lambda: len(self.starts()) == count and len(self.ends()) == count
                      and self.watcher_log().count("Sync worker exited with code") >= count,
                      f"Expected {count} completed worker run(s)")

    def assert_quiet(self, count, duration=0.7):
        log_before = self.watcher_log()
        time.sleep(duration)
        self.assertEqual(len(self.starts()), count, self.watcher_log())
        self.assertEqual(self.watcher_log(), log_before, "Idle watcher wrote additional log entries")
        self.assertIsNone(self.processes[0].poll(), "Resident watcher exited while idle")

    def test_startup_recovery_leaves_no_idle_python_child_or_extra_runs(self):
        self.launch()
        self.wait_for_idle(1)
        for handle in self.child_handles.values():
            self.assertEqual(self.kernel.WaitForSingleObject(handle, 0), 0)
        self.assert_quiet(1, duration=1.0)

    def test_vdf_and_thumbnail_updates_do_not_launch_workers(self):
        self.launch()
        self.wait_for_idle(1)
        (self.userdata / "12345" / "760" / "screenshots.vdf").write_text("metadata", encoding="utf-8")
        thumbnails = self.screenshots / "thumbnails"
        thumbnails.mkdir()
        (thumbnails / "ignored.jpg").write_bytes(b"thumbnail")
        (self.screenshots / "metadata.vdf").write_text("metadata", encoding="utf-8")
        (self.userdata / "unrelated.txt").write_text("unrelated", encoding="utf-8")
        self.assert_quiet(1)

    def test_burst_is_debounced_into_one_worker(self):
        self.launch()
        self.wait_for_idle(1)
        for index in range(10):
            path = self.screenshots / f"burst-{index}.jpg"
            path.write_bytes(b"first")
            path.write_bytes(b"complete-image")
        self.wait_for_idle(2)
        self.assert_quiet(2)

    def test_moving_populated_game_directory_schedules_recovery(self):
        staged_game = self.root / "staging" / "440"
        (staged_game / "screenshots").mkdir(parents=True)
        (staged_game / "screenshots" / "already-present.jpg").write_bytes(b"image")
        self.launch()
        self.wait_for_idle(1)
        # Moving a populated subtree need not emit one event per contained file.
        # Both paths belong to this test's freshly-created temporary directory.
        destination = self.screenshots.parents[1] / "440"
        self.assertTrue(staged_game.resolve().is_relative_to(self.root.resolve()))
        self.assertTrue(destination.resolve().is_relative_to(self.root.resolve()))
        staged_game.rename(destination)
        self.wait_for_idle(2)
        self.assert_quiet(2)

    def test_event_during_active_worker_is_preserved(self):
        self.launch([{"exit": 0}, {"exit": 0, "hold": "release-worker"}, {"exit": 0}])
        self.wait_for_idle(1)
        (self.screenshots / "first.png").write_bytes(b"first")
        self.wait_for(lambda: len(self.starts()) == 2, "The held worker did not start")
        (self.screenshots / "second.png").write_bytes(b"second")
        time.sleep(0.4)
        self.assertEqual(len(self.starts()), 2, "Workers must not overlap")
        (self.root / "release-worker").write_text("release", encoding="utf-8")
        self.wait_for_idle(3)
        entries = self.events()
        previous_end = next(e for e in entries if e["event"] == "end" and e["run"] == 2)
        next_start = next(e for e in entries if e["event"] == "start" and e["run"] == 3)
        self.assertGreaterEqual(next_start["time"], previous_end["time"])
        self.assert_quiet(3)

    def test_pending_exit_retries_and_success_returns_to_idle(self):
        self.launch([{"exit": 10}, {"exit": 0}])
        self.wait_for(lambda: len(self.ends()) == 1, "First pending run did not finish")
        first_end = self.ends()[0]
        time.sleep(0.8)
        self.assertEqual(len(self.starts()), 1, "Pending work should use backoff")
        self.wait_for_idle(2)
        self.assertGreaterEqual(self.starts()[1]["time"] - first_end["time"], 4.8)
        self.assert_quiet(2, duration=5.5)

    def test_duplicate_config_is_excluded_by_mutex(self):
        first = self.launch()
        self.wait_for_idle(1)
        duplicate = self.launch()
        self.assertEqual(duplicate.wait(timeout=5), 0)
        self.assertIsNone(first.poll())
        self.assertIn("duplicate instance exited", self.watcher_log())
        self.assert_quiet(1)

    def test_terminating_parent_also_terminates_owned_python_child(self):
        parent = self.launch([{"exit": 0, "hold": "never-released"}])
        self.wait_for(lambda: len(self.starts()) == 1 and len(self.child_handles) == 1,
                      "Held child was not captured")
        child = next(iter(self.child_handles.values()))
        self.assertEqual(self.kernel.WaitForSingleObject(child, 0), 258)
        parent.terminate()
        parent.wait(timeout=5)
        self.assertEqual(self.kernel.WaitForSingleObject(child, 5000), 0,
                         "Closing the watcher must close its job and terminate the child")
        self.assertEqual(self.ends(), [], "The held child should be terminated by its job")


if __name__ == "__main__":
    unittest.main()
