import re

import pytest

from app.remote_idempotency import fingerprint_remote_request
from app.remote_requests import RemoteRequest


def request(**overrides):
    value = {
        "provider": "zerotier",
        "operation": "enable",
        "parameters": {},
        "expected_revision": 3,
        "confirmed_connection_loss": True,
    }
    value.update(overrides)
    return RemoteRequest.from_dict(value)


def test_same_request_has_same_fingerprint():
    remote_request = request()
    assert fingerprint_remote_request(remote_request) == fingerprint_remote_request(
        remote_request
    )


def test_reversed_mapping_order_has_same_fingerprint():
    original = request(parameters={"network_id": "0123456789abcdef"})
    original_fields = original.to_dict()
    reversed_fields = dict(reversed(list(original_fields.items())))
    reversed_fields["parameters"] = dict(
        reversed(list(reversed_fields["parameters"].items()))
    )
    equivalent = RemoteRequest.from_dict(reversed_fields)

    assert fingerprint_remote_request(original) == fingerprint_remote_request(equivalent)


@pytest.mark.parametrize(
    "overrides",
    [
        {"provider": "tailscale"},
        {"operation": "connect"},
        {"parameters": {"network_id": "0123456789abcdef"}},
        {"expected_revision": 4},
        {"confirmed_connection_loss": False},
    ],
)
def test_business_field_changes_alter_fingerprint(overrides):
    assert fingerprint_remote_request(request()) != fingerprint_remote_request(
        request(**overrides)
    )


def test_fingerprint_is_lowercase_64_character_hex():
    assert re.fullmatch(r"[0-9a-f]{64}", fingerprint_remote_request(request()))


def test_fingerprint_does_not_contain_raw_parameter_text():
    raw_parameter = "https://portal.company.com"
    remote_request = request(
        provider="peanut",
        operation="save_address",
        parameters={"https_url": raw_parameter},
    )

    assert raw_parameter not in fingerprint_remote_request(remote_request)


def test_fingerprinting_performs_no_file_or_network_io(monkeypatch):
    remote_request = request()

    def forbidden(*args, **kwargs):
        pytest.fail("fingerprinting must not perform file or network I/O")

    import builtins
    import socket
    import urllib.request

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)

    assert fingerprint_remote_request(remote_request)


@pytest.mark.parametrize("value", [None, {}, "not a request"])
def test_rejects_non_remote_request(value):
    with pytest.raises(TypeError, match="RemoteRequest"):
        fingerprint_remote_request(value)
