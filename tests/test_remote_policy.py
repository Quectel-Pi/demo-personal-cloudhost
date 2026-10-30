from itertools import product

import pytest

from app.remote_policy import (
    Capability,
    Ingress,
    OPERATION_CAPABILITIES,
    PolicyDecision,
    Role,
    WriteRequirement,
    capability_for_operation,
    evaluate_policy,
)
from app.remote_jobs import Operation


def expected_allowed(role: Role, ingress: Ingress, capability: Capability) -> bool:
    if capability is Capability.SUMMARY:
        return role in {Role.USER, Role.ADMIN}
    return role is Role.ADMIN and ingress in {Ingress.LAN, Ingress.PRIVATE}


@pytest.mark.parametrize(
    ("role", "ingress", "capability"),
    list(product(Role, Ingress, Capability)),
    ids=lambda value: value.value,
)
def test_complete_role_ingress_capability_matrix(role, ingress, capability):
    decision = evaluate_policy(role, ingress, capability)
    assert decision.allowed is expected_allowed(role, ingress, capability)

    expected_requirements = (
        frozenset(WriteRequirement)
        if capability
        in {
            Capability.INSTALL,
            Capability.START,
            Capability.CONNECT,
            Capability.ENABLE,
            Capability.REQUEST_AUTHORIZATION,
            Capability.DISCONNECT,
            Capability.DISABLE,
            Capability.LEAVE,
            Capability.SWITCH,
            Capability.SAVE_ADDRESS,
        }
        else frozenset()
    )
    assert decision.write_requirements == expected_requirements


@pytest.mark.parametrize(
    ("role", "ingress", "capability"),
    [
        ("admin", Ingress.LAN, Capability.SUMMARY),
        (Role.ADMIN, "lan", Capability.DETAILS),
        (Role.ADMIN, Ingress.LAN, "install"),
        (Role.ADMIN, Ingress.PUBLIC, Capability.CONNECT),
        (Role.USER, Ingress.PRIVATE, Capability.PUBLIC_CERTIFICATE),
        (object(), Ingress.PRIVATE, Capability.SUMMARY),
        (Role.ADMIN, object(), Capability.DETAILS),
        (Role.ADMIN, Ingress.LAN, object()),
        (Ingress.LAN, Role.ADMIN, Capability.START),
        (Role.ADMIN, Ingress.LAN, Role.USER),
    ],
)
def test_invalid_strings_and_direct_type_abuse_fail_closed(role, ingress, capability):
    assert evaluate_policy(role, ingress, capability) == PolicyDecision(
        False,
        (
            frozenset(WriteRequirement)
            if type(capability) is Capability
            and capability
            in {
                Capability.INSTALL,
                Capability.START,
                Capability.CONNECT,
                Capability.ENABLE,
                Capability.REQUEST_AUTHORIZATION,
                Capability.DISCONNECT,
                Capability.DISABLE,
                Capability.LEAVE,
                Capability.SWITCH,
                Capability.SAVE_ADDRESS,
            }
            else frozenset()
        ),
    )


@pytest.mark.parametrize(
    "capability",
    [
        Capability.DETAILS,
        Capability.THIRD_PARTY_DETAILS,
        Capability.REAUTH,
        Capability.INSTALL,
        Capability.START,
        Capability.CONNECT,
        Capability.ENABLE,
        Capability.DISCONNECT,
        Capability.DISABLE,
        Capability.LEAVE,
        Capability.SWITCH,
        Capability.SAVE_ADDRESS,
        Capability.PUBLIC_CERTIFICATE,
    ],
)
def test_admin_public_ingress_never_grants_protected_remote_capabilities(capability):
    assert not evaluate_policy(Role.ADMIN, Ingress.PUBLIC, capability).allowed


def test_every_remote_operation_has_an_explicit_policy_capability():
    expected = {
        Operation.CHECK_CONFIG: Capability.CONNECT,
        Operation.STOP: Capability.DISCONNECT,
        Operation.RETRY: Capability.START,
        Operation.ENABLE_AUTOSTART: Capability.ENABLE,
        Operation.DISABLE_AUTOSTART: Capability.DISABLE,
        Operation.UNBIND: Capability.LEAVE,
        Operation.VERIFY_ADDRESS: Capability.SAVE_ADDRESS,
        Operation.SET_AUTOSTART: Capability.ENABLE,
        Operation.RESET_LOCAL_CONFIG: Capability.DISABLE,
        Operation.RESET_ACCOUNT_CONFIG: Capability.LEAVE,
        Operation.RECOVER_RESET: Capability.LEAVE,
        Operation.INSTALL: Capability.INSTALL,
        Operation.START: Capability.START,
        Operation.ENABLE: Capability.ENABLE,
        Operation.CONNECT: Capability.CONNECT,
        Operation.REQUEST_AUTHORIZATION: Capability.REQUEST_AUTHORIZATION,
        Operation.DISCONNECT: Capability.DISCONNECT,
        Operation.DISABLE: Capability.DISABLE,
        Operation.LEAVE: Capability.LEAVE,
        Operation.SWITCH: Capability.SWITCH,
        Operation.SAVE_ADDRESS: Capability.SAVE_ADDRESS,
    }

    assert set(OPERATION_CAPABILITIES) == set(Operation)
    assert dict(OPERATION_CAPABILITIES) == expected
    assert {
        operation: capability_for_operation(operation) for operation in Operation
    } == expected


@pytest.mark.parametrize("operation", ["install", object(), Capability.INSTALL])
def test_operation_capability_binding_rejects_non_operations(operation):
    with pytest.raises(TypeError, match="operation must be Operation"):
        capability_for_operation(operation)


def test_request_authorization_uses_complete_write_requirements():
    assert evaluate_policy(
        Role.ADMIN, Ingress.LAN, Capability.REQUEST_AUTHORIZATION
    ) == PolicyDecision(True, frozenset(WriteRequirement))
    assert evaluate_policy(
        Role.ADMIN, Ingress.PRIVATE, Capability.REQUEST_AUTHORIZATION
    ) == PolicyDecision(True, frozenset(WriteRequirement))
    for role, ingress in product(Role, Ingress):
        if (role, ingress) not in {
            (Role.ADMIN, Ingress.LAN),
            (Role.ADMIN, Ingress.PRIVATE),
        }:
            assert evaluate_policy(
                role, ingress, Capability.REQUEST_AUTHORIZATION
            ) == PolicyDecision(False, frozenset(WriteRequirement))
