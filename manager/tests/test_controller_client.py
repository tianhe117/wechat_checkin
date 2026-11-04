"""Wire-level contract tests using the real controller and an in-memory fake ADB.

No real configuration, ADB process or phone is accessed.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import logging
from pathlib import Path
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zlib

from src.controller_client import (
    ControllerClient, ControllerImageError, ControllerProtocolError,
    ControllerTransportError, MAX_RESPONSE_BYTES,
)


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("controller_contract_fixture", ROOT / "controller" / "main.py")
controller = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = controller
spec.loader.exec_module(controller)

KEY = "client-contract-test-key-00000000"
SERIAL = "test-device:5555"


def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)


PNG = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
       + chunk(b"IDAT", zlib.compress(b"\x00\x11\x22\x33")) + chunk(b"IEND", b""))


class FakeAdb:
    def __init__(self):
        self.commands = []
        self.captures = 0
        self.fail_cleanup = False
        self.screenshot_error = None
        self.capture_started = threading.Event()
        self.release_capture = threading.Event()
        self.release_capture.set()

    def devices(self):
        return [{"serial": SERIAL, "state": "device", "usable": True, "brand": "测试品牌", "model": None}]

    def select(self, serial):
        if serial and serial != SERIAL:
            raise controller.APIError(503, "DEVICE_UNAVAILABLE", "目标设备未连接")
        return SERIAL

    def check_package(self, serial, package):
        pass

    def command(self, *args, serial):
        self.commands.append((serial, args))
        if self.fail_cleanup and self.captures:
            raise controller.APIError(502, "ADB_FAILED", "模拟收尾失败")
        return ""

    def launch(self, serial, package):
        self.commands.append((serial, ("launch", package)))

    def screenshot(self, serial):
        self.captures += 1
        self.capture_started.set()
        if not self.release_capture.wait(5):
            raise RuntimeError("Test did not release fake capture")
        if self.screenshot_error is not None:
            raise self.screenshot_error
        return PNG


@contextmanager
def running_server(server):
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


class ContractTests(unittest.TestCase):
    def setUp(self):
        from werkzeug.serving import make_server, WSGIRequestHandler

        class QuietHandler(WSGIRequestHandler):
            def log_request(self, *args, **kwargs):
                pass

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        config = controller.Config(
            adb_exe=Path("unused-adb"), api_key=KEY, host="127.0.0.1", port=8765,
            default_serial=SERIAL, default_package="com.example.default", default_delay=10,
            max_delay=120, command_timeout=20, data_dir=Path(self.directory.name), result_retention_hours=24,
        )
        self.adb = FakeAdb()
        self.service = controller.Controller(config, adb=self.adb, sleeper=lambda seconds: None)
        self.addCleanup(self.service.close)
        server = make_server("127.0.0.1", 0, controller.create_app(config, controller=self.service),
                             threaded=True, request_handler=QuietHandler)
        self.server_context = running_server(server)
        self.url = self.server_context.__enter__()
        self.addCleanup(self.server_context.__exit__, None, None, None)
        self.client = ControllerClient(self.url, KEY)

    def test_all_six_endpoints_and_local_image_persistence(self):
        self.assertFalse(self.client.health().payload["busy"])
        devices = self.client.devices().payload["devices"]
        self.assertEqual(devices[0]["serial"], SERIAL)
        self.assertIsNone(devices[0]["model"])
        result = self.client.run_app("app-1", package="com.example.app", delay=0, serial=SERIAL)
        self.assertEqual(result.http_status, 200)
        self.assertEqual(result.status, "succeeded")
        self.assertTrue(result.terminal)
        self.assertEqual(result.payload["delay"], 0)
        self.assertEqual(result.payload["package"], "com.example.app")
        self.assertEqual(result.payload["cleanup"], {"home": True, "force_stop": True, "lock": True})
        self.assertEqual(result.payload["steps"][-1]["name"], "lock_after")
        self.assertEqual(self.adb.commands[-1], (SERIAL, ("shell", "input", "keyevent", "KEYCODE_SLEEP")))
        self.assertEqual(result.screenshot_bytes(), PNG)
        target = Path(self.directory.name) / "manager-images" / "app-1.png"
        self.assertEqual(result.save_screenshot(target), target)
        self.assertEqual(target.read_bytes(), PNG)
        self.assertEqual(list(target.parent.iterdir()), [target])
        self.assertEqual(self.client.get_request("app-1").payload, result.payload)
        self.assertEqual(self.client.screenshot("screen-1").status, "succeeded")
        self.assertEqual(self.client.test_device("test-1").status, "succeeded")
        self.assertEqual(self.adb.captures, 3)

    def test_screenshot_wakes_unlocks_and_keeps_current_app(self):
        result = self.client.screenshot("wake-for-screenshot")
        self.assertEqual(result.status, "succeeded")
        self.assertEqual([step["name"] for step in result.payload["steps"]], ["wake", "unlock", "screen_wait", "screenshot"])
        self.assertEqual(self.adb.commands, [(SERIAL, ("shell", "input", "keyevent", "KEYCODE_WAKEUP")),
                                            (SERIAL, ("shell", "wm", "dismiss-keyguard"))])
        self.assertEqual(result.screenshot_bytes(), PNG)

    def test_omitted_package_uses_default_and_replay_keeps_original_default(self):
        result = self.client.run_app("default-package", delay=0)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.payload["package"], "com.example.default")
        self.service.config = replace(self.service.config, default_package="com.example.changed")
        replay = self.client.run_app("default-package", package="", delay=0)
        self.assertEqual(replay.payload["package"], "com.example.default")
        self.assertEqual(self.adb.captures, 1)

    def test_auth_failure_and_device_unavailable_are_structured_responses(self):
        result = ControllerClient(self.url, "x" * 32).health()
        self.assertEqual(result.http_status, 401)
        self.assertEqual(result.error["code"], "UNAUTHORIZED")
        result = self.client.test_device("unavailable", serial="missing-device")
        self.assertEqual(result.http_status, 503)
        self.assertEqual(result.error["code"], "DEVICE_UNAVAILABLE")
        self.assertIsNone(result.status)  # Not accepted; no execution record exists.
        self.assertEqual(self.adb.captures, 0)

    def test_cleanup_failure_keeps_screenshot_and_all_errors_even_on_http_502(self):
        self.adb.fail_cleanup = True
        result = self.client.run_app("cleanup-failed", package="com.example.app", delay=0)
        self.assertEqual((result.http_status, result.status), (502, "failed"))
        self.assertEqual(result.error["step"], "home_after")
        self.assertEqual([item["step"] for item in result.cleanup_errors], ["home_after", "stop_after", "lock_after"])
        self.assertEqual(result.screenshot_bytes(), PNG)
        saved = Path(self.directory.name) / "failed.png"
        result.save_screenshot(saved)
        self.assertEqual(saved.read_bytes(), PNG)
        queried = self.client.get_request("cleanup-failed")
        self.assertEqual((queried.http_status, queried.status), (200, "failed"))
        self.assertEqual(queried.error, result.error)

    def test_confirmed_adb_timeout_is_a_remote_failure_not_transport_error(self):
        self.adb.screenshot_error = controller.APIError(504, "ADB_TIMEOUT", "模拟命令超时")
        result = self.client.screenshot("adb-timeout")
        self.assertEqual((result.http_status, result.status), (504, "failed"))
        self.assertEqual((result.error["code"], result.error["step"]), ("ADB_TIMEOUT", "screenshot"))
        self.assertIsNone(result.save_screenshot(Path(self.directory.name) / "absent.png"))

    def test_duplicate_inflight_busy_conflict_and_terminal_replay(self):
        self.adb.release_capture.clear()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.client.screenshot, "inflight")
            try:
                self.assertTrue(self.adb.capture_started.wait(2))
                duplicate = self.client.screenshot("inflight")
                self.assertEqual((duplicate.http_status, duplicate.status), (202, "running"))
                self.assertFalse(duplicate.terminal)
                queried = self.client.get_request("inflight")
                self.assertEqual((queried.http_status, queried.status), (200, "running"))
                busy = self.client.test_device("another-id")
                self.assertEqual((busy.http_status, busy.error["code"]), (409, "DEVICE_BUSY"))
                conflict = self.client.test_device("inflight")
                self.assertEqual((conflict.http_status, conflict.error["code"]), (409, "REQUEST_CONFLICT"))
            finally:
                self.adb.release_capture.set()
            self.assertEqual(future.result(2).status, "succeeded")
        self.assertEqual(self.client.screenshot("inflight").status, "succeeded")
        self.assertEqual(self.adb.captures, 1)

    def test_not_found_expired_and_unknown_never_start_an_operation(self):
        missing = self.client.get_request("not-found")
        self.assertEqual((missing.http_status, missing.error["code"]), (404, "REQUEST_NOT_FOUND"))
        self.assertEqual(self.adb.captures, 0)
        self.client.screenshot("expired")
        with self.service.store.connect() as db:
            db.execute("UPDATE requests SET expires_at=0 WHERE request_id='expired'")
        expired = self.client.get_request("expired")
        self.assertEqual((expired.http_status, expired.status), (410, "succeeded"))
        self.assertTrue(expired.expired)
        self.assertTrue(expired.terminal)
        self.assertIsNone(expired.screenshot_bytes())
        self.assertEqual(self.client.screenshot("expired").http_status, 410)
        self.service.store.accept("interrupted", "screenshot", {"serial": SERIAL})
        self.service.store.recover()
        unknown = self.client.get_request("interrupted")
        self.assertEqual((unknown.http_status, unknown.status), (200, "unknown"))
        self.assertTrue(unknown.terminal)
        self.assertEqual(unknown.error["code"], "RESULT_UNKNOWN")
        self.assertEqual(self.adb.captures, 1)

    def test_http_timeout_keeps_id_and_can_be_resolved_with_get(self):
        client = ControllerClient(self.url, KEY, read_timeout=.05)
        self.adb.release_capture.clear()
        try:
            with self.assertRaises(ControllerTransportError) as raised:
                client.screenshot("timed-out")
            self.assertEqual(raised.exception.request_id, "timed-out")
            self.assertTrue(raised.exception.outcome_uncertain)
            self.assertTrue(self.adb.capture_started.wait(2))
        finally:
            self.adb.release_capture.set()
        self.service.thread.join(2)
        self.assertEqual(client.get_request("timed-out").status, "succeeded")
        self.assertEqual(self.adb.captures, 1)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.status = 200
        self.media_type = "application/json"
        self.headers = {}
        self.raw = b'{"request_id":"probe","status":"succeeded"}'
        self.calls = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.respond()

            def do_POST(self):
                self.respond()

            def respond(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                fixture.calls.append((self.command, self.path, dict(self.headers), body))
                self.send_response(fixture.status)
                self.send_header("Content-Type", fixture.media_type)
                for key, value in fixture.headers.items():
                    self.send_header(key, str(value))
                self.end_headers()
                self.wfile.write(fixture.raw)

            def log_message(self, *args):
                pass

        self.server_context = running_server(ThreadingHTTPServer(("127.0.0.1", 0), Handler))
        self.url = self.server_context.__enter__()
        self.addCleanup(self.server_context.__exit__, None, None, None)
        self.client = ControllerClient(self.url + "/", KEY)

    def test_wire_body_omits_empty_serial_and_preserves_explicit_zero(self):
        self.client.run_app("probe", package="com.example.app", delay=0)
        method, path, headers, raw = self.calls[0]
        self.assertEqual((method, path), ("POST", "/api/v1/app/run"))
        self.assertEqual(headers["Authorization"], "Bearer " + KEY)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(json.loads(raw), {"request_id": "probe", "package": "com.example.app", "delay": 0})
        self.assertNotIn(KEY, repr(self.client))
        self.assertNotIn("base64", repr(self.client.screenshot("probe")))

    def test_redirect_is_not_followed_or_retried(self):
        self.status = 307
        self.headers["Location"] = self.url + "/elsewhere"
        with self.assertRaises(ControllerProtocolError) as raised:
            self.client.screenshot("probe")
        self.assertEqual(raised.exception.http_status, 307)
        self.assertTrue(raised.exception.outcome_uncertain)
        self.assertEqual(len(self.calls), 1)

    def test_invalid_json_envelopes_and_request_id_mismatch(self):
        for raw in (b"not-json", b"[]", b"{}", b'{"request_id":"other","status":"succeeded"}',
                    b'{"request_id":"probe","status":"completed"}',
                    b'{"request_id":"probe","status":[]}',
                    b'{"request_id":"probe","status":"failed","cleanup_errors":["bad"]}'):
            with self.subTest(raw=raw):
                self.raw = raw
                with self.assertRaises(ControllerProtocolError) as raised:
                    self.client.screenshot("probe")
                self.assertEqual(raised.exception.request_id, "probe")
                self.assertTrue(raised.exception.outcome_uncertain)
                self.assertNotIn(raw.decode(), str(raised.exception))

    def test_html_oversize_truncated_and_encoded_responses(self):
        self.media_type, self.raw = "text/html", b"<html>gateway error</html>"
        with self.assertRaises(ControllerProtocolError):
            self.client.screenshot("probe")
        self.media_type = "application/json"
        self.headers = {"Content-Length": MAX_RESPONSE_BYTES + 1}
        with self.assertRaises(ControllerProtocolError):
            self.client.screenshot("probe")
        self.headers = {"Content-Length": len(self.raw) + 100}
        with self.assertRaises(ControllerTransportError):
            self.client.screenshot("probe")
        self.headers = {"Content-Encoding": "gzip"}
        with self.assertRaises(ControllerProtocolError):
            self.client.screenshot("probe")

    def test_invalid_error_response_is_protocol_failure(self):
        self.status, self.raw = 502, b'{"error":"bad gateway"}'
        with self.assertRaises(ControllerProtocolError):
            self.client.get_request("probe")

    def test_empty_and_unusable_devices_are_not_transport_errors(self):
        for devices in ([], [{"serial": "offline-phone", "state": "offline", "usable": False}],
                        [{"serial": "unauthorized-phone", "state": "unauthorized", "usable": False,
                          "brand": None, "model": None}]):
            self.raw = json.dumps({"devices": devices}).encode()
            result = self.client.devices()
            self.assertEqual(result.http_status, 200)
            self.assertEqual(result.payload["devices"], devices)

    def test_malformed_health_and_devices_are_protocol_errors(self):
        for endpoint, payload in (("health", {"busy": "false"}), ("devices", {"devices": None}),
                                  ("devices", {"devices": [None]}),
                                  ("devices", {"devices": [{"serial": "test", "state": "device", "usable": "true"}]})):
            self.raw = json.dumps(payload).encode()
            with self.assertRaises(ControllerProtocolError):
                getattr(self.client, endpoint)()

    def test_invalid_image_keeps_result_and_never_replaces_existing_file(self):
        import base64

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "existing.png"
            target.write_bytes(PNG)
            for data in (b"not png", PNG[:-1], PNG + b"trailing", PNG[:45] + b"bad-crc" + PNG[52:]):
                self.raw = json.dumps({"request_id": "probe", "status": "succeeded", "screenshot": {
                    "content_type": "image/png", "captured_at": "2026-11-04T09:00:00+08:00",
                    "base64": base64.b64encode(data).decode("ascii"),
                }}).encode()
                result = self.client.screenshot("probe")
                with self.assertRaises(ControllerImageError) as raised:
                    result.save_screenshot(target)
                self.assertIs(raised.exception.response, result)
                self.assertEqual(raised.exception.response.status, "succeeded")
                self.assertEqual(target.read_bytes(), PNG)
                self.assertEqual(list(target.parent.iterdir()), [target])

    def test_invalid_base64_content_type_and_capture_time(self):
        import base64

        screenshot = {"content_type": "image/png", "captured_at": "2026-11-04T09:00:00+08:00",
                      "base64": base64.b64encode(PNG).decode()}
        for changes in ({"base64": "bad!"}, {"content_type": "image/jpeg"}, {"captured_at": "no-time"},
                        {"captured_at": "2026-11-04T09:00:00"}, {"base64": None}):
            self.raw = json.dumps({"request_id": "probe", "status": "failed", "screenshot": {**screenshot, **changes}}).encode()
            with self.assertRaises(ControllerImageError):
                self.client.screenshot("probe").screenshot_bytes()

    def test_connection_failure_and_separate_timeout_budgets(self):
        # Capture the real connection API's timeout settings without waiting minutes.
        import http.client

        connection = unittest.mock.MagicMock()
        response = connection.getresponse.return_value
        response.status = 200
        response.read.return_value = self.raw
        response.getheader.side_effect = lambda key, default=None: {"Content-Type": "application/json"}.get(key, default)
        client = ControllerClient(self.url, KEY, connect_timeout=2, read_timeout=360, query_timeout=9)
        with patch.object(http.client, "HTTPConnection", return_value=connection) as factory:
            client.run_app("probe", package="com.example.app", delay=120)
            self.assertEqual(factory.call_args.kwargs["timeout"], 2)
            connection.sock.settimeout.assert_called_with(480)
            client.get_request("probe")
            connection.sock.settimeout.assert_called_with(9)
            connection.connect.side_effect = ConnectionRefusedError("sensitive low-level error")
            with self.assertRaises(ControllerTransportError) as raised:
                client.get_request("probe")
            self.assertFalse(raised.exception.outcome_uncertain)
            self.assertNotIn("sensitive", str(raised.exception))

    def test_bad_configuration_and_arguments_fail_before_network(self):
        for url in ("", "file:///tmp/controller", self.url + "/api/v1", "http://user:pass@localhost", self.url + "?key=value",
                    self.url + "#section", "http://127.0.0.1:0", self.url + "\r\nX-Test: bad"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ControllerClient(url, KEY)
        for key in ("", "short", "x" * 32 + "\r\n", "密钥" * 32):
            with self.assertRaises(ValueError):
                ControllerClient(self.url, key)
        for timeout in (0, -1, float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                ControllerClient(self.url, KEY, read_timeout=timeout)
        for request_id in (None, "", "x" * 65, "../secret", "含中文"):
            with self.assertRaises(ValueError):
                self.client.get_request(request_id)
        for delay in (-1, True, 1.5, None):
            with self.assertRaises(ValueError):
                self.client.run_app("probe", package="com.example.app", delay=delay)
        for package in (None, "com.example;bad", "single"):
            with self.assertRaises(ValueError):
                self.client.run_app("probe", package=package, delay=0)
        for serial in (None, "bad serial", "-leading-dash"):
            with self.assertRaises(ValueError):
                self.client.screenshot("probe", serial=serial)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main()
