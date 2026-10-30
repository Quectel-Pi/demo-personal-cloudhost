"""Offline evidence only. No SDK, systemctl, PAM or vendor writes are executed."""

import json
import os
import socket
import subprocess
import time
from dataclasses import replace
from pathlib import Path
import asyncio

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.errors import ApiError
from app.main import create_app
from app.peanut_control import Controller
from app.peanut_credentials import Credentials, PeanutError, inspect_sdk, read_credentials
from app.peanut_protocol import (
    OfficialAdapter, management_url, normalize_address, normalize_ssh_address, public_addresses,
)
from app.remote_jobs import RemoteJob
from app.remote_policy import Capability, Ingress, Role, evaluate_policy
from app.remote_status import Provider
from conftest import TEST_PASSWORD, TEST_USER


@pytest.fixture
def app_file(tmp_path):
    directory = tmp_path / "credentials"
    directory.mkdir(mode=0o750)
    file = directory / "app.json"
    file.write_text('{"app_id":"123","app_key":"test-only-not-a-real-key"}')
    file.chmod(0o640)
    return file


def read_test(path):
    return read_credentials(path, uid=os.getuid(), gid=os.getgid())


def test_credentials_valid_and_not_repr(app_file):
    credentials = read_test(app_file)
    assert credentials.app_id == "123"
    assert credentials.app_key not in repr(credentials)
    assert credentials.app_id not in repr(credentials)


@pytest.mark.parametrize("raw", [
    "", "[]", "{}", "{", '{"app_id":"123","app_key":""}',
    '{"app_id":"123","app_key":"<YOUR_PHTUNNEL_APP_KEY>"}',
    '{"app_id":"123","app_key":"a","app_key":"b"}',
    '{"app_id":"１２３","app_key":"a"}',
    '{"app_id":123,"app_key":"a"}',
    '{"app_id":"123","app_key":"a","extra":1}',
    '{"app_id":"123","app_key":"a\\n"}', "x" * 8193,
])
def test_credentials_reject_invalid(app_file, raw):
    app_file.write_text(raw)
    with pytest.raises(PeanutError, match="invalid"):
        read_test(app_file)


@pytest.mark.parametrize("mode", [0o600, 0o644, 0o660, 0o777])
def test_credentials_reject_mode(app_file, mode):
    app_file.chmod(mode)
    with pytest.raises(PeanutError, match="unsafe_permissions"):
        read_test(app_file)


def test_credentials_no_links_and_parent_mode(app_file):
    target = app_file.with_name("original")
    app_file.rename(target)
    app_file.symlink_to(target)
    with pytest.raises(PeanutError, match="unsafe_permissions"):
        read_test(app_file)
    app_file.unlink()
    app_file.parent.chmod(0o755)
    with pytest.raises(PeanutError, match="unsafe_permissions"):
        read_test(target)


def test_credentials_missing_and_nonregular(app_file):
    app_file.unlink()
    with pytest.raises(PeanutError, match="missing"):
        read_test(app_file)
    os.mkfifo(app_file, 0o640)
    with pytest.raises(PeanutError, match="unsafe_permissions"):
        read_test(app_file)


