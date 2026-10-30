import pytest

from app.remote_jobs import JobError, Operation, RemoteJob, RollbackState
from app.remote_status import Provider, TaskState


def job_payload(**overrides):
    payload = {
        "job_id": "job_opaque-123",
        "provider": "zerotier",
        "operation": "enable",
        "status": "queued",
        "phase": "preflight",
        "reason": None,
        "next_action": None,
        "config_revision": 4,
        "rollback": "not_started",
    }
    payload.update(overrides)
    return payload


def test_job_schema_round_trips_fixed_response_shape():
    payload = job_payload()
    job = RemoteJob.from_dict(payload)

    assert job.to_dict() == payload
    assert RemoteJob.from_dict(job.to_dict()) == job
    assert "error" not in job.to_dict()


@pytest.mark.parametrize("operation", list(Operation))
def test_job_schema_accepts_only_fixed_operations(operation):
    payload = job_payload(operation=operation.value)
    assert RemoteJob.from_dict(payload).operation is operation


@pytest.mark.parametrize("status", list(TaskState))
def test_job_schema_accepts_defined_task_states(status):
    payload = job_payload(status=status.value)
    if status is TaskState.WAITING_USER:
        payload.update(reason="controller_authorization_required", next_action="open_official_console")
    elif status is TaskState.FAILED:
        payload["error"] = {
            "code": "E9000",
            "message": "internal error",
            "reason": "operation_failed",
            "retryable": False,
        }
    elif status is TaskState.RECOVERY_REQUIRED:
        payload["rollback"] = "recovery_required"

    assert RemoteJob.from_dict(payload).status is status


def test_waiting_user_requires_action_guidance():
    with pytest.raises(ValueError, match="require phase"):
        RemoteJob.from_dict(job_payload(status="waiting_user", next_action="open_official_console"))


def test_failed_and_recovery_states_require_consistent_metadata():
    with pytest.raises(ValueError, match="require a structured error"):
        RemoteJob.from_dict(job_payload(status="failed"))
    with pytest.raises(ValueError, match="failed or required rollback"):
        RemoteJob.from_dict(job_payload(status="recovery_required"))


def test_job_schema_rejects_unknown_fields_and_states():
    payload = job_payload(unredacted_stdout="password=secret")
    with pytest.raises(ValueError, match="unexpected"):
        RemoteJob.from_dict(payload)
    with pytest.raises(ValueError):
        RemoteJob.from_dict(job_payload(status="complete"))
    with pytest.raises(ValueError):
        RemoteJob.from_dict(job_payload(provider="other"))
    with pytest.raises(ValueError):
        RemoteJob.from_dict(job_payload(operation="execute"))


@pytest.mark.parametrize(
    "value",
    ["", "../job", "job\nsecret", "x" * 129, None],
)
def test_job_id_is_bounded_and_opaque(value):
    with pytest.raises(ValueError):
        RemoteJob.from_dict(job_payload(job_id=value))


@pytest.mark.parametrize(
    "field,value",
    [
        ("phase", "installing; cat /etc/shadow"),
        ("reason", "raw stderr: token=secret"),
        ("next_action", "https://attacker.invalid/"),
        ("config_revision", True),
        ("config_revision", 0),
        ("rollback", "successful"),
    ],
)
def test_job_metadata_is_strict_and_cannot_embed_raw_diagnostics(field, value):
    with pytest.raises((TypeError, ValueError)):
        RemoteJob.from_dict(job_payload(**{field: value}))


def test_structured_job_error_uses_registered_message_and_safe_codes():
    error = {
        "code": "E9000",
        "message": "internal error",
        "detail": "public ingress cannot modify remote access",
        "reason": "signature_check_failed",
        "retryable": False,
    }
    payload = job_payload(
        status="failed",
        error=error,
        reason="signature_check_failed",
        rollback="not_required",
    )
    job = RemoteJob.from_dict(payload)

    assert job.error == JobError.from_dict(error)
    assert job.to_dict()["error"] == error
    assert "secret" not in repr(job.to_dict())


@pytest.mark.parametrize(
    "error",
    [
        {"code": "E9999", "message": "bad", "reason": "failed", "retryable": False},
        {"code": "E9000", "message": "raw password=secret", "reason": "failed", "retryable": False},
        {"code": "E9000", "message": "internal error", "reason": "stderr: secret", "retryable": False},
        {"code": "E9000", "message": "internal error", "detail": "raw stderr: operation failed", "reason": "failed", "retryable": False},
        {"code": "E9000", "message": "internal error", "detail": "token=secret", "reason": "failed", "retryable": False},
        {"code": "E9000", "message": "internal error", "reason": "failed", "retryable": "false"},
        {"code": "E9000", "message": "internal error", "reason": "failed", "retryable": False, "stderr": "secret"},
    ],
)
def test_job_error_rejects_unknown_messages_raw_diagnostics_and_invalid_types(error):
    with pytest.raises((ValueError, TypeError)):
        JobError.from_dict(error)


def test_direct_models_reject_string_enum_substitutes():
    payload = job_payload()
    job = RemoteJob.from_dict(payload)
    with pytest.raises((TypeError, ValueError)):
        RemoteJob(
            job_id=job.job_id,
            provider="zerotier",
            operation=job.operation,
            status=job.status,
            phase=job.phase,
            reason=job.reason,
            next_action=job.next_action,
            config_revision=job.config_revision,
            rollback=job.rollback,
        )

    assert Provider.ZEROTIER is job.provider
