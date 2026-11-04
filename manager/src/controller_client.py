"""Synchronous API v1 client; call from workers, never from a page handler.

Every valid API response is returned, including non-2xx results. Only transport
and protocol failures raise exceptions. This keeps failed-operation screenshots,
cleanup errors and expired results available to the task/recovery layer.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from datetime import datetime
import http.client
import json
import math
from pathlib import Path
import re
import struct
import tempfile
from urllib.parse import urlsplit
import zlib


REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
SERIAL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:\[\]-]{0,255}\Z")
PACKAGE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\Z")
STATUSES = {"accepted", "running", "succeeded", "failed", "unknown"}
TERMINAL = {"succeeded", "failed", "unknown"}
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024


class ControllerError(RuntimeError):
    """A local failure, not a controller-reported business error."""

    def __init__(self, message, *, request_id=None, http_status=None, outcome_uncertain=False):
        super().__init__(message)
        self.request_id = request_id
        self.http_status = http_status
        # A broken POST response never proves the phone did not execute.
        self.outcome_uncertain = outcome_uncertain


class ControllerTransportError(ControllerError):
    pass


class ControllerProtocolError(ControllerError):
    pass


class ControllerImageError(ControllerError):
    def __init__(self, message, response):
        super().__init__(message, request_id=response.request_id, http_status=response.http_status)
        self.response = response


def _valid_png(data):
    """Validate chunk boundaries and CRCs, without decompressing untrusted pixels."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    offset, header, pixels = 8, False, False
    while offset + 12 <= len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        end = offset + 12 + length
        if end > len(data):
            return False
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + length]
        crc = struct.unpack_from(">I", data, offset + 8 + length)[0]
        if zlib.crc32(kind + payload) & 0xffffffff != crc:
            return False
        if not header:
            if kind != b"IHDR" or length != 13 or not all(struct.unpack(">II", payload[:8])):
                return False
            header = True
        elif kind == b"IHDR":
            return False
        if kind == b"IDAT":
            pixels = True
        if kind == b"IEND":
            return length == 0 and pixels and end == len(data)
        offset = end
    return False


@dataclass(frozen=True)
class ControllerResponse:
    http_status: int
    payload: dict = field(repr=False)

    @property
    def request_id(self):
        return self.payload.get("request_id")

    @property
    def status(self):
        return self.payload.get("status")

    @property
    def terminal(self):
        return self.status in TERMINAL

    @property
    def expired(self):
        return self.http_status == 410

    @property
    def error(self):
        return self.payload.get("error")

    @property
    def cleanup_errors(self):
        return self.payload.get("cleanup_errors", [])

    def screenshot_bytes(self):
        """Image errors retain this response so remote execution status is not lost."""
        screenshot = self.payload.get("screenshot")
        if screenshot is None:
            return None
        try:
            if not isinstance(screenshot, dict) or screenshot.get("content_type") != "image/png":
                raise ValueError
            captured = datetime.fromisoformat(screenshot["captured_at"])
            if captured.utcoffset() is None:
                raise ValueError
            encoded = screenshot["base64"]
            if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
                raise ValueError
            data = base64.b64decode(encoded, validate=True)
            if len(data) > MAX_IMAGE_BYTES or not _valid_png(data):
                raise ValueError
        except (KeyError, TypeError, ValueError, binascii.Error, OverflowError):
            raise ControllerImageError("控制端截图不是有效的 Base64 PNG 或缺少带时区的采集时间", self) from None
        return data

    def save_screenshot(self, destination):
        """Atomically save to a manager-selected path; never use a remote filename."""
        data = self.screenshot_bytes()
        if data is None:
            return None
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix="." + path.name + ".", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(data)
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return path


def _valid_error(value):
    return (isinstance(value, dict) and isinstance(value.get("code"), str) and bool(value["code"])
            and isinstance(value.get("message"), str)
            and (value.get("step") is None or isinstance(value["step"], str)))


