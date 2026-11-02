"""A narrowly scoped, local-only CDP transport for the Steam client.

Discovery and the capability probe are read-only. An evaluation supplied by the
caller is sent exactly once: a timeout can leave its outcome unknown.
"""

from __future__ import annotations

import importlib
from http.client import HTTPConnection
import json
import math
import re
import socket
from socket import create_connection as _connect_tcp
import threading
import time
from urllib import error, parse, request


class CDPError(RuntimeError):
    """Steam returned an invalid response or rejected an evaluation."""


class CDPUnavailable(CDPError):
    """A trusted local Steam debugging target could not be reached."""


class CDPTimeout(CDPError):
    """The deadline expired; an evaluation already sent must not be replayed."""


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _Deadline:
    """Abort blocking socket I/O at an elapsed-time deadline, even on drips/pings."""

    def __init__(self, seconds: float):
        self.expired = False
        self._sockets = []
        self._lock = threading.Lock()
        self._timer = threading.Timer(seconds, self._expire)
        self._timer.daemon = True
        self._timer.start()

    @staticmethod
    def _abort(connection):
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except (OSError, ValueError):
            pass

    def track(self, connection):
        with self._lock:
            if not self.expired:
                self._sockets.append(connection)
                return
        self._abort(connection)

    def _expire(self):
        with self._lock:
            self.expired = True
            sockets = self._sockets[:]
        for connection in sockets:
            self._abort(connection)

    def stop(self):
        self._timer.cancel()
        # Prevent a late timer from aborting a connection reused by the next call.
        self._timer.join()


class _DeadlineHTTPConnection(HTTPConnection):
    def __init__(self, *args, guard, **kwargs):
        self._guard = guard
        super().__init__(*args, **kwargs)

    def connect(self):
        super().connect()
        self._guard.track(self.sock)


class _DeadlineHTTPHandler(request.HTTPHandler):
    def __init__(self, guard):
        super().__init__()
        self._guard = guard

    def http_open(self, req):
        return self.do_open(_DeadlineHTTPConnection, req, guard=self._guard)


_MAX_DISCOVERY_BYTES = 1024 * 1024
_MAX_MESSAGE_BYTES = 16 * 1024 * 1024
_PROBE = 'Boolean(typeof SteamClient !== "undefined" && SteamClient && SteamClient.Screenshots)'
_AUTHORITY = re.compile(r"(?:127\.0\.0\.1|\[::1\])(?::[0-9]+)?\Z")


