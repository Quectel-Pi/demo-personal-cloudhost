from dataclasses import replace
from datetime import datetime, timezone

import pytest

from app.remote_status import (
    AddressSource,
    AddressVerification,
    AdminMetadata,
    AuthorizationState,
    Capabilities,
    ClientTrust,
    DaemonState,
    EvidenceNetworkContext,
    HttpsState,
    InstallationState,
    ManagementIntent,
    NetworkState,
    PeanutBindingState,
    PeanutClientReport,
    PeanutPublicIpState,
    Provider,
    ReachabilityState,
    ReachabilityEvidence,
    RemoteStatus,
    RemoteAddress,
    TaskState,
    admin_status,
    public_summary,
)


ENUMS = [
    InstallationState,
    DaemonState,
    AuthorizationState,
    NetworkState,
    HttpsState,
    Provider,
    ReachabilityState,
    TaskState,
    AddressSource,
    AddressVerification,
    ClientTrust,
    EvidenceNetworkContext,
]
STATUS_ENUM_FIELDS = [
    ("installation", InstallationState),
    ("daemon", DaemonState),
    ("authorization", AuthorizationState),
    ("network", NetworkState),
    ("https", HttpsState),
    ("management_intent.active_provider", Provider),
    ("reachability", ReachabilityState),
    ("task", TaskState),
]


def evidence_payload() -> dict:
    return {
        "tested_at": "2026-10-22T03:59:00Z",
        "provider": "tailscale",
        "config_revision": 7,
        "network_context": "mobile_data",
        "certificate_sha256": "a1" * 32,
        "verifier": "external-tester",
        "stale": False,
    }


def status_payload() -> dict:
    return {
        "installation": "installed",
        "daemon": "running",
        "authorization": "authorized",
        "network": "connected",
        "https": "endpoint_ready",
        "management_intent": {
            "enabled": True,
            "active_provider": "tailscale",
            "paused_this_boot": False,
            "config_revision": 7,
        },
        "reachability": "verified",
        "task": "succeeded",
        "observed_at": "2026-10-22T04:00:00Z",
        "stale": False,
        "reachability_evidence": evidence_payload(),
    }


@pytest.mark.parametrize(
    "enum_type",
    ENUMS,
    ids=lambda enum_type: enum_type.__name__,
)
def test_every_enum_value_is_accepted_and_round_trips(enum_type):
    for expected in enum_type:
        assert enum_type.parse(expected.value) is expected


@pytest.mark.parametrize(
    ("field", "state"),
    [
        (field, state)
        for field, enum_type in STATUS_ENUM_FIELDS
        for state in enum_type
    ],
    ids=lambda case: str(case),
)
def test_every_enum_value_is_accepted_in_status_schema(field, state):
    payload = status_payload()
    if field == "management_intent.active_provider":
        payload["management_intent"]["active_provider"] = state.value
        if state is Provider.NONE:
            payload["reachability"] = "unverified"
            payload["reachability_evidence"] = None
        else:
            payload["reachability_evidence"]["provider"] = state.value
    else:
        payload[field] = state.value
        if (
            (field == "network" and state is not NetworkState.CONNECTED)
            or (field == "https" and state is not HttpsState.ENDPOINT_READY)
        ):
            payload["reachability"] = "unverified"

    status = RemoteStatus.from_dict(payload)

    if field == "management_intent.active_provider":
        assert status.management_intent.active_provider is state
    else:
        assert getattr(status, field) is state


@pytest.mark.parametrize(
    "enum_type",
    ENUMS,
    ids=lambda enum_type: enum_type.__name__,
)
def test_every_enum_rejects_unknown_values_and_non_strings(enum_type):
    with pytest.raises(ValueError):
        enum_type.parse("not-a-contract-value")
    with pytest.raises(ValueError):
        enum_type.parse(None)


@pytest.mark.parametrize(
    ("field", "enum_type"),
    [
        ("installation", InstallationState),
        ("daemon", DaemonState),
        ("authorization", AuthorizationState),
        ("network", NetworkState),
        ("https", HttpsState),
        ("reachability", ReachabilityState),
        ("task", TaskState),
    ],
)
def test_status_schema_rejects_unknown_layer_values(field, enum_type):
    payload = status_payload()
    payload[field] = "not-a-contract-value"
    with pytest.raises(ValueError):
        RemoteStatus.from_dict(payload)


