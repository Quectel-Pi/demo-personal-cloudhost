"""Policy-gated pure projections for remote status views.

This helper is not authentication, trusted ingress classification, or endpoint
security. Callers must obtain role and ingress through trusted server-side
mechanisms and must still enforce no-store and all other endpoint controls.
"""

from __future__ import annotations

from typing import Any

from app.remote_policy import Capability, Ingress, Role, evaluate_policy
from app.remote_status import AdminMetadata, RemoteStatus, admin_status, public_summary


def project_remote_view(
    role: Role,
    ingress: Ingress,
    capability: Capability,
    status: RemoteStatus,
    metadata: AdminMetadata | None = None,
) -> dict[str, Any]:
    """Return the exact authorized summary or admin status projection."""

    if type(role) is not Role:
        raise TypeError("role must be Role")
    if type(ingress) is not Ingress:
        raise TypeError("ingress must be Ingress")
    if type(capability) is not Capability:
        raise TypeError("capability must be Capability")
    if capability not in {Capability.SUMMARY, Capability.DETAILS}:
        raise ValueError("capability must be SUMMARY or DETAILS")
    if not isinstance(status, RemoteStatus):
        raise TypeError("status must be RemoteStatus")
    if metadata is not None and not isinstance(metadata, AdminMetadata):
        raise TypeError("metadata must be AdminMetadata or None")

    decision = evaluate_policy(
        role, ingress, capability, provider=status.management_intent.active_provider
    )
    if not decision.allowed:
        raise PermissionError("role and ingress are not allowed this remote view")

    if capability is Capability.SUMMARY:
        return public_summary(status)
    if metadata is None:
        raise TypeError("metadata is required for the DETAILS view")
    return admin_status(status, metadata)
