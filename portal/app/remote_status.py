"""Strict provider-neutral remote status contract and role-specific projections."""

from __future__ import annotations

import ipaddress
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, TypeVar
from urllib.parse import urlsplit


class _StringEnum(str, Enum):
    @classmethod
    def parse(cls, value: object) -> _StringEnum:
        if not isinstance(value, str):
            raise ValueError(f"{cls.__name__} must be a string")
        try:
            return cls(value)
        except ValueError as exc:
            raise ValueError(f"invalid {cls.__name__}: {value!r}") from exc


class InstallationState(_StringEnum):
    NOT_INSTALLED = "not_installed"
    INSTALLING = "installing"
    INSTALLED = "installed"
    INCOMPATIBLE = "incompatible"
    UNKNOWN = "unknown"


class DaemonState(_StringEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    FAILED = "failed"
    UNKNOWN = "unknown"


class AuthorizationState(_StringEnum):
    NOT_REQUIRED = "not_required"
    NEEDS_LOGIN = "needs_login"
    NEEDS_AUTHORIZATION = "needs_authorization"
    AUTHORIZED = "authorized"
    EXPIRED = "expired"
    DENIED = "denied"
    UNKNOWN = "unknown"
    OPERATOR_CONFIRMED = "operator_confirmed"


class NetworkState(_StringEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class HttpsState(_StringEnum):
    OFF = "off"
    CONFIGURING = "configuring"
    NEEDS_AUTHORIZATION = "needs_authorization"
    ENDPOINT_READY = "endpoint_ready"
    FAILED = "failed"
    UNKNOWN = "unknown"


class Provider(_StringEnum):
    NONE = "none"
    TAILSCALE = "tailscale"
    ZEROTIER = "zerotier"
    PEANUT = "peanut"


class ReachabilityState(_StringEnum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    FAILED = "failed"
    STALE = "stale"


class TaskState(_StringEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    RECOVERY_REQUIRED = "recovery_required"


class AddressSource(_StringEnum):
    """Origin only: neither provider reports nor user configuration prove access."""

    PROVIDER_REPORTED = "provider_reported"
    USER_CONFIGURED = "user_configured"


class AddressVerification(_StringEnum):
    """Internal evidence labels; operator confirmation is not verification."""

    UNVERIFIED = "unverified"
    OPERATOR_CONFIRMED = "operator_confirmed"
    VERIFIED = "verified"
    FAILED = "failed"
    STALE = "stale"


class ClientTrust(_StringEnum):
    """Explicit client certificate-trust evidence, never inferred from HTTPS."""

    UNKNOWN = "unknown"
    OPERATOR_CONFIRMED = "operator_confirmed"
    VERIFIED = "verified"
    FAILED = "failed"
    STALE = "stale"


class PeanutBindingState(_StringEnum):
    """Binding state reported by the official local Peanut client."""

    UNKNOWN = "unknown"
    UNBOUND = "unbound"
    PENDING = "pending"
    BOUND = "bound"


class PeanutPublicIpState(_StringEnum):
    """Whether the official Peanut client reported a usable public IP."""

    UNKNOWN = "unknown"
    NOT_REPORTED = "not_reported"
    REPORTED = "reported"


EnumType = TypeVar("EnumType", bound=_StringEnum)


def _require_enum(value: object, enum_type: type[EnumType], field: str) -> None:
    if not isinstance(value, enum_type):
        raise TypeError(f"{field} must be {enum_type.__name__}")


def _require_exact_keys(value: Mapping[str, Any], expected: frozenset[str], field: str) -> None:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(repr(key) for key in actual - expected)
        raise ValueError(
            f"{field} schema mismatch (missing={missing}, unexpected={unexpected})"
        )


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("observed_at must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise ValueError("observed_at must be an ISO-8601 string") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("observed_at must include a timezone")
    return parsed


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class EvidenceNetworkContext(_StringEnum):
    """External test transport only; no network IDs, hostnames, or credentials."""

    MOBILE_DATA = "mobile_data"
    EXTERNAL_NETWORK = "external_network"


_EVIDENCE_KEYS = frozenset(
    {
        "tested_at", "provider", "config_revision", "network_context",
        "certificate_sha256", "verifier", "stale",
    }
)


@dataclass(frozen=True)
class ReachabilityEvidence:
    """Externally supplied evidence, not a probe or an authenticity guarantee.

    Verifier is a non-secret local principal label (1–64 ASCII identifier chars).
    No expiry interval is inferred: the evidence producer must mark staleness.
    """

    tested_at: datetime
    provider: Provider
    config_revision: int
    network_context: EvidenceNetworkContext
    certificate_sha256: str | None
    verifier: str
    stale: bool

    def __post_init__(self) -> None:
        if not isinstance(self.tested_at, datetime):
            raise TypeError("evidence.tested_at must be a datetime")
        if self.tested_at.tzinfo is None or self.tested_at.utcoffset() is None:
            raise ValueError("evidence.tested_at must include a timezone")
        _require_enum(self.provider, Provider, "evidence.provider")
        if self.provider is Provider.NONE:
            raise ValueError("evidence.provider must identify a remote provider")
        if type(self.config_revision) is not int or self.config_revision < 1:
            raise ValueError("evidence.config_revision must be a positive integer")
        _require_enum(self.network_context, EvidenceNetworkContext, "evidence.network_context")
        if self.certificate_sha256 is not None and (
            not isinstance(self.certificate_sha256, str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", self.certificate_sha256) is None
        ):
            raise ValueError("evidence.certificate_sha256 must be 64 hex characters or null")
        if not isinstance(self.verifier, str) or re.fullmatch(
            r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,63}", self.verifier
        ) is None:
            raise ValueError("evidence.verifier must be a bounded non-secret principal label")
        if type(self.stale) is not bool:
            raise TypeError("evidence.stale must be a boolean")

    def to_dict(self) -> dict[str, Any]:
        return {
            "tested_at": _format_timestamp(self.tested_at),
            "provider": self.provider.value,
            "config_revision": self.config_revision,
            "network_context": self.network_context.value,
            "certificate_sha256": self.certificate_sha256,
            "verifier": self.verifier,
            "stale": self.stale,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ReachabilityEvidence:
        _require_exact_keys(value, _EVIDENCE_KEYS, "reachability_evidence")
        return cls(
            tested_at=_parse_timestamp(value["tested_at"]),
            provider=Provider.parse(value["provider"]),
            config_revision=value["config_revision"],
            network_context=EvidenceNetworkContext.parse(value["network_context"]),
            certificate_sha256=value["certificate_sha256"],
            verifier=value["verifier"],
            stale=value["stale"],
        )


@dataclass(frozen=True)
class ManagementIntent:
    enabled: bool
    active_provider: Provider
    paused_this_boot: bool | None
    config_revision: int

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise TypeError("management_intent.enabled must be a boolean")
        _require_enum(self.active_provider, Provider, "management_intent.active_provider")
        if self.paused_this_boot is not None and type(self.paused_this_boot) is not bool:
            raise TypeError("management_intent.paused_this_boot must be a boolean or null")
        if type(self.config_revision) is not int or self.config_revision < 1:
            raise ValueError("management_intent.config_revision must be a positive integer")

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "active_provider": self.active_provider.value,
            "paused_this_boot": self.paused_this_boot,
            "config_revision": self.config_revision,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ManagementIntent:
        if not isinstance(value, Mapping):
            raise ValueError("management_intent must be an object")
        expected = frozenset(
            {"enabled", "active_provider", "paused_this_boot", "config_revision"}
        )
        _require_exact_keys(value, expected, "management_intent")
        return cls(
            enabled=value["enabled"],
            active_provider=Provider.parse(value["active_provider"]),
            paused_this_boot=value["paused_this_boot"],
            config_revision=value["config_revision"],
        )


_STATUS_KEYS = frozenset(
    {
        "installation",
        "daemon",
        "authorization",
        "network",
        "https",
        "management_intent",
        "reachability",
        "task",
        "observed_at",
        "stale",
        "reachability_evidence",
    }
)


@dataclass(frozen=True)
class RemoteStatus:
    installation: InstallationState
    daemon: DaemonState
    authorization: AuthorizationState
    network: NetworkState
    https: HttpsState
    management_intent: ManagementIntent
    reachability: ReachabilityState
    task: TaskState | None
    observed_at: datetime
    stale: bool
    reachability_evidence: ReachabilityEvidence | None = None

    def __post_init__(self) -> None:
        _require_enum(self.installation, InstallationState, "installation")
        _require_enum(self.daemon, DaemonState, "daemon")
        _require_enum(self.authorization, AuthorizationState, "authorization")
        _require_enum(self.network, NetworkState, "network")
        _require_enum(self.https, HttpsState, "https")
        if not isinstance(self.management_intent, ManagementIntent):
            raise TypeError("management_intent must be ManagementIntent")
        _require_enum(self.reachability, ReachabilityState, "reachability")
        if self.task is not None:
            _require_enum(self.task, TaskState, "task")
        if not isinstance(self.observed_at, datetime):
            raise TypeError("observed_at must be a datetime")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must include a timezone")
        if type(self.stale) is not bool:
            raise TypeError("stale must be a boolean")
        evidence = self.reachability_evidence
        if evidence is not None and not isinstance(evidence, ReachabilityEvidence):
            raise TypeError("reachability_evidence must be ReachabilityEvidence or null")
        if self.reachability is ReachabilityState.VERIFIED:
            if evidence is None:
                raise ValueError("verified reachability requires evidence")
            if evidence.certificate_sha256 is None:
                raise ValueError("verified reachability requires a certificate SHA256")
            if self.network is not NetworkState.CONNECTED:
                raise ValueError("verified reachability requires a connected network")
            if self.https is not HttpsState.ENDPOINT_READY:
                raise ValueError("verified reachability requires an HTTPS endpoint ready")
            if self.stale or evidence.stale:
                raise ValueError("verified reachability requires non-stale status and evidence")
            if (
                evidence.provider is not self.management_intent.active_provider
                or evidence.config_revision != self.management_intent.config_revision
            ):
                raise ValueError("verified reachability evidence must match current provider/revision")
            if evidence.tested_at > self.observed_at:
                raise ValueError("verified reachability evidence cannot be newer than the observation")

    def to_dict(self) -> dict[str, Any]:
        return {
            "installation": self.installation.value,
            "daemon": self.daemon.value,
            "authorization": self.authorization.value,
            "network": self.network.value,
            "https": self.https.value,
            "management_intent": self.management_intent.to_dict(),
            "reachability": self.reachability.value,
            "task": self.task.value if self.task is not None else None,
            "observed_at": _format_timestamp(self.observed_at),
            "stale": self.stale,
            "reachability_evidence": (
                self.reachability_evidence.to_dict()
                if self.reachability_evidence is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RemoteStatus:
        if not isinstance(value, Mapping):
            raise ValueError("remote_status must be an object")
        _require_exact_keys(value, _STATUS_KEYS, "remote_status")
        if not isinstance(value["management_intent"], Mapping):
            raise ValueError("management_intent must be an object")
        task = None if value["task"] is None else TaskState.parse(value["task"])
        return cls(
            installation=InstallationState.parse(value["installation"]),
            daemon=DaemonState.parse(value["daemon"]),
            authorization=AuthorizationState.parse(value["authorization"]),
            network=NetworkState.parse(value["network"]),
            https=HttpsState.parse(value["https"]),
            management_intent=ManagementIntent.from_dict(value["management_intent"]),
            reachability=ReachabilityState.parse(value["reachability"]),
            task=task,
            observed_at=_parse_timestamp(value["observed_at"]),
            stale=value["stale"],
            reachability_evidence=(
                ReachabilityEvidence.from_dict(value["reachability_evidence"])
                if value["reachability_evidence"] is not None else None
            ),
        )


def public_summary(status: RemoteStatus) -> dict[str, Any]:
    """Project only the PRD-approved summary fields; never synthesize success."""

    if not isinstance(status, RemoteStatus):
        raise TypeError("status must be RemoteStatus")
    intent = status.management_intent
    return {
        "summary": {
            "active_provider": intent.active_provider.value,
            "enabled": intent.enabled,
            "paused_this_boot": intent.paused_this_boot,
            "connection": status.network.value,
            "https": status.https.value,
            "reachability": status.reachability.value,
        },
        "observed_at": _format_timestamp(status.observed_at),
        "stale": status.stale,
    }


_CAPABILITY_KEYS = frozenset(
    {
        "install", "start", "enable", "connect", "request_authorization",
        "disconnect", "disable", "leave", "switch", "save_address",
    }
)


@dataclass(frozen=True)
class Capabilities:
    """Observed action availability; every flag must be supplied explicitly."""

    install: bool
    start: bool
    enable: bool
    connect: bool
    request_authorization: bool
    disconnect: bool
    disable: bool
    leave: bool
    switch: bool
    save_address: bool

    def __post_init__(self) -> None:
        for name in _CAPABILITY_KEYS:
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"capabilities.{name} must be a boolean")

    def to_dict(self) -> dict[str, bool]:
        return {name: getattr(self, name) for name in sorted(_CAPABILITY_KEYS)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Capabilities:
        _require_exact_keys(value, _CAPABILITY_KEYS, "capabilities")
        return cls(**value)


def _validate_address_url(value: str) -> None:
    """Validate HTTPS root-address syntax only; never resolve or contact a host."""

    if (
        not value or len(value) > 2048
        or any(char.isspace() or unicodedata.category(char) == "Cc" for char in value)
        or any(char in value for char in "\\?#")
    ):
        raise ValueError("address.url has invalid length or characters")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = parsed.hostname
    except ValueError as exc:
        raise ValueError("address.url is invalid") from exc
    if (
        parsed.scheme != "https" or not host or "@" in parsed.netloc
        or parsed.path not in ("", "/") or parsed.netloc.endswith(":")
        or (port is not None and not 1 <= port <= 65535)
        or "%" in host
    ):
        raise ValueError("address.url must be an HTTPS root URL without credentials")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            ascii_host = host.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ValueError("address.url hostname is invalid") from exc
        labels = ascii_host.split(".")
        if (
            len(ascii_host) > 253 or len(labels) < 2
            or all(re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)", label) for label in labels)
            or any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in labels
            )
            or ascii_host.endswith(
                (".localhost", ".local", ".internal", ".test", ".example", ".invalid", ".onion")
            )
        ):
            raise ValueError("address.url hostname is invalid or reserved")
    else:
        if not address.is_global:
            raise ValueError("address.url must not contain a local or reserved IP")


@dataclass(frozen=True)
class RemoteAddress:
    url: str | None
    source: AddressSource | None
    verification: AddressVerification

    def __post_init__(self) -> None:
        if self.url is not None:
            if not isinstance(self.url, str):
                raise TypeError("address.url must be a string or null")
            _validate_address_url(self.url)
        if self.source is not None:
            _require_enum(self.source, AddressSource, "address.source")
        _require_enum(self.verification, AddressVerification, "address.verification")
        if (self.url is None) != (self.source is None):
            raise ValueError("address.url and source must both be present or both null")
        if self.url is None and self.verification is not AddressVerification.UNVERIFIED:
            raise ValueError("an absent address must remain unverified")

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "source": self.source.value if self.source is not None else None,
            "verification": self.verification.value,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RemoteAddress:
        _require_exact_keys(value, frozenset({"url", "source", "verification"}), "address")
        return cls(
            url=value["url"],
            source=None if value["source"] is None else AddressSource.parse(value["source"]),
            verification=AddressVerification.parse(value["verification"]),
        )


_PEANUT_REPORT_KEYS = frozenset(
    {
        "device_sn",
        "client_state",
        "binding_state",
        "public_ip_state",
        "public_ip",
        "observed_at",
        "stale",
    }
)


@dataclass(frozen=True)
class PeanutClientReport:
    """Normalized, admin-only fields from a real official-client observation.

    This is a data contract, not a CLI parser or a source of default/demo data.
    Callers must set unknown or stale values explicitly when the client output
    cannot be trusted or parsed.
    """

    device_sn: str | None
    client_state: DaemonState
    binding_state: PeanutBindingState
    public_ip_state: PeanutPublicIpState
    public_ip: str | None
    observed_at: datetime
    stale: bool

    def __post_init__(self) -> None:
        if self.device_sn is not None and (
            not isinstance(self.device_sn, str)
            or not 1 <= len(self.device_sn) <= 128
            or any(ord(char) < 32 or ord(char) == 127 for char in self.device_sn)
        ):
            raise ValueError("device_sn must be a bounded string or null")
        _require_enum(self.client_state, DaemonState, "peanut_client_report.client_state")
        _require_enum(
            self.binding_state, PeanutBindingState, "peanut_client_report.binding_state"
        )
        _require_enum(
            self.public_ip_state, PeanutPublicIpState, "peanut_client_report.public_ip_state"
        )
        if self.public_ip_state is PeanutPublicIpState.REPORTED:
            if not isinstance(self.public_ip, str):
                raise ValueError("reported public_ip must be an IP address")
            try:
                address = ipaddress.ip_address(self.public_ip)
            except ValueError as exc:
                raise ValueError("reported public_ip must be an IP address") from exc
            if not address.is_global:
                raise ValueError("reported public_ip must be globally routable")
            object.__setattr__(self, "public_ip", address.compressed)
        elif self.public_ip is not None:
            raise ValueError("public_ip must be null unless public_ip_state is reported")
        if not isinstance(self.observed_at, datetime):
            raise TypeError("peanut_client_report.observed_at must be a datetime")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("peanut_client_report.observed_at must include a timezone")
        if type(self.stale) is not bool:
            raise TypeError("peanut_client_report.stale must be a boolean")

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_sn": self.device_sn,
            "client_state": self.client_state.value,
            "binding_state": self.binding_state.value,
            "public_ip_state": self.public_ip_state.value,
            "public_ip": self.public_ip,
            "observed_at": _format_timestamp(self.observed_at),
            "stale": self.stale,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PeanutClientReport:
        _require_exact_keys(value, _PEANUT_REPORT_KEYS, "peanut_client_report")
        return cls(
            device_sn=value["device_sn"],
            client_state=DaemonState.parse(value["client_state"]),
            binding_state=PeanutBindingState.parse(value["binding_state"]),
            public_ip_state=PeanutPublicIpState.parse(value["public_ip_state"]),
            public_ip=value["public_ip"],
            observed_at=_parse_timestamp(value["observed_at"]),
            stale=value["stale"],
        )


@dataclass(frozen=True)
class AdminMetadata:
    """Explicit admin-only observations, separate from public status layers."""

    managed: bool
    capabilities: Capabilities
    address: RemoteAddress
    client_trust: ClientTrust
    peanut_client_report: PeanutClientReport | None = None

    def __post_init__(self) -> None:
        if type(self.managed) is not bool:
            raise TypeError("managed must be a boolean")
        if not isinstance(self.capabilities, Capabilities):
            raise TypeError("capabilities must be Capabilities")
        if not isinstance(self.address, RemoteAddress):
            raise TypeError("address must be RemoteAddress")
        _require_enum(self.client_trust, ClientTrust, "client_trust")
        if self.peanut_client_report is not None and not isinstance(
            self.peanut_client_report, PeanutClientReport
        ):
            raise TypeError("peanut_client_report must be PeanutClientReport or None")

    def to_dict(self) -> dict[str, Any]:
        return {
            "managed": self.managed,
            "capabilities": self.capabilities.to_dict(),
            "address": self.address.to_dict(),
            "client_trust": self.client_trust.value,
            "peanut_client_report": (
                self.peanut_client_report.to_dict()
                if self.peanut_client_report is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AdminMetadata:
        _require_exact_keys(
            value,
            frozenset(
                {"managed", "capabilities", "address", "client_trust", "peanut_client_report"}
            ),
            "admin_metadata",
        )
        return cls(
            managed=value["managed"],
            capabilities=Capabilities.from_dict(value["capabilities"]),
            address=RemoteAddress.from_dict(value["address"]),
            client_trust=ClientTrust.parse(value["client_trust"]),
            peanut_client_report=(
                PeanutClientReport.from_dict(value["peanut_client_report"])
                if value["peanut_client_report"] is not None else None
            ),
        )


def admin_status(status: RemoteStatus, metadata: AdminMetadata) -> dict[str, Any]:
    """Project I1's admin shape; caller must enforce role/ingress authorization."""

    if not isinstance(metadata, AdminMetadata):
        raise TypeError("metadata must be AdminMetadata")
    result = public_summary(status)
    result["config_revision"] = status.management_intent.config_revision
    result["details"] = {
        "installation": status.installation.value,
        "daemon": status.daemon.value,
        "authorization": status.authorization.value,
        "task": status.task.value if status.task is not None else None,
        **metadata.to_dict(),
    }
    if status.reachability_evidence is not None:
        result["details"]["reachability_evidence"] = status.reachability_evidence.to_dict()
    return result
