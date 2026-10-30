"""Pure remote-operation business requests (RA-014/017/018, schema only).

This model intentionally excludes authorization transport, including ephemeral
reauth grants, and is not an endpoint contract by itself. It performs no I/O,
authorization, revision comparison, persistence, or execution. Switch remains
unsupported until its target-configuration source and shape are decided.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .config import _is_zerotier_id, _validate_remote_https_url
from .remote_jobs import Operation
from .remote_status import Provider


_REQUEST_KEYS = frozenset(
    {
        "provider",
        "operation",
        "parameters",
        "expected_revision",
        "confirmed_connection_loss",
    }
)
_LOSS_OPERATIONS = frozenset(
    {Operation.DISCONNECT, Operation.DISABLE, Operation.LEAVE}
)


def _validate_parameters(
    provider: Provider, operation: Operation, parameters: Mapping[str, str]
) -> None:
    if not isinstance(parameters, Mapping):
        raise TypeError("parameters must be an object")
    allowed: frozenset[str] = frozenset()
    required: frozenset[str] = frozenset()
    if operation in (Operation.ENABLE, Operation.CONNECT) and provider is Provider.ZEROTIER:
        allowed = frozenset({"network_id"})
    elif operation is Operation.SAVE_ADDRESS:
        if provider is not Provider.PEANUT:
            raise ValueError("save_address is only supported for Peanut")
        allowed = required = frozenset({"https_url"})
    elif operation is Operation.LEAVE and provider is not Provider.ZEROTIER:
        raise ValueError("leave is only supported for ZeroTier")

    actual = set(parameters)
    if actual - allowed or required - actual:
        raise ValueError("parameters schema mismatch")
    for key, value in parameters.items():
        if type(value) is not str:
            raise TypeError(f"parameters.{key} must be a string")
        if key == "network_id" and not _is_zerotier_id(value):
            raise ValueError("network_id must be 16 hex characters")
        if key == "https_url":
            if not value or value != value.strip():
                raise ValueError("https_url must be a nonempty unpadded string")
            _validate_remote_https_url(value)


@dataclass(frozen=True)
class RemoteRequest:
    """Strict non-switch input; configuration availability is not inferred.

    ZeroTier enable/connect may omit network_id to reference saved configuration;
    a supplied ID must be valid. Other providers always use saved configuration.
    Connection-loss confirmation is explicit on every request and must be true
    for disconnect, disable, and leave. It does not grant authorization.
    """

    provider: Provider
    operation: Operation
    parameters: Mapping[str, str]
    expected_revision: int
    confirmed_connection_loss: bool

    def __post_init__(self) -> None:
        if not isinstance(self.provider, Provider) or self.provider is Provider.NONE:
            raise ValueError("provider must identify a remote provider")
        if not isinstance(self.operation, Operation):
            raise TypeError("operation must be Operation")
        if self.operation is Operation.SWITCH:
            raise ValueError("switch is unsupported: target configuration schema unresolved")
        if self.operation in {
            Operation.CHECK_CONFIG, Operation.STOP, Operation.RETRY, Operation.ENABLE_AUTOSTART,
            Operation.DISABLE_AUTOSTART, Operation.UNBIND, Operation.VERIFY_ADDRESS,
            Operation.SET_AUTOSTART, Operation.RESET_LOCAL_CONFIG,
            Operation.RESET_ACCOUNT_CONFIG, Operation.RECOVER_RESET,
        }:
            raise ValueError("use the strict Peanut action endpoint for this operation")
        if type(self.expected_revision) is not int or self.expected_revision < 1:
            raise ValueError("expected_revision must be a positive integer")
        if type(self.confirmed_connection_loss) is not bool:
            raise TypeError("confirmed_connection_loss must be a boolean")
        if self.operation in _LOSS_OPERATIONS and not self.confirmed_connection_loss:
            raise ValueError("explicit connection-loss confirmation is required")
        if not isinstance(self.parameters, Mapping):
            raise TypeError("parameters must be an object")
        parameters = dict(self.parameters)
        _validate_parameters(self.provider, self.operation, parameters)
        object.__setattr__(self, "parameters", MappingProxyType(parameters))

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider.value,
            "operation": self.operation.value,
            "parameters": dict(self.parameters),
            "expected_revision": self.expected_revision,
            "confirmed_connection_loss": self.confirmed_connection_loss,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RemoteRequest:
        if not isinstance(value, Mapping):
            raise TypeError("remote_request must be an object")
        if set(value) != _REQUEST_KEYS:
            raise ValueError("remote_request schema mismatch")
        return cls(
            provider=Provider.parse(value["provider"]),
            operation=Operation.parse(value["operation"]),
            parameters=value["parameters"],
            expected_revision=value["expected_revision"],
            confirmed_connection_loss=value["confirmed_connection_loss"],
        )
