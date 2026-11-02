"""Narrow Steam screenshot bridge over a caller-provided local CDP connection.

The calls and range semantics are based on the installed Steam ``steamui/sp.js``:
``window.App.GetCurrentUser().strSteamID``, inclusive screenshot ranges, and
``UploadLocalScreenshot(strGameID, hHandle, ePrivacy)``. No web credentials are
read. This module never starts Steam, changes debugging settings, or retries an
upload. A CDP transport must implement ``evaluate(expression, timeout=...)``.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
from typing import Any


STEAM_ID_BASE = 76561197960265728
MAX_ACCOUNT_ID = 2**32 - 1
MAX_SAFE_INTEGER = 2**53 - 1
MAX_SCREENSHOTS = 1_000_000
PRIVACY = {"private": 2, "friends": 4, "public": 8, "unlisted": 16}
_VISIBILITY = {value: name for name, value in PRIVACY.items()}


class SteamBridgeError(RuntimeError):
    """The native screenshot API or identity could not be safely validated."""


class SteamPublishUncertain(SteamBridgeError):
    """An upload may have started; reconcile native metadata before retrying."""


@dataclass(frozen=True)
class SteamScreenshot:
    account_id: str
    str_game_id: str
    handle: int
    path: Path
    uploaded: bool
    # Steam's local ugcHandle maps to remote hcontent_file, not publishedfileid.
    ugc_id: str | None
    visibility: str | None
    published_file_id: str | None = None

    @property
    def game_id(self) -> str:
        return self.str_game_id

    @property
    def published_id(self) -> str | None:
        return self.published_file_id


# Only an action and JSON data vary. Never interpolate paths or identifiers into
# executable JavaScript. Project native objects to these fields before returning
# them, so the CDP response cannot accidentally include unrelated account data.
_JAVASCRIPT = r"""(async function(args) {
  const api = globalThis.SteamClient?.Screenshots;
  const methods = [
    "GetAllAppsLocalScreenshotsCount", "GetAllAppsLocalScreenshotsRange",
    "GetAllAppsLocalScreenshots", "GetLocalScreenshotByHandle",
    "GetLocalScreenshotPath", "UploadLocalScreenshot"
  ];
  const capabilities = Object.fromEntries(methods.map(name => [name, typeof api?.[name] === "function"]));
  const identity = () => {
    const app = globalThis.App;
    if (typeof app?.BHasCurrentUser !== "function" || typeof app?.GetCurrentUser !== "function")
      return {ok: false, code: "identity_unsupported"};
    if (app.BHasCurrentUser() !== true) return {ok: false, code: "not_logged_in"};
    const user = app.GetCurrentUser();
    const id = user?.strSteamID;
    if (typeof id !== "string" || !/^[0-9]{17}$/.test(id))
      return {ok: false, code: "identity_invalid"};
    const account = BigInt(id) - 76561197960265728n;
    if (account <= 0n || account > 4294967295n) return {ok: false, code: "identity_invalid"};
    return {ok: true, steam_id64: id, account_id: account.toString(), offline: user.bIsOfflineMode === true};
  };
  const failure = code => ({ok: false, code});
  const sameIdentity = expected => {
    const current = identity();
    return current.ok && current.steam_id64 === expected;
  };
  const pathKey = path => /^[A-Za-z]:[\\/]/.test(path) || path.startsWith("\\\\")
    ? path.replace(/\//g, "\\").toLowerCase() : path;
  const pack = async shot => {
    if (!shot || typeof shot !== "object" || typeof shot.strGameID !== "string" ||
        !/^[0-9]+$/.test(shot.strGameID) || !Number.isSafeInteger(shot.hHandle) || shot.hHandle < 0 ||
        typeof shot.bUploaded !== "boolean") throw new Error("invalid_native_screenshot");
    const path = await api.GetLocalScreenshotPath(shot.strGameID, shot.hHandle);
    return {strGameID: shot.strGameID, hHandle: shot.hHandle, path,
      bUploaded: shot.bUploaded, ePrivacy: shot.ePrivacy,
      ugcHandle: shot.ugcHandle, publishedFileID: shot.publishedFileID};
  };
  try {
    const current = identity();
    const canList = (capabilities.GetAllAppsLocalScreenshotsCount && capabilities.GetAllAppsLocalScreenshotsRange)
      || capabilities.GetAllAppsLocalScreenshots;
    const supported = canList && capabilities.GetLocalScreenshotByHandle &&
      capabilities.GetLocalScreenshotPath && capabilities.UploadLocalScreenshot;
    if (args.action === "doctor") return {ok: true, capabilities, identity: current, supported};
    if (!current.ok) return current;
    if (current.steam_id64 !== args.steam_id64) return failure("account_changed");
    if (!supported) return failure("api_unsupported");
    let result;
    if (args.action === "count") {
      if (!capabilities.GetAllAppsLocalScreenshotsCount) return failure("api_unsupported");
      result = {count: await api.GetAllAppsLocalScreenshotsCount()};
    } else if (args.action === "range" || args.action === "all") {
      const shots = args.action === "range"
        ? await api.GetAllAppsLocalScreenshotsRange(args.start, args.end)
        : await api.GetAllAppsLocalScreenshots();
      if (!Array.isArray(shots)) return failure("screenshot_list_invalid");
      const rows = [];
      for (const shot of shots) rows.push(await pack(shot));
      result = {screenshots: rows};
    } else if (args.action === "get" || args.action === "publish") {
      const shot = await api.GetLocalScreenshotByHandle(args.game_id, args.handle);
      const row = await pack(shot);
      if (row.strGameID !== args.game_id || row.hHandle !== args.handle) return failure("screenshot_mismatch");
      if (args.action === "get") result = {screenshot: row};
      else {
        if (!sameIdentity(args.steam_id64)) return failure("account_changed");
        if (identity().offline) return failure("offline");
        if (typeof row.path !== "string" || pathKey(row.path) !== pathKey(args.path))
          return failure("path_changed");
        if (row.bUploaded) return {ok: true, accepted: false, already_uploaded: true};
        // Do not add awaits between the final identity check and initiating the
        // native upload. Exceptions after this point mean outcome unknown.
        if (!sameIdentity(args.steam_id64)) return failure("account_changed");
        try {
          const accepted = await api.UploadLocalScreenshot(args.game_id, args.handle, args.privacy);
          if (typeof accepted !== "boolean") return failure("publish_uncertain");
          if (!sameIdentity(args.steam_id64)) return failure("publish_uncertain");
          return {ok: true, accepted};
        } catch (_) { return failure("publish_uncertain"); }
      }
    } else return failure("action_invalid");
    if (!sameIdentity(args.steam_id64)) return failure("account_changed");
    return {ok: true, ...result};
  } catch (_) { return failure("native_read_failed"); }
})"""


_ERRORS = {
    "identity_unsupported": "Steam's logged-in account identity API is unavailable",
    "not_logged_in": "Steam has no verified logged-in user",
    "identity_invalid": "Steam returned an invalid account identity; refusing to guess",
    "account_changed": "Steam's logged-in account changed; screenshot operation stopped",
    "api_unsupported": "This Steam client does not expose the required screenshot APIs",
    "screenshot_list_invalid": "Steam returned an invalid screenshot list",
    "screenshot_mismatch": "Steam returned a different screenshot than requested",
    "path_changed": "Steam's registered screenshot path changed; publication stopped",
    "offline": "Steam is in offline mode",
    "native_read_failed": "Steam could not read or validate native screenshot metadata",
}


def _integer(value: Any, name: str, minimum: int = 0, maximum: int = MAX_SAFE_INTEGER) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise SteamBridgeError(f"Steam returned an invalid {name}")
    return value


def _decimal(value: Any, name: str, maximum: int, minimum: int = 1) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}", value):
        raise SteamBridgeError(f"Steam returned an invalid {name}; a decimal string is required")
    number = int(value)
    if not minimum <= number <= maximum:
        raise SteamBridgeError(f"Steam returned an out-of-range {name}")
    return str(number)


def _account_from_steamid(value: Any) -> tuple[str, str]:
    steamid = _decimal(value, "Steam ID", STEAM_ID_BASE + MAX_ACCOUNT_ID, STEAM_ID_BASE + 1)
    return str(int(steamid) - STEAM_ID_BASE), steamid


class SteamBridge:
    def __init__(self, cdp, *, timeout: float = 30, page_size: int = 1000):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Steam bridge timeout must be positive and finite")
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 1000:
            raise ValueError("Steam screenshot page size must be between 1 and 1000")
        self.cdp = cdp
        self.timeout = timeout
        self.page_size = page_size

    def _request(self, action: str, *, publishing: bool = False, **args) -> dict:
        payload = {"action": action, **args}
        expression = f"/* screenshotinone:{action} */\n{_JAVASCRIPT}({json.dumps(payload, ensure_ascii=True, allow_nan=False)})"
        try:
            result = self.cdp.evaluate(expression, timeout=self.timeout)
        except Exception:
            if publishing:
                raise SteamPublishUncertain("Steam upload outcome is unknown; reconcile metadata before retrying") from None
            raise SteamBridgeError("Cannot read Steam screenshot metadata through the local CDP connection") from None
        if not isinstance(result, dict) or type(result.get("ok")) is not bool:
            if publishing:
                raise SteamPublishUncertain("Steam upload confirmation was invalid; reconcile metadata before retrying")
            raise SteamBridgeError("Steam returned an invalid bridge response")
        if not result["ok"]:
            code = result.get("code")
            if code == "publish_uncertain":
                raise SteamPublishUncertain("Steam upload outcome is unknown; reconcile metadata before retrying")
            # Only fixed codes returned by our script are considered definite
            # preflight failures. An unfamiliar publication reply fails uncertain.
            if publishing and code not in _ERRORS:
                raise SteamPublishUncertain("Steam upload outcome is unknown; reconcile metadata before retrying")
            raise SteamBridgeError(_ERRORS.get(code, "Steam screenshot operation failed validation"))
        return result

    def doctor(self) -> dict:
        result = self._request("doctor")
        capabilities = result.get("capabilities")
        names = ("GetAllAppsLocalScreenshotsCount", "GetAllAppsLocalScreenshotsRange", "GetAllAppsLocalScreenshots",
                 "GetLocalScreenshotByHandle", "GetLocalScreenshotPath", "UploadLocalScreenshot")
        if not isinstance(capabilities, dict) or any(type(capabilities.get(name)) is not bool for name in names):
            raise SteamBridgeError("Steam returned invalid screenshot capabilities")
        can_list = (capabilities[names[0]] and capabilities[names[1]]) or capabilities[names[2]]
        supported = bool(can_list and all(capabilities[name] for name in names[3:]))
        account_id = steamid = None
        reason = None
        identity = result.get("identity")
        offline = False
        if not isinstance(identity, dict) or type(identity.get("ok")) is not bool:
            raise SteamBridgeError("Steam returned an invalid identity response")
        if identity["ok"]:
            account_id, steamid = _account_from_steamid(identity.get("steam_id64"))
            if identity.get("account_id") != account_id:
                raise SteamBridgeError("Steam account identity fields did not agree")
            if type(identity.get("offline")) is not bool:
                raise SteamBridgeError("Steam returned an invalid connection state")
            offline = identity["offline"]
            if offline:
                reason = _ERRORS["offline"]
        else:
            reason = _ERRORS.get(identity.get("code"), "Steam account identity is unverified")
        if not supported:
            reason = _ERRORS["api_unsupported"]
        return {"ready": bool(supported and account_id and not offline), "supported": supported,
                "account_id": account_id, "steam_id64": steamid, "capabilities": capabilities,
                "identity_source": "App.GetCurrentUser().strSteamID", "offline": offline, "reason": reason}

    def _ready(self) -> dict:
        status = self.doctor()
        if not status["ready"]:
            raise SteamBridgeError(status["reason"] or "Steam screenshot bridge is not ready")
        return status

    @staticmethod
    def _shot(row: Any, account_id: str) -> SteamScreenshot:
        if not isinstance(row, dict):
            raise SteamBridgeError("Steam returned an invalid screenshot")
        gameid = _decimal(row.get("strGameID"), "game ID", 2**64 - 1)
        handle = _integer(row.get("hHandle"), "screenshot handle")
        path = row.get("path")
        if not isinstance(path, str) or not path or any(ord(char) < 32 for char in path) or not Path(path).is_absolute():
            raise SteamBridgeError("Steam returned an invalid absolute screenshot path")
        if type(row.get("bUploaded")) is not bool:
            raise SteamBridgeError("Steam returned an invalid uploaded status")
        identifiers = {}
        for field, label in (("ugcHandle", "screenshot content handle"), ("publishedFileID", "published screenshot ID")):
            value = row.get(field)
            if value is None or value == "" or value == "0" or (type(value) is int and value == 0):
                identifiers[field] = None
                continue
            # Steam uses k_UGCHandleInvalid (UINT64_MAX) on local screenshots
            # that have no cloud content handle. It is never upload evidence,
            # including when bUploaded has an inconsistent true value.
            if field == "ugcHandle" and value == str(2**64 - 1):
                identifiers[field] = None
                continue
            identifiers[field] = _decimal(value, label, 2**64 - 2)
        privacy = row.get("ePrivacy")
        if privacy is not None and (isinstance(privacy, bool) or not isinstance(privacy, int)):
            raise SteamBridgeError("Steam returned an invalid screenshot privacy state")
        return SteamScreenshot(account_id, gameid, handle, Path(path), row["bUploaded"],
                               identifiers["ugcHandle"], _VISIBILITY.get(privacy), identifiers["publishedFileID"])

    def list_screenshots(self) -> list[SteamScreenshot]:
        status = self._ready()
        identity = status["steam_id64"]
        capabilities = status["capabilities"]
        raw = []
        if capabilities["GetAllAppsLocalScreenshotsCount"] and capabilities["GetAllAppsLocalScreenshotsRange"]:
            count = _integer(self._request("count", steam_id64=identity).get("count"), "screenshot count", maximum=MAX_SCREENSHOTS)
            for start in range(0, count, self.page_size):
                end = min(count - 1, start + self.page_size - 1)  # Native end index is inclusive.
                page = self._request("range", steam_id64=identity, start=start, end=end).get("screenshots")
                if not isinstance(page, list) or len(page) != end - start + 1:
                    raise SteamBridgeError("Steam screenshot library changed or returned an incomplete page; retry listing")
                raw.extend(page)
            after = _integer(self._request("count", steam_id64=identity).get("count"), "screenshot count", maximum=MAX_SCREENSHOTS)
            if after != count:
                raise SteamBridgeError("Steam screenshot library changed while listing; retry listing")
        else:
            raw = self._request("all", steam_id64=identity).get("screenshots")
            if not isinstance(raw, list) or len(raw) > MAX_SCREENSHOTS:
                raise SteamBridgeError("Steam returned an invalid screenshot list")
        shots = [self._shot(row, status["account_id"]) for row in raw]
        handles = set()
        paths = set()
        for shot in shots:
            key = (shot.str_game_id, shot.handle)
            path = os.path.normcase(os.path.normpath(str(shot.path)))
            if key in handles or path in paths:
                raise SteamBridgeError("Steam returned duplicate screenshot handles or paths; refusing an ambiguous mapping")
            handles.add(key)
            paths.add(path)
        return shots

    def get_screenshot(self, game_id: str, handle: int) -> SteamScreenshot:
        game_id = _decimal(game_id, "game ID", 2**64 - 1)
        handle = _integer(handle, "screenshot handle")
        status = self._ready()
        result = self._request("get", steam_id64=status["steam_id64"], game_id=game_id, handle=handle)
        shot = self._shot(result.get("screenshot"), status["account_id"])
        if (shot.str_game_id, shot.handle) != (game_id, handle):
            raise SteamBridgeError("Steam returned a different screenshot than requested")
        return shot

    def publish(self, shot: SteamScreenshot, visibility: str = "private") -> bool:
        if visibility not in PRIVACY:
            raise ValueError("Steam visibility must be private, friends, public, or unlisted")
        if not isinstance(shot, SteamScreenshot):
            raise TypeError("publish requires a native SteamScreenshot record")
        account = _decimal(shot.account_id, "account ID", MAX_ACCOUNT_ID)
        gameid = _decimal(shot.str_game_id, "game ID", 2**64 - 1)
        handle = _integer(shot.handle, "screenshot handle")
        if not isinstance(shot.path, Path) or not shot.path.is_absolute():
            raise SteamBridgeError("Screenshot record has an invalid absolute path")
        status = self._ready()
        if status["account_id"] != account:
            raise SteamBridgeError("Steam's logged-in account differs from the screenshot owner")
        result = self._request("publish", publishing=True, steam_id64=status["steam_id64"],
                               game_id=gameid, handle=handle, path=str(shot.path), privacy=PRIVACY[visibility])
        if type(result.get("accepted")) is not bool:
            raise SteamPublishUncertain("Steam upload confirmation was invalid; reconcile metadata before retrying")
        return result["accepted"]

    def close(self) -> None:
        self.cdp.close()
