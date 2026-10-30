import json
from dataclasses import FrozenInstanceError

import pytest

from app.remote_jobs import Operation
from app.remote_requests import RemoteRequest
from app.remote_status import Provider


PROVIDERS = ["tailscale", "zerotier", "peanut"]
EMPTY_OPERATIONS = [
    "install", "start", "request_authorization", "disconnect", "disable",
    "enable", "connect",
]


def payload(**overrides):
    result = {
        "provider": "zerotier",
        "operation": "enable",
        "parameters": {},
        "expected_revision": 3,
        "confirmed_connection_loss": True,
    }
    result.update(overrides)
    return result


@pytest.mark.parametrize("operation", [
    "set_autostart", "reset_local_config", "reset_account_config", "recover_reset",
])
def test_reset_and_autostart_require_strict_peanut_endpoint(operation):
    with pytest.raises(ValueError, match="strict Peanut action endpoint"):
        RemoteRequest.from_dict(payload(provider="peanut", operation=operation))


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("operation", EMPTY_OPERATIONS)
def test_empty_parameter_pairs_round_trip(provider, operation):
    data = payload(provider=provider, operation=operation)
    request = RemoteRequest.from_dict(data)
    assert request.provider is Provider(provider)
    assert request.operation is Operation(operation)
    assert request.to_dict() == data
    assert RemoteRequest.from_dict(json.loads(json.dumps(request.to_dict()))) == request


@pytest.mark.parametrize("operation", ["enable", "connect"])
@pytest.mark.parametrize("network_id", ["0123456789abcdef", "ABCDEF0123456789"])
def test_zerotier_network_id(operation, network_id):
    data = payload(operation=operation, parameters={"network_id": network_id})
    assert RemoteRequest.from_dict(data).to_dict() == data


def test_zerotier_leave():
    data = payload(operation="leave")
    assert RemoteRequest.from_dict(data).to_dict() == data


@pytest.mark.parametrize("url", [
    "https://portal.company.com", "https://portal.company.com/",
    "https://portal.company.com:8443/", "https://8.8.8.8/",
    "https://[2606:4700:4700::1111]/", "https://例子.公司/",
])
def test_peanut_save_address_is_pure(monkeypatch, url):
    import socket
    import urllib.request

    def forbidden(*args, **kwargs):
        pytest.fail("request schema must not perform network I/O")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    data = payload(provider="peanut", operation="save_address", parameters={"https_url": url})
    assert RemoteRequest.from_dict(data).to_dict() == data


@pytest.mark.parametrize("provider", PROVIDERS)
def test_switch_is_explicitly_unresolved(provider):
    with pytest.raises(ValueError, match="switch.*unresolved"):
        RemoteRequest.from_dict(payload(
            provider=provider, operation="switch",
            parameters={"target_provider": "zerotier", "network_id": "0123456789abcdef"},
        ))


@pytest.mark.parametrize("provider", ["tailscale", "peanut"])
def test_leave_only_zerotier(provider):
    with pytest.raises(ValueError, match="leave"):
        RemoteRequest.from_dict(payload(provider=provider, operation="leave"))


@pytest.mark.parametrize("provider", ["tailscale", "zerotier"])
def test_save_address_only_peanut(provider):
    with pytest.raises(ValueError, match="save_address"):
        RemoteRequest.from_dict(payload(
            provider=provider, operation="save_address",
            parameters={"https_url": "https://portal.company.com"},
        ))


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("operation", EMPTY_OPERATIONS + ["leave", "save_address"])
def test_unknown_parameters_rejected_for_every_pair(provider, operation):
    with pytest.raises((TypeError, ValueError)):
        RemoteRequest.from_dict(payload(
            provider=provider, operation=operation, parameters={"arbitrary": "value"},
        ))


@pytest.mark.parametrize("provider", ["tailscale", "peanut"])
@pytest.mark.parametrize("operation", ["enable", "connect"])
def test_other_providers_cannot_supply_network_id(provider, operation):
    with pytest.raises(ValueError):
        RemoteRequest.from_dict(payload(
            provider=provider, operation=operation,
            parameters={"network_id": "0123456789abcdef"},
        ))


@pytest.mark.parametrize("operation", ["enable", "connect"])
@pytest.mark.parametrize("value", [
    None, True, 1234567890123456, 1.5, [], {}, "",
    "0123456789abcde", "0123456789abcdef0", "0123456789abcdeg",
    " 0123456789abcdef", "0123456789abcdef\n",
])
def test_invalid_network_ids(operation, value):
    with pytest.raises((TypeError, ValueError)):
        RemoteRequest.from_dict(payload(operation=operation, parameters={"network_id": value}))


@pytest.mark.parametrize("key", [
    "reauth_grant", "password", "idempotency_key", "command", "target_provider", 1,
])
def test_unknown_top_level_fields_including_authorization_transport(key):
    data = payload()
    data[key] = "not-accepted"
    with pytest.raises(ValueError, match="schema mismatch"):
        RemoteRequest.from_dict(data)


