from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from screenshotinone.steam_bridge import (
    STEAM_ID_BASE, SteamBridge, SteamBridgeError, SteamPublishUncertain, SteamScreenshot,
)


ACCOUNT = "12345"
STEAMID = str(STEAM_ID_BASE + int(ACCOUNT))
METHODS = (
    "GetAllAppsLocalScreenshotsCount", "GetAllAppsLocalScreenshotsRange", "GetAllAppsLocalScreenshots",
    "GetLocalScreenshotByHandle", "GetLocalScreenshotPath", "UploadLocalScreenshot",
)


def arguments(expression):
    return json.loads(expression[expression.rfind("})(") + 3:-1])


class FakeCDP:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []
        self.expressions = []
        self.closed = False
        self.capabilities = {name: name != "GetAllAppsLocalScreenshots" for name in METHODS}
        self.identity = {"ok": True, "account_id": ACCOUNT, "steam_id64": STEAMID, "offline": False}
        self.publish_result = {"ok": True, "accepted": True}
        self.override = {}

    def evaluate(self, expression, *, timeout):
        self.expressions.append(expression)
        args = arguments(expression)
        self.calls.append(args)
        action = args["action"]
        if action in self.override:
            value = self.override[action]
            if isinstance(value, Exception):
                raise value
            return value
        if action == "doctor":
            return {"ok": True, "identity": self.identity, "capabilities": self.capabilities, "supported": True}
        if args.get("steam_id64") != self.identity["steam_id64"]:
            return {"ok": False, "code": "account_changed"}
        if action == "count":
            return {"ok": True, "count": len(self.rows)}
        if action == "range":
            return {"ok": True, "screenshots": self.rows[args["start"]:args["end"] + 1]}
        if action == "all":
            return {"ok": True, "screenshots": self.rows}
        if action == "get":
            shot = next((r for r in self.rows if r["strGameID"] == args["game_id"] and r["hHandle"] == args["handle"]), None)
            return {"ok": True, "screenshot": shot}
        if action == "publish":
            if isinstance(self.publish_result, Exception):
                raise self.publish_result
            return self.publish_result
        raise AssertionError(action)

    def close(self):
        self.closed = True


class SteamBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.rows = [self.row(index) for index in range(5)]
        self.cdp = FakeCDP(self.rows)
        self.bridge = SteamBridge(self.cdp, page_size=2)

    def tearDown(self):
        self.temporary.cleanup()

    def row(self, index=0):
        return {"strGameID": "570", "hHandle": index + 1, "path": str(self.root / f"screenshot-{index}.jpg"),
                "bUploaded": False, "ePrivacy": 2, "ugcHandle": "0"}

    def test_doctor_only_reports_verified_string_identity(self):
        status = self.bridge.doctor()
        self.assertTrue(status["ready"])
        self.assertEqual(status["account_id"], ACCOUNT)
        self.assertEqual(status["steam_id64"], STEAMID)
        self.assertEqual([c["action"] for c in self.cdp.calls], ["doctor"])

    def test_numeric_steam_id_is_rejected_without_precision_guess(self):
        self.cdp.identity["steam_id64"] = int(STEAMID)
        with self.assertRaises(SteamBridgeError):
            self.bridge.doctor()

    def test_identity_account_fields_must_agree(self):
        self.cdp.identity["account_id"] = "12346"
        with self.assertRaises(SteamBridgeError):
            self.bridge.doctor()

    def test_unsupported_and_logged_out_clients_fail_closed(self):
        self.cdp.capabilities["GetLocalScreenshotPath"] = False
        self.assertFalse(self.bridge.doctor()["ready"])
        with self.assertRaises(SteamBridgeError):
            self.bridge.list_screenshots()
        self.cdp.capabilities["GetLocalScreenshotPath"] = True
        self.cdp.identity = {"ok": False, "code": "not_logged_in"}
        self.assertIsNone(self.bridge.doctor()["account_id"])
        with self.assertRaises(SteamBridgeError):
            self.bridge.list_screenshots()

    def test_modern_ranges_are_inclusive_and_verified(self):
        shots = self.bridge.list_screenshots()
        pages = [(c["start"], c["end"]) for c in self.cdp.calls if c["action"] == "range"]
        self.assertEqual(pages, [(0, 1), (2, 3), (4, 4)])
        self.assertEqual(len(shots), 5)
        self.assertEqual(shots[0].account_id, ACCOUNT)
        self.assertEqual(shots[0].game_id, "570")
        self.assertEqual(shots[0].str_game_id, "570")
        self.assertEqual(shots[0].path, self.root / "screenshot-0.jpg")
        self.assertEqual(shots[0].visibility, "private")
        self.assertEqual(sum(c["action"] == "count" for c in self.cdp.calls), 2)
        self.assertNotIn("all", [c["action"] for c in self.cdp.calls])

    def test_old_all_api_is_only_used_as_feature_detected_fallback(self):
        self.cdp.capabilities["GetAllAppsLocalScreenshotsCount"] = False
        self.cdp.capabilities["GetAllAppsLocalScreenshotsRange"] = False
        self.cdp.capabilities["GetAllAppsLocalScreenshots"] = True
        self.assertEqual(len(self.bridge.list_screenshots()), 5)
        self.assertEqual([c["action"] for c in self.cdp.calls], ["doctor", "all"])

    def test_incomplete_page_is_not_silently_accepted(self):
        self.cdp.override["range"] = {"ok": True, "screenshots": []}
        with self.assertRaisesRegex(SteamBridgeError, "incomplete page"):
            self.bridge.list_screenshots()

    def test_duplicate_handles_and_duplicate_paths_fail_closed(self):
        self.cdp.rows[1]["hHandle"] = self.cdp.rows[0]["hHandle"]
        with self.assertRaisesRegex(SteamBridgeError, "duplicate"):
            self.bridge.list_screenshots()
        self.cdp.rows[1]["hHandle"] = 2
        self.cdp.rows[1]["path"] = self.cdp.rows[0]["path"]
        with self.assertRaisesRegex(SteamBridgeError, "duplicate"):
            self.bridge.list_screenshots()

    def test_shot_shapes_are_strict(self):
        invalid = [
            {"strGameID": 570}, {"strGameID": "1);publish()"}, {"hHandle": True}, {"hHandle": 2**53},
            {"bUploaded": "false"}, {"path": "relative.jpg"}, {"ugcHandle": 123456789012345678},
            {"ePrivacy": True}, {"publishedFileID": 123456789012345678},
        ]
        for patch in invalid:
            with self.subTest(patch=patch):
                self.cdp.rows = [{**self.row(), **patch}]
                with self.assertRaises(SteamBridgeError):
                    self.bridge.list_screenshots()

    def test_get_keeps_content_handle_and_published_id_distinct(self):
        self.cdp.rows[0].update(bUploaded=True, ugcHandle="123456789012345678", publishedFileID="987654321", ePrivacy=4)
        shot = self.bridge.get_screenshot("570", 1)
        self.assertTrue(shot.uploaded)
        self.assertEqual(shot.ugc_id, "123456789012345678")
        self.assertEqual(shot.published_file_id, "987654321")
        self.assertEqual(shot.published_id, "987654321")
        self.assertEqual(shot.visibility, "friends")
        self.cdp.override["get"] = {"ok": True, "screenshot": self.row(1)}
        with self.assertRaisesRegex(SteamBridgeError, "different screenshot"):
            self.bridge.get_screenshot("570", 1)

    def test_content_handle_never_becomes_a_published_file_id(self):
        self.cdp.rows[0].update(bUploaded=True, ugcHandle="123456789012345678")
        shot = self.bridge.get_screenshot("570", 1)
        self.assertEqual(shot.ugc_id, "123456789012345678")
        self.assertIsNone(shot.published_file_id)

    def test_native_invalid_ugc_sentinel_never_confirms_an_upload(self):
        for uploaded in (False, True):
            with self.subTest(uploaded=uploaded):
                self.cdp.rows[0].update(bUploaded=uploaded, ugcHandle="18446744073709551615", publishedFileID="0")
                shot = self.bridge.get_screenshot("570", 1)
                self.assertEqual(shot.uploaded, uploaded)
                self.assertIsNone(shot.ugc_id)
                self.assertIsNone(shot.published_file_id)

    def test_invalid_maximum_published_id_is_rejected(self):
        self.cdp.rows[0].update(publishedFileID="18446744073709551615")
        with self.assertRaises(SteamBridgeError):
            self.bridge.get_screenshot("570", 1)

    def test_publish_revalidates_identity_and_uses_native_privacy_values(self):
        shot = self.bridge.get_screenshot("570", 1)
        for name, value in [("private", 2), ("friends", 4), ("public", 8), ("unlisted", 16)]:
            self.assertTrue(self.bridge.publish(shot, name))
            self.assertEqual(self.cdp.calls[-1]["privacy"], value)
            self.assertEqual(self.cdp.calls[-1]["game_id"], "570")
            self.assertEqual(self.cdp.calls[-1]["path"], str(shot.path))
        prior = len(self.cdp.calls)
        with self.assertRaises(SteamBridgeError):
            self.bridge.publish(replace(shot, account_id="12346"))
        self.assertEqual([c["action"] for c in self.cdp.calls[prior:]], ["doctor"])

    def test_publish_false_and_unknown_outcome_remain_distinct(self):
        shot = self.bridge.get_screenshot("570", 1)
        self.cdp.publish_result = {"ok": True, "accepted": False}
        self.assertFalse(self.bridge.publish(shot))
        for outcome in [TimeoutError("private error content"), {"ok": True, "accepted": "yes"},
                        {"ok": False, "code": "publish_uncertain"}, None]:
            with self.subTest(outcome=outcome):
                self.cdp.publish_result = outcome
                count = sum(c["action"] == "publish" for c in self.cdp.calls)
                with self.assertRaises(SteamPublishUncertain) as raised:
                    self.bridge.publish(shot)
                self.assertNotIn("private error content", str(raised.exception))
                self.assertEqual(sum(c["action"] == "publish" for c in self.cdp.calls), count + 1)

    def test_native_preflight_failure_is_definite(self):
        shot = self.bridge.get_screenshot("570", 1)
        self.cdp.publish_result = {"ok": False, "code": "path_changed"}
        with self.assertRaises(SteamBridgeError) as raised:
            self.bridge.publish(shot)
        self.assertNotIsInstance(raised.exception, SteamPublishUncertain)

    def test_data_is_serialized_as_json_and_close_releases_connection(self):
        shot = self.bridge.get_screenshot("570", 1)
        path = self.root / "quote' double\" backtick` ${injection}.jpg"
        self.bridge.publish(replace(shot, path=path))
        self.assertEqual(arguments(self.cdp.expressions[-1])["path"], str(path))
        self.bridge.close()
        self.assertTrue(self.cdp.closed)

    @unittest.skipUnless(shutil.which("node"), "Node is optional for executing the static JavaScript contract test")
    def test_static_javascript_executes_native_preflight_without_ui_actions(self):
        shot = self.bridge.get_screenshot("570", 1)
        self.bridge.publish(shot)
        expression = self.cdp.expressions[-1]
        # Run our actual generated JS in an isolated process with a native API
        # double. This never connects to Steam or the network.
        runner = r"""
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
let calls = [];
globalThis.App = {
  BHasCurrentUser: () => true,
  GetCurrentUser: () => ({strSteamID: input.steamid, bIsOfflineMode: false})
};
globalThis.SteamClient = {Screenshots: {
  GetAllAppsLocalScreenshotsCount: async () => 1,
  GetAllAppsLocalScreenshotsRange: async () => [input.row],
  GetLocalScreenshotByHandle: async (game, handle) => {calls.push(['get',game,handle]);return input.row;},
  GetLocalScreenshotPath: async (game, handle) => {calls.push(['path',game,handle]);return input.path;},
  UploadLocalScreenshot: async (...args) => {calls.push(['publish',...args]);return true;}
}};
(async () => {const result = await eval(input.expression);process.stdout.write(JSON.stringify({result,calls}));})()
 .catch(() => process.exit(1));
"""
        def execute(path, steamid=STEAMID):
            payload = {"expression": expression, "steamid": steamid, "row": self.row(), "path": path}
            result = subprocess.run([shutil.which("node"), "-e", runner], input=json.dumps(payload),
                                    text=True, capture_output=True, check=True, timeout=10)
            return json.loads(result.stdout)
        success = execute(str(shot.path))
        self.assertEqual(success["result"], {"ok": True, "accepted": True})
        self.assertEqual(success["calls"][-1], ["publish", "570", 1, 2])
        changed = execute(str(self.root / "different.jpg"))
        self.assertEqual(changed["result"], {"ok": False, "code": "path_changed"})
        self.assertFalse(any(c[0] == "publish" for c in changed["calls"]))
        bad_identity = execute(str(shot.path), int(STEAMID))
        self.assertEqual(bad_identity["result"], {"ok": False, "code": "identity_invalid"})
        self.assertEqual(bad_identity["calls"], [])


if __name__ == "__main__":
    unittest.main()
