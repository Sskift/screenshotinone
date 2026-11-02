from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from screenshotinone.cdp import CDPError, CDPTimeout, CDPUnavailable, SteamCDP


def result(identifier, value):
    return json.dumps({"id": identifier, "result": {"result": {"type": "object", "value": value}}})


class FakeTimeout(Exception):
    pass


class FakeSocket:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.sent = []
        self.timeouts = []
        self.closed = False

    def settimeout(self, value):
        self.timeouts.append(value)

    def send(self, value):
        self.sent.append(json.loads(value))

    def recv(self):
        value = next(self.messages)
        if isinstance(value, Exception):
            raise value
        return value

    def close(self, **kwargs):
        self.closed = True


def target(endpoint="http://127.0.0.1:8080", **changes):
    value = {"type": "page", "title": "SharedJSContext", "url": "about:blank",
             "webSocketDebuggerUrl": endpoint.replace("http:", "ws:") + "/devtools/page/steam"}
    value.update(changes)
    return value


@contextmanager
def discovery_server(responses):
    visits = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            visits.append(self.path)
            status, body, headers = responses.get(self.path, (404, b"", {}))
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", visits
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class CDPTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("screenshotinone.cdp._connect_tcp", return_value=Mock())
        self.raw_connect = patcher.start()
        self.addCleanup(patcher.stop)

    def module(self, sockets):
        return SimpleNamespace(create_connection=Mock(side_effect=sockets), WebSocketTimeoutException=FakeTimeout)

    def connect(self, client, module, targets=None):
        return (patch("screenshotinone.cdp.importlib.import_module", return_value=module),
                patch.object(client, "_discover", return_value=targets or [target(client.endpoint)]))

    def test_constructor_is_lazy_and_rejects_nonlocal_or_ambiguous_endpoints(self):
        with patch("screenshotinone.cdp.request.build_opener") as opener, \
                patch("screenshotinone.cdp.importlib.import_module") as importer:
            SteamCDP()
            SteamCDP("http://[::1]:8080/")
            opener.assert_not_called()
            importer.assert_not_called()
        for endpoint in ("https://127.0.0.1:8080", "http://localhost:8080", "http://127.0.0.2",
                         "http://example.com", "http://127.0.0.1@evil.test", "http://u:p@127.0.0.1",
                         "http://127.0.0.1/x", "http://127.0.0.1?token=secret", "http://127.0.0.1#x",
                         "http://127.0.0.1:0", "http://[::ffff:127.0.0.1]", "\nhttp://127.0.0.1"):
            with self.subTest(endpoint=endpoint), self.assertRaises(CDPUnavailable):
                SteamCDP(endpoint)

    def test_timeout_validation(self):
        for value in (0, -1, float("nan"), float("inf"), True, "10"):
            with self.subTest(value=value), self.assertRaises(CDPError):
                SteamCDP(timeout=value)

    def test_missing_optional_dependency_is_actionable(self):
        with patch("screenshotinone.cdp.importlib.import_module", side_effect=ImportError("secret")):
            with self.assertRaisesRegex(CDPUnavailable, r'pip install "\.\[steam\]"'):
                SteamCDP().evaluate("1")

    def test_pairs_ids_and_ignores_events_without_replaying(self):
        connection = FakeSocket([result(1, True), '{"method":"Runtime.consoleAPICalled"}',
                                 result(900, "unrelated"), result(2, {"uploaded": True})])
        module = self.module([connection])
        client = SteamCDP()
        importer, discovery = self.connect(client, module)
        with importer, discovery, client:
            self.assertEqual(client.evaluate("performUpload()"), {"uploaded": True})
        self.assertTrue(connection.closed)
        self.assertEqual(len(connection.sent), 2)
        self.assertEqual(connection.sent[1]["params"], {
            "expression": "performUpload()", "awaitPromise": True, "returnByValue": True})
        options = module.create_connection.call_args.kwargs
        self.assertEqual(options["http_no_proxy"], ["*"])
        self.assertEqual(options["redirect_limit"], 0)
        self.assertTrue(options["suppress_origin"])

    def test_rejects_injected_socket_authorities_without_connecting(self):
        for url in ("ws://evil.test:8080/x", "ws://127.0.0.1:9999/x", "ws://[::1]:8080/x",
                    "ws://u:p@127.0.0.1:8080/x", "wss://127.0.0.1:8080/x"):
            client = SteamCDP()
            module = self.module([])
            importer, discovery = self.connect(client, module, [target(webSocketDebuggerUrl=url)])
            with self.subTest(url=url), importer, discovery, self.assertRaises(CDPUnavailable):
                client.evaluate("performUpload()")
            module.create_connection.assert_not_called()

    def test_skips_generic_browser_and_requires_steam_capabilities(self):
        unsuitable = FakeSocket([result(1, False)])
        steam = FakeSocket([result(2, True), result(3, 42)])
        module = self.module([unsuitable, steam])
        client = SteamCDP()
        candidates = [target(title="Browser", url="https://example.com"),
                      target(), target(title="Steam", url="https://steamloopback.host/library")]
        importer, discovery = self.connect(client, module, candidates)
        with importer, discovery:
            self.assertEqual(client.evaluate("40+2"), 42)
        self.assertTrue(unsuitable.closed)
        self.assertEqual(module.create_connection.call_count, 2)
        self.assertEqual(len(unsuitable.sent), 1)
        client.close()

    def test_exception_details_and_protocol_errors_are_redacted(self):
        for bad in ({"id": 2, "result": {"exceptionDetails": {"text": "secret-token"}}},
                    {"id": 2, "error": {"message": "secret-token"}},
                    {"id": 2, "result": {"result": {"unserializableValue": "secret-token"}}}):
            connection = FakeSocket([result(1, True), json.dumps(bad)])
            client = SteamCDP()
            importer, discovery = self.connect(client, self.module([connection]))
            with importer, discovery, self.assertRaises(CDPError) as caught:
                client.evaluate("performUpload()")
            self.assertNotIn("secret-token", str(caught.exception))
            self.assertTrue(connection.closed)
            self.assertEqual(len(connection.sent), 2)

    def test_timeout_or_disconnect_never_replays_requested_evaluation(self):
        for failure, expected in ((FakeTimeout("secret"), CDPTimeout), ("", CDPError),
                                  (OSError("secret"), CDPError)):
            connection = FakeSocket([result(1, True), failure])
            module = self.module([connection])
            client = SteamCDP()
            importer, discovery = self.connect(client, module)
            with importer, discovery, self.assertRaises(expected) as caught:
                client.evaluate("performUpload()")
            self.assertNotIn("secret", str(caught.exception))
            self.assertEqual(module.create_connection.call_count, 1)
            self.assertEqual(len(connection.sent), 2)
            self.assertTrue(connection.closed)

    def test_deadline_includes_unrelated_messages(self):
        client = SteamCDP(timeout=1)
        connection = FakeSocket(['{"method":"event"}'] * 20)
        client._socket = connection
        client._websocket = self.module([])
        with patch("screenshotinone.cdp.time.monotonic", side_effect=[0, .05, .1, .2, .6, .8, 1.1]):
            with self.assertRaises(CDPTimeout):
                client.evaluate("performUpload()")
        self.assertEqual(len(connection.sent), 1)
        self.assertTrue(connection.closed)

    def test_discovery_fallback_and_proxy_bypass(self):
        responses = {"/json/list": (404, b"", {})}
        with discovery_server(responses) as (endpoint, visits):
            responses["/json"] = (200, json.dumps([target(endpoint)]).encode(), {})
            client = SteamCDP(endpoint)
            connection = FakeSocket([result(1, True), result(2, 7)])
            with patch("screenshotinone.cdp.importlib.import_module", return_value=self.module([connection])), \
                    patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1", "no_proxy": ""}):
                self.assertEqual(client.evaluate("7"), 7)
            self.assertEqual(visits, ["/json/list", "/json"])
            client.close()

    def test_discovery_never_follows_redirects(self):
        responses = {"/json/list": (302, b"", {"Location": "/secret"})}
        with discovery_server(responses) as (endpoint, visits):
            module = self.module([])
            with patch("screenshotinone.cdp.importlib.import_module", return_value=module):
                with self.assertRaises(CDPUnavailable):
                    SteamCDP(endpoint).evaluate("1")
            self.assertEqual(visits, ["/json/list"])
            module.create_connection.assert_not_called()

    def test_discovery_response_size_is_bounded(self):
        responses = {"/json/list": (200, b"[" + b" " * 1024, {})}
        with discovery_server(responses) as (endpoint, visits), \
                patch("screenshotinone.cdp._MAX_DISCOVERY_BYTES", 128), \
                patch("screenshotinone.cdp.importlib.import_module", return_value=self.module([])):
            with self.assertRaisesRegex(CDPUnavailable, "size limit"):
                SteamCDP(endpoint).evaluate("1")

    def test_deadline_aborts_drip_fed_http_body(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "100")
                self.end_headers()
                try:
                    for _ in range(100):
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(.02)
                except (OSError, ConnectionError):
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = SteamCDP(f"http://127.0.0.1:{server.server_port}", timeout=.12)
            started = time.monotonic()
            with patch("screenshotinone.cdp.importlib.import_module", return_value=self.module([])):
                with self.assertRaises(CDPTimeout):
                    client.evaluate("1")
            self.assertLess(time.monotonic() - started, .6)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_deadline_aborts_socket_when_recv_never_returns_a_message(self):
        reader, writer = socket.socketpair()
        finished = threading.Event()

        class StreamingSocket(FakeSocket):
            sock = reader

            def recv(self):
                # Models recv() consuming ping/fragment traffic internally.
                while self.sock.recv(1):
                    pass
                return ""

            def close(self, **kwargs):
                super().close(**kwargs)
                self.sock.close()

        def drip():
            try:
                while not finished.wait(.02):
                    writer.send(b"x")
            except OSError:
                pass

        thread = threading.Thread(target=drip, daemon=True)
        thread.start()
        connection = StreamingSocket([])
        client = SteamCDP(timeout=.12)
        client._socket = connection
        client._websocket = self.module([])
        started = time.monotonic()
        try:
            with self.assertRaises(CDPTimeout):
                client.evaluate("performUpload()")
            self.assertLess(time.monotonic() - started, .6)
            self.assertEqual(len(connection.sent), 1)
        finally:
            finished.set()
            reader.close()
            writer.close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