def test_status_schema_rejects_unknown_provider():
    payload = status_payload()
    payload["management_intent"]["active_provider"] = "unknown-provider"
    with pytest.raises(ValueError):
        RemoteStatus.from_dict(payload)


@pytest.mark.parametrize("state", list(TaskState))
def test_every_task_state_is_accepted_by_status_schema(state):
    payload = status_payload()
    payload["task"] = state.value
    assert RemoteStatus.from_dict(payload).task is state


def test_timestamp_and_stale_are_required_and_serialized_explicitly():
    payload = status_payload()
    payload["observed_at"] = "2026-10-22T12:00:00+08:00"
    payload["stale"] = True
    payload["reachability"] = "stale"

    status = RemoteStatus.from_dict(payload)
    serialized = status.to_dict()

    assert serialized["observed_at"] == "2026-10-22T04:00:00Z"
    assert serialized["stale"] is True
    assert RemoteStatus.from_dict(serialized) == status

    for missing in ("observed_at", "stale"):
        incomplete = status_payload()
        del incomplete[missing]
        with pytest.raises(ValueError):
            RemoteStatus.from_dict(incomplete)


@pytest.mark.parametrize("timestamp", ["2026-10-22T04:00:00", "not-a-time", None])
def test_timestamp_requires_an_explicit_timezone_and_valid_iso_format(timestamp):
    payload = status_payload()
    payload["observed_at"] = timestamp
    with pytest.raises(ValueError):
        RemoteStatus.from_dict(payload)


@pytest.mark.parametrize("stale", ["false", 0, 1, None])
def test_stale_requires_a_boolean(stale):
    payload = status_payload()
    payload["stale"] = stale
    with pytest.raises(TypeError):
        RemoteStatus.from_dict(payload)


@pytest.mark.parametrize(
    "revision",
    [0, -1, True, 1.0, "1"],
)
def test_management_intent_revision_is_a_strict_positive_integer(revision):
    payload = status_payload()
    payload["management_intent"]["config_revision"] = revision
    with pytest.raises(ValueError):
        RemoteStatus.from_dict(payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("enabled", 1),
        ("paused_this_boot", "false"),
    ],
)
def test_management_intent_booleans_are_strict(field, value):
    payload = status_payload()
    payload["management_intent"][field] = value
    with pytest.raises(TypeError):
        RemoteStatus.from_dict(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update({"provider_detail": "vendor-secret"}),
        lambda payload: payload.update({"config_revision": 7}),
        lambda payload: payload.update({"details": {"network_id": "secret-id"}}),
        lambda payload: payload.update({"identity": "user@example.invalid"}),
        lambda payload: payload.update({"authorization_link": "https://secret.invalid"}),
    ],
    ids=("provider-detail", "revision", "details", "identity", "auth-link"),
)
def test_contract_rejects_unmodeled_provider_or_sensitive_fields(mutate):
    payload = status_payload()
    mutate(payload)
    with pytest.raises(ValueError):
        RemoteStatus.from_dict(payload)


def test_management_intent_schema_rejects_missing_and_unexpected_keys():
    payload = status_payload()
    del payload["management_intent"]["paused_this_boot"]
    with pytest.raises(ValueError):
        RemoteStatus.from_dict(payload)

    payload = status_payload()
    payload["management_intent"]["network_id"] = "secret-id"
    with pytest.raises(ValueError):
        RemoteStatus.from_dict(payload)


def test_management_intent_allows_unknown_boot_pause_state():
    payload = status_payload()
    payload["management_intent"]["paused_this_boot"] = None

    parsed = RemoteStatus.from_dict(payload)

    assert parsed.management_intent.paused_this_boot is None
    assert parsed.to_dict()["management_intent"]["paused_this_boot"] is None


def test_public_projection_has_only_approved_keys_and_no_private_details():
    payload = status_payload()
    payload["management_intent"]["active_provider"] = "zerotier"
    payload["reachability_evidence"]["provider"] = "zerotier"
    status = RemoteStatus.from_dict(payload)

    projected = public_summary(status)

    assert set(projected) == {"summary", "observed_at", "stale"}
    assert set(projected["summary"]) == {
        "active_provider",
        "enabled",
        "paused_this_boot",
        "connection",
        "https",
        "reachability",
    }
    assert projected == {
        "summary": {
            "active_provider": "zerotier",
            "enabled": True,
            "paused_this_boot": False,
            "connection": "connected",
            "https": "endpoint_ready",
            "reachability": "verified",
        },
        "observed_at": "2026-10-22T04:00:00Z",
        "stale": False,
    }
    serialized = repr(projected)
    for forbidden in (
        "details",
        "config_revision",
        "network_id",
        "identity",
        "task",
        "authorization_link",
        "vendor-secret",
    ):
        assert forbidden not in serialized
    assert "config_revision" not in projected
    assert "details" not in projected


