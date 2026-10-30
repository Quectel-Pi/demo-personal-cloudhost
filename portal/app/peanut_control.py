"""Dedicated fixed-operation controller. Root owns persistence, not the portal.

The UDS authenticates the portal UID, not the end user. The portal is trusted to
enforce its session/admin/CSRF boundary (PRD 12.3).
"""

from __future__ import annotations

import base64
import grp
import hashlib
import json
import logging
import os
import pwd
import secrets
import socket
import socketserver
import sqlite3
import stat
import struct
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .errors import MESSAGES
from .peanut_credentials import (
    SDK_SHA256, SDK_VERSION, PeanutError, inspect_sdk, read_credentials,
)
from .peanut_protocol import OfficialAdapter, https_request, normalize_address, normalize_ssh_address
from .remote_jobs import JobError, Operation, RemoteJob, RollbackState
from .remote_status import Provider, TaskState

SOCKET_PATH = "/run/cloudhost-peanut/control.sock"
STATE_DIR = Path("/var/lib/cloudhost-peanut-control")
ADAPTER_FILE = Path("/etc/cloudhost-peanut/adapter.json")
PAUSE_FILE = Path("/run/cloudhost-peanut/paused")
SDK_UNIT = "cloudhost-peanut.service"
logger = logging.getLogger("cloudhost.peanut")


def stamp(value: float | None = None) -> str:
    return datetime.fromtimestamp(value or time.time(), timezone.utc).isoformat()


