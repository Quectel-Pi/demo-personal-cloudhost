from dataclasses import replace
from datetime import datetime

from fastapi.testclient import TestClient

from app.main import create_app
from conftest import TEST_PASSWORD, TEST_USER


def test_remote_summary_requires_login(client):
    response = client.get("/api/remote/status")
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


def test_remote_summary_reports_unknown_runtime_without_success_claim(authed):
    response = authed.get("/api/remote/status")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["summary"] == {
        "active_provider": "none",
        "enabled": False,
        "paused_this_boot": None,
        "connection": "unknown",
        "https": "unknown",
        "reachability": "unverified",
    }
    response_time = datetime.fromisoformat(payload["observed_at"].replace("Z", "+00:00"))
    assert response_time.tzinfo is not None
    assert payload["stale"] is True
    body = response.text
    for private_field in (
        "device_sn",
        "public_ip",
        "binding_state",
        "authorization",
        "config_revision",
    ):
        assert private_field not in body


def test_peanut_summary_reports_missing_controller(config, fake_auth):
    cfg = replace(
        config,
        remote_enabled=True,
        remote_provider="peanut",
        remote_config_revision=4,
    )
    app = create_app(cfg, audit_enabled=False, authenticator=fake_auth)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        login = client.post(
            "/auth/login",
            data={"username": TEST_USER, "password": TEST_PASSWORD},
        )
        assert login.status_code == 204
        response = client.get("/api/remote/status")

    assert response.status_code == 502
    assert response.json()["error"]["detail"] == "controller_unavailable"
    assert "SN" not in response.text