def test_private_deployment_credentials_require_owner_only_file(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    from install_peanut import import_deployment_credentials, read_deployment_credentials

    source = tmp_path / "peanut-app.deploy.json"
    source.write_text('{"app_id":"12345","app_key":"private-test-key"}')
    source.chmod(0o600)
    assert read_deployment_credentials(source, uid=os.getuid()) == ("12345", "private-test-key")
    directory = tmp_path / "credentials"
    directory.mkdir()
    directory.chmod(0o750)
    target = directory / "app.json"
    assert import_deployment_credentials(
        source, target, source_uid=os.getuid(), target_uid=os.getuid(), target_gid=os.getgid(),
    )
    assert not source.exists()
    assert target.stat().st_mode & 0o777 == 0o640
    assert read_test(target).app_id == "12345"

    source.write_text('{"app_id":"67890","app_key":"another-private-test-key"}')
    source.chmod(0o600)
    before = target.read_bytes()
    assert not import_deployment_credentials(
        source, target, source_uid=os.getuid(), target_uid=os.getuid(), target_gid=os.getgid(),
    )
    assert source.exists()
    assert target.read_bytes() == before

    source.chmod(0o640)
    with pytest.raises(ValueError, match="unsafe deployment credential file"):
        read_deployment_credentials(source, uid=os.getuid())
    source.chmod(0o600)
    source.write_text('{"app_id":"12345","app_key":"YOUR_PHTUNNEL_APP_KEY"}')
    source.chmod(0o600)
    with pytest.raises(ValueError, match="invalid deployment credentials"):
        read_deployment_credentials(source, uid=os.getuid())
    target = tmp_path / "target.json"
    target.write_text('{"app_id":"12345","app_key":"test"}')
    source.unlink()
    source.symlink_to(target)
    with pytest.raises(ValueError, match="unable to read deployment credential file safely"):
        read_deployment_credentials(source, uid=os.getuid())


def test_adapter_evidence_permissions_are_group_read_only(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    from install_peanut import install_adapter_evidence_permissions

    evidence = tmp_path / "adapter.json"
    evidence.write_text('{"evidence":"fixture"}')
    evidence.chmod(0o600)
    install_adapter_evidence_permissions(
        evidence, sdk_gid=os.getgid(), owner_uid=os.getuid()
    )
    info = evidence.stat()
    assert info.st_uid == os.getuid()
    assert info.st_gid == os.getgid()
    assert info.st_mode & 0o777 == 0o640
    assert evidence.read_text() == '{"evidence":"fixture"}'

    link = tmp_path / "adapter-link.json"
    link.symlink_to(evidence)
    with pytest.raises(ValueError, match="unable to secure adapter evidence permissions"):
        install_adapter_evidence_permissions(
            link, sdk_gid=os.getgid(), owner_uid=os.getuid()
        )


def test_sdk_missing_bad_elf_hash_and_symlink(tmp_path):
    sdk = tmp_path / "phtunnel"
    assert inspect_sdk(sdk) == "not_installed"
    sdk.write_bytes(b"\x7fELF" + b"x" * 100)
    sdk.chmod(0o755)
    assert inspect_sdk(sdk, uid=os.getuid()) == "incompatible"
    link = tmp_path / "link"
    link.symlink_to(sdk)
    assert inspect_sdk(link) == "unknown"


@pytest.fixture
def controller(tmp_path):
    active = {"state": "inactive"}
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        assert argv[0] == "/usr/bin/systemctl" and argv[2] == "cloudhost-peanut.service"
        assert kwargs["env"] == {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"}
        if argv[1] in {"start", "restart"}:
            active["state"] = "active"
        if argv[1] == "stop":
            active["state"] = "inactive"
        return subprocess.CompletedProcess(argv, 0, (
            f"ActiveState={active['state']}\nMainPID={'42' if active['state'] == 'active' else '0'}\nNRestarts=0"
        ), "")

    adapter = OfficialAdapter(rpc_transport=lambda method: {
        "result_code": 0,
        "data": ({"device_sn": "test-sn", "device_sn_pwd": "test-device-secret",
                  "status": 1, "public_ip": "8.8.8.8"}
                 if method == "getsn" else {"url": "", "account": ""}),
    })
    instance = Controller(
        tmp_path / "state", runner=runner,
        credentials=lambda: Credentials("123", "private-test-key", "fingerprint"),
        installation=lambda: "installed", adapter=adapter, evidence=lambda: {},
        pause_file=tmp_path / "run" / "paused", boot_id="test-boot",
    )
    instance.test_calls = calls
    yield instance
    instance.close()


def finish(controller, job):
    for _ in range(200):
        result = controller.job(job["job_id"])
        if result["status"] not in {"queued", "running"}:
            RemoteJob.from_dict(result)
            return result
        time.sleep(0.005)
    pytest.fail("offline job did not finish")


def submit(controller, operation, **fields):
    return controller.submit("admin", f"test-{operation}-{controller.revision}", {
        "operation": operation, "expected_revision": controller.revision, **fields,
    })


@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_set_autostart_has_no_runtime_or_material_side_effects(controller, running, enabled):
    if running:
        finish(controller, submit(controller, "start"))
    controller.pause_file.parent.mkdir(parents=True, exist_ok=True)
    controller.pause_file.write_text("test-boot")
    materials = {"verification": {"state": "endpoint_ready"}, "address": "https://example.net",
                 "ssh_address": {"host": "ssh.example.net", "port": 22},
                 "external_confirmation": {"confirmed": True}}
    with controller.db:
        for key, value in materials.items():
            controller.put(key, value)
    controller.bindings["qr"] = {"sid": "old"}
    controller.redirects["redirect"] = {"sid": "old"}
    controller.challenge = ("challenge", time.time() + 10)
    before_calls = list(controller.test_calls)
    before_revision = controller.revision
    result = finish(controller, submit(controller, "set_autostart", enabled=enabled,
                                       confirmed_autostart=enabled))
    assert result["status"] == "succeeded"
    assert controller.get("enabled") is enabled
    assert controller.revision == before_revision + 1
    assert controller.test_calls == before_calls
    assert controller.pause_file.read_text() == "test-boot"
    assert controller.bindings and controller.redirects and controller.challenge
    for key, value in materials.items():
        assert controller.get(key) == value


@pytest.mark.parametrize("fields", [
    {}, {"enabled": None}, {"enabled": 1}, {"enabled": "false"},
    {"enabled": True}, {"enabled": True, "confirmed_autostart": 1},
])
def test_set_autostart_strict_validation(controller, fields):
    with pytest.raises(PeanutError):
        submit(controller, "set_autostart", **fields)
    assert controller.revision == 1
    assert not controller.test_calls


def test_set_autostart_is_available_without_sdk_or_credentials(controller):
    controller.credentials = lambda: pytest.fail("autostart setting must not inspect credentials")
    controller.installation = lambda: pytest.fail("autostart setting must not inspect SDK")
    result = finish(controller, submit(controller, "set_autostart", enabled=True,
                                       confirmed_autostart=True))
    assert result["status"] == "succeeded"
    assert controller.get("enabled") is True
    assert not controller.test_calls


def seed_reset(controller):
    finish(controller, submit(controller, "start"))
    with controller.db:
        for key, value in {
            "enabled": True, "address": "https://web.example.net",
            "ssh_address": {"host": "ssh.example.net", "port": 41022},
            "verification": {"state": "endpoint_ready", "timestamp": time.time(),
                             "observed_at": "offline-test"},
            "external_confirmation": {"confirmed": True},
            "unrelated": "retained",
        }.items():
            controller.put(key, value)
    controller.bindings.update({"a": {"sid": "one"}, "b": {"sid": "two"}})
    controller.redirects.update({"a": {"sid": "one"}, "b": {"sid": "two"}})
    controller.challenge = ("old-challenge", time.time() + 10)


def account_reset_fixture(controller, *, bound=True):
    controller.evidence = lambda: {
        "rpc_isolated": True, "binding_verified": True, "unbinding_verified": True,
    }
    original = controller.adapter.rpc_transport
    remote = {"bound": bound, "writes": 0}
    controller.adapter.rpc_transport = lambda method: (
        {"result_code": 0, "data": {
            "url": "https://b.oray.com/manage" if remote["bound"] else "",
            "account": "tester" if remote["bound"] else "",
        }} if method == "getmgrurl" else original(method)
    )
    controller.status()
    if bound:
        sn, account, when = controller.binding_candidate
        controller.binding_candidate = (sn, account, when - 4)
        controller.last_probe = 0
        assert controller.status()["binding"]["state"] == "bound"

    def unbind(sn, password):
        assert sn == "test-sn"
        remote["writes"] += 1
        remote["bound"] = False

    controller.adapter.unbind = unbind
    return remote


def test_reset_local_deferred_transaction_and_all_session_invalidation(controller):
    seed_reset(controller)
    credentials = controller.credentials()
    old_revision = controller.revision
    request = {"operation": "reset_local_config", "expected_revision": old_revision,
               "confirmed_connection_loss": True}
    job = controller.submit("admin", "local-reset-once", request, defer=True)
    assert not controller.get("enabled")
    assert controller.paused()
    assert controller.get("address") == "https://web.example.net"
    assert not controller.bindings and not controller.redirects and controller.challenge is None
    assert not any(argv[1] == "stop" for argv in controller.test_calls)
    assert controller.submit("admin", "local-reset-once", request) == job
    with pytest.raises(PeanutError, match="operation_in_progress"):
        submit(controller, "start")
    controller.activate(job["job_id"], "admin")
    value = finish(controller, job)
    assert value["status"] == "succeeded"
    assert value["reason"] == "local_config_cleared_binding_retained"
    assert controller.status()["daemon"]["state"] == "stopped"
    assert controller.get("address") == ""
    for key in ("ssh_address", "verification", "external_confirmation", "reset_progress"):
        assert controller.get(key) is None
    assert controller.get("unrelated") == "retained"
    assert controller.credentials() == credentials
    assert controller.revision > old_revision
    assert controller.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
    assert controller.submit("admin", "local-reset-once", request)["job_id"] == job["job_id"]
    with pytest.raises(PeanutError, match="revision_conflict"):
        controller.request("address", "admin", "old-tab", {
            "expected_revision": old_revision, "https_url": "https://old.example.net",
            "_idempotency_key": "old-address-write",
        })


@pytest.mark.parametrize("operation,fields", [
    ("reset_local_config", {}),
    ("reset_account_config", {"confirmed_connection_loss": True}),
    ("reset_account_config", {"confirmed_connection_loss": True, "confirmed_account_unbind": True}),
    ("start", {"enabled": False}),
    ("start", {"confirmed_autostart": False}),
])
def test_reset_confirmations_gate_and_scoped_fields(controller, operation, fields):
    with pytest.raises(PeanutError):
        submit(controller, operation, **fields)
    assert controller.revision == 1
    assert controller.get("reset_progress") is None
    assert not controller.test_calls or all(call[1] == "show" for call in controller.test_calls)


@pytest.mark.parametrize("bound", [True, False])
def test_account_reset_confirmed_before_stop(controller, bound):
    seed_reset(controller)
    remote = account_reset_fixture(controller, bound=bound)
    original = controller.runner

    def runner(argv, **kwargs):
        if argv[1] == "stop":
            assert not remote["bound"]
            assert controller.get("address") == "https://web.example.net"
        return original(argv, **kwargs)

    controller.runner = runner
    result = finish(controller, submit(controller, "reset_account_config",
                                       confirmed_connection_loss=True, confirmed_account_unbind=True))
    assert result["status"] == "succeeded"
    assert result["reason"] == "account_unbound_config_cleared"
    assert remote["writes"] == int(bound)
    assert controller.get("address") == ""
    assert not controller.get("enabled")
    assert controller.get("reset_progress") is None


def test_account_reset_waits_for_bounded_delayed_confirmation(controller, monkeypatch):
    seed_reset(controller)
    remote = account_reset_fixture(controller)
    original_rpc = controller.adapter.rpc
    delayed = []
    sleeps = []

    def rpc(method):
        if method == "getmgrurl" and remote["writes"] and len(delayed) < 2:
            delayed.append(True)
            return {"url": "https://b.oray.com/manage", "account": "tester"}
        return original_rpc(method)

    monkeypatch.setattr(controller.adapter, "rpc", rpc)
    monkeypatch.setattr("app.peanut_control.time.sleep", lambda seconds: sleeps.append(seconds))
    job = submit(controller, "reset_account_config", confirmed_connection_loss=True,
                 confirmed_account_unbind=True)
    controller.pool.shutdown(wait=True)
    result = controller.job(job["job_id"])
    RemoteJob.from_dict(result)
    assert result["status"] == "succeeded"
    assert remote["writes"] == 1
    assert sleeps == [1, 1]


def test_account_context_change_never_unbinds_new_account(controller):
    seed_reset(controller)
    remote = account_reset_fixture(controller)
    job = controller.submit("admin", "context-change-reset", {
        "operation": "reset_account_config", "expected_revision": controller.revision,
        "confirmed_connection_loss": True, "confirmed_account_unbind": True,
    }, defer=True)
    original_rpc = controller.adapter.rpc
    controller.adapter.rpc = lambda method: (
        {"url": "https://b.oray.com/manage", "account": "different"}
        if method == "getmgrurl" else original_rpc(method)
    )
    controller.activate(job["job_id"], "admin")
    result = finish(controller, job)
    assert result["status"] == "recovery_required"
    assert result["reason"] == "binding_changed"
    assert remote["writes"] == 0


@pytest.mark.parametrize("enabled", [False, True])
def test_set_autostart_persists_across_controller_and_boot(controller, tmp_path, enabled):
    finish(controller, submit(controller, "set_autostart", enabled=enabled,
                              confirmed_autostart=enabled))
    reopened = Controller(
        tmp_path / "state", runner=controller.runner, credentials=controller.credentials,
        installation=controller.installation, evidence=lambda: {},
        pause_file=controller.pause_file, boot_id="next-boot",
    )
    try:
        assert reopened.get("enabled") is enabled
        assert not controller.test_calls
        reopened.restore()
        assert any(call[1] == "start" for call in controller.test_calls) is enabled
    finally:
        reopened.close()


def test_account_reset_does_not_start_stopped_sdk(controller):
    controller.evidence = lambda: {
        "rpc_isolated": True, "binding_verified": True, "unbinding_verified": True,
    }
    with pytest.raises(PeanutError, match="sdk_not_running"):
        submit(controller, "reset_account_config", confirmed_connection_loss=True,
               confirmed_account_unbind=True)
    assert all(call[1] == "show" for call in controller.test_calls)


@pytest.mark.parametrize("fault", ["official_rejected", "transport_failed"])
def test_account_unknown_blocks_new_keys_and_recovers_without_reunbind(controller, fault):
    seed_reset(controller)
    remote = account_reset_fixture(controller)

    def unknown(*args):
        remote["writes"] += 1
        raise PeanutError(fault)

    controller.adapter.unbind = unknown
    value = finish(controller, submit(controller, "reset_account_config",
                                      confirmed_connection_loss=True, confirmed_account_unbind=True))
    assert value["status"] == ("failed" if fault == "official_rejected" else "recovery_required")
    assert value["phase"] == "unbinding"
    assert controller.get("address") == "https://web.example.net"
    assert controller.get("enabled") is False
    for operation in ("reset_account_config", "reset_local_config", "unbind", "start", "set_autostart"):
        with pytest.raises(PeanutError, match="reset_recovery_required"):
            submit(controller, operation, confirmed_connection_loss=True,
                   confirmed_account_unbind=True, **(
                       {"enabled": False} if operation == "set_autostart" else {}
                   ))
    value = finish(controller, submit(controller, "recover_reset", confirmed_connection_loss=True))
    assert value["status"] == "recovery_required"
    assert remote["writes"] == 1
    remote["bound"] = False
    value = finish(controller, submit(controller, "recover_reset", confirmed_connection_loss=True))
    assert value["status"] == "succeeded"
    assert remote["writes"] == 1


@pytest.mark.parametrize("failure", ["stop", "transaction"])
def test_reset_partial_failure_preserves_addresses_then_recovers(controller, monkeypatch, failure):
    seed_reset(controller)
    remote = account_reset_fixture(controller)
    original_runner, original_put = controller.runner, controller.put
    failed = []

    def runner(argv, **kwargs):
        if failure == "stop" and argv[1] == "stop" and not failed:
            failed.append(True)
            raise OSError("offline stop failure")
        return original_runner(argv, **kwargs)

    def put(key, value):
        if failure == "transaction" and key == "ssh_address" and not failed:
            failed.append(True)
            raise RuntimeError("offline transaction failure")
        return original_put(key, value)

    controller.runner = runner
    monkeypatch.setattr(controller, "put", put)
    result = finish(controller, submit(controller, "reset_account_config",
                                       confirmed_connection_loss=True, confirmed_account_unbind=True))
    assert result["status"] == "recovery_required"
    assert controller.get("address") == "https://web.example.net"
    assert controller.get("ssh_address")["host"] == "ssh.example.net"
    assert controller.get("reset_progress")["unbound_confirmed"] is True
    result = finish(controller, submit(controller, "recover_reset", confirmed_connection_loss=True))
    assert result["status"] == "succeeded"
    assert remote["writes"] == 1
    assert controller.get("address") == ""


def test_reset_restart_keeps_gate_phase_and_idempotency(controller, tmp_path):
    seed_reset(controller)
    request = {"operation": "reset_local_config", "expected_revision": controller.revision,
               "confirmed_connection_loss": True}
    job = controller.submit("admin", "interrupted-reset", request, defer=True)
    reopened = Controller(
        tmp_path / "state", runner=controller.runner, credentials=controller.credentials,
        installation=controller.installation, evidence=lambda: {},
        pause_file=controller.pause_file, boot_id="test-boot",
    )
    try:
        assert reopened.status()["reset_pending"]
        assert reopened.job(job["job_id"])["status"] == "recovery_required"
        assert reopened.submit("admin", "interrupted-reset", request)["job_id"] == job["job_id"]
        with pytest.raises(PeanutError, match="reset_recovery_required"):
            submit(reopened, "start")
        before = len(controller.test_calls)
        reopened.restore()
        assert len(controller.test_calls) == before
        result = finish(reopened, submit(reopened, "recover_reset", confirmed_connection_loss=True))
        assert result["status"] == "succeeded"
    finally:
        reopened.close()


def test_idempotency_key_cannot_change_operations(controller):
    body = {"operation": "set_autostart", "expected_revision": 1, "enabled": False}
    finish(controller, controller.submit("admin", "one-logical-action", body))
    with pytest.raises(PeanutError, match="idempotency_conflict"):
        controller.submit("admin", "one-logical-action", {
            "operation": "reset_local_config", "expected_revision": controller.revision,
            "confirmed_connection_loss": True,
        })


def test_control_start_stop_identity_intent_and_restart(controller):
    assert controller.status()["intent"] == {"enabled": False, "paused_this_boot": False}
    assert finish(controller, submit(controller, "start"))["status"] == "succeeded"
    assert controller.status()["daemon"]["state"] == "running"
    assert controller.status()["device"]["state"] == "unknown"
    assert controller.status()["binding"]["state"] == "unknown"
    assert not controller.status()["capabilities"]["qr_binding"]
    assert finish(controller, submit(controller, "enable_autostart"))["status"] == "succeeded"
    assert finish(controller, submit(controller, "stop", confirmed_connection_loss=True))["status"] == "succeeded"
    assert controller.status()["intent"] == {"enabled": True, "paused_this_boot": True}
    before = len(controller.test_calls)
    controller.restore()
    assert len(controller.test_calls) == before
    assert controller.get("running_fingerprint") == "fingerprint"
    controller.boot_id = "next-boot"
    controller.restore()
    assert controller.test_calls[-1][1] == "start"


def test_idempotency_and_revision_survive_restart(controller):
    body = {"operation": "check_config", "expected_revision": 1}
    job = controller.submit("admin", "same-key", body)
    assert finish(controller, job)["status"] == "succeeded"
    assert controller.submit("admin", "same-key", body)["job_id"] == job["job_id"]
    with pytest.raises(PeanutError, match="idempotency_conflict"):
        controller.submit("admin", "same-key", {**body, "confirmed_connection_loss": False})
    with pytest.raises(PeanutError, match="revision_conflict"):
        controller.submit("admin", "next-key", {**body, "expected_revision": 999})
    assert controller.job(job["job_id"])["status"] == "succeeded"


@pytest.mark.parametrize("fields", [
    {"operation": "reboot"}, {"operation": "start", "unit": "ssh"},
    {"operation": "stop"}, {"operation": "unbind", "confirmed_connection_loss": True},
    {"operation": "start", "expected_revision": True},
])
def test_reject_before_any_system_action(controller, fields):
    body = {"expected_revision": 1, **fields}
    with pytest.raises(PeanutError):
        controller.submit("admin", "test-key", body)
    assert not controller.test_calls


def test_unbind_gate_no_vendor_write(controller):
    with pytest.raises(PeanutError, match="unbinding_unverified"):
        submit(controller, "unbind", confirmed_connection_loss=True, confirmed_account_unbind=True)
    with pytest.raises(PeanutError, match="qr_adapter_unverified"):
        controller.request("binding_create", "admin", "sid", {
            "expected_revision": 1, "_idempotency_key": "test-binding-key",
        })
    assert controller.bindings == {}


@pytest.mark.parametrize("code", [1, -1, "0", True, None])
def test_official_business_error_is_not_success(code):
    adapter = OfficialAdapter(rpc_transport=lambda method: {"result_code": code, "data": {}})
    with pytest.raises(PeanutError, match="rpc_result_failed"):
        adapter.rpc("getsn")


@pytest.mark.parametrize("status,expected", [(0, "offline"), (1, "online"), (2, "logging_in"),
                                           (3, "retrying"), (99, "unknown")])
def test_getsn_layers_not_binding(controller, status, expected):
    controller.evidence = lambda: {"rpc_isolated": True}
    controller.adapter.rpc_transport = lambda method: {
        "result_code": 0, "data": {"device_sn": "sn", "device_sn_pwd": "secret",
                                  "public_ip": "", "status": status},
    }
    finish(controller, submit(controller, "start"))
    value = controller.status()
    assert value["device"]["state"] == expected
    assert value["binding"]["state"] == "unknown"
    assert "secret" not in json.dumps(value) and '"sn"' not in json.dumps(value)


def test_binding_two_consistent_samples(controller, monkeypatch):
    controller.evidence = lambda: {"rpc_isolated": True, "binding_verified": True}
    original = controller.adapter.rpc_transport
    controller.adapter.rpc_transport = lambda method: (
        {"result_code": 0, "data": {"url": "https://b.oray.com/manage", "account": "tester"}}
        if method == "getmgrurl" else original(method)
    )
    first = controller._sample(controller.evidence())
    assert first["binding"]["state"] == "unknown"
    sn, account, when = controller.binding_candidate
    controller.binding_candidate = (sn, account, when - 4)
    assert controller._sample(controller.evidence())["binding"]["state"] == "bound"
    controller.adapter.rpc_transport = lambda method: {"result_code": 99}
    assert controller._sample(controller.evidence())["binding"]["state"] == "unknown"
    assert controller.binding_candidate is None


def test_qr_real_response_contract_and_expiry_gate():
    calls = []
    def transport(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/qrcode"):
            return 200, "application/json", json.dumps({
                "qrcode": "https://b.oray.com/scan", "qrcodeimg": "https://images.oray.com/qr",
                "expires": 60,
            }).encode()
        return 200, "image/png", b"\x89PNG\r\n\x1a\nfixture"
    adapter = OfficialAdapter(transport=transport)
    with pytest.raises(PeanutError, match="expires_unverified"):
        adapter.qr("sn", "password", expires_mode="unverified", image_hosts=["images.oray.com"])
    deadline, mime, data = adapter.qr(
        "sn", "password", expires_mode="relative_seconds", image_hosts=["images.oray.com"]
    )
    assert time.time() < deadline < time.time() + 61
    assert mime == "image/png" and data.startswith(b"\x89PNG")
    assert calls[0][1]["body"] == {"sn": "sn", "password": "password"}
    with pytest.raises(PeanutError, match="untrusted_image_host"):
        adapter.qr("sn", "password", expires_mode="relative_seconds", image_hosts=[])


@pytest.mark.parametrize("url", [
    "http://b.oray.com/", "https://b.oray.com.evil.org/", "https://b.oray.com@evil.org/",
    "https://b.oray.com:4430/", "https://console.hsk.oray.com/\r\nbad",
])
def test_management_host_validation(url):
    with pytest.raises(PeanutError):
        management_url(url)


def test_dns_all_addresses_checked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 443, 0, 0)),
    ])
    with pytest.raises(PeanutError, match="unsafe_address"):
        public_addresses("public.oray.com", 443)


