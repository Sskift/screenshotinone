from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from screenshotinone.source import discover_steam_roots, scan_screenshots


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "Steam"
        self.originals = self.root / "userdata/123/760/remote/456/screenshots"
        self.originals.mkdir(parents=True)

    def test_only_original_images_are_scanned(self):
        (self.originals / "one.JPG").write_bytes(b"image")
        (self.originals / "two.webp").write_bytes(b"image")
        (self.originals / "metadata.json").write_text("{}")
        (self.originals / "thumbnails").mkdir()
        (self.originals / "thumbnails/one.JPG").write_bytes(b"thumbnail")
        (self.originals / "directory.png").mkdir()
        unrelated = self.root / "userdata/not-an-account/760/remote/456/screenshots"
        unrelated.mkdir(parents=True)
        (unrelated / "ignore.png").write_bytes(b"image")
        screenshots = scan_screenshots([self.root, self.root])
        self.assertEqual(
            [image.relative_path for image in screenshots],
            ["123/456/one.JPG", "123/456/two.webp"],
        )

    def test_multiple_accounts_and_games_keep_identifiers(self):
        (self.originals / "same.png").write_bytes(b"one")
        other = self.root / "userdata/789/760/remote/1000/screenshots/same.png"
        other.parent.mkdir(parents=True)
        other.write_bytes(b"two")
        self.assertEqual(
            {item.relative_path for item in scan_screenshots([self.root])},
            {"123/456/same.png", "789/1000/same.png"},
        )

    def test_missing_root_is_an_error(self):
        with self.assertRaises(FileNotFoundError):
            scan_screenshots([self.root / "missing"])

    def test_root_without_userdata_is_empty(self):
        other = self.root / "no-userdata"
        other.mkdir()
        self.assertEqual(scan_screenshots([other]), [])

    def test_symlinked_images_and_folders_are_excluded(self):
        external = Path(self.temp.name) / "outside.png"
        external.write_bytes(b"private unrelated file")
        try:
            (self.originals / "link.png").symlink_to(external)
        except (OSError, NotImplementedError):
            self.skipTest("Symbolic link creation is unavailable")
        remote = self.root / "userdata/123/760/remote"
        (remote / "999").symlink_to(remote / "456", target_is_directory=True)
        (self.originals / "actual.png").write_bytes(b"real screenshot")
        self.assertEqual(
            [item.relative_path for item in scan_screenshots([self.root])],
            ["123/456/actual.png"],
        )

    def test_discovery_accepts_explicit_environment_root(self):
        with patch.dict("os.environ", {"STEAM_PATH": str(self.root)}):
            roots = discover_steam_roots()
        self.assertIn(self.root.resolve(), roots)
        self.assertEqual(len(roots), len(set(roots)))


if __name__ == "__main__":
    unittest.main()
