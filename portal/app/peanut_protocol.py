"""Documented official interfaces and DNS-pinned HTTPS. No SDK is run here."""

from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
import time
from urllib.parse import urlsplit

from .config import ConfigError, _validate_remote_https_url, _validate_remote_ssh_host
from .peanut_credentials import PeanutError, strict_object

OFFICIAL_API = "https://hsk-embed.oray.com"
MANAGEMENT_HOSTS = frozenset({"b.oray.com", "console.hsk.oray.com"})


def public_addresses(host: str, port: int) -> list[str]:
    try:
        addresses = list(dict.fromkeys(
            item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        ))
    except OSError as exc:
        raise PeanutError("dns_failed") from exc
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise PeanutError("unsafe_address", code="E5001")
    return addresses


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self) -> None:
        raw = socket.create_connection((self.address, self.port), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def https_request(
    url: str, *, body: dict | None = None, limit: int = 65536, timeout: float = 15
) -> tuple[int, str, bytes]:
    try:
        parsed = urlsplit(url)
        port = parsed.port or 443
    except ValueError as exc:
        raise PeanutError("unsafe_address", code="E5001") from exc
    if (
        parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
        or parsed.password is not None or parsed.fragment or "\\" in url
        or any(ord(c) < 33 or ord(c) == 127 for c in url)
    ):
        raise PeanutError("unsafe_address", code="E5001")
    addresses = public_addresses(parsed.hostname, port)
    connection = PinnedHTTPS(parsed.hostname, port, addresses[0], timeout)
    started = time.monotonic()
    try:
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json, image/png, image/jpeg"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        connection.request(
            "POST" if body is not None else "GET",
            (parsed.path or "/") + ("?" + parsed.query if parsed.query else ""),
            body=payload, headers=headers,
        )
        response = connection.getresponse()
        chunks = bytearray()
        while True:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError
            if connection.sock:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(16384, limit + 1 - len(chunks)))
            chunks.extend(chunk)
            if len(chunks) > limit:
                raise PeanutError("response_too_large")
            if not chunk:
                break
        return response.status, response.getheader("Content-Type", "").split(";")[0], bytes(chunks)
    except (TimeoutError, socket.timeout) as exc:
        raise PeanutError("official_timeout", code="E5003") from exc
    except (OSError, http.client.HTTPException) as exc:
        raise PeanutError("transport_failed") from exc
    finally:
        connection.close()


def json_object(raw: bytes) -> dict:
    try:
        value = json.loads(raw, object_pairs_hook=strict_object)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError) as exc:
        raise PeanutError("invalid_protocol") from exc


def management_url(url: object) -> str:
    if not isinstance(url, str) or len(url) > 4096:
        raise PeanutError("invalid_protocol")
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme == "https" and parsed.hostname in MANAGEMENT_HOSTS
            and parsed.port in (None, 443) and parsed.username is None
            and parsed.password is None and not parsed.fragment
            and "\\" not in url and not any(ord(c) < 33 or ord(c) == 127 for c in url)
        )
    except ValueError:
        valid = False
    if not valid:
        raise PeanutError("untrusted_official_host")
    return url


def normalize_ssh_address(value: dict) -> dict:
    if (
        not isinstance(value, dict) or set(value) != {"host", "port"}
        or not isinstance(value["host"], str) or type(value["port"]) is not int
        or not 1 <= value["port"] <= 65535
        or (not value["host"] and value["port"] != 22)
    ):
        raise PeanutError("invalid_ssh_mapping", code="E5001")
    try:
        if value["host"]:
            _validate_remote_ssh_host(value["host"])
        host = value["host"].encode("idna").decode("ascii").lower()
    except (ConfigError, UnicodeError) as exc:
        raise PeanutError("invalid_ssh_mapping", code="E5001") from exc
    return {"host": host, "port": value["port"]}