class ControllerClient:
    """One connection per call: safe to share across independent worker threads.

    No redirects, environment proxies or automatic retries. Request IDs and any
    retry/poll decisions belong to the durable task layer. run_app requires an
    explicit delay; empty package/serial follow the controller's defaults.
    read_timeout is a socket read timeout excluding delay; query_timeout is for GET.
    Defaults cover the controller's default 20-second ADB command timeout. Increase
    read_timeout when deploying a controller with a larger command_timeout.
    """

    def __init__(self, controller_url, api_key, *, connect_timeout=5, read_timeout=360, query_timeout=15):
        if (not isinstance(controller_url, str) or not controller_url
                or any(ord(char) <= 32 for char in controller_url) or "\\" in controller_url):
            raise ValueError("控制端地址应为 http(s)://主机:端口")
        try:
            address = urlsplit(controller_url)
            port = address.port
            if (address.scheme not in {"http", "https"} or not address.hostname
                    or address.username is not None or address.password is not None
                    or address.path not in {"", "/"} or address.query or address.fragment
                    or port is not None and not 1 <= port <= 65535):
                raise ValueError
        except ValueError:
            raise ValueError("控制端地址须为 HTTP(S) 服务根地址，不含账号、路径、查询或片段") from None
        if (not isinstance(api_key, str) or not 32 <= len(api_key) <= 512
                or any(not 33 <= ord(char) <= 126 for char in api_key)):
            raise ValueError("控制端密钥须为 32～512 位可打印 ASCII 字符")
        for value in (connect_timeout, read_timeout, query_timeout):
            if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
                raise ValueError("HTTP 超时须为有限的正数秒数")
        self.controller_url = f"{address.scheme}://{address.netloc}"
        self._scheme, self._host, self._port = address.scheme, address.hostname, port
        self._api_key = api_key
        self.connect_timeout, self.read_timeout, self.query_timeout = connect_timeout, read_timeout, query_timeout

    def health(self):
        return self._request("GET", "health")

    def devices(self):
        return self._request("GET", "devices")

    def get_request(self, request_id):
        self._validate_request_id(request_id)
        return self._request("GET", "requests/" + request_id, request_id=request_id)

    def run_app(self, request_id, *, delay, package="", serial=""):
        if not isinstance(package, str) or len(package) > 255 or package and not PACKAGE.fullmatch(package):
            raise ValueError("请填写有效的应用包名")
        if type(delay) is not int or delay < 0:
            raise ValueError("delay 须为非负整数秒数")
        params = {"delay": delay}
        if package:
            params["package"] = package
        return self._operate("app/run", request_id, serial, **params)

    def screenshot(self, request_id, *, serial=""):
        return self._operate("screenshot", request_id, serial)

    def test_device(self, request_id, *, serial=""):
        return self._operate("device/test", request_id, serial)

    @staticmethod
    def _validate_request_id(request_id):
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise ValueError("request_id 须为 1～64 位字母、数字、短横线或下划线")

    def _operate(self, operation, request_id, serial, **params):
        self._validate_request_id(request_id)
        if not isinstance(serial, str) or serial and not SERIAL.fullmatch(serial):
            raise ValueError("设备序列号格式不正确")
        body = {"request_id": request_id, **params}
        if serial:
            body["serial"] = serial
        return self._request("POST", operation, request_id=request_id, body=body,
                             timeout=self.read_timeout + params.get("delay", 0))

    def _request(self, method, endpoint, *, request_id=None, body=None, timeout=None):
        context = {"request_id": request_id, "http_status": None, "outcome_uncertain": method == "POST"}
        connection_type = http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
        connection = connection_type(self._host, self._port, timeout=self.connect_timeout)
        headers = {"Authorization": "Bearer " + self._api_key, "Accept": "application/json", "Accept-Encoding": "identity"}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=True).encode("ascii")
            headers["Content-Type"] = "application/json"
        try:
            connection.connect()
            connection.sock.settimeout(timeout if timeout is not None else self.query_timeout)
            connection.request(method, "/api/v1/" + endpoint, body=data, headers=headers)
            response = connection.getresponse()
            context["http_status"] = response.status
            if 300 <= response.status < 400:
                raise ControllerProtocolError("控制端返回重定向，请直接配置正确的服务地址", **context)
            declared_length = response.getheader("Content-Length")
            if declared_length is not None:
                if not declared_length.isascii() or not declared_length.isdecimal():
                    raise ControllerProtocolError("控制端响应长度无效", **context)
                if len(declared_length.lstrip("0")) > len(str(MAX_RESPONSE_BYTES)):
                    raise ControllerProtocolError("控制端响应超过大小限制", **context)
                declared_length = int(declared_length.lstrip("0") or "0")
                if declared_length > MAX_RESPONSE_BYTES:
                    raise ControllerProtocolError("控制端响应超过大小限制", **context)
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ControllerProtocolError("控制端响应超过大小限制", **context)
            if declared_length is not None and len(raw) != declared_length:
                raise ControllerTransportError("控制端响应中断；操作请求须用原编号查询确认", **context)
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise ControllerProtocolError("控制端返回了不支持的响应编码", **context)
            content_type = response.getheader("Content-Type", "").partition(";")[0].strip().lower()
            if content_type != "application/json":
                raise ControllerProtocolError("控制端未返回 JSON 响应", **context)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeError, RecursionError):
                raise ControllerProtocolError("控制端 JSON 响应无法解析", **context) from None
            self._validate_response(payload, response.status, endpoint, request_id, context)
            return ControllerResponse(response.status, payload)
        except (OSError, http.client.HTTPException):
            raise ControllerTransportError("无法连接控制端、请求超时或响应中断；操作请求须用原编号查询确认", **context) from None
        finally:
            connection.close()

    @staticmethod
    def _validate_response(payload, http_status, endpoint, request_id, context):
        def invalid():
            raise ControllerProtocolError("控制端响应结构或请求编号与 API 契约不符", **context)

        if not isinstance(payload, dict):
            invalid()
        if payload.get("error") is not None and not _valid_error(payload["error"]):
            invalid()
        if (not isinstance(payload.get("cleanup_errors", []), list)
                or any(not _valid_error(item) for item in payload.get("cleanup_errors", []))):
            invalid()
        status = payload.get("status")
        if status is not None and (not isinstance(status, str) or status not in STATUSES):
            invalid()
        if request_id is not None:
            if payload.get("request_id") is not None and payload["request_id"] != request_id:
                invalid()
            if (200 <= http_status < 300 or http_status == 410) and (payload.get("request_id") != request_id or status is None):
                invalid()
        if not 200 <= http_status < 300:
            if not _valid_error(payload.get("error")):
                invalid()
            return
        if http_status == 202 and status not in {"accepted", "running"}:
            invalid()
        if endpoint == "health":
            if (not isinstance(payload.get("version"), str) or type(payload.get("busy")) is not bool
                    or type(payload.get("uptime_seconds")) is not int):
                invalid()
        elif endpoint == "devices":
            if not isinstance(payload.get("devices"), list):
                invalid()
            for device in payload["devices"]:
                if (not isinstance(device, dict) or not isinstance(device.get("serial"), str)
                        or not isinstance(device.get("state"), str) or type(device.get("usable")) is not bool
                        or any(device.get(key) is not None and not isinstance(device[key], str) for key in ("brand", "model"))):
                    invalid()
