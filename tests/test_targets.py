from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from urllib.parse import parse_qs, unquote, urlsplit

from screenshotinone.targets import FolderTarget, NetclipTarget, TargetError


class DriveFixture:
    def __init__(self):
        self.entries = []
        self.contents = {}
        self.uploads = 0
        self.downloads = 0
        self.max_upload = 1024 * 1024
        self.lose_confirmation = False
        self.reject_status = None
        self.requests = []

    def add(self, name, parent, kind, content=b""):
        entry = {"id": len(self.entries) + 1, "parentId": parent,
                 "kind": kind, "name": name, "bytes": len(content)}
        self.entries.append(entry)
        if kind == "file":
            self.contents[entry["id"]] = content
        return entry


@contextmanager
def drive_server():
    fixture = DriveFixture()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_json(self, status, body):
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def request_parts(self):
            parts = urlsplit(self.path)
            parent = parse_qs(parts.query).get("parent", [None])[0]
            return parts.path, int(parent) if parent else None

        def do_GET(self):
            fixture.requests.append(("GET", self.path))
            path, parent = self.request_parts()
            if fixture.reject_status:
                self.send_json(fixture.reject_status, {"error": "secret-server-detail"})
            elif path == "/api/drive/entries":
                self.send_json(200, {"entries": [e for e in fixture.entries if e["parentId"] == parent],
                                     "maxUploadBytes": fixture.max_upload})
            elif path.endswith("/download"):
                fixture.downloads += 1
                identifier = int(path.split("/")[-2])
                content = fixture.contents[identifier]
                self.send_response(200)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            else:
                self.send_json(404, {"error": "Not found"})

        def do_POST(self):
            fixture.requests.append(("POST", self.path))
            path, parent = self.request_parts()
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if path == "/api/drive/folders":
                data = json.loads(body)
                if any(e["parentId"] == data["parentId"] and e["name"].lower() == data["name"].lower()
                       for e in fixture.entries):
                    return self.send_json(409, {"error": "already exists"})
                entry = fixture.add(data["name"], data["parentId"], "folder")
                self.send_json(201, {"entry": entry})
            elif path == "/api/drive/upload":
                fixture.uploads += 1
                name = unquote(self.headers["X-Filename"])
                existing = [e["name"].lower() for e in fixture.entries if e["parentId"] == parent]
                original = name
                index = 2
                while name.lower() in existing:
                    source = Path(original)
                    name = f"{source.stem} ({index}){source.suffix}"
                    index += 1
                entry = fixture.add(name, parent, "file", body)
                if fixture.lose_confirmation:
                    fixture.lose_confirmation = False
                    self.send_response(201)
                    self.send_header("Content-Length", "1")
                    self.end_headers()
                    self.wfile.write(b"{")
                else:
                    self.send_json(201, {"entry": entry})
            else:
                self.send_json(404, {"error": "Not found"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield fixture, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class TargetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.snapshot = self.root / "snapshot.bin"
        self.content = b"original screenshot bytes\x00\xff"
        self.snapshot.write_bytes(self.content)
        self.digest = hashlib.sha256(self.content).hexdigest()
        self.screenshot = SimpleNamespace(account_id="123", app_id="570",
                                          path=Path("20261103_0001.jpg"),
                                          relative_path=Path("123/570/20261103_0001.jpg"))

    def tearDown(self):
        self.temporary.cleanup()

    def test_constructors_have_no_writes_or_network(self):
        destination = self.root / "not-created"
        target = FolderTarget(destination)
        remote = NetclipTarget("http://127.0.0.1:1")
        self.assertFalse(destination.exists())
        self.assertIsNone(remote.folder_url)
        self.assertTrue(target.identity.startswith("folder:"))

    def test_folder_is_append_only_and_idempotent(self):
        target = FolderTarget(self.root / "cloud")
        receipt = Path(target.upload(self.screenshot, self.snapshot, self.digest))
        self.assertEqual(receipt.read_bytes(), self.content)
        self.assertEqual(receipt.parent.relative_to(target.destination).as_posix(), "123/570")
        self.assertEqual(target.upload(self.screenshot, self.snapshot, self.digest), str(receipt))
        replacement = b"revised screenshot"
        self.snapshot.write_bytes(replacement)
        second = Path(target.upload(self.screenshot, self.snapshot, hashlib.sha256(replacement).hexdigest()))
        self.assertNotEqual(second, receipt)
        self.assertEqual(receipt.read_bytes(), self.content)
        self.assertEqual(second.read_bytes(), replacement)
        self.assertEqual(len(list(target.destination.rglob("*.jpg"))), 2)
        self.assertFalse(list(target.destination.rglob("*.tmp")))

    def test_folder_refuses_to_replace_conflicting_file(self):
        target = FolderTarget(self.root / "cloud")
        path = Path(target.upload(self.screenshot, self.snapshot, self.digest))
        path.write_bytes(b"unrelated user content")
        with self.assertRaises(TargetError):
            target.upload(self.screenshot, self.snapshot, self.digest)
        self.assertEqual(path.read_bytes(), b"unrelated user content")

    def test_snapshot_digest_mismatch_does_not_create_destination(self):
        target = FolderTarget(self.root / "cloud")
        with self.assertRaises(TargetError):
            target.upload(self.screenshot, self.snapshot, "0" * 64)
        self.assertFalse(target.destination.exists())

    def test_invalid_account_cannot_escape_destination(self):
        self.screenshot.account_id = "../escape"
        target = FolderTarget(self.root / "cloud")
        with self.assertRaises(TargetError):
            target.upload(self.screenshot, self.snapshot, self.digest)
        self.assertFalse(target.destination.exists())

    def test_netclip_upload_groups_and_reconciles_by_content(self):
        with drive_server() as (drive, url):
            target = NetclipTarget(url)
            first = target.upload(self.screenshot, self.snapshot, self.digest)
            second = NetclipTarget(url).upload(self.screenshot, self.snapshot, self.digest)
            self.assertEqual(first, second)
            self.assertEqual(drive.uploads, 1)
            self.assertEqual(drive.downloads, 1)
            self.assertEqual([e["name"] for e in drive.entries[:3]], ["Steam Screenshots", "123", "570"])
            self.assertIn(self.digest, drive.entries[3]["name"])
            self.assertEqual(drive.contents[4], self.content)
            self.assertEqual(target.folder_url, url + "/?view=drive&folder=1")

    def test_netclip_reconciles_lost_upload_confirmation(self):
        with drive_server() as (drive, url):
            drive.lose_confirmation = True
            target = NetclipTarget(url)
            with self.assertRaises(TargetError):
                target.upload(self.screenshot, self.snapshot, self.digest)
            receipt = target.upload(self.screenshot, self.snapshot, self.digest)
            self.assertTrue(receipt.endswith("/4/download"))
            self.assertEqual(drive.uploads, 1)
            self.assertEqual(drive.downloads, 1)

    def test_netclip_rejects_corrupt_existing_content(self):
        with drive_server() as (drive, url):
            target = NetclipTarget(url)
            target.upload(self.screenshot, self.snapshot, self.digest)
            drive.contents[4] = b"x" * len(self.content)
            with self.assertRaisesRegex(TargetError, "SHA-256"):
                target.upload(self.screenshot, self.snapshot, self.digest)
            self.assertEqual(drive.uploads, 1)

    def test_netclip_limit_prevents_folder_creation(self):
        with drive_server() as (drive, url):
            drive.max_upload = 1
            with self.assertRaisesRegex(TargetError, "upload limit"):
                NetclipTarget(url).upload(self.screenshot, self.snapshot, self.digest)
            self.assertEqual(drive.entries, [])
            self.assertEqual(drive.uploads, 0)

    def test_netclip_reports_status_without_server_response_details(self):
        with drive_server() as (drive, url):
            drive.reject_status = 503
            with self.assertRaisesRegex(TargetError, "HTTP 503") as raised:
                NetclipTarget(url).upload(self.screenshot, self.snapshot, self.digest)
            self.assertNotIn("secret-server-detail", str(raised.exception))

    def test_netclip_unicode_names_are_encoded_and_bounded(self):
        self.screenshot.path = Path("截图" * 150 + ".png")
        with drive_server() as (drive, url):
            NetclipTarget(url).upload(self.screenshot, self.snapshot, self.digest)
            name = drive.entries[-1]["name"]
            self.assertLessEqual(len(name.encode("utf-8")), 240)
            self.assertTrue(name.startswith("截图"))
            self.assertTrue(name.endswith(".png"))

    def test_netclip_constructor_rejects_credentials_and_invalid_folder(self):
        for url in ["ftp://host", "http://user:password@host", "http://host?token=secret", "http://host#fragment"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                NetclipTarget(url)
        with self.assertRaises(ValueError):
            NetclipTarget("http://host", "../escape")


if __name__ == "__main__":
    unittest.main()