def test_unknown_remains_unknown_and_daemon_state_does_not_infer_success():
    payload = status_payload()
    payload["daemon"] = "running"
    payload["network"] = "unknown"
    payload["reachability"] = "unverified"
    payload["https"] = "unknown"

    projected = public_summary(RemoteStatus.from_dict(payload))

    assert projected["summary"]["connection"] == "unknown"
    assert projected["summary"]["reachability"] == "unverified"
    assert projected["summary"]["https"] == "unknown"


def test_status_does_not_synthesize_missing_values_or_public_fallbacks():
    payload = status_payload()
    del payload["network"]
    with pytest.raises(ValueError):
        RemoteStatus.from_dict(payload)
    with pytest.raises(TypeError):
        public_summary(None)


def test_datetime_model_requires_timezone():
    with pytest.raises(ValueError, match="timezone"):
        RemoteStatus(
            installation=InstallationState.UNKNOWN,
            daemon=DaemonState.UNKNOWN,
            authorization=AuthorizationState.UNKNOWN,
            network=NetworkState.UNKNOWN,
            https=HttpsState.UNKNOWN,
            management_intent=ManagementIntent(
                enabled=False,
                active_provider=Provider.NONE,
                paused_this_boot=False,
                config_revision=1,
            ),
            reachability=ReachabilityState.UNVERIFIED,
            task=None,
            observed_at=datetime(2026, 10, 22),
            stale=True,
        )


OPERATIONS = (
    "install", "start", "enable", "connect", "request_authorization",
    "disconnect", "disable", "leave", "switch", "save_address",
)


def metadata_payload() -> dict:
    return {
        "managed": False,
        "capabilities": {operation: False for operation in OPERATIONS},
        "address": {"url": None, "source": None, "verification": "unverified"},
        "client_trust": "unknown",
        "peanut_client_report": None,
    }


def test_admin_projection_exact_shape_and_public_redaction():
    status = RemoteStatus.from_dict(status_payload())
    metadata = AdminMetadata.from_dict(metadata_payload())
    public_before = public_summary(status)
    result = admin_status(status, metadata)
    assert result == {
        **public_before,
        "config_revision": 7,
        "details": {
            "installation": "installed",
            "daemon": "running",
            "authorization": "authorized",
            "task": "succeeded",
            "managed": False,
            "capabilities": {operation: False for operation in OPERATIONS},
            "address": {"url": None, "source": None, "verification": "unverified"},
            "client_trust": "unknown",
            "peanut_client_report": None,
            "reachability_evidence": evidence_payload(),
        },
    }
    assert set(result) == {"summary", "observed_at", "stale", "config_revision", "details"}
    assert set(result["details"]) == {
        "installation", "authorization", "managed", "capabilities", "address", "client_trust",
        "peanut_client_report", "reachability_evidence", "daemon", "task",
    }
    result["summary"]["connection"] = "changed"
    result["details"]["capabilities"]["install"] = True
    assert public_summary(status) == public_before
    assert metadata.to_dict() == metadata_payload()


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("value", [True, False])
def test_capabilities_preserve_explicit_boolean_values(operation, value):
    payload = metadata_payload()
    payload["capabilities"][operation] = value
    metadata = AdminMetadata.from_dict(payload)
    assert metadata.to_dict() == payload


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("value", [None, 0, 1, "true", "false"])
def test_capabilities_reject_non_boolean_values(operation, value):
    payload = metadata_payload()
    payload["capabilities"][operation] = value
    with pytest.raises(TypeError):
        AdminMetadata.from_dict(payload)


@pytest.mark.parametrize("operation", OPERATIONS)
def test_capabilities_require_every_operation(operation):
    payload = metadata_payload()
    del payload["capabilities"][operation]
    with pytest.raises(ValueError):
        AdminMetadata.from_dict(payload)