@pytest.mark.parametrize("url", [
    "http://public.oray.com", "https://127.0.0.1", "https://[::1]",
    "https://name.local", "https://user@public.oray.com",
    "https://public.oray.com/path", "https://public.oray.com/?query",
    "https://public.oray.com/#fragment", "https://public.oray.com\\evil",
])
def test_address_rejects_unsafe_before_network(monkeypatch, url):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: pytest.fail("must not resolve invalid URL"))
    with pytest.raises(PeanutError):
        normalize_address(url)


class LocalClient:
    def __init__(self, controller):
        self.controller = controller
        self.calls = []

    def call(self, action, *, actor="", sid="", body=None):
        self.calls.append(action)
        try:
            return self.controller.request(action, actor, sid, body or {})
        except PeanutError as exc:
            raise ApiError(exc.code, detail=exc.reason) from exc


def test_api_permissions_and_input_before_execution(config, fake_auth, controller):
    app = create_app(replace(config, remote_admin_users=(TEST_USER,)), authenticator=fake_auth)
    app.state.peanut = LocalClient(controller)
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        assert client.get("/api/remote/peanut").status_code == 401
        client.post("/auth/login", data={"username": TEST_USER, "password": TEST_PASSWORD})
        assert client.post("/api/remote/peanut/actions", json={
            "operation": "start", "expected_revision": 1,
        }).status_code == 403
        client.headers.update({"X-CSRF-Token": client.cookies["ch_csrf"], "Idempotency-Key": "test-key"})
        assert client.post("/api/remote/peanut/actions", json={
            "operation": "start", "expected_revision": 1, "unit": "ssh",
        }).status_code == 400
        assert not controller.test_calls
        response = client.post("/api/remote/peanut/actions", json={
            "operation": "check_config", "expected_revision": 1,
        })
        assert response.status_code == 202
        finish(controller, response.json())
        assert client.get("/api/remote/jobs/" + response.json()["job_id"]).json()["status"] == "succeeded"
        assert client.get("/api/remote/peanut").headers["cache-control"] == "no-store"