def _duration(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CDPError("CDP timeout must be a positive finite number of seconds")
    if not math.isfinite(value) or value <= 0:
        raise CDPError("CDP timeout must be a positive finite number of seconds")
    return float(value)


def _local_url(value: str, scheme: str):
    try:
        if not isinstance(value, str) or re.search(r"[\x00-\x20\x7f]", value):
            raise ValueError
        parts = parse.urlsplit(value)
        if parts.scheme != scheme or not _AUTHORITY.fullmatch(parts.netloc):
            raise ValueError
        if parts.username is not None or parts.password is not None or parts.fragment:
            raise ValueError
        port = parts.port if parts.port is not None else 80
        if not 1 <= port <= 65535:
            raise ValueError
        return parts, port
    except (TypeError, ValueError):
        raise CDPUnavailable("Steam CDP requires a numeric loopback address (127.0.0.1 or [::1])") from None


def _trusted_target(target) -> bool:
    if not isinstance(target, dict) or target.get("type") not in ("page", "other"):
        return False
    if target.get("title") == "SharedJSContext":
        return True
    try:
        location = parse.urlsplit(target.get("url", ""))
        return (location.scheme in ("http", "https")
                and location.hostname == "steamloopback.host"
                and location.username is None and location.password is None)
    except (TypeError, ValueError):
        return False


class SteamCDP:
    """Evaluate JavaScript only in a validated local Steam renderer.

    The constructor performs no network requests. Install ``.[steam]`` to enable
    the optional websocket-client transport. Instances serialize evaluations.
    """

    def __init__(self, endpoint: str = "http://127.0.0.1:8080", timeout: float = 10):
        parts, self._port = _local_url(endpoint, "http")
        if parts.path not in ("", "/") or parts.query:
            raise CDPUnavailable("Steam CDP endpoint must be an HTTP origin without a path or query")
        self.endpoint = f"http://{parts.netloc}"
        self.timeout = _duration(timeout)
        self._host = parts.hostname
        self._socket = None
        self._websocket = None
        self._next_id = 0
        self._lock = threading.RLock()
        self._guard = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def close(self) -> None:
        """Release the connection without waiting for a close handshake."""
        with self._lock:
            connection, self._socket = self._socket, None
            if connection is not None:
                try:
                    connection.close(timeout=0)
                except Exception:
                    # Cleanup must not hide the original error or cause a retry.
                    pass

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CDPTimeout("Steam CDP timed out; check Steam before retrying an operation")
        return remaining

    def _load_websocket(self):
        if self._websocket is None:
            try:
                self._websocket = importlib.import_module("websocket")
            except ImportError:
                raise CDPUnavailable('Steam support requires websocket-client; run: python -m pip install ".[steam]"') from None
        return self._websocket

    def _is_timeout(self, exc: Exception) -> bool:
        timeout_class = getattr(self._websocket, "WebSocketTimeoutException", TimeoutError)
        return (self._guard is not None and self._guard.expired) or isinstance(
            exc, (TimeoutError, socket.timeout, timeout_class))

    def _discover(self, deadline: float) -> list:
        opener = request.build_opener(request.ProxyHandler({}), _NoRedirect(),
                                     _DeadlineHTTPHandler(self._guard))
        for path in ("/json/list", "/json"):
            try:
                req = request.Request(self.endpoint + path, headers={"Accept": "application/json"})
                with opener.open(req, timeout=self._remaining(deadline)) as response:
                    data = response.read(_MAX_DISCOVERY_BYTES + 1)
                self._remaining(deadline)
                if len(data) > _MAX_DISCOVERY_BYTES:
                    raise CDPUnavailable("Steam CDP discovery response exceeds the size limit")
                targets = json.loads(data)
                if not isinstance(targets, list):
                    raise CDPUnavailable("Steam CDP discovery did not return a target list")
                return targets
            except error.HTTPError as exc:
                if path == "/json/list" and exc.code in (404, 405):
                    continue
                raise CDPUnavailable("Steam CDP discovery was refused; redirects are not allowed") from None
            except CDPError:
                raise
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise CDPUnavailable("Steam CDP discovery returned invalid JSON") from None
            except Exception as exc:
                reason = exc.reason if isinstance(exc, error.URLError) else exc
                if self._is_timeout(reason):
                    raise CDPTimeout("Steam CDP discovery timed out") from None
                raise CDPUnavailable("Cannot reach local Steam CDP; start Steam with -cef-enable-debugging") from None
        raise CDPUnavailable("Steam CDP discovery endpoint was not found")

    def _validate_socket_url(self, value: str) -> str:
        parts, port = _local_url(value, "ws")
        if parts.hostname != self._host or port != self._port:
            raise CDPUnavailable("Steam CDP target must use the same loopback host and port")
        return value

    def _connect(self, deadline: float) -> None:
        module = self._load_websocket()
        candidates = [target for target in self._discover(deadline) if _trusted_target(target)]
        # Prefer Steam's shared context over individual panels.
        candidates.sort(key=lambda target: target.get("title") != "SharedJSContext")
        for target in candidates:
            try:
                url = self._validate_socket_url(target.get("webSocketDebuggerUrl", ""))
                # Preconnect directly so even a stalled upgrade handshake can
                # be aborted at the shared deadline, without any proxy lookup.
                raw_socket = _connect_tcp((self._host, self._port), timeout=self._remaining(deadline))
                self._guard.track(raw_socket)
                try:
                    self._socket = module.create_connection(
                        url, timeout=self._remaining(deadline), suppress_origin=True,
                        http_no_proxy=["*"], redirect_limit=0, enable_multithread=True,
                        socket=raw_socket,
                    )
                except Exception:
                    raw_socket.close()
                    raise
                if self._evaluate_once(_PROBE, deadline) is True:
                    return
            except CDPTimeout:
                self.close()
                raise
            except Exception as exc:
                if self._is_timeout(exc):
                    self.close()
                    raise CDPTimeout("Steam CDP connection or capability probe timed out") from None
                # Trying another target is safe: only the read-only probe ran.
            self.close()
        raise CDPUnavailable("No trusted Steam renderer exposes SteamClient.Screenshots; open Steam and enable client debugging")

    def _evaluate_once(self, expression: str, deadline: float):
        self._next_id += 1
        identifier = self._next_id
        payload = {"id": identifier, "method": "Runtime.evaluate", "params": {
            "expression": expression, "awaitPromise": True, "returnByValue": True,
        }}
        try:
            self._socket.settimeout(self._remaining(deadline))
            self._socket.send(json.dumps(payload, ensure_ascii=True))
            while True:
                self._socket.settimeout(self._remaining(deadline))
                raw = self._socket.recv()
                self._remaining(deadline)
                if not raw:
                    raise CDPError("Steam CDP disconnected; the operation outcome may be unknown")
                if not isinstance(raw, (str, bytes)) or len(raw) > _MAX_MESSAGE_BYTES:
                    raise CDPError("Steam CDP returned an oversized or invalid message")
                response = json.loads(raw)
                if not isinstance(response, dict):
                    raise CDPError("Steam CDP returned an invalid response")
                if type(response.get("id")) is not int or response["id"] != identifier:
                    continue
                if "error" in response:
                    raise CDPError("Steam rejected the CDP evaluation; check client compatibility")
                result = response.get("result")
                if not isinstance(result, dict):
                    raise CDPError("Steam CDP response is missing its result")
                if "exceptionDetails" in result:
                    raise CDPError("JavaScript evaluation failed in Steam; check client compatibility")
                value = result.get("result")
                if not isinstance(value, dict):
                    raise CDPError("Steam CDP response is missing its value")
                if "value" in value:
                    return value["value"]
                if value.get("type") == "undefined":
                    return None
                raise CDPError("Steam evaluation did not return a JSON value")
        except CDPError:
            raise
        except Exception as exc:
            if self._is_timeout(exc):
                raise CDPTimeout("Steam evaluation timed out; its outcome is unknown, so check Steam before retrying") from None
            raise CDPError("Steam CDP connection failed; the operation outcome may be unknown") from None

    def evaluate(self, expression: str, *, timeout: float | None = None):
        """Return the JSON value of one evaluation, without replaying it on failure."""
        if not isinstance(expression, str) or not expression.strip():
            raise CDPError("CDP expression must be a non-empty string")
        duration = self.timeout if timeout is None else _duration(timeout)
        deadline = time.monotonic() + duration
        if not self._lock.acquire(timeout=duration):
            raise CDPTimeout("Another Steam CDP evaluation is still running")
        try:
            self._guard = _Deadline(self._remaining(deadline))
            if self._socket is not None and getattr(self._socket, "sock", None) is not None:
                self._guard.track(self._socket.sock)
            if self._socket is None:
                self._connect(deadline)
            return self._evaluate_once(expression, deadline)
        except CDPError:
            self.close()
            raise
        finally:
            if self._guard is not None:
                self._guard.stop()
                self._guard = None
            self._lock.release()