@pytest.mark.parametrize(
    "field",
    ["managed", "capabilities", "address", "client_trust", "peanut_client_report"],
)
def test_admin_metadata_requires_explicit_fields(field):
    payload = metadata_payload()
    del payload[field]
    with pytest.raises(ValueError):
        AdminMetadata.from_dict(payload)


@pytest.mark.parametrize("section", ["metadata", "capabilities", "address"])
def test_admin_schema_rejects_extra_provider_fields(section):
    payload = metadata_payload()
    target = payload if section == "metadata" else payload[section]
    target["network_id"] = "secret-network"
    with pytest.raises(ValueError):
        AdminMetadata.from_dict(payload)


@pytest.mark.parametrize("source", list(AddressSource))
@pytest.mark.parametrize("verification", list(AddressVerification))
def test_address_evidence_labels_round_trip_without_promotion(source, verification):
    payload = metadata_payload()
    payload["address"] = {
        "url": "https://remote.example.org/",
        "source": source.value,
        "verification": verification.value,
    }
    metadata = AdminMetadata.from_dict(payload)
    assert metadata.to_dict() == payload
    status_data = status_payload()
    status_data.update(
        authorization="operator_confirmed", network="unknown", https="unknown",
        reachability="unverified",
    )
    status = RemoteStatus.from_dict(status_data)
    result = admin_status(status, metadata)
    assert result["details"]["address"]["verification"] == verification.value
    assert result["details"]["authorization"] == "operator_confirmed"
    assert result["summary"]["reachability"] == "unverified"
    assert result["summary"]["connection"] == "unknown"
    assert result["details"]["client_trust"] == "unknown"
    public = public_summary(status)
    assert set(public) == {"summary", "observed_at", "stale"}
    assert "remote.example.org" not in repr(public)
    assert "address" not in repr(public)


def test_peanut_client_report_is_admin_only_and_preserves_report_freshness():
    payload = metadata_payload()
    payload["peanut_client_report"] = {
        "device_sn": "SN-private-to-admin",
        "client_state": "running",
        "binding_state": "bound",
        "public_ip_state": "reported",
        "public_ip": "1.1.1.1",
        "observed_at": "2026-10-22T11:59:00+08:00",
        "stale": True,
    }
    metadata = AdminMetadata.from_dict(payload)
    status = RemoteStatus.from_dict(status_payload())

    result = admin_status(status, metadata)
    report = result["details"]["peanut_client_report"]
    assert report == {
        "device_sn": "SN-private-to-admin",
        "client_state": "running",
        "binding_state": "bound",
        "public_ip_state": "reported",
        "public_ip": "1.1.1.1",
        "observed_at": "2026-10-22T03:59:00Z",
        "stale": True,
    }
    public = public_summary(status)
    assert "SN-private-to-admin" not in repr(public)
    assert "1.1.1.1" not in repr(public)
    assert "peanut_client_report" not in public["summary"]


@pytest.mark.parametrize(
    "binding_state",
    list(PeanutBindingState),
)
def test_peanut_binding_state_round_trips_without_success_defaults(binding_state):
    report = PeanutClientReport(
        device_sn=None,
        client_state=DaemonState.UNKNOWN,
        binding_state=binding_state,
        public_ip_state=PeanutPublicIpState.UNKNOWN,
        public_ip=None,
        observed_at=datetime(2026, 10, 22, tzinfo=timezone.utc),
        stale=True,
    )
    assert PeanutClientReport.from_dict(report.to_dict()) == report


@pytest.mark.parametrize(
    ("state", "ip"),
    [
        (PeanutPublicIpState.UNKNOWN, None),
        (PeanutPublicIpState.NOT_REPORTED, None),
        (PeanutPublicIpState.REPORTED, "1.1.1.1"),
        (PeanutPublicIpState.REPORTED, "2606:4700:4700::1111"),
    ],
)
def test_peanut_public_ip_report_has_explicit_presence_state(state, ip):
    report = PeanutClientReport(
        device_sn="SN-1",
        client_state=DaemonState.RUNNING,
        binding_state=PeanutBindingState.BOUND,
        public_ip_state=state,
        public_ip=ip,
        observed_at=datetime(2026, 10, 22, tzinfo=timezone.utc),
        stale=False,
    )
    assert PeanutClientReport.from_dict(report.to_dict()) == report