def test_legacy_api_default_confirmations_keep_persisted_idempotency_digest(config, fake_auth, controller):
    original = controller.submit(TEST_USER, "legacy-api-action", {
        "operation": "check_config", "expected_revision": 1,
        "confirmed_connection_loss": False, "confirmed_account_unbind": False,
    })
    finish(controller, original)
    app = create_app(replace(config, remote_admin_users=(TEST_USER,)), authenticator=fake_auth)
    app.state.peanut = LocalClient(controller)
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        client.post("/auth/login", data={"username": TEST_USER, "password": TEST_PASSWORD})
        client.headers.update({
            "X-CSRF-Token": client.cookies["ch_csrf"], "Idempotency-Key": "legacy-api-action",
        })
        response = client.post("/api/remote/peanut/actions", json={
            "operation": "check_config", "expected_revision": 1,
        })
        assert response.status_code == 202
        assert response.json()["job_id"] == original["job_id"]


@pytest.mark.parametrize("operation,fields", [
    ("reset_local_config", {"confirmed_connection_loss": True}),
    ("reset_account_config", {"confirmed_connection_loss": True, "confirmed_account_unbind": True}),
    ("set_autostart", {"enabled": False}),
])
def test_reset_api_permissions_csrf_keys_and_validation(config, fake_auth, controller, operation, fields):
    app = create_app(replace(config, remote_admin_users=(TEST_USER,)), authenticator=fake_auth)
    app.state.peanut = LocalClient(controller)
    body = {"operation": operation, "expected_revision": 1, **fields}
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        assert client.post("/api/remote/peanut/actions", json=body).status_code == 401
        client.post("/auth/login", data={"username": TEST_USER, "password": TEST_PASSWORD})
        assert client.post("/api/remote/peanut/actions", json=body).status_code == 403
        client.headers["X-CSRF-Token"] = client.cookies["ch_csrf"]
        assert client.post("/api/remote/peanut/actions", json=body).status_code == 400
        client.headers["Idempotency-Key"] = "reset-api-once"
        app.state.config = replace(app.state.config, remote_admin_users=())
        assert client.post("/api/remote/peanut/actions", json=body).status_code == 403
        assert 'id="peanut-reset-options" hidden' in client.get("/").text
        app.state.config = replace(app.state.config, remote_admin_users=(TEST_USER,))
        if operation == "set_autostart":
            for invalid in ({}, {"enabled": 0}, {"enabled": None}, {"enabled": True},
                            {"enabled": False, "confirmed_autostart": "true"}):
                response = client.post("/api/remote/peanut/actions", json={
                    "operation": operation, "expected_revision": 1, **invalid,
                })
                assert response.status_code == 400
        else:
            assert client.post("/api/remote/peanut/actions", json={
                "operation": operation, "expected_revision": 1,
            }).status_code == 400
        if operation == "reset_account_config":
            assert client.post("/api/remote/peanut/actions", json=body).status_code == 409
        else:
            response = client.post("/api/remote/peanut/actions", json=body)
            assert response.status_code == 202
            assert finish(controller, response.json())["status"] == "succeeded"
            assert client.get("/api/status").status_code == 200
            assert client.post("/api/remote/peanut/actions", json=body).json()["job_id"] == response.json()["job_id"]


