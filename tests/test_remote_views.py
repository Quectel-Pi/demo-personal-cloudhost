from datetime import datetime, timezone
from itertools import product

import pytest

from app.remote_policy import Capability, Ingress, Role
from app.remote_status import (
    AdminMetadata,
    AddressSource,
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
    RemoteAddress,
    RemoteStatus,
    TaskState,
    AddressVerification,
    admin_status,
    public_summary,
)
from app.remote_views import project_remote_view


def make_status() -> RemoteStatus:
    return RemoteStatus(
        installation=InstallationState.INSTALLED,
        daemon=DaemonState.RUNNING,
        authorization=AuthorizationState.AUTHORIZED,
        network=NetworkState.CONNECTED,
        https=HttpsState.ENDPOINT_READY,
        management_intent=ManagementIntent(
            enabled=True,
            active_provider=Provider.TAILSCALE,
            paused_this_boot=False,
            config_revision=7,
        ),
        reachability=ReachabilityState.VERIFIED,
        task=TaskState.SUCCEEDED,
        observed_at=datetime(2026, 10, 22, 4, tzinfo=timezone.utc),
        stale=False,
        reachability_evidence=ReachabilityEvidence(
            tested_at=datetime(2026, 10, 22, 3, 59, tzinfo=timezone.utc),
            provider=Provider.TAILSCALE,
            config_revision=7,
            network_context=EvidenceNetworkContext.MOBILE_DATA,
            certificate_sha256="a1" * 32,
            verifier="external-tester",
            stale=False,
        ),
    )


def make_metadata() -> AdminMetadata:
    return AdminMetadata(
        managed=True,
        capabilities=Capabilities(
            install=False,
            start=True,
            enable=True,
            connect=True,
            request_authorization=False,
            disconnect=True,
            disable=True,
            leave=True,
            switch=True,
            save_address=True,
        ),
        address=RemoteAddress(
            url="https://remote-view-test.example.org",
            source=AddressSource.USER_CONFIGURED,
            verification=AddressVerification.UNVERIFIED,
        ),
        client_trust=ClientTrust.UNKNOWN,
        peanut_client_report=PeanutClientReport(
            device_sn="SN-test-private",
            client_state=DaemonState.RUNNING,
            binding_state=PeanutBindingState.BOUND,
            public_ip_state=PeanutPublicIpState.REPORTED,
            public_ip="1.1.1.1",
            observed_at=datetime(2026, 10, 22, 3, 59, tzinfo=timezone.utc),
            stale=False,
        ),
    )


def is_allowed(role: Role, ingress: Ingress, capability: Capability) -> bool:
    if capability is Capability.SUMMARY:
        return role in {Role.USER, Role.ADMIN}
    return role is Role.ADMIN and ingress in {Ingress.LAN, Ingress.PRIVATE}


@pytest.mark.parametrize(
    ("role", "ingress", "capability"),
    list(product(Role, Ingress, (Capability.SUMMARY, Capability.DETAILS))),
    ids=lambda value: value.value,
)
def test_role_ingress_view_matrix_returns_only_authorized_projection(
    role, ingress, capability
):
    status = make_status()
    metadata = make_metadata()
    expected_keys = (
        {"summary", "observed_at", "stale"}
        if capability is Capability.SUMMARY
        else {"summary", "observed_at", "stale", "config_revision", "details"}
    )

    if not is_allowed(role, ingress, capability):
        with pytest.raises(PermissionError):
            project_remote_view(role, ingress, capability, status, metadata)
        return

    actual = project_remote_view(role, ingress, capability, status, metadata)
    expected = (
        public_summary(status)
        if capability is Capability.SUMMARY
        else admin_status(status, metadata)
    )
    assert actual == expected
    assert set(actual) == expected_keys
    assert set(actual["summary"]) == {
        "active_provider",
        "enabled",
        "paused_this_boot",
        "connection",
        "https",
        "reachability",
    }
    if capability is Capability.DETAILS:
        assert set(actual["details"]) == {
            "installation",
            "daemon",
            "authorization",
            "task",
            "managed",
            "capabilities",
            "address",
            "client_trust",
            "peanut_client_report",
            "reachability_evidence",
        }
        assert "SN-test-private" not in repr(actual["summary"])