@pytest.mark.parametrize(
    ("state", "ip"),
    [
        (PeanutPublicIpState.UNKNOWN, "1.1.1.1"),
        (PeanutPublicIpState.NOT_REPORTED, "1.1.1.1"),
        (PeanutPublicIpState.REPORTED, None),
        (PeanutPublicIpState.REPORTED, "not-an-ip"),
        (PeanutPublicIpState.REPORTED, "192.168.1.10"),
        (PeanutPublicIpState.REPORTED, "2001:db8::1"),
    ],
)
def test_peanut_public_ip_report_rejects_inconsistent_or_nonpublic_values(state, ip):
    with pytest.raises(ValueError):
        PeanutClientReport(
            device_sn=None,
            client_state=DaemonState.UNKNOWN,
            binding_state=PeanutBindingState.UNKNOWN,
            public_ip_state=state,
            public_ip=ip,
            observed_at=datetime(2026, 10, 22, tzinfo=timezone.utc),
            stale=False,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("device_sn", ""),
        ("device_sn", "bad\nsn"),
        ("device_sn", "S" * 129),
        ("client_state", "online"),
        ("binding_state", "logged_in"),
        ("public_ip_state", "available"),
        ("observed_at", "2026-10-22T00:00:00"),
        ("stale", 0),
    ],
)
def test_peanut_report_rejects_invalid_or_unverified_fields(field, value):
    payload = {
        "device_sn": "SN-1",
        "client_state": "running",
        "binding_state": "bound",
        "public_ip_state": "reported",
        "public_ip": "1.1.1.1",
        "observed_at": "2026-10-22T00:00:00Z",
        "stale": False,
    }
    payload[field] = value
    with pytest.raises((ValueError, TypeError)):
        PeanutClientReport.from_dict(payload)


def test_peanut_report_rejects_missing_or_unmodeled_fields():
    report = {
        "device_sn": "SN-1",
        "client_state": "running",
        "binding_state": "bound",
        "public_ip_state": "reported",
        "public_ip": "1.1.1.1",
        "observed_at": "2026-10-22T00:00:00Z",
        "stale": False,
    }
    for changed in (
        {key: value for key, value in report.items() if key != "device_sn"},
        {**report, "authorization_url": "https://example.org/"},
    ):
        with pytest.raises(ValueError):
            PeanutClientReport.from_dict(changed)


@pytest.mark.parametrize("trust", list(ClientTrust))
def test_client_trust_requires_and_preserves_explicit_evidence(trust):
    payload = metadata_payload()
    payload["client_trust"] = trust.value
    metadata = AdminMetadata.from_dict(payload)
    assert metadata.client_trust is trust
    status = RemoteStatus.from_dict(status_payload())
    assert admin_status(status, metadata)["details"]["client_trust"] == trust.value
    assert "client_trust" not in repr(public_summary(status))


@pytest.mark.parametrize("field", ["url", "source", "verification"])
def test_admin_address_requires_every_field(field):
    payload = metadata_payload()
    del payload["address"][field]
    with pytest.raises(ValueError):
        AdminMetadata.from_dict(payload)


def test_admin_managed_state_is_explicit_not_inferred_from_configuration():
    status = RemoteStatus.from_dict(status_payload())
    for managed in (True, False):
        payload = metadata_payload()
        payload["managed"] = managed
        result = admin_status(status, AdminMetadata.from_dict(payload))
        assert result["details"]["managed"] is managed
        assert not any(result["details"]["capabilities"].values())


@pytest.mark.parametrize(
    "section,field,value",
    [
        ("metadata", "managed", 1),
        ("metadata", "client_trust", "trusted"),
        ("address", "source", "configured"),
        ("address", "verification", "confirmed"),
        ("address", "url", 1),
        ("metadata", "capabilities", None),
        ("metadata", "address", None),
    ],
)
def test_admin_schema_rejects_invalid_values(section, field, value):
    payload = metadata_payload()
    target = payload if section == "metadata" else payload[section]
    target[field] = value
    with pytest.raises((ValueError, TypeError)):
        AdminMetadata.from_dict(payload)