def test_public_reset_revokes_old_host_but_lan_can_read_persistent_result(config, fake_auth, controller):
    seed_reset(controller)
    with controller.db:
        controller.put("address", "https://portal.oray.com")
    controller.evidence = lambda: {"ingress_verified": True}
    key = config.session_secret_path.parent / "peanut-gateway.key"
    key.write_text("a" * 64)
    app = create_app(replace(config, remote_provider="peanut", remote_admin_users=(TEST_USER,)),
                     authenticator=fake_auth)
    app.state.peanut = LocalClient(controller)
    with TestClient(app, base_url="https://portal.oray.com", client=("127.0.0.1", 1)) as public:
        public.headers["X-Cloudhost-Transport"] = "a" * 64
        assert public.post("/auth/login", data={
            "username": TEST_USER, "password": TEST_PASSWORD,
        }).status_code == 204
        public.headers.update({
            "X-CSRF-Token": public.cookies["__Host-ch_csrf"], "Idempotency-Key": "public-reset-once",
        })
        response = public.post("/api/remote/peanut/actions", json={
            "operation": "reset_local_config", "expected_revision": controller.revision,
            "confirmed_connection_loss": True,
        })
        assert response.status_code == 202
        result = finish(controller, response.json())
        assert result["status"] == "succeeded"
        denied = public.get("/api/remote/peanut")
        assert denied.status_code == 403
        assert denied.json()["error"]["detail"] == "unregistered_public_host"
    with TestClient(app, client=("127.0.0.1", 2)) as lan:
        assert lan.post("/auth/login", data={
            "username": TEST_USER, "password": TEST_PASSWORD,
        }).status_code == 204
        assert lan.get("/api/remote/jobs/" + result["job_id"]).json() == result
        status = lan.get("/api/remote/peanut").json()
        assert status["address"] == {
            "https_url": "", "ssh": None, "source": "user_configured",
            "mapping": "not_automatically_verified",
        }
        summary = lan.get("/api/remote/status").json()
        assert summary["summary"]["enabled"] is False
        assert summary["summary"]["connection"] != "connected"
        assert summary["summary"]["https"] != "endpoint_ready"


@pytest.mark.parametrize("port", [22, 41042, 65535, 1])
def test_registered_ssh_mapping_persists_and_preserves_legacy_writes(controller, tmp_path, monkeypatch, port):
    monkeypatch.setattr("app.peanut_control.normalize_address", lambda url: url)
    https = "https://web.example.net"
    body = {"https_url": https, "ssh": {"host": "SSH.Example.NET", "port": port},
            "expected_revision": 1, "_idempotency_key": "address-first"}
    saved = controller.request("address", "admin", "sid", body)
    assert saved["ssh"] == {"host": "ssh.example.net", "port": port}
    assert controller.request("address", "admin", "sid", body) == saved
    assert controller.revision == 2
    saved = controller.request("address", "admin", "sid", {
        "https_url": "https://other.example.net", "expected_revision": 2,
        "_idempotency_key": "https-only",
    })
    assert saved["ssh"] == {"host": "ssh.example.net", "port": port}
    reopened = Controller(
        tmp_path / "state", runner=controller.runner, credentials=controller.credentials,
        installation=controller.installation, evidence=lambda: {},
        pause_file=controller.pause_file, boot_id="test-boot",
    )
    try:
        assert reopened.status()["address"]["ssh"] == saved["ssh"]
        assert reopened.get("address") == "https://other.example.net"
        assert all(argv[1] == "show" for argv in controller.test_calls)
        cleared = reopened.request("address", "admin", "sid", {
            "https_url": https, "ssh": {"host": "", "port": 22},
            "expected_revision": 3, "_idempotency_key": "clear-ssh",
        })
        assert cleared["ssh"] == {"host": "", "port": 22}
    finally:
        reopened.close()