def adapter_evidence(path: Path = ADAPTER_FILE) -> dict:
    """No shipped evidence enables an untested vendor capability."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if info.st_uid != 0 or info.st_mode & 0o022 or not stat.S_ISREG(info.st_mode):
                raise PeanutError("unsafe_adapter_evidence", code="E5004")
            raw = stream.read(8193)
        value = json.loads(raw)
        if (
            len(raw) > 8192 or not isinstance(value, dict)
            or value.get("sdk_sha256") != SDK_SHA256
            or not isinstance(value.get("evidence"), str) or not value["evidence"].strip()
            or any(type(value.get(k, False)) is not bool
                   for k in ("rpc_isolated", "binding_verified", "unbinding_verified", "ingress_verified"))
        ):
            raise PeanutError("invalid_adapter_evidence", code="E5004")
        hosts = value.get("image_hosts", [])
        if not isinstance(hosts, list) or not all(
            isinstance(host, str) and "." in host and "*" not in host and "/" not in host
            for host in hosts
        ):
            raise PeanutError("invalid_adapter_evidence", code="E5004")
        return value
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise PeanutError("invalid_adapter_evidence", code="E5004") from exc


class Controller:
    def __init__(
        self, directory: Path = STATE_DIR, *, runner=subprocess.run,
        credentials=read_credentials, installation=inspect_sdk,
        adapter: OfficialAdapter | None = None, evidence=adapter_evidence,
        pause_file: Path = PAUSE_FILE, boot_id: str | None = None,
        transport=https_request,
    ) -> None:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(directory / "control.sqlite3", check_same_thread=False)
        os.chmod(directory / "control.sqlite3", 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, actor TEXT NOT NULL, operation TEXT NOT NULL,
                idem TEXT NOT NULL, digest TEXT NOT NULL, result TEXT NOT NULL,
                UNIQUE(actor, operation, idem));
            CREATE TABLE IF NOT EXISTS requests (
                actor TEXT, sid TEXT, action TEXT, idem TEXT, digest TEXT, response TEXT,
                PRIMARY KEY(actor,sid,action,idem));
        """)
        self.runner, self.credentials, self.installation = runner, credentials, installation
        self.adapter = adapter or OfficialAdapter()
        self.evidence = evidence
        self.transport = transport
        self.pause_file = pause_file
        self.boot_id = boot_id or Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="peanut")
        self.busy = False
        self.bindings: dict[str, dict] = {}
        self.redirects: dict[str, dict] = {}
        self.challenge: tuple[str, float] | None = None
        self.last_probe = 0.0
        self.probe_cache: dict = {}
        self.binding_candidate: tuple[str, str, float] | None = None
        self.last_verify = 0.0
        self.process_instance: str | None = None
        self.identity_sn: str | None = None
        self.started_jobs: set[str] = set()
        self._recover()

    def close(self) -> None:
        self.pool.shutdown(wait=True)
        self.db.close()

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))

    @property
    def revision(self) -> int:
        return self.get("revision", 1)

    def _recover(self) -> None:
        with self.lock, self.db:
            for job_id, raw in self.db.execute("SELECT id,result FROM jobs").fetchall():
                value = json.loads(raw)
                if value["status"] in ("queued", "running"):
                    value.update(
                        status="recovery_required", reason="controller_interrupted",
                        next_action="recover_reset" if self.get("reset_progress") else "check_state",
                        rollback="recovery_required",
                    )
                    self.db.execute("UPDATE jobs SET result=? WHERE id=?", (json.dumps(value), job_id))
            self.put("verification", None)

    def paused(self) -> bool:
        try:
            return self.pause_file.read_text().strip() == self.boot_id
        except FileNotFoundError:
            return False

    def restore(self) -> None:
        """Called only by the installed controller at boot, never at portal startup."""
        with self.lock, self.db:
            if self.get("enabled", False) and not self.paused():
                try:
                    credentials = self.credentials()
                    if self.installation() != "installed":
                        raise PeanutError("sdk_incompatible")
                    if self.systemctl("show").get("ActiveState") == "active":
                        return
                    self.systemctl("start")
                    self.put("running_fingerprint", credentials.fingerprint)
                except PeanutError as exc:
                    logger.error("autostart failed: %s", exc.reason)

    def systemctl(self, operation: str) -> dict[str, str]:
        if operation not in {"start", "stop", "restart", "reset-failed", "show"}:
            raise PeanutError("unsupported_operation")
        argv = ["/usr/bin/systemctl", operation, SDK_UNIT]
        if operation == "show":
            argv += ["--property=ActiveState,MainPID,NRestarts,ExecMainStatus", "--no-pager"]
        try:
            result = self.runner(
                argv, capture_output=True, text=True,
                timeout=35 if operation in {"start", "restart"} else 15, check=False,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PeanutError("systemd_unavailable") from exc
        if result.returncode:
            raise PeanutError("systemd_failed")
        return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)

    def invalidate(self) -> None:
        self.put("verification", None)
        self.put("external_confirmation", None)
        self.last_probe = 0
        self.binding_candidate = None
        self.bindings.clear()
        self.redirects.clear()
        self.challenge = None
        self.probe_cache = {}

    @staticmethod
    def layer(state: str, source: str, *, observed: str | None, reason: str | None = None) -> dict:
        return dict(state=state, source=source, observed_at=observed, stale=observed is None, reason=reason)

    def status(self) -> dict:
        with self.lock, self.db:
            now = stamp()
            try:
                credential = self.credentials()
                state = "valid"
                previous = self.get("running_fingerprint")
                if previous and previous != credential.fingerprint:
                    state = "changed_pending_restart"
            except PeanutError as exc:
                state = exc.reason
            installation = self.installation()
            try:
                props = self.systemctl("show")
                instance = props.get("MainPID")
                if self.process_instance is not None and instance != self.process_instance:
                    self.invalidate()
                self.process_instance = instance
                daemon = {
                    "active": "running", "activating": "starting", "inactive": "stopped",
                    "failed": "failed", "deactivating": "stopping",
                }.get(props.get("ActiveState"), "unknown")
                daemon_layer = self.layer(daemon, "systemd", observed=now)
                daemon_layer["restarts"] = props.get("NRestarts")
            except PeanutError as exc:
                daemon = "unknown"
                daemon_layer = self.layer("unknown", "systemd", observed=None, reason=exc.reason)
            try:
                evidence = self.evidence()
                gate_reason = "adapter_not_verified"
            except PeanutError as exc:
                evidence = {}
                gate_reason = exc.reason
            if daemon != "running" and state == "changed_pending_restart":
                state = "valid"
            if daemon == "running" and evidence.get("rpc_isolated"):
                if time.monotonic() - self.last_probe >= 5:
                    self.probe_cache = self._sample(evidence)
                    self.last_probe = time.monotonic()
            else:
                self.probe_cache = {}
            unknown = self.layer("unknown", "sdk_rpc", observed=None, reason=gate_reason)
            verification = self.get("verification")
            if daemon != "running":
                verification = None
            if verification and time.time() - verification["timestamp"] > 300:
                verification = {**verification, "state": "stale"}
            return {
                "schema_version": 1, "provider": "peanut", "revision": self.revision,
                "generated_at": now,
                "app_config": self.layer(state, "credential_file", observed=now),
                "installation": {
                    **self.layer(installation, "pinned_sha256_elf", observed=now),
                    "version": SDK_VERSION if installation == "installed" else None,
                    "runtime_dependencies": "not_verified",
                },
                "daemon": daemon_layer,
                "device": self.probe_cache.get("device", unknown),
                "binding": self.probe_cache.get("binding", unknown),
                "intent": {"enabled": self.get("enabled", False), "paused_this_boot": self.paused()},
                "address": {"https_url": self.get("address", ""), "ssh": self.get("ssh_address"),
                            "source": "user_configured",
                            "mapping": "not_automatically_verified"},
                "https": self.layer(
                    verification["state"] if verification else "unverified",
                    "board_or_lan_challenge", observed=verification["observed_at"] if verification else None,
                ),
                "verification": verification,
                "last_job": self._last_job(),
                "reset_pending": self.get("reset_progress") is not None,
                "external_confirmation": self.get("external_confirmation"),
                "capabilities": {
                    "qr_binding": bool(evidence.get("rpc_isolated")
                                       and evidence.get("image_hosts") and evidence.get("expires_mode")
                                       in {"relative_seconds", "unix_seconds"}),
                    "management": bool(evidence.get("rpc_isolated") and evidence.get("binding_verified")),
                    "unbind": bool(evidence.get("rpc_isolated") and evidence.get("unbinding_verified")),
                    "public_ingress": bool(evidence.get("ingress_verified")),
                    "password_login": False, "mapping_crud": False,
                    "unavailable_reason": gate_reason,
                },
            }

    def _last_job(self) -> dict | None:
        row = self.db.execute("SELECT result FROM jobs ORDER BY rowid DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None

    def _sample(self, evidence: dict) -> dict:
        now = stamp()
        try:
            device = self.adapter.rpc("getsn")
            if self.identity_sn is not None and self.identity_sn != device["device_sn"]:
                self.invalidate()
            self.identity_sn = device["device_sn"]
            device_state = {0: "offline", 1: "online", 2: "logging_in", 3: "retrying"}.get(device["status"], "unknown")
            if device_state != "online":
                self.put("verification", None)
            binding = self.layer("unknown", "getmgrurl", observed=None, reason="binding_unverified")
            if evidence.get("binding_verified"):
                manager = self.adapter.rpc("getmgrurl")
                if not manager["url"] and not manager["account"]:
                    binding = self.layer("unbound", "getmgrurl", observed=now)
                    self.binding_candidate = None
                elif manager["url"] and manager["account"] and device["device_sn"]:
                    candidate = (device["device_sn"], manager["account"])
                    old = self.binding_candidate
                    bound = old is not None and old[:2] == candidate and time.monotonic() - old[2] >= 3
                    if old is None or old[:2] != candidate:
                        self.binding_candidate = (*candidate, time.monotonic())
                    binding = self.layer("bound" if bound else "unknown", "getmgrurl_two_samples", observed=now)
                    if bound:
                        binding["account"] = manager["account"][:1] + "***"
                else:
                    self.binding_candidate = None
                    binding = self.layer("unknown", "getmgrurl", observed=now, reason="contradictory_binding")
            return {
                "device": self.layer(device_state, "getsn", observed=now),
                "binding": binding,
            }
        except PeanutError as exc:
            self.binding_candidate = None
            self.put("verification", None)
            return {
                name: self.layer("unknown", "sdk_rpc", observed=None, reason=exc.reason)
                for name in ("device", "binding")
            }

    def check_revision(self, expected: object) -> None:
        if type(expected) is not int or expected != self.revision:
            raise PeanutError("revision_conflict", code="E5005")

    def submit(self, actor: str, key: str, body: dict, *, defer: bool = False) -> dict:
        allowed = {"operation", "expected_revision", "confirmed_connection_loss",
                   "confirmed_account_unbind", "enabled", "confirmed_autostart"}
        if set(body) - allowed or not {"operation", "expected_revision"} <= set(body):
            raise PeanutError("invalid_request", code="E5001")
        try:
            operation = Operation(body["operation"])
        except (ValueError, TypeError) as exc:
            raise PeanutError("unsupported_operation", code="E5001") from exc
        if operation not in {
            Operation.CHECK_CONFIG, Operation.START, Operation.STOP, Operation.RETRY,
            Operation.ENABLE_AUTOSTART, Operation.DISABLE_AUTOSTART, Operation.UNBIND,
            Operation.VERIFY_ADDRESS,
            Operation.SET_AUTOSTART, Operation.RESET_LOCAL_CONFIG,
            Operation.RESET_ACCOUNT_CONFIG, Operation.RECOVER_RESET,
        }:
            raise PeanutError("unsupported_operation", code="E5001")
        for name in ("confirmed_connection_loss", "confirmed_account_unbind", "confirmed_autostart"):
            if name in body and type(body[name]) is not bool:
                raise PeanutError("invalid_confirmation", code="E5001")
        if operation is Operation.SET_AUTOSTART:
            if type(body.get("enabled")) is not bool:
                raise PeanutError("invalid_autostart", code="E5001")
            if body["enabled"] and body.get("confirmed_autostart") is not True:
                raise PeanutError("autostart_confirmation_required", code="E5001")
        elif {"enabled", "confirmed_autostart"} & set(body):
            raise PeanutError("invalid_request", code="E5001")
        resets = {Operation.RESET_LOCAL_CONFIG, Operation.RESET_ACCOUNT_CONFIG}
        if operation in {Operation.STOP, Operation.RETRY, Operation.DISABLE_AUTOSTART,
                         Operation.UNBIND, Operation.RECOVER_RESET} | resets:
            if body.get("confirmed_connection_loss") is not True:
                raise PeanutError("connection_loss_confirmation_required", code="E5001")
        if operation in {Operation.UNBIND, Operation.RESET_ACCOUNT_CONFIG} and body.get("confirmed_account_unbind") is not True:
            raise PeanutError("account_confirmation_required", code="E5001")
        if not isinstance(key, str) or not 8 <= len(key) <= 128 or not key.isascii() or not key.isprintable():
            raise PeanutError("idempotency_key_required", code="E5001")
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        with self.lock, self.db:
            row = self.db.execute(
                "SELECT digest,result FROM jobs WHERE actor=? AND idem=?",
                (actor, key),
            ).fetchone()
            if row:
                if row[0] != digest:
                    raise PeanutError("idempotency_conflict", code="E5005")
                return json.loads(row[1])
            self.check_revision(body["expected_revision"])
            if self.busy:
                raise PeanutError("operation_in_progress", code="E5005")
            pending = self.get("reset_progress")
            if pending and operation not in {Operation.CHECK_CONFIG, Operation.RECOVER_RESET}:
                raise PeanutError("reset_recovery_required", code="E5005")
            if operation is Operation.RECOVER_RESET and not pending:
                raise PeanutError("no_reset_to_recover", code="E5005")
            if operation is Operation.UNBIND and not self.status()["capabilities"]["unbind"]:
                raise PeanutError("unbinding_unverified", code="E5004")
            if operation is Operation.RESET_ACCOUNT_CONFIG:
                status = self.status()
                if not status["capabilities"]["unbind"] or not self.evidence().get("binding_verified"):
                    raise PeanutError("unbinding_unverified", code="E5004")
                if status["daemon"]["state"] != "running":
                    raise PeanutError("sdk_not_running", code="E5005")
                if status["binding"]["stale"] or status["binding"]["state"] not in {"bound", "unbound"}:
                    raise PeanutError("binding_not_confirmed", code="E5005")
            if operation is Operation.VERIFY_ADDRESS:
                if time.monotonic() - self.last_verify < 30:
                    raise PeanutError("probe_rate_limited", code="E5005")
                self.last_verify = time.monotonic()
            job = RemoteJob(
                secrets.token_urlsafe(24), Provider.PEANUT, operation, TaskState.QUEUED,
                "accepted", None, "poll_job", self.revision, RollbackState.NOT_REQUIRED,
            )
            self.db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?)",
                            (job.job_id, actor, operation.value, key, digest, json.dumps(job.to_dict())))
            if operation in resets:
                self.put("reset_progress", {
                    "operation": operation.value, "unbind_attempted": False,
                    "unbound_confirmed": False, "phase": "accepted",
                    "device_sn": self.identity_sn if operation is Operation.RESET_ACCOUNT_CONFIG else None,
                    "account": self.binding_candidate[1] if (
                        operation is Operation.RESET_ACCOUNT_CONFIG and self.binding_candidate
                    ) else None,
                })
            if operation is not Operation.CHECK_CONFIG:
                self.put("revision", self.revision + 1)
                if operation is not Operation.SET_AUTOSTART:
                    self.invalidate()
            if operation in {Operation.STOP, Operation.DISABLE_AUTOSTART} | resets:
                self.pause_file.parent.mkdir(parents=True, exist_ok=True)
                self.pause_file.write_text(self.boot_id)
                if operation is Operation.DISABLE_AUTOSTART or operation in resets:
                    self.put("enabled", False)
            if operation is Operation.SET_AUTOSTART:
                self.put("enabled", body["enabled"])
            self.busy = True
        if not defer:
            self.activate(job.job_id, actor)
        return job.to_dict()

    def activate(self, job_id: str, actor: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT actor,result FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row[0] != actor:
                raise PeanutError("job_not_found", code="E9001")
            value = json.loads(row[1])
            if value["status"] == "queued" and job_id not in self.started_jobs:
                self.started_jobs.add(job_id)
                self.pool.submit(self._execute, RemoteJob.from_dict(value), actor)
            return value

    def job(self, job_id: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT result FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise PeanutError("job_not_found", code="E9001")
            return json.loads(row[0])

    def _save_job(self, value: dict) -> None:
        self.db.execute("UPDATE jobs SET result=? WHERE id=?", (json.dumps(value), value["job_id"]))

    def _execute(self, job: RemoteJob, actor: str) -> None:
        value = job.to_dict()
        try:
            with self.lock, self.db:
                value.update(status="running", phase="executing")
                self._save_job(value)
            operation = job.operation
            if operation in {Operation.RESET_LOCAL_CONFIG, Operation.RESET_ACCOUNT_CONFIG,
                             Operation.RECOVER_RESET}:
                self._reset(value)
                return
            elif operation is Operation.SET_AUTOSTART:
                pass
            elif operation is Operation.CHECK_CONFIG:
                self.credentials()
            elif operation in {Operation.START, Operation.RETRY, Operation.ENABLE_AUTOSTART}:
                credentials = self.credentials()
                if self.installation() != "installed":
                    raise PeanutError("sdk_incompatible", code="E5001")
                if operation is Operation.ENABLE_AUTOSTART:
                    with self.lock, self.db:
                        self.put("enabled", True)
                else:
                    if operation is Operation.START:
                        props = self.systemctl("show")
                        if (props.get("ActiveState") == "active"
                                and self.get("running_fingerprint") != credentials.fingerprint):
                            raise PeanutError("explicit_retry_required", code="E5005")
                    self.pause_file.unlink(missing_ok=True)
                    if operation is Operation.RETRY:
                        self.systemctl("stop")
                        self.systemctl("reset-failed")
                    self.systemctl("start")
                    props = self.systemctl("show")
                    if props.get("ActiveState") != "active" or props.get("MainPID") in (None, "", "0"):
                        raise PeanutError("sdk_not_running")
                    with self.lock, self.db:
                        self.put("running_fingerprint", credentials.fingerprint)
            elif operation in {Operation.STOP, Operation.DISABLE_AUTOSTART}:
                self.pause_file.parent.mkdir(parents=True, exist_ok=True)
                self.pause_file.write_text(self.boot_id)
                with self.lock, self.db:
                    self.invalidate()
                    if operation is Operation.DISABLE_AUTOSTART:
                        self.put("enabled", False)
                self.systemctl("stop")
                if self.systemctl("show").get("ActiveState") not in {"inactive", "failed"}:
                    raise PeanutError("sdk_not_stopped")
            elif operation is Operation.UNBIND:
                device = self.adapter.rpc("getsn")
                if not device["device_sn"] or not device["device_sn_pwd"]:
                    raise PeanutError("device_identity_missing")
                try:
                    self.adapter.unbind(device["device_sn"], device["device_sn_pwd"])
                    manager = self.adapter.rpc("getmgrurl")
                    if manager["url"] or manager["account"]:
                        raise PeanutError("unbind_not_confirmed")
                except PeanutError:
                    value.update(status="recovery_required", reason="unbind_outcome_unknown",
                                 next_action="check_binding", rollback="recovery_required")
                    raise
            elif operation is Operation.VERIFY_ADDRESS:
                self._verify()
            with self.lock, self.db:
                value.update(status="succeeded", phase="completed", next_action="refresh_status",
                             config_revision=self.revision)
                self._save_job(value)
        except PeanutError as exc:
            with self.lock, self.db:
                if self.get("reset_progress") and job.operation in {
                    Operation.RESET_LOCAL_CONFIG, Operation.RESET_ACCOUNT_CONFIG, Operation.RECOVER_RESET,
                }:
                    value.update(status="recovery_required", reason=exc.reason,
                                 next_action="recover_reset", rollback="recovery_required")
                    if exc.reason == "official_rejected":
                        value.update(status="failed", error=JobError(
                            exc.code, MESSAGES[exc.code], None, exc.reason, False,
                        ).to_dict())
                elif value["status"] != "recovery_required":
                    error = JobError(exc.code, MESSAGES[exc.code], None, exc.reason, False)
                    value.update(status="failed", phase="failed", reason=exc.reason,
                                 next_action="check_state", error=error.to_dict())
                self._save_job(value)
        except Exception:
            logger.exception("controller job failed: %s", job.job_id)
            with self.lock, self.db:
                value.update(status="recovery_required",
                             phase=value["phase"] if self.get("reset_progress") else "interrupted",
                             reason="internal_error",
                             next_action="recover_reset" if self.get("reset_progress") else "check_state",
                             rollback="recovery_required")
                self._save_job(value)
        finally:
            with self.lock:
                self.busy = False
                self.started_jobs.discard(job.job_id)
            logger.info("actor=%s operation=%s job=%s result=%s", actor, job.operation.value,
                        job.job_id, value["status"])

    def _reset(self, value: dict) -> None:
        progress = self.get("reset_progress")
        if not progress:
            raise PeanutError("no_reset_to_recover", code="E5005")

        def phase(name: str) -> None:
            with self.lock, self.db:
                progress["phase"] = name
                self.put("reset_progress", progress)
                value["phase"] = name
                self._save_job(value)

        if progress["operation"] == "reset_account_config" and not progress["unbound_confirmed"]:
            phase("checking_binding")
            evidence = self.evidence()
            if not all(evidence.get(key) for key in ("rpc_isolated", "binding_verified", "unbinding_verified")):
                raise PeanutError("unbinding_unverified", code="E5004")
            if self.systemctl("show").get("ActiveState") != "active":
                raise PeanutError("sdk_not_running")
            device = self.adapter.rpc("getsn")
            if not device["device_sn"] or device["device_sn"] != progress["device_sn"]:
                raise PeanutError("device_identity_changed")
            manager = self.adapter.rpc("getmgrurl")
            if bool(manager["url"]) != bool(manager["account"]):
                raise PeanutError("binding_not_confirmed")
            if manager["url"]:
                if progress["unbind_attempted"]:
                    raise PeanutError("unbind_outcome_unknown")
                if manager["account"] != progress["account"]:
                    raise PeanutError("binding_changed")
                if not device["device_sn"] or not device["device_sn_pwd"]:
                    raise PeanutError("device_identity_missing")
                progress["unbind_attempted"] = True
                phase("unbinding")
                self.adapter.unbind(device["device_sn"], device["device_sn_pwd"])
                phase("confirming_unbound")
                for attempt in range(4):
                    manager = self.adapter.rpc("getmgrurl")
                    if not manager["url"] and not manager["account"]:
                        break
                    if attempt < 3:
                        time.sleep(1)
                else:
                    raise PeanutError("unbind_outcome_unknown")
            progress["unbound_confirmed"] = True
            phase("account_unbound")
        phase("stopping")
        self.systemctl("stop")
        props = self.systemctl("show")
        if props.get("ActiveState") not in {"inactive", "failed"} or props.get("MainPID") not in {"0", ""}:
            raise PeanutError("sdk_not_stopped")
        phase("clearing_local")
        with self.lock, self.db:
            self.put("address", "")
            self.put("ssh_address", None)
            self.put("running_fingerprint", None)
            self.invalidate()
            self.process_instance = None
            self.identity_sn = None
            self.put("revision", self.revision + 1)
            self.put("reset_progress", None)
            value.update(status="succeeded", phase="completed", reason=(
                "account_unbound_config_cleared" if progress["unbound_confirmed"]
                else "local_config_cleared_binding_retained"
            ), next_action="start_when_ready", config_revision=self.revision)
            self._save_job(value)

    def _verify(self) -> None:
        with self.lock:
            address = self.get("address", "")
            revision = self.revision
            self.challenge = (secrets.token_urlsafe(32), time.time() + 20)
            challenge = self.challenge[0]
        try:
            normalize_address(address)
            status, _, raw = self.transport(
                address + "/.well-known/cloudhost-peanut-check", limit=4096
            )
            if status != 200 or raw != challenge.encode():
                raise PeanutError("challenge_mismatch")
            result = "endpoint_ready"
        except PeanutError:
            with self.lock, self.db:
                self.put("verification", {
                    "state": "failed", "observed_at": stamp(), "timestamp": time.time(),
                    "network_context": "board_or_lan", "revision": revision,
                })
            raise
        finally:
            with self.lock:
                self.challenge = None
        with self.lock, self.db:
            self.put("verification", {
                "state": result, "observed_at": stamp(), "timestamp": time.time(),
                "network_context": "board_or_lan", "revision": revision,
                "scope": "tls_and_gateway_challenge_only",
            })

    def binding(self, sid: str, binding_id: str) -> dict:
        value = self.bindings.get(binding_id)
        if value is None or value["sid"] != sid:
            raise PeanutError("binding_not_found", code="E9001")
        if time.time() >= value["deadline"]:
            value.update(state="expired", image=None)
        return value

    @staticmethod
    def binding_view(binding_id: str, value: dict) -> dict:
        return {
            "binding_id": binding_id, "generation": value["generation"], "state": value["state"],
            "expires_at": stamp(value["deadline"]),
            "image_path": f"/api/remote/peanut/bindings/{binding_id}/image?generation={value['generation']}",
        }

    def request(self, action: str, actor: str, sid: str, body: dict) -> dict:
        writes = {"address", "external_confirmation", "binding_create", "binding_refresh",
                  "binding_cancel", "management", "clear_session"}
        if action not in writes:
            return self._request(action, actor, sid, body)
        body = dict(body)
        key = body.pop("_idempotency_key", "")
        if not isinstance(key, str) or not 8 <= len(key) <= 128 or not key.isascii() or not key.isprintable():
            raise PeanutError("idempotency_key_required", code="E5001")
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        identity = (actor, sid, action, key)
        with self.lock:
            row = self.db.execute(
                "SELECT digest,response FROM requests WHERE actor=? AND sid=? AND action=? AND idem=?",
                identity,
            ).fetchone()
            if row:
                if row[0] != digest:
                    raise PeanutError("idempotency_conflict", code="E5005")
                response = json.loads(row[1])
                if "error" in response:
                    raise PeanutError(response["error"]["reason"], code=response["error"]["code"])
                return response["data"]
            with self.db:
                self.db.execute("INSERT INTO requests VALUES (?,?,?,?,?,?)", (
                    *identity, digest, json.dumps({"error": {"reason": "request_interrupted_check_state",
                                                           "code": "E5005"}}),
                ))
            try:
                result = self._request(action, actor, sid, body)
                response = {"data": result}
            except PeanutError as exc:
                response = {"error": {"code": exc.code, "reason": exc.reason}}
                with self.db:
                    self.db.execute(
                        "UPDATE requests SET response=? WHERE actor=? AND sid=? AND action=? AND idem=?",
                        (json.dumps(response), *identity),
                    )
                raise
            with self.db:
                self.db.execute(
                    "UPDATE requests SET response=? WHERE actor=? AND sid=? AND action=? AND idem=?",
                    (json.dumps(response), *identity),
                )
            return result

    def _request(self, action: str, actor: str, sid: str, body: dict) -> dict:
        if action == "status":
            return self.status()
        if action == "submit":
            return self.submit(actor, body["key"], body["request"], defer=True)
        if action == "activate_job":
            return self.activate(body["id"], actor)
        if action == "job":
            return self.job(body["id"])
        if action == "challenge":
            with self.lock:
                if self.challenge and time.time() < self.challenge[1]:
                    return {"challenge": self.challenge[0]}
                raise PeanutError("challenge_expired", code="E9001")
        if action == "address_context":
            with self.lock:
                return {"https_url": self.get("address", ""),
                        "ingress_verified": bool(self.evidence().get("ingress_verified"))}
        with self.lock, self.db:
            if self.busy:
                raise PeanutError("operation_in_progress", code="E5005")
            if self.get("reset_progress") and action != "clear_session":
                raise PeanutError("reset_recovery_required", code="E5005")
            if action in {"address", "external_confirmation", "binding_create", "binding_refresh",
                          "binding_cancel", "management"}:
                self.check_revision(body.get("expected_revision"))
            if action == "address":
                ssh = normalize_ssh_address(body["ssh"]) if body.get("ssh") is not None else None
                address = normalize_address(body["https_url"])
                self.put("address", address)
                if ssh is not None:
                    self.put("ssh_address", ssh)
                self.invalidate()
                self.put("revision", self.revision + 1)
                return {"revision": self.revision, "https_url": self.get("address"),
                        "ssh": self.get("ssh_address"),
                        "verification": "unverified", "source": "user_configured"}
            if action == "external_confirmation":
                if body.get("confirmed") is not True or body.get("network_context") != "external_mobile":
                    raise PeanutError("invalid_confirmation", code="E5001")
                self.put("external_confirmation", {
                    "source": "operator_confirmed", "network_context": "external_mobile",
                    "observed_at": stamp(), "revision": self.revision, "actor": actor,
                })
                return self.get("external_confirmation")
            if action.startswith("binding_"):
                if action in {"binding_get", "binding_image", "binding_cancel", "binding_refresh"}:
                    value = self.binding(sid, body["id"])
                    binding_id = body["id"]
                    if action != "binding_get" and body.get("generation") != value["generation"]:
                        raise PeanutError("generation_conflict", code="E5005")
                    if action == "binding_cancel":
                        value.update(state="cancelled", image=None)
                    elif action == "binding_image":
                        if value["state"] != "waiting" or value["image"] is None:
                            raise PeanutError("binding_expired", code="E9001")
                        return {"mime": value["mime"], "image": base64.b64encode(value["image"]).decode()}
                    elif action == "binding_get" and value["state"] == "waiting":
                        binding_state = self.status()["binding"]["state"]
                        if binding_id not in self.bindings:
                            raise PeanutError("device_generation_changed", code="E5005")
                        if binding_state == "bound":
                            value.update(state="bound", image=None)
                    if action != "binding_refresh":
                        return self.binding_view(binding_id, value)
                if action in {"binding_create", "binding_refresh"}:
                    now = time.time()
                    self.bindings = {k: v for k, v in self.bindings.items() if v["deadline"] > now}
                    if len(self.bindings) >= 32:
                        raise PeanutError("binding_limit_reached", code="E5005")
                    if any(v["sid"] == sid and now - v.get("created_at", 0) < 3
                           for v in self.bindings.values()):
                        raise PeanutError("binding_rate_limited", code="E5005")
                    status = self.status()
                    if not status["capabilities"]["qr_binding"]:
                        raise PeanutError("qr_adapter_unverified", code="E5004")
                    if status["daemon"]["state"] != "running":
                        raise PeanutError("sdk_not_running")
                    for existing in self.bindings.values():
                        if existing["sid"] == sid:
                            existing.update(state="cancelled", image=None)
                    evidence = self.evidence()
                    device = self.adapter.rpc("getsn")
                    if not device["device_sn"] or not device["device_sn_pwd"]:
                        raise PeanutError("device_identity_missing")
                    deadline, mime, image = self.adapter.qr(
                        device["device_sn"], device["device_sn_pwd"],
                        expires_mode=evidence["expires_mode"], image_hosts=evidence["image_hosts"],
                    )
                    generation = value["generation"] + 1 if action == "binding_refresh" else 1
                    binding_id = body["id"] if action == "binding_refresh" else secrets.token_urlsafe(24)
                    self.bindings[binding_id] = {
                        "sid": sid, "generation": generation, "state": "waiting", "deadline": deadline,
                        "mime": mime, "image": image, "created_at": time.time(),
                    }
                    return self.binding_view(binding_id, self.bindings[binding_id])
                raise PeanutError("unsupported_operation", code="E5001")
            if action == "management":
                self.redirects = {k: v for k, v in self.redirects.items() if v["expires"] > time.time()}
                if len(self.redirects) >= 32:
                    raise PeanutError("intent_limit_reached", code="E5005")
                if not self.status()["capabilities"]["management"]:
                    raise PeanutError("management_unverified", code="E5004")
                manager = self.adapter.rpc("getmgrurl")
                if not manager["url"] or not manager["account"]:
                    raise PeanutError("binding_not_confirmed", code="E5004")
                intent = secrets.token_urlsafe(24)
                self.redirects[intent] = {"sid": sid, "expires": time.time() + 60}
                return {"redirect_path": "/api/remote/peanut/management-redirect/" + intent}
            if action == "management_redirect":
                intent = self.redirects.get(body["id"])
                if not intent or intent["sid"] != sid or intent["expires"] <= time.time():
                    raise PeanutError("intent_expired", code="E9001")
                del self.redirects[body["id"]]
                manager = self.adapter.rpc("getmgrurl")
                if not manager["url"] or not manager["account"]:
                    raise PeanutError("binding_not_confirmed", code="E5004")
                return {"url": manager["url"]}
            if action == "clear_session":
                self.bindings = {k: v for k, v in self.bindings.items() if v["sid"] != sid}
                self.redirects = {k: v for k, v in self.redirects.items() if v["sid"] != sid}
                return {"cleared": True}
            raise PeanutError("unsupported_operation", code="E5001")


def serve() -> None:
    portal_uid = pwd.getpwnam(os.environ["CLOUDHOST_PORTAL_USER"]).pw_uid
    portal_gid = grp.getgrnam(os.environ["CLOUDHOST_PORTAL_USER"]).gr_gid
    controller = Controller()

    class Handler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            self.request.settimeout(20)
            _, uid, _ = struct.unpack("3i", self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid not in {0, portal_uid}:
                return
            try:
                raw = self.rfile.readline(65537)
                if len(raw) > 65536:
                    raise PeanutError("request_too_large", code="E5001")
                request = json.loads(raw)
                if (
                    not isinstance(request, dict)
                    or set(request) != {"action", "actor", "sid", "body"}
                    or not all(isinstance(request[k], str) and len(request[k]) <= 256
                               for k in ("action", "actor", "sid"))
                    or not isinstance(request["body"], dict)
                ):
                    raise PeanutError("invalid_request", code="E5001")
                result = {"data": controller.request(**request)}
            except PeanutError as exc:
                result = {"error": {"code": exc.code, "reason": exc.reason}}
            except (ValueError, KeyError, TypeError):
                result = {"error": {"code": "E5001", "reason": "invalid_request"}}
            except Exception:
                logger.exception("controller request failed")
                result = {"error": {"code": "E9000", "reason": "internal_error"}}
            self.wfile.write(json.dumps(result).encode() + b"\n")

    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True

    Path(SOCKET_PATH).unlink(missing_ok=True)
    with Server(SOCKET_PATH, Handler) as server:
        os.chown(SOCKET_PATH, 0, portal_gid)
        os.chmod(SOCKET_PATH, 0o660)
        controller.restore()
        try:
            server.serve_forever()
        finally:
            controller.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    os.umask(0o077)
    serve()
