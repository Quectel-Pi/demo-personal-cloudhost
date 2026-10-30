"""Authenticated gateway context only; never an authorization/role grant."""

from __future__ import annotations

import hmac
from urllib.parse import urlsplit

from starlette.concurrency import run_in_threadpool
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse

from .errors import ApiError

PUBLIC_SESSION_COOKIE = "__Host-ch_session"
PUBLIC_CSRF_COOKIE = "__Host-ch_csrf"


def public_ingress(request: HTTPConnection) -> bool:
    return request.scope.get("state", {}).get("peanut_public", False) is True


def cookie_names(request: HTTPConnection) -> tuple[str, str]:
    if public_ingress(request):
        return PUBLIC_SESSION_COOKIE, PUBLIC_CSRF_COOKIE
    return "ch_session", "ch_csrf"


def origin_allowed(request: HTTPConnection) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True
    scheme = "https" if public_ingress(request) else {
        "ws": "http", "wss": "https",
    }.get(request.url.scheme, request.url.scheme)
    return origin == scheme + "://" + request.headers.get("host", "")


class IngressMiddleware:
    def __init__(self, app, *, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        request = Request(scope) if scope["type"] == "http" else HTTPConnection(scope)
        marker = request.headers.get("x-cloudhost-transport", "")
        scope.setdefault("state", {})["peanut_public"] = False
        try:
            if marker:
                from .deps import is_loopback

                if (
                    not self.token or not marker.isascii() or not hmac.compare_digest(marker, self.token)
                    or not request.client or not is_loopback(request.client.host)
                ):
                    raise ApiError("E1003", detail="invalid_gateway_context")
                context = await run_in_threadpool(request.app.state.peanut.call, "address_context")
                address = urlsplit(context["https_url"])
                if (
                    not address.hostname
                    or request.headers.get("host", "").lower() != address.netloc.lower()
                ):
                    raise ApiError("E1003", detail="unregistered_public_host")
                if scope["path"] != "/.well-known/cloudhost-peanut-check" and not context["ingress_verified"]:
                    raise ApiError("E5004", detail="https_ingress_not_verified")
                scope["state"]["peanut_public"] = True
            if scope["type"] == "http" and scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
                if not origin_allowed(request):
                    raise ApiError("E1003", detail="cross_origin_request")
        except ApiError as exc:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await JSONResponse(exc.payload(), status_code=exc.status_code,
                                   headers={"Cache-Control": "no-store"})(scope, receive, send)
            return
        await self.app(scope, receive, send)