@pytest.mark.parametrize("ssh", [
    {"host": "https://ssh.example.net", "port": 22},
    {"host": "ssh.example.net/path", "port": 22},
    {"host": "ssh.example.net?q=1", "port": 22},
    {"host": "ssh.example.net:42", "port": 22},
    {"host": "ssh.example.net;touch /tmp/a", "port": 22},
    {"host": "-oProxyCommand=bad", "port": 22},
    {"host": "127.0.0.1", "port": 22},
    {"host": "192.168.1.2", "port": 22},
    {"host": "localhost", "port": 22},
    {"host": " ssh.example.net", "port": 22},
    {"host": "", "port": 41042},
    {"host": "ssh.example.net", "port": 0},
    {"host": "ssh.example.net", "port": 65536},
    {"host": "ssh.example.net", "port": True},
    {"host": "ssh.example.net", "port": "22"},
    {"host": None, "port": 22}, {}, {"host": "ssh.example.net"},
])
def test_invalid_ssh_is_rejected_atomically(controller, monkeypatch, ssh):
    monkeypatch.setattr("app.peanut_control.normalize_address", lambda url: url)
    with controller.db:
        controller.put("address", "https://original.example.net")
    with pytest.raises(PeanutError, match="invalid_ssh_mapping"):
        controller.request("address", "admin", "sid", {
            "https_url": "https://replacement.example.net", "ssh": ssh,
            "expected_revision": 1, "_idempotency_key": "bad-mapping",
        })
    assert controller.get("address") == "https://original.example.net"
    assert controller.get("ssh_address") is None
    assert controller.revision == 1
    assert not controller.test_calls


def test_ssh_address_api_preserves_security_boundaries(config, fake_auth, controller, monkeypatch):
    monkeypatch.setattr("app.peanut_control.normalize_address", lambda url: url)
    app = create_app(replace(config, remote_admin_users=(TEST_USER,)), authenticator=fake_auth)
    app.state.peanut = LocalClient(controller)
    body = {"https_url": "https://web.example.net", "ssh": {"host": "ssh.example.net", "port": 41042},
            "expected_revision": 1}
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        assert client.put("/api/remote/peanut/address", json=body).status_code == 401
        client.post("/auth/login", data={"username": TEST_USER, "password": TEST_PASSWORD})
        assert client.put("/api/remote/peanut/address", json=body).status_code == 403
        client.headers.update({"X-CSRF-Token": client.cookies["ch_csrf"], "Idempotency-Key": "ssh-api-save"})
        assert client.put("/api/remote/peanut/address", json=body, headers={
            "Origin": "https://evil.org",
        }).status_code == 403
        response = client.put("/api/remote/peanut/address", json=body)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["ssh"] == body["ssh"]
        assert client.put("/api/remote/peanut/address", json=body).json() == response.json()
        client.headers["Idempotency-Key"] = "ssh-api-stale"
        assert client.put("/api/remote/peanut/address", json=body).status_code == 409
        body["expected_revision"] = 2
        body["ssh"]["port"] = "22"
        assert client.put("/api/remote/peanut/address", json=body).status_code == 400
        assert controller.revision == 2
        assert not controller.test_calls


def test_ssh_hostname_normalization_without_network():
    assert normalize_ssh_address({"host": "SSH.Example.NET", "port": 22}) == {
        "host": "ssh.example.net", "port": 22,
    }
    assert normalize_ssh_address({"host": "", "port": 22}) == {"host": "", "port": 22}


def test_invalid_https_cannot_partially_save_ssh(controller, monkeypatch):
    def reject(url):
        raise PeanutError("unsafe_address", code="E5001")

    monkeypatch.setattr("app.peanut_control.normalize_address", reject)
    with pytest.raises(PeanutError, match="unsafe_address"):
        controller.request("address", "admin", "sid", {
            "https_url": "http://invalid", "ssh": {"host": "ssh.example.net", "port": 22},
            "expected_revision": 1, "_idempotency_key": "invalid-https",
        })
    assert controller.get("ssh_address") is None
    assert controller.get("address") is None
    assert controller.revision == 1


def test_ssh_registration_requires_remote_admin(config, fake_auth, controller):
    app = create_app(replace(config, remote_admin_users=()), authenticator=fake_auth)
    app.state.peanut = LocalClient(controller)
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        client.post("/auth/login", data={"username": TEST_USER, "password": TEST_PASSWORD})
        client.headers.update({"X-CSRF-Token": client.cookies["ch_csrf"], "Idempotency-Key": "not-admin"})
        assert client.put("/api/remote/peanut/address", json={
            "https_url": "https://web.example.net", "ssh": {"host": "ssh.example.net", "port": 22},
            "expected_revision": 1,
        }).status_code == 403
        assert not app.state.peanut.calls
        assert controller.get("ssh_address") is None


def test_public_admin_policy_does_not_expand_other_providers():
    for provider in Provider:
        decision = evaluate_policy(Role.ADMIN, Ingress.PUBLIC, Capability.START, provider=provider)
        assert decision.allowed is (provider is Provider.PEANUT)
        assert not evaluate_policy(Role.USER, Ingress.PUBLIC, Capability.START, provider=provider).allowed


def test_public_cookie_isolation_and_spoofing(config, fake_auth, controller):
    key = config.session_secret_path.parent / "peanut-gateway.key"
    key.write_text("a" * 64)
    app = create_app(replace(config, remote_admin_users=(TEST_USER,)), authenticator=fake_auth)
    local = LocalClient(controller)
    original_call = local.call
    local.call = lambda action, **kw: (
        {"https_url": "https://portal.oray.com", "ingress_verified": True}
        if action == "address_context" else original_call(action, **kw)
    )
    app.state.peanut = local
    with TestClient(app, base_url="https://portal.oray.com", client=("127.0.0.1", 1)) as client:
        data = {"username": TEST_USER, "password": TEST_PASSWORD}
        assert 'data-i18n="experience.boundaryPublic"' not in client.get("/").text
        assert client.post("/auth/login", data=data, headers={
            "X-Cloudhost-Transport": "forged",
        }).status_code == 403
        client.headers["X-Cloudhost-Transport"] = "a" * 64
        for path in ("/", "/login"):
            html = client.get(path).text
            assert 'data-i18n="experience.boundaryPublic"' in html
            assert 'data-i18n="login.brandSubPublic"' in html
            assert "LAN edition" not in html
            assert "局域网版" not in html
        assert client.post("/auth/login", data=data).status_code == 204
        dashboard = client.get("/").text
        assert 'data-i18n="experience.boundaryPublic"' in dashboard
        assert 'data-i18n="dashboard.footerPrefixPublic"' in dashboard
        assert "this connection is not encrypted" not in dashboard
        assert "HTTP port 80 only" not in dashboard
        sid = client.cookies["__Host-ch_session"]
        assert "ch_session" not in client.cookies
        assert client.get("/api/remote/peanut").status_code == 200
        assert client.post("/auth/login", data=data).status_code == 403
        assert client.post("/auth/logout", headers={
            "Origin": "https://evil.org", "X-CSRF-Token": client.cookies["__Host-ch_csrf"]
        }).status_code == 403
        client.headers.pop("X-Cloudhost-Transport")
        # Host/scheme/proxy claims alone do not select public copy or a public session.
        with TestClient(app, base_url="https://portal.oray.com") as lan:
            assert lan.post("/auth/login", data=data, headers={"X-Forwarded-Proto": "https"}).status_code == 204
            dashboard = lan.get("/").text
            assert 'data-i18n="experience.boundaryPublic"' not in dashboard
            assert 'data-i18n="dashboard.footerPrefix"' in dashboard
            lan.cookies.clear()
            login = lan.get("/login").text
            assert 'data-i18n="experience.boundary"' in login
            assert 'data-i18n="login.brandSub"' in login
            assert "局域网版" not in login and "LAN edition" not in login
            assert "this connection is not encrypted" in login
        client.cookies.set("ch_session", sid)
        assert client.get("/api/status").status_code == 401


