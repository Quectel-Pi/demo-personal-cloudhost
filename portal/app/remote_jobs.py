"""Strict remote job response contract; this module performs no job execution."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .errors import MESSAGES
from .remote_status import Provider, TaskState


class _CodeEnum(str, Enum):
    @classmethod
    def parse(cls, value: object) -> _CodeEnum:
        if not isinstance(value, str):
            raise ValueError(f"{cls.__name__} must be a string")
        try:
            return cls(value)
        except ValueError as exc:
            raise ValueError(f"invalid {cls.__name__}: {value!r}") from exc


class Operation(_CodeEnum):
    CHECK_CONFIG = "check_config"
    STOP = "stop"
    RETRY = "retry"
    ENABLE_AUTOSTART = "enable_autostart"
    DISABLE_AUTOSTART = "disable_autostart"
    UNBIND = "unbind"
    VERIFY_ADDRESS = "verify_address"
    SET_AUTOSTART = "set_autostart"
    RESET_LOCAL_CONFIG = "reset_local_config"
    RESET_ACCOUNT_CONFIG = "reset_account_config"
    RECOVER_RESET = "recover_reset"
    INSTALL = "install"
    START = "start"
    ENABLE = "enable"
    CONNECT = "connect"
    REQUEST_AUTHORIZATION = "request_authorization"
    DISCONNECT = "disconnect"
    DISABLE = "disable"
    LEAVE = "leave"
    SWITCH = "switch"
    SAVE_ADDRESS = "save_address"


class RollbackState(_CodeEnum):
    NOT_REQUIRED = "not_required"
    NOT_STARTED = "not_started"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery_required"


_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SENSITIVE_DETAIL = re.compile(
    r"(?i)(?:\b(?:password|passwd|token|secret|credential|cookie|sid|bearer)\b"
    r"|private key|https?://|-----BEGIN|/home/|/root/|/var/lib/|/etc/"
    r"|\b(?:stdout|stderr|traceback)\b)"
)


def _validate_code(value: str | None, field: str) -> None:
    if value is not None and (
        not isinstance(value, str) or _SAFE_CODE.fullmatch(value) is None
    ):
        raise ValueError(f"{field} must be a stable lowercase code")


def _validate_detail(value: str | None) -> None:
    if value is None:
        return
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 256
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or _SENSITIVE_DETAIL.search(value) is not None
    ):
        raise ValueError("error.detail must be a bounded safe message")


@dataclass(frozen=True)
class JobError:
    """Safe, structured error data; raw command output is intentionally unsupported."""

    code: str
    message: str
    detail: str | None
    reason: str
    retryable: bool

    def __post_init__(self) -> None:
        expected = MESSAGES.get(self.code)
        if expected is None:
            raise ValueError("error.code is not a registered API error")
        if self.message != expected:
            raise ValueError("error.message must use the registered safe message")
        _validate_detail(self.detail)
        _validate_code(self.reason, "error.reason")
        if type(self.retryable) is not bool:
            raise TypeError("error.retryable must be a boolean")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "reason": self.reason,
            "retryable": self.retryable,
        }
        if self.detail is not None:
            result["detail"] = self.detail
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> JobError:
        if not isinstance(value, Mapping):
            raise ValueError("error must be an object")
        keys = {"code", "message", "detail", "reason", "retryable"}
        required = frozenset(keys - {"detail"})
        actual = set(value)
        if not required <= actual or actual - keys:
            raise ValueError("error schema mismatch")
        return cls(
            code=value["code"],
            message=value["message"],
            detail=value.get("detail"),
            reason=value["reason"],
            retryable=value["retryable"],
        )


@dataclass(frozen=True)
class RemoteJob:
    """A redacted job result, not an execution request or a persistence layer."""

    job_id: str
    provider: Provider
    operation: Operation
    status: TaskState
    phase: str | None
    reason: str | None
    next_action: str | None
    config_revision: int
    rollback: RollbackState
    error: JobError | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.job_id, str) or _JOB_ID.fullmatch(self.job_id) is None:
            raise ValueError("job_id must be a bounded opaque identifier")
        if not isinstance(self.provider, Provider) or self.provider is Provider.NONE:
            raise ValueError("provider must identify a configured remote provider")
        if not isinstance(self.operation, Operation):
            raise TypeError("operation must be Operation")
        if not isinstance(self.status, TaskState):
            raise TypeError("status must be TaskState")
        _validate_code(self.phase, "phase")
        _validate_code(self.reason, "reason")
        _validate_code(self.next_action, "next_action")
        if type(self.config_revision) is not int or self.config_revision < 1:
            raise ValueError("config_revision must be a positive integer")
        if not isinstance(self.rollback, RollbackState):
            raise TypeError("rollback must be RollbackState")
        if self.error is not None and not isinstance(self.error, JobError):
            raise TypeError("error must be JobError or null")
        if self.status is TaskState.WAITING_USER and (
            self.phase is None or self.reason is None or self.next_action is None
        ):
            raise ValueError("waiting_user jobs require phase, reason, and next_action")
        if self.status is TaskState.FAILED and self.error is None:
            raise ValueError("failed jobs require a structured error")
        if self.status is not TaskState.FAILED and self.error is not None:
            raise ValueError("only failed jobs may include an error")
        if (
            self.status is TaskState.RECOVERY_REQUIRED
            and self.rollback not in (RollbackState.FAILED, RollbackState.RECOVERY_REQUIRED)
        ):
            raise ValueError("recovery_required jobs require a failed or required rollback")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "job_id": self.job_id,
            "provider": self.provider.value,
            "operation": self.operation.value,
            "status": self.status.value,
            "phase": self.phase,
            "reason": self.reason,
            "next_action": self.next_action,
            "config_revision": self.config_revision,
            "rollback": self.rollback.value,
        }
        if self.error is not None:
            result["error"] = self.error.to_dict()
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RemoteJob:
        if not isinstance(value, Mapping):
            raise ValueError("remote_job must be an object")
        expected = frozenset(
            {
                "job_id",
                "provider",
                "operation",
                "status",
                "phase",
                "reason",
                "next_action",
                "config_revision",
                "rollback",
                "error",
            }
        )
        actual = set(value)
        required = expected - {"error"}
        if not required <= actual or actual - expected:
            missing = sorted(required - actual)
            unexpected = sorted(repr(key) for key in actual - expected)
            raise ValueError(
                f"remote_job schema mismatch (missing={missing}, unexpected={unexpected})"
            )
        return cls(
            job_id=value["job_id"],
            provider=Provider.parse(value["provider"]),
            operation=Operation.parse(value["operation"]),
            status=TaskState.parse(value["status"]),
            phase=value["phase"],
            reason=value["reason"],
            next_action=value["next_action"],
            config_revision=value["config_revision"],
            rollback=RollbackState.parse(value["rollback"]),
            error=JobError.from_dict(value["error"]) if "error" in value else None,
        )