@pytest.mark.parametrize(
    "url",
    [
        "", "http://remote.example.org/", "https://user:password@remote.example.org/",
        "https://remote.example.org/path", "https://remote.example.org/?secret=1",
        "https://remote.example.org/#secret", "https://remote.example.org/\n",
        "https://remote.example.org:0/", "https://remote.example.org:65536/",
        "https://remote.example.org:/", "https://localhost/", "https://host.local/",
        "https://127.0.0.1/", "https://192.0.2.1/", "https://127.1/",
        "https://0x7f.0.0.1/", "https://[::1]/", "https://remote.example.org\\evil/",
        "https://bad_host.example.org/",
    ],
)
def test_admin_address_rejects_unsafe_url_syntax_without_probing(url):
    payload = metadata_payload()
    payload["address"].update(url=url, source="user_configured")
    with pytest.raises(ValueError):
        AdminMetadata.from_dict(payload)


@pytest.mark.parametrize(
    "address",
    [
        {"url": None, "source": "user_configured", "verification": "unverified"},
        {"url": "https://remote.example.org/", "source": None, "verification": "unverified"},
        {"url": None, "source": None, "verification": "verified"},
    ],
)
def test_admin_address_rejects_inconsistent_or_absent_verified_address(address):
    payload = metadata_payload()
    payload["address"] = address
    with pytest.raises(ValueError):
        AdminMetadata.from_dict(payload)


def test_admin_projection_requires_models_and_has_no_success_defaults():
    status = RemoteStatus.from_dict(status_payload())
    with pytest.raises(TypeError):
        admin_status(status, None)
    with pytest.raises(TypeError):
        admin_status(None, AdminMetadata.from_dict(metadata_payload()))
    with pytest.raises(TypeError):
        Capabilities()
    with pytest.raises(TypeError):
        AdminMetadata(
            managed=False,
            capabilities=Capabilities.from_dict(metadata_payload()["capabilities"]),
            address=RemoteAddress(None, None, AddressVerification.UNVERIFIED),
            client_trust="unknown",
        )


def test_verified_requires_evidence_not_caller_success_defaults():
    payload = status_payload()
    payload["reachability_evidence"] = None
    with pytest.raises(ValueError, match="requires evidence"):
        RemoteStatus.from_dict(payload)
    status = RemoteStatus.from_dict(status_payload())
    with pytest.raises(ValueError, match="requires evidence"):
        replace(status, reachability_evidence=None)
    with pytest.raises(TypeError, match="ReachabilityEvidence"):
        replace(status, reachability_evidence=evidence_payload())


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider", "zerotier"),
        ("config_revision", 8),
        ("stale", True),
        ("tested_at", "2026-10-22T04:00:01Z"),
    ],
)
def test_verified_rejects_unbound_stale_or_future_evidence(field, value):
    payload = status_payload()
    payload["reachability_evidence"][field] = value
    with pytest.raises(ValueError):
        RemoteStatus.from_dict(payload)


def test_verified_rejects_stale_observation():
    payload = status_payload()
    payload["stale"] = True
    with pytest.raises(ValueError, match="non-stale"):
        RemoteStatus.from_dict(payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("certificate_sha256", ""),
        ("certificate_sha256", "a" * 63),
        ("certificate_sha256", "g" * 64),
        ("certificate_sha256", "sha256:" + "a" * 64),
        ("certificate_sha256", 12),
        ("network_context", "lan"),
        ("network_context", "https://example.org/"),
        ("network_context", None),
        ("verifier", ""),
        ("verifier", "a" * 65),
        ("verifier", "user@example.org"),
        ("verifier", "user\nsecret"),
        ("verifier", "../secret"),
        ("verifier", None),
        ("provider", "none"),
        ("provider", "oray"),
        ("config_revision", True),
        ("config_revision", "7"),
        ("config_revision", 0),
        ("tested_at", "2026-10-22T03:59:00"),
        ("stale", "false"),
    ],
)
def test_evidence_rejects_invalid_bounded_values(field, value):
    payload = evidence_payload()
    payload[field] = value
    with pytest.raises((ValueError, TypeError)):
        ReachabilityEvidence.from_dict(payload)


@pytest.mark.parametrize("field", list(evidence_payload()))
def test_evidence_requires_all_fields(field):
    payload = evidence_payload()
    del payload[field]
    with pytest.raises(ValueError):
        ReachabilityEvidence.from_dict(payload)


def test_evidence_rejects_freeform_provider_details():
    payload = evidence_payload()
    payload["network_id"] = "secret"
    with pytest.raises(ValueError):
        ReachabilityEvidence.from_dict(payload)