def test_binding_sid_generation_cancel_and_expiry(controller):
    controller.evidence = lambda: {
        "rpc_isolated": True, "binding_verified": False,
        "expires_mode": "relative_seconds", "image_hosts": ["images.oray.com"],
    }
    controller.adapter.qr = lambda *args, **kwargs: (time.time() + 60, "image/png", b"official-fixture")
    finish(controller, submit(controller, "start"))
    assert controller.status()["capabilities"]["qr_binding"]
    assert controller.status()["binding"]["state"] == "unknown"
    assert not controller.status()["capabilities"]["management"]
    body = {"expected_revision": controller.revision, "_idempotency_key": "qr-create-key"}
    first = controller.request("binding_create", "admin", "sid-one", body)
    assert controller.request("binding_create", "admin", "sid-one", body) == first
    assert "test-device-secret" not in json.dumps(first)
    with pytest.raises(PeanutError, match="binding_not_found"):
        controller.request("binding_get", "admin", "sid-two", {"id": first["binding_id"]})
    image = controller.request("binding_image", "admin", "sid-one",
                               {"id": first["binding_id"], "generation": 1})
    assert image["mime"] == "image/png"
    with pytest.raises(PeanutError, match="generation_conflict"):
        controller.request("binding_image", "admin", "sid-one",
                           {"id": first["binding_id"], "generation": 2})
    controller.bindings[first["binding_id"]]["created_at"] -= 4
    refreshed = controller.request("binding_refresh", "admin", "sid-one", {
        "id": first["binding_id"], "generation": 1, "expected_revision": controller.revision,
        "_idempotency_key": "qr-refresh-key",
    })
    assert refreshed["generation"] == 2
    with pytest.raises(PeanutError, match="generation_conflict"):
        controller.request("binding_image", "admin", "sid-one",
                           {"id": first["binding_id"], "generation": 1})
    cancelled = controller.request("binding_cancel", "admin", "sid-one", {
        "id": first["binding_id"], "generation": 2, "expected_revision": controller.revision,
        "_idempotency_key": "qr-cancel-key",
    })
    assert cancelled["state"] == "cancelled"
    assert controller.bindings[first["binding_id"]]["image"] is None
    controller.bindings[first["binding_id"]]["deadline"] = time.time() - 1
    assert controller.binding("sid-one", first["binding_id"])["state"] == "expired"


def test_management_intent_single_use_and_never_stores_raw_url(controller):
    controller.evidence = lambda: {"rpc_isolated": True, "binding_verified": True}
    controller.adapter.rpc_transport = lambda method: {
        "result_code": 0, "data": {"account": "tester", "url": "https://b.oray.com/manage?secret=fixture"},
    }
    value = controller.request("management", "admin", "sid-one", {
        "expected_revision": 1, "_idempotency_key": "manager-key",
    })
    intent_id = value["redirect_path"].split("/")[-1]
    assert "secret" not in repr(controller.redirects)
    assert "secret" not in repr(controller.db.execute("SELECT response FROM requests").fetchall())
    with pytest.raises(PeanutError, match="intent_expired"):
        controller.request("management_redirect", "admin", "sid-two", {"id": intent_id})
    assert controller.request("management_redirect", "admin", "sid-one", {"id": intent_id})["url"].startswith("https://b.oray.com/")
    with pytest.raises(PeanutError, match="intent_expired"):
        controller.request("management_redirect", "admin", "sid-one", {"id": intent_id})


def test_challenge_requires_fresh_exact_response_and_saved_address(controller, monkeypatch):
    monkeypatch.setattr("app.peanut_control.normalize_address", lambda url: url)
    with controller.db:
        controller.put("address", "https://portal.oray.com")
    def transport(url, **kwargs):
        assert url == "https://portal.oray.com/.well-known/cloudhost-peanut-check"
        assert kwargs == {"limit": 4096}
        return 200, "text/plain", controller.request("challenge", "", "", {})["challenge"].encode()
    controller.transport = transport
    assert finish(controller, submit(controller, "verify_address"))["status"] == "succeeded"
    assert controller.get("verification")["scope"] == "tls_and_gateway_challenge_only"
    assert controller.get("verification")["revision"] == controller.revision
    assert controller.challenge is None
    controller.last_verify -= 31
    controller.transport = lambda *args, **kwargs: (200, "text/html", b"homepage")
    result = finish(controller, submit(controller, "verify_address"))
    assert result["status"] == "failed" and result["reason"] == "challenge_mismatch"
    assert controller.get("verification")["state"] == "failed"


def test_restart_marks_pending_job_recovery_required_without_replaying(controller):
    job = submit(controller, "check_config")
    finish(controller, job)
    value = controller.job(job["job_id"])
    value["status"] = "running"
    with controller.db:
        controller._save_job(value)
    directory = Path(controller.db.execute("PRAGMA database_list").fetchone()[2]).parent
    replacement = Controller(
        directory, runner=lambda *args, **kwargs: pytest.fail("must not replay on recovery"),
        credentials=controller.credentials, evidence=lambda: {},
        pause_file=controller.pause_file, boot_id="test-boot",
    )
    try:
        result = replacement.job(job["job_id"])
        assert result["status"] == "recovery_required"
        RemoteJob.from_dict(result)
    finally:
        replacement.close()


def test_websocket_origin_and_logout_revoke_connection(app, authed):
    from starlette.websockets import WebSocketDisconnect
    calls = []

    class Backend:
        def __init__(self):
            self.queue = asyncio.Queue()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def send(self, data):
            await self.queue.put(data)

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self.queue.get()

    def connector(url, **kwargs):
        calls.append(url)
        return Backend()

    app.state.terminal_connector = connector
    with pytest.raises(WebSocketDisconnect):
        with authed.websocket_connect("/term/ws", headers={"Origin": "https://evil.org"}):
            pass
    assert calls == []
    with authed.websocket_connect("/term/ws", headers={"Origin": "http://testserver"}) as ws:
        ws.send_text("hello")
        assert ws.receive_text() == "hello"
        assert authed.post("/auth/logout").status_code == 204
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    assert calls == ["ws://127.0.0.1:7681/term/ws"]


