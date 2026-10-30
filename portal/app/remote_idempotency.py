"""Pure canonical fingerprints for validated remote business requests."""

from __future__ import annotations

import hashlib
import json

from .remote_requests import RemoteRequest


def fingerprint_remote_request(request: RemoteRequest) -> str:
    """Return the SHA-256 digest of a request's canonical business fields.

    The digest covers provider, operation, parameters, expected_revision, and
    confirmed_connection_loss. It excludes the idempotency key itself, actor,
    session, ingress, and any reauthentication grant. The caller must
    separately scope a persisted idempotency key by authenticated administrator
    and operation. This helper does not implement lookup, storage, conflict
    responses, or execution deduplication.
    """
    if not isinstance(request, RemoteRequest):
        raise TypeError("request must be a RemoteRequest")

    canonical_json = json.dumps(
        request.to_dict(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(canonical_json.encode("utf-8", errors="strict")).hexdigest()