@pytest.mark.parametrize("role", [Role.USER, Role.ADMIN])
@pytest.mark.parametrize("ingress", list(Ingress))
def test_summary_is_exact_public_projection_without_admin_details(role, ingress):
    status = make_status()
    metadata = make_metadata()

    result = project_remote_view(
        role, ingress, Capability.SUMMARY, status, metadata
    )

    assert result == public_summary(status)
    assert set(result) == {"summary", "observed_at", "stale"}
    assert set(result["summary"]) == {
        "active_provider",
        "enabled",
        "paused_this_boot",
        "connection",
        "https",
        "reachability",
    }
    serialized = repr(result)
    assert "revision" not in serialized
    assert "config_revision" not in serialized
    assert "details" not in serialized
    assert "remote-view-test.example.org" not in serialized


@pytest.mark.parametrize("role", [Role.ANONYMOUS, Role.USER])
@pytest.mark.parametrize("ingress", list(Ingress))
def test_denied_details_cannot_leak_supplied_metadata(role, ingress):
    metadata = make_metadata()
    with pytest.raises(PermissionError) as error:
        project_remote_view(
            role, ingress, Capability.DETAILS, make_status(), metadata
        )

    assert "remote-view-test.example.org" not in str(error.value)


@pytest.mark.parametrize(
    ("role", "ingress", "capability"),
    [
        (Role.USER, Ingress.LAN, Capability.SUMMARY),
        (Role.ADMIN, Ingress.PUBLIC, Capability.DETAILS),
        (Role.ANONYMOUS, Ingress.PRIVATE, Capability.DETAILS),
    ],
)
def test_metadata_is_not_required_for_summary_or_denied_details(
    role, ingress, capability
):
    if capability is Capability.SUMMARY:
        assert project_remote_view(
            role, ingress, capability, make_status()
        ) == public_summary(make_status())
    else:
        with pytest.raises(PermissionError):
            project_remote_view(role, ingress, capability, make_status())


def test_authorized_details_requires_admin_metadata():
    with pytest.raises(TypeError, match="metadata is required"):
        project_remote_view(
            Role.ADMIN, Ingress.LAN, Capability.DETAILS, make_status()
        )


@pytest.mark.parametrize(
    ("role", "ingress", "capability", "status", "metadata", "error"),
    [
        ("admin", Ingress.LAN, Capability.SUMMARY, make_status(), None, TypeError),
        (Role.ADMIN, "lan", Capability.SUMMARY, make_status(), None, TypeError),
        (Role.ADMIN, Ingress.LAN, "summary", make_status(), None, TypeError),
        (
            Role.ADMIN,
            Ingress.LAN,
            Capability.INSTALL,
            make_status(),
            None,
            ValueError,
        ),
        (Role.ADMIN, Ingress.LAN, Capability.SUMMARY, None, None, TypeError),
        (
            Role.ADMIN,
            Ingress.LAN,
            Capability.SUMMARY,
            make_status(),
            object(),
            TypeError,
        ),
        (
            object(),
            Ingress.LAN,
            Capability.SUMMARY,
            make_status(),
            make_metadata(),
            TypeError,
        ),
        (
            Role.ADMIN,
            object(),
            Capability.DETAILS,
            make_status(),
            make_metadata(),
            TypeError,
        ),
    ],
    ids=(
        "role-string",
        "ingress-string",
        "capability-string",
        "unsupported-capability",
        "missing-status",
        "malformed-metadata",
        "role-object",
        "ingress-object",
    ),
)
def test_malformed_inputs_raise_explicit_errors(
    role, ingress, capability, status, metadata, error
):
    with pytest.raises(error):
        project_remote_view(role, ingress, capability, status, metadata)