def test_evidence_direct_model_requires_enum_and_timezone_types():
    evidence = ReachabilityEvidence.from_dict(evidence_payload())
    for overrides in (
        {"provider": "tailscale"},
        {"network_context": "mobile_data"},
        {"tested_at": "2026-10-22T03:59:00Z"},
    ):
        with pytest.raises(TypeError):
            replace(evidence, **overrides)
    with pytest.raises(ValueError, match="timezone"):
        replace(evidence, tested_at=datetime(2026, 10, 22))


@pytest.mark.parametrize("network", list(EvidenceNetworkContext))
@pytest.mark.parametrize("fingerprint", [None, "AB" * 32])
def test_valid_evidence_serializes_and_is_admin_only(network, fingerprint):
    payload = status_payload()
    if fingerprint is None:
        payload["reachability"] = "unverified"
    payload["reachability_evidence"].update(
        network_context=network.value,
        certificate_sha256=fingerprint,
        tested_at="2026-10-22T11:59:00+08:00",
    )
    status = RemoteStatus.from_dict(payload)
    expected = {**payload["reachability_evidence"], "tested_at": "2026-10-22T03:59:00Z"}
    assert status.to_dict()["reachability_evidence"] == expected
    assert RemoteStatus.from_dict(status.to_dict()) == status
    admin = admin_status(status, AdminMetadata.from_dict(metadata_payload()))
    assert admin["details"]["reachability_evidence"] == expected
    public = public_summary(status)
    assert set(public) == {"summary", "observed_at", "stale"}
    assert set(public["summary"]) == {
        "active_provider", "enabled", "paused_this_boot", "connection", "https", "reachability",
    }
    for private in (
        "reachability_evidence", "tested_at", "network_context", "certificate_sha256",
        "verifier", "external-tester", "config_revision",
    ):
        assert private not in repr(public)


@pytest.mark.parametrize(
    "state", [ReachabilityState.UNVERIFIED, ReachabilityState.FAILED, ReachabilityState.STALE],
)
@pytest.mark.parametrize("has_evidence", [False, True])
def test_nonverified_states_preserve_absent_or_historical_evidence_without_success(
    state, has_evidence,
):
    payload = status_payload()
    payload["reachability"] = state.value
    payload["stale"] = True
    if has_evidence:
        payload["reachability_evidence"].update(
            provider="zerotier", config_revision=6, stale=True,
        )
    else:
        payload["reachability_evidence"] = None
    status = RemoteStatus.from_dict(payload)
    assert status.reachability is state
    assert status.to_dict() == payload
    assert public_summary(status)["summary"]["reachability"] == state.value
    details = admin_status(status, AdminMetadata.from_dict(metadata_payload()))["details"]
    if has_evidence:
        assert details["reachability_evidence"]["stale"] is True
    else:
        assert "reachability_evidence" not in details


@pytest.mark.parametrize("network", [NetworkState.DISCONNECTED, NetworkState.UNKNOWN])
def test_verified_rejects_nonconnected_network(network):
        payload = status_payload()
        payload["network"] = network.value
        with pytest.raises(ValueError, match="connected network"):
            RemoteStatus.from_dict(payload)


@pytest.mark.parametrize("https", [HttpsState.OFF, HttpsState.UNKNOWN])
def test_verified_rejects_https_not_ready(https):
        payload = status_payload()
        payload["https"] = https.value
        with pytest.raises(ValueError, match="HTTPS endpoint ready"):
            RemoteStatus.from_dict(payload)


def test_verified_requires_certificate_fingerprint():
        payload = status_payload()
        payload["reachability_evidence"]["certificate_sha256"] = None
        with pytest.raises(ValueError, match="certificate SHA256"):
            RemoteStatus.from_dict(payload)


@pytest.mark.parametrize("task", [None, *TaskState])
def test_admin_projection_preserves_explicit_nullable_task_state(task):
    payload = status_payload()
    payload["task"] = task.value if task is not None else None
    status = RemoteStatus.from_dict(payload)
    details = admin_status(status, AdminMetadata.from_dict(metadata_payload()))["details"]
    assert details["daemon"] == payload["daemon"]
    assert details["task"] == payload["task"]
    assert "daemon" not in repr(public_summary(status))
    assert "task" not in repr(public_summary(status))