def normalize_address(url: str) -> str:
    try:
        _validate_remote_https_url(url)
        if not url:
            raise ConfigError("empty")
        parsed = urlsplit(url)
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower()
        public_addresses(host, parsed.port or 443)
        netloc = f"[{host}]" if ":" in host else host
        if parsed.port and parsed.port != 443:
            netloc += f":{parsed.port}"
        return f"https://{netloc}"
    except (ConfigError, ValueError) as exc:
        raise PeanutError("unsafe_address", code="E5001") from exc


class OfficialAdapter:
    def __init__(self, *, transport=https_request, rpc_transport=None) -> None:
        self.transport = transport
        self.rpc_transport = rpc_transport or self._rpc

    @staticmethod
    def _rpc(method: str) -> dict:
        if method not in {"getsn", "getmgrurl"}:
            raise PeanutError("unsupported_operation")
        connection = http.client.HTTPConnection("127.0.0.1", 16062, timeout=3)
        try:
            connection.request("GET", "/ora_service/" + method)
            response = connection.getresponse()
            raw = response.read(65537)
            if response.status != 200 or len(raw) > 65536:
                raise PeanutError("rpc_failed")
            return json_object(raw)
        except (OSError, http.client.HTTPException) as exc:
            raise PeanutError("rpc_unavailable") from exc
        finally:
            connection.close()

    def rpc(self, method: str) -> dict:
        value = self.rpc_transport(method)
        if type(value.get("result_code")) is not int or value["result_code"] != 0:
            raise PeanutError("rpc_result_failed")
        data = value.get("data")
        if not isinstance(data, dict):
            raise PeanutError("invalid_protocol")
        if method == "getsn":
            if (
                type(data.get("status")) is not int
                or not all(isinstance(data.get(k), str) and len(data[k]) <= 256
                           for k in ("device_sn", "device_sn_pwd", "public_ip"))
            ):
                raise PeanutError("invalid_protocol")
        else:
            if not all(isinstance(data.get(k), str) for k in ("url", "account")):
                raise PeanutError("invalid_protocol")
            if (len(data["account"]) > 256 or data["account"] != data["account"].strip()
                    or any(ord(c) < 32 or ord(c) == 127 for c in data["account"])):
                raise PeanutError("invalid_protocol")
            if data["url"]:
                management_url(data["url"])
        return data

    def qr(self, sn: str, password: str, *, expires_mode: str, image_hosts: list[str]) -> tuple[float, str, bytes]:
        status, _, raw = self.transport(
            OFFICIAL_API + "/devices/qrcode", body={"sn": sn, "password": password}
        )
        if status != 200:
            raise PeanutError("official_rejected")
        value = json_object(raw)
        expires = value.get("expires")
        if (
            type(expires) not in (int, float) or expires <= 0
            or not isinstance(value.get("qrcode"), str) or not value["qrcode"]
            or not isinstance(value.get("qrcodeimg"), str)
        ):
            raise PeanutError("invalid_protocol")
        now = time.time()
        if expires_mode == "relative_seconds":
            deadline = now + expires
        elif expires_mode == "unix_seconds":
            deadline = float(expires)
        else:
            raise PeanutError("expires_unverified", code="E5004")
        if not now < deadline <= now + 86400:
            raise PeanutError("invalid_expiry")
        image_url = value["qrcodeimg"]
        try:
            parsed = urlsplit(image_url)
            trusted_image = parsed.hostname in image_hosts and parsed.port in (None, 443)
        except ValueError:
            trusted_image = False
        if not trusted_image:
            raise PeanutError("untrusted_image_host")
        status, mime, image = self.transport(image_url, limit=1024 * 1024)
        if status != 200 or mime not in {"image/png", "image/jpeg"}:
            raise PeanutError("invalid_image")
        if not ((mime == "image/png" and image.startswith(b"\x89PNG\r\n\x1a\n"))
                or (mime == "image/jpeg" and image.startswith(b"\xff\xd8\xff"))):
            raise PeanutError("invalid_image")
        return deadline, mime, image

    def unbind(self, sn: str, password: str) -> None:
        status, _, _ = self.transport(
            OFFICIAL_API + "/devices/unbinding", body={"sn": sn, "password": password}
        )
        if status != 200:
            raise PeanutError("official_rejected")
