"""Pure role/ingress policy for the remote-management permission matrix.

This model neither authenticates users nor classifies ingress. Ingress is a
strict enum that a future trusted server integration must classify; it must
never be derived from browser-supplied headers. This policy is not endpoint
security by itself. It only decides role/ingress eligibility and does not
implement CSRF, confirmation, reauthentication, revision, or authorization
checks. Write decisions expose those separate requirements for enforcement by
a future endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from .remote_jobs import Operation
from .remote_status import Provider


class Role(Enum):
    ANONYMOUS = "anonymous"
    USER = "user"
    ADMIN = "admin"


class Ingress(Enum):
    LAN = "lan"
    PRIVATE = "private"
    PUBLIC = "public"


class Capability(Enum):
    SUMMARY = "summary"
    DETAILS = "details"
    THIRD_PARTY_DETAILS = "third_party_details"
    REAUTH = "reauth"
    INSTALL = "install"
    START = "start"
    CONNECT = "connect"
    ENABLE = "enable"
    REQUEST_AUTHORIZATION = "request_authorization"
    DISCONNECT = "disconnect"
    DISABLE = "disable"
    LEAVE = "leave"
    SWITCH = "switch"
    SAVE_ADDRESS = "save_address"
    PUBLIC_CERTIFICATE = "public_certificate"


OPERATION_CAPABILITIES: Mapping[Operation, Capability] = MappingProxyType(
    {
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
    }
)


class WriteRequirement(Enum):
    CSRF = "csrf"
    CONFIRMATION = "confirmation"
    ACTUAL_REAUTHENTICATION = "actual_reauthentication"
    REVISION = "revision"
    AUTHORIZATION = "authorization"


_ADMIN_LV = frozenset({(Role.ADMIN, Ingress.LAN), (Role.ADMIN, Ingress.PRIVATE)})
_SUMMARY_ROLES = frozenset({Role.USER, Role.ADMIN})
_WRITE_CAPABILITIES = frozenset(
    {
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
)
_WRITE_REQUIREMENTS = frozenset(WriteRequirement)


@dataclass(frozen=True)
class PolicyDecision:
    """Eligibility result plus separate requirements for any write operation."""

    allowed: bool
    write_requirements: frozenset[WriteRequirement]


def capability_for_operation(operation: Operation) -> Capability:
    """Return the policy capability bound to a fixed remote job operation."""
    if type(operation) is not Operation:
        raise TypeError("operation must be Operation")
    return OPERATION_CAPABILITIES[operation]


def evaluate_policy(
    role: Role, ingress: Ingress, capability: Capability, *, provider: Provider | None = None
) -> PolicyDecision:
    """Evaluate only the remote role/ingress matrix; reject non-enum inputs."""
    is_write = type(capability) is Capability and capability in _WRITE_CAPABILITIES
    requirements = _WRITE_REQUIREMENTS if is_write else frozenset()

    if (
        type(role) is not Role
        or type(ingress) is not Ingress
        or type(capability) is not Capability
    ):
        return PolicyDecision(False, requirements)

    if capability is Capability.SUMMARY:
        allowed = role in _SUMMARY_ROLES
    elif capability in {
        Capability.DETAILS,
        Capability.THIRD_PARTY_DETAILS,
        Capability.REAUTH,
        Capability.PUBLIC_CERTIFICATE,
        *_WRITE_CAPABILITIES,
    }:
        allowed = (role, ingress) in _ADMIN_LV or (
            provider is Provider.PEANUT and role is Role.ADMIN and ingress is Ingress.PUBLIC
        )
    else:
        allowed = False

    return PolicyDecision(allowed, requirements)