@pytest.mark.parametrize("key", list(payload()))
def test_all_fields_are_required(key):
    data = payload()
    del data[key]
    with pytest.raises(ValueError, match="schema mismatch"):
        RemoteRequest.from_dict(data)


@pytest.mark.parametrize("value", [None, True, 1, "object", [], ()])
def test_request_must_be_object(value):
    with pytest.raises(TypeError):
        RemoteRequest.from_dict(value)


@pytest.mark.parametrize("field,values", [
    ("provider", [None, True, 1, [], {}, "none", "other", "ZEROTIER"]),
    ("operation", [None, True, 1, [], {}, "execute", "reset", "uninstall", "ENABLE"]),
    ("parameters", [None, True, 1, "", [], [("network_id", "0123456789abcdef")]]),
    ("expected_revision", [None, True, False, 0, -1, 1.0, "3", [], {}]),
    ("confirmed_connection_loss", [None, 0, 1, "true", [], {}]),
])
def test_wrong_scalars_and_revisions(field, values):
    for value in values:
        with pytest.raises((TypeError, ValueError)):
            RemoteRequest.from_dict(payload(**{field: value}))


@pytest.mark.parametrize("revision", [1, 3, 2**64])
def test_positive_integer_revisions(revision):
    assert RemoteRequest.from_dict(payload(expected_revision=revision)).expected_revision == revision


@pytest.mark.parametrize("operation", ["disconnect", "disable", "leave"])
def test_loss_requires_explicit_true(operation):
    with pytest.raises(ValueError, match="confirmation"):
        RemoteRequest.from_dict(payload(operation=operation, confirmed_connection_loss=False))


@pytest.mark.parametrize("operation", ["install", "start", "request_authorization", "enable", "connect"])
def test_non_loss_operations_accept_explicit_false(operation):
    assert not RemoteRequest.from_dict(payload(
        operation=operation, confirmed_connection_loss=False,
    )).confirmed_connection_loss


@pytest.mark.parametrize("url", [
    None, True, 123, [], {}, "", " https://portal.company.com",
    "https://portal.company.com ", "http://portal.company.com",
    "javascript:alert(1)", "data:text/plain,hello",
    "https://user@portal.company.com", "https://user:pass@portal.company.com",
    "https://portal.company.com/path", "https://portal.company.com?",
    "https://portal.company.com?x=1", "https://portal.company.com#",
    "https://portal.company.com#fragment", "https://portal.company.com\\evil",
    "https://portal.company.com/\n", "https://portal.company.com:\t443",
    "https://portal.company.com:0", "https://portal.company.com:65536",
    "https://portal.company.com:", "https://portal.company.com:abc",
    "https://localhost", "https://host.local", "https://host.internal",
    "https://host.test", "https://host.example", "https://host.invalid",
    "https://host.onion", "https://127.0.0.1", "https://192.168.1.1",
    "https://0.0.0.0", "https://169.254.169.254", "https://[::1]",
    "https://[fc00::1]", "https://192.0.2.1", "https://2130706433",
    "https://0x7f.0.0.1", "https://portal.company.com.",
    "https://portal.company.com,other.com", "https://" + "a" * 2048 + ".com",
])
def test_invalid_addresses(url):
    with pytest.raises((TypeError, ValueError)):
        RemoteRequest.from_dict(payload(
            provider="peanut", operation="save_address", parameters={"https_url": url},
        ))


def test_save_address_requires_url_and_no_extra_fields():
    for parameters in [{}, {"https_url": "https://portal.company.com", "verified": True}]:
        with pytest.raises(ValueError):
            RemoteRequest.from_dict(payload(
                provider="peanut", operation="save_address", parameters=parameters,
            ))


def test_direct_construction_enforces_types_and_switch_rejection():
    for changes in [
        {"provider": "zerotier"}, {"operation": "enable"}, {"expected_revision": True},
        {"confirmed_connection_loss": 1}, {"parameters": {"network_id": None}},
        {"operation": Operation.SWITCH},
    ]:
        arguments = payload(provider=Provider.ZEROTIER, operation=Operation.ENABLE)
        arguments.update(changes)
        with pytest.raises((TypeError, ValueError)):
            RemoteRequest(**arguments)


def test_parameters_are_defensively_copied_and_serialization_is_detached():
    data = payload(parameters={"network_id": "0123456789abcdef"})
    request = RemoteRequest.from_dict(data)
    data["parameters"]["network_id"] = "invalid"
    serialized = request.to_dict()
    serialized["parameters"]["network_id"] = "invalid"
    assert request.parameters["network_id"] == "0123456789abcdef"
    with pytest.raises(TypeError):
        request.parameters["network_id"] = "invalid"
    with pytest.raises(FrozenInstanceError):
        request.expected_revision = 5
    assert set(request.to_dict()) == set(payload())
