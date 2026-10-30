"""状态接口契约（设计文档 9.2 / F1 / A1）。"""

from __future__ import annotations

import time

from app.status import StatusCollector, flatten


STATUS_FIELDS = {
    "time",
    "cpu.percent",
    "cpu.load1",
    "cpu.load5",
    "cpu.load15",
    "mem.total_mb",
    "mem.used_mb",
    "mem.percent",
    "temp.cpu_c",
    "disk.root_total_gb",
    "disk.root_used_gb",
    "disk.root_percent",
    "net.iface",
    "net.rx_kbps",
    "net.tx_kbps",
    "uptime_sec",
}


def test_status_contract(authed):
    response = authed.get("/api/status")
    assert response.status_code == 200
    payload = response.json()

    flat = flatten(payload)
    missing = STATUS_FIELDS - set(flat)
    assert not missing, f"缺少契约字段：{sorted(missing)}"

    assert isinstance(payload["time"], str) and "T" in payload["time"]
    assert 0 <= payload["cpu"]["percent"] <= 100
    assert 0 <= payload["mem"]["percent"] <= 100
    assert 0 <= payload["disk"]["root_percent"] <= 100
    assert payload["mem"]["total_mb"] > 0
    assert payload["disk"]["root_total_gb"] > 0
    assert isinstance(payload["net"]["iface"], str) and payload["net"]["iface"]
    assert isinstance(payload["uptime_sec"], int) and payload["uptime_sec"] >= 0
    # 温度允许为 null（文档 9.2）
    assert payload["temp"]["cpu_c"] is None or payload["temp"]["cpu_c"] > -20


def test_status_repeated_sampling(authed):
    """A1：连续 10 次采样无失败。"""

    for _ in range(10):
        response = authed.get("/api/status")
        assert response.status_code == 200, response.text


def test_net_rate_is_non_negative():
    collector = StatusCollector()
    first = collector.collect()
    time.sleep(0.05)
    second = collector.collect()
    assert second["net"]["rx_kbps"] >= 0
    assert second["net"]["tx_kbps"] >= 0
    assert first["net"]["iface"] == second["net"]["iface"]


def test_overview_aggregates(authed):
    response = authed.get("/api/overview")
    assert response.status_code == 200
    payload = response.json()
    assert payload["user"] == "admin"
    assert payload["links"]["files"] == "/files/"
    assert payload["links"]["site"] == "/site/"
    assert payload["version"]
    assert payload["tls_mode"] == "off"
    assert "status" in payload and "services" in payload


def test_public_config(authed):
    response = authed.get("/api/config")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status_interval_ms"] == 1000
    assert payload["tls_mode"] == "off"
    assert payload["access_mode"] == "lan"
    # 不允许泄漏任何密钥字段
    assert "session_secret_path" not in payload
    assert "password_hash_path" not in payload


def test_healthz_is_public(client):
    response = client.get("/api/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_services_contract(authed):
    response = authed.get("/api/services")
    assert response.status_code == 200
    payload = response.json()
    assert payload["allowed"] == [
        "caddy",
        "cloudhost-portal",
        "cloudhost-files",
        "cloudhost-ttyd",
    ]
    for svc in payload["services"]:
        assert set(svc) == {"name", "state", "enabled", "since", "detail"}
        assert svc["state"] in ("running", "stopped", "failed", "starting")
        assert isinstance(svc["enabled"], bool)