def test_existing_service_job_persists_failure(config):
    from app.services import ServiceManager
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "fixture systemd failure")
    directory = config.state_dir / "test-service-jobs"
    manager = ServiceManager(runner=runner, job_dir=directory)
    job_id = manager.restart("caddy", actor="tester")
    for _ in range(200):
        value = manager.job(job_id)
        if value["status"] == "failed":
            break
        time.sleep(.005)
    assert value["reason"] == "systemd_restart_failed"
    replacement = ServiceManager(runner=runner, job_dir=directory)
    assert replacement.job(job_id)["status"] == "failed"


def test_config_rotation_requires_explicit_retry(controller):
    finish(controller, submit(controller, "start"))
    controller.credentials = lambda: Credentials("123", "rotated-test-key", "rotated")
    assert controller.status()["app_config"]["state"] == "changed_pending_restart"
    assert finish(controller, submit(controller, "start"))["reason"] == "explicit_retry_required"
    assert controller.get("running_fingerprint") == "fingerprint"
    assert finish(controller, submit(controller, "retry", confirmed_connection_loss=True))["status"] == "succeeded"
    assert controller.get("running_fingerprint") == "rotated"


def test_stop_intent_is_persisted_before_worker(controller, monkeypatch):
    finish(controller, submit(controller, "enable_autostart"))
    pending = []
    monkeypatch.setattr(controller.pool, "submit", lambda *args: pending.append(args))
    job = submit(controller, "disable_autostart", confirmed_connection_loss=True)
    assert controller.job(job["job_id"])["status"] == "queued"
    assert controller.paused()
    assert controller.get("enabled") is False
    assert pending


def test_transport_submission_waits_for_response_activation(controller):
    job = controller.request("submit", "admin", "sid-one", {
        "key": "deferred-start-key",
        "request": {"operation": "start", "expected_revision": controller.revision},
    })
    assert controller.job(job["job_id"])["status"] == "queued"
    assert not any(argv[1] == "start" for argv in controller.test_calls)
    with pytest.raises(PeanutError, match="job_not_found"):
        controller.activate(job["job_id"], "different-user")
    controller.request("activate_job", "admin", "sid-one", {"id": job["job_id"]})
    assert finish(controller, job)["status"] == "succeeded"


def test_unbind_post_request_unknown_is_not_safe_to_repeat(controller):
    controller.evidence = lambda: {"rpc_isolated": True, "unbinding_verified": True}
    def rpc(method):
        if method == "getsn":
            return {"device_sn": "test-sn", "device_sn_pwd": "test-secret"}
        raise PeanutError("rpc_unavailable")
    controller.adapter.rpc = rpc
    controller.adapter.unbind = lambda *args: None
    value = finish(controller, submit(controller, "unbind", confirmed_connection_loss=True,
                                     confirmed_account_unbind=True))
    assert value["status"] == "recovery_required"
    assert value["reason"] == "unbind_outcome_unknown"


def test_fixed_gateway_render_contract(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    from install_peanut import gateway_values
    from render_config import DEFAULT_CONFIG, render_template
    config = {**DEFAULT_CONFIG, "system_home": "/home/tester",
              "cloud_root": "/home/tester/cloud", "site_dir": "/home/tester/cloud/www"}
    target = tmp_path / "Caddyfile"
    render_template(Path(__file__).resolve().parents[1] / "deploy" / "Caddyfile.tmpl",
                    target, gateway_values(config, "tester", "a" * 64))
    rendered = target.read_text()
    assert "bind 127.0.0.1" in rendered and ":18080" in rendered
    assert "X-Cloudhost-Transport" in rendered and "a" * 64 in rendered
    assert "localhost:7681" not in rendered
    assert "/auth/verify" in rendered and "/auth/ingress" in rendered
    assert "127.0.0.1:8080" in rendered
    assert "HTTPS required" not in rendered
    assert "request_header -Forwarded" in rendered
    assert "request_header -X-Forwarded-Proto" in rendered
    assert "uri /auth/ingress" in rendered


def test_deployment_credentials_are_private_and_never_overwritten(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    from install_peanut import install_app_credentials

    directory = tmp_path / "credential-target"
    directory.mkdir()
    directory.chmod(0o750)
    path = directory / "app.json"
    uid, gid = os.geteuid(), os.getegid()
    assert install_app_credentials(
        path, "12345", "test-only-not-a-real-key", uid=uid, gid=gid
    )
    info = path.stat()
    assert info.st_uid == uid and info.st_gid == gid
    assert info.st_mode & 0o777 == 0o640
    assert read_credentials(path, uid=uid, gid=gid).app_id == "12345"
    original = path.read_bytes()
    assert not install_app_credentials(path, "67890", "another-test-key", uid=uid, gid=gid)
    assert path.read_bytes() == original

    invalid = directory / "invalid.json"
    invalid.write_text('{"app_id":"not-an-id","app_key":"test-only"}')
    invalid.chmod(0o640)
    before = invalid.read_bytes()
    with pytest.raises(ValueError, match="refusing to replace"):
        install_app_credentials(invalid, "67890", "another-test-key", uid=uid, gid=gid)
    assert invalid.read_bytes() == before


def test_packaging_excludes_credentials_identity_logs_and_links(tmp_path, monkeypatch, capsys):
    import importlib.util
    import sys
    import tarfile
    script = Path(__file__).resolve().parents[3] / "pack_cloudhost.py"
    spec = importlib.util.spec_from_file_location("peanut_pack_test", script)
    pack = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pack)
    source = tmp_path / "demos" / "35-cloudhost"
    source.mkdir(parents=True)
    for name in ("app.json", "adapter.json", "phtunnel.json", "control.sqlite3",
                 "control.sqlite3-wal", "control.sqlite3-shm", "phtunnel.log.1", "peanut-gateway.key"):
        (source / name).write_text("private-fixture")
    (source / "safe.txt").write_text("safe")
    (source / "link").symlink_to(source / "safe.txt")
    (source / "cloudhost-peanut").mkdir()
    (source / "cloudhost-peanut" / "identity").write_text("private-fixture")
    (source / "peanut-app.json.example").write_text('{"app_id":"placeholder"}')
    local_credentials = source / "peanut-app.local.json"
    local_credentials.write_text('{"app_id":"12345","app_key":"private-test-key"}')
    local_credentials.chmod(0o600)
    monkeypatch.chdir(tmp_path)
    archive_path = tmp_path / "source.tar"
    monkeypatch.setattr(sys, "argv", ["pack_cloudhost.py", str(archive_path)])
    assert pack.main() == 0
    with tarfile.open(archive_path) as archive:
        assert set(archive.getnames()) == {
            "35-cloudhost/safe.txt", "35-cloudhost/peanut-app.json.example",
        }
        assert all("private-test-key" not in archive.extractfile(name).read().decode()
                   for name in archive.getnames())
    assert not (tmp_path / "peanut-app.deploy.json").exists()
    monkeypatch.setattr(sys, "argv", [
        "pack_cloudhost.py", str(archive_path), "--include-private-peanut-credentials",
    ])
    assert pack.main() == 0
    with tarfile.open(archive_path) as archive:
        assert all("private-test-key" not in archive.extractfile(name).read().decode()
                   for name in archive.getnames())
    sidecar = tmp_path / "peanut-app.deploy.json"
    assert sidecar.read_text() == local_credentials.read_text()
    assert sidecar.stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr(sys, "argv", ["pack_cloudhost.py", str(archive_path)])
    assert pack.main() == 1
    assert "private sidecar already exists" in capsys.readouterr().err
