"""Admin/session/CSRF boundary for the dedicated Peanut controller."""

from __future__ import annotations

import base64
import logging
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator

from .deps import current_session, is_remote_admin, require_csrf
from .errors import ApiError

router = APIRouter(prefix="/api/remote", tags=["peanut"])
logger = logging.getLogger(__name__)


def activate_after_response(client, actor: str, sid: str, job_id: str) -> None:
    try:
        client.call("activate_job", actor=actor, sid=sid, body={"id": job_id})
    except ApiError:
        logger.error("Peanut job activation unavailable; inspect queued job %s from LAN", job_id)


class Revision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: StrictInt = Field(ge=1)


class Action(Revision):
    operation: Literal[
        "check_config", "start", "stop", "retry", "enable_autostart",
        "disable_autostart", "unbind", "verify_address",
        "set_autostart", "reset_local_config", "reset_account_config", "recover_reset",
    ]
    confirmed_connection_loss: StrictBool = False
    confirmed_account_unbind: StrictBool = False
    enabled: StrictBool | None = None
    confirmed_autostart: StrictBool = False

    @model_validator(mode="after")
    def validate_action_fields(self) -> Action:
        if self.operation == "set_autostart":
            if self.enabled is None:
                raise ValueError("enabled is required")
            if self.enabled and not self.confirmed_autostart:
                raise ValueError("autostart confirmation is required")
        elif {"enabled", "confirmed_autostart"} & self.model_fields_set:
            raise ValueError("autostart fields require set_autostart")
        if self.operation in {"reset_local_config", "reset_account_config", "recover_reset"}:
            if not self.confirmed_connection_loss:
                raise ValueError("connection-loss confirmation is required")
            if self.operation == "reset_account_config" and not self.confirmed_account_unbind:
                raise ValueError("account-unbind confirmation is required")
        return self


class Generation(Revision):
    generation: StrictInt = Field(ge=1)


class SSHAddress(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: StrictStr = Field(max_length=253)
    port: StrictInt = Field(ge=1, le=65535)


class Address(Revision):
    https_url: StrictStr = Field(min_length=1, max_length=2048)
    ssh: SSHAddress | None = None


class ExternalConfirmation(Revision):
    network_context: Literal["external_mobile"]
    confirmed: StrictBool


def admin(request: Request, *, write: bool = False):
    session = current_session(request)
    if not is_remote_admin(session, request.app.state.config):
        raise ApiError("E1003", detail="remote_admin_required")
    if write:
        require_csrf(request, session)
        key = request.headers.get("Idempotency-Key", "")
        if not 8 <= len(key) <= 128 or not key.isascii() or not key.isprintable():
            raise ApiError("E1001", detail="idempotency_key_required")
    return session


def call(request: Request, action: str, body: dict | None = None, *, write=False):
    session = admin(request, write=write)
    payload = dict(body or {})
    if write and action != "submit":
        payload["_idempotency_key"] = request.headers["Idempotency-Key"]
    return request.app.state.peanut.call(action, actor=session.user, sid=session.sid, body=payload)


@router.get("/peanut")
def status(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return call(request, "status")


@router.post("/peanut/actions", status_code=202)
def actions(request: Request, body: Action, response: Response, background_tasks: BackgroundTasks):
    response.headers["Cache-Control"] = "no-store"
    payload = body.model_dump(exclude={"enabled", "confirmed_autostart"})
    if body.operation == "set_autostart":
        payload.update(enabled=body.enabled, confirmed_autostart=body.confirmed_autostart)
    result = call(request, "submit", {
        "key": request.headers.get("Idempotency-Key", ""),
        "request": payload,
    }, write=True)
    request.app.state.audit.write(
        "peanut_action_accepted", user=current_session(request).user,
        operation=body.operation, job_id=result["job_id"],
    )
    session = current_session(request)
    background_tasks.add_task(activate_after_response, request.app.state.peanut,
                              session.user, session.sid, result["job_id"])
    return result


@router.get("/jobs/{job_id}")
def job(request: Request, job_id: str, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return call(request, "job", {"id": job_id})


@router.put("/peanut/address")
def address(request: Request, body: Address, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return call(request, "address", body.model_dump(exclude_unset=True), write=True)


@router.post("/peanut/address/verify", status_code=202)
def verify_address(request: Request, body: Revision, response: Response, background_tasks: BackgroundTasks):
    response.headers["Cache-Control"] = "no-store"
    result = call(request, "submit", {
        "key": request.headers.get("Idempotency-Key", ""),
        "request": {**body.model_dump(), "operation": "verify_address"},
    }, write=True)
    session = current_session(request)
    background_tasks.add_task(activate_after_response, request.app.state.peanut,
                              session.user, session.sid, result["job_id"])
    return result


@router.post("/peanut/external-confirmations")
def external_confirmation(request: Request, body: ExternalConfirmation, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return call(request, "external_confirmation", body.model_dump(), write=True)


@router.post("/peanut/bindings")
def create_binding(request: Request, body: Revision, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return call(request, "binding_create", body.model_dump(), write=True)


@router.get("/peanut/bindings/{binding_id}")
def binding(request: Request, binding_id: str, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return call(request, "binding_get", {"id": binding_id})


@router.get("/peanut/bindings/{binding_id}/image")
def binding_image(request: Request, binding_id: str, generation: int):
    data = call(request, "binding_image", {"id": binding_id, "generation": generation})
    return Response(
        base64.b64decode(data["image"], validate=True), media_type=data["mime"],
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/peanut/bindings/{binding_id}/refresh")
def refresh_binding(request: Request, binding_id: str, body: Generation, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return call(request, "binding_refresh", {**body.model_dump(), "id": binding_id}, write=True)


@router.post("/peanut/bindings/{binding_id}/cancel")
def cancel_binding(request: Request, binding_id: str, body: Generation, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return call(request, "binding_cancel", {**body.model_dump(), "id": binding_id}, write=True)


@router.post("/peanut/management-intents")
def management(request: Request, body: Revision, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return call(request, "management", body.model_dump(), write=True)


@router.get("/peanut/management-redirect/{intent_id}")
def management_redirect(request: Request, intent_id: str):
    value = call(request, "management_redirect", {"id": intent_id})
    return RedirectResponse(value["url"], status_code=303, headers={
        "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
    })


@router.post("/peanut/clear-session", status_code=204)
def clear_session(request: Request):
    call(request, "clear_session", write=True)
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
