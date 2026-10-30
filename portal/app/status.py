"""系统状态采集（设计文档 9.2 / F1 / A1）。

采集字段严格对齐 ``GET /api/status`` 契约；任何单项采集失败都不允许让接口
整体 500，只有全部手段都不可用时才抛 ``ApiError('E1006')``。

psutil 不可用时提供 ``/proc`` 降级实现，便于在开发机上跑单元测试。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import ApiError

try:  # pragma: no cover - 依赖可用性随环境变化
    import psutil
except Exception:  # noqa: BLE001
    psutil = None  # type: ignore[assignment]

_THERMAL_CANDIDATES = (
    "/sys/class/thermal/thermal_zone0/temp",
    "/sys/class/thermal/thermal_zone1/temp",
    "/sys/class/hwmon/hwmon0/temp1_input",
)

# 主网卡判定顺序：配置项 > 默认路由网卡 > 有流量的非回环网卡
_VIRTUAL_PREFIXES = ("lo", "docker", "veth", "br-", "virbr", "zt", "tun", "tap")


@dataclass
class NetSample:
    iface: str
    rx_bytes: int
    tx_bytes: int
    ts: float


class StatusCollector:
    """有状态的采集器，网络速率依赖两次采样的差值。"""

    def __init__(self, *, primary_iface: str | None = None) -> None:
        self._primary_iface = primary_iface
        self._last: NetSample | None = None
        self._last_cpu: tuple[float, int] | None = None

    # ------------------------------------------------------------------ #
    def collect(self, *, interval_ms: int = 1000) -> dict[str, Any]:
        try:
            payload = {
                "time": _rfc3339(),
                "cpu": self._cpu(),
                "mem": self._mem(),
                "temp": self._temp(),
                "disk": self._disk(),
                "net": self._net(interval_ms),
                "uptime_sec": int(_uptime_sec()),
            }
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一转为 E1006
            raise ApiError("E1006", detail=str(exc)) from exc
        if payload["cpu"]["percent"] is None and payload["mem"]["percent"] is None:
            raise ApiError("E1006", detail="cpu and memory unavailable")
        return payload

    # ------------------------------------------------------------------ #
    def _cpu(self) -> dict[str, Any]:
        if psutil is not None:
            percent = float(psutil.cpu_percent(interval=None))
            load1, load5, load15 = (
                (os.getloadavg() + (0.0, 0.0, 0.0))[:3] if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0)
            )
        else:  # pragma: no cover - 降级路径
            percent = _proc_cpu_percent(self)
            load1, load5, load15 = _proc_loadavg()
        load1, load5, load15 = (float(load1), float(load5), float(load15))
        return {
            "percent": round(max(0.0, min(100.0, percent)), 1),
            "load1": round(load1, 2),
            "load5": round(load5, 2),
            "load15": round(load15, 2),
            "cores": int(psutil.cpu_count() if psutil is not None else (os.cpu_count() or 1)),
        }

    def _mem(self) -> dict[str, Any]:
        if psutil is not None:
            mem = psutil.virtual_memory()
            total_mb = mem.total / (1024 * 1024)
            used_mb = (mem.total - mem.available) / (1024 * 1024)
            percent = mem.percent
        else:  # pragma: no cover - 降级路径
            total_kb, avail_kb = _proc_meminfo()
            total_mb = total_kb / 1024
            used_mb = (total_kb - avail_kb) / 1024
            percent = used_mb / total_mb * 100 if total_mb else 0.0
        return {
            "total_mb": round(total_mb, 1),
            "used_mb": round(used_mb, 1),
            "percent": round(float(percent), 1),
        }

    def _temp(self) -> dict[str, Any]:
        if psutil is not None:
            getter = getattr(psutil, "sensors_temperatures", None)
            if getter is not None:
                try:
                    groups = getter() or {}
                except Exception:  # noqa: BLE001 - 部分内核不支持
                    groups = {}
                for entries in groups.values():
                    for entry in entries:
                        current = getattr(entry, "current", None)
                        if current:
                            return {"cpu_c": round(float(current), 1)}
        for candidate in _THERMAL_CANDIDATES:
            try:
                raw = Path(candidate).read_text(encoding="ascii").strip()
                value = float(raw)
            except (OSError, ValueError):
                continue
            if value > 1000:  # 毫摄氏度
                value /= 1000.0
            if 0 < value < 200:
                return {"cpu_c": round(value, 1)}
        return {"cpu_c": None}

    def _disk(self) -> dict[str, Any]:
        target = os.environ.get("CLOUDHOST_DISK_PATH", "/")
        if psutil is not None:
            usage = psutil.disk_usage(target)
            total_gb = usage.total / (1024**3)
            used_gb = usage.used / (1024**3)
            percent = usage.percent
        else:  # pragma: no cover - 降级路径
            stat = os.statvfs(target)
            total = stat.f_blocks * stat.f_frsize
            free = stat.f_bavail * stat.f_frsize
            total_gb = total / (1024**3)
            used_gb = (total - free) / (1024**3)
            percent = used_gb / total_gb * 100 if total_gb else 0.0
        return {
            "root_total_gb": round(total_gb, 1),
            "root_used_gb": round(used_gb, 1),
            "root_percent": round(float(percent), 1),
        }

    def _net(self, interval_ms: int) -> dict[str, Any]:
        iface, rx, tx = self._read_net_counters()
        now = time.monotonic()
        rx_kbps = tx_kbps = 0.0
        last = self._last
        if last is not None and now > last.ts and last.iface == iface:
            elapsed = now - last.ts
            rx_kbps = max(0.0, (rx - last.rx_bytes) * 8 / 1000 / elapsed)
            tx_kbps = max(0.0, (tx - last.tx_bytes) * 8 / 1000 / elapsed)
        self._last = NetSample(iface=iface, rx_bytes=rx, tx_bytes=tx, ts=now)
        return {
            "iface": iface,
            "rx_kbps": round(rx_kbps, 1),
            "tx_kbps": round(tx_kbps, 1),
            "interval_ms": interval_ms,
        }

    def _read_net_counters(self) -> tuple[str, int, int]:
        candidates: list[str] = []
        if self._primary_iface:
            candidates.append(self._primary_iface)
        if psutil is not None:
            stats = psutil.net_io_counters(pernic=True)
            if not candidates:
                default_iface = _default_route_iface()
                if default_iface and default_iface in stats:
                    candidates.append(default_iface)
            candidates.extend(n for n in stats if n not in candidates)
            for name in candidates:
                if name.startswith(_VIRTUAL_PREFIXES):
                    continue
                counters = stats.get(name)
                if counters is not None:
                    return name, int(counters.bytes_recv), int(counters.bytes_sent)
            if stats:
                name = next(iter(stats))
                return name, int(stats[name].bytes_recv), int(stats[name].bytes_sent)
        counters = _proc_net_dev()
        for name in candidates or list(counters):
            if name in counters:
                return name, counters[name][0], counters[name][1]
        if counters:
            name = next(iter(counters))
            return name, counters[name][0], counters[name][1]
        return "none", 0, 0


def _rfc3339() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _uptime_sec() -> float:
    if psutil is not None:
        return time.time() - psutil.boot_time()
    try:
        return float(Path("/proc/uptime").read_text(encoding="ascii").split()[0])
    except (OSError, ValueError, IndexError):  # pragma: no cover
        return 0.0


def _default_route_iface() -> str | None:
    try:
        for line in Path("/proc/net/route").read_text(encoding="ascii").splitlines()[1:]:
            fields = line.split()
            if len(fields) > 2 and fields[1] == "00000000":
                return fields[0]
    except OSError:  # pragma: no cover
        return None
    return None


def _proc_cpu_percent(collector: StatusCollector) -> float:  # pragma: no cover
    try:
        fields = Path("/proc/stat").read_text(encoding="ascii").splitlines()[0].split()[1:]
        values = [float(v) for v in fields[:8]]
    except (OSError, ValueError, IndexError):
        return 0.0
    idle = values[3] + values[4]
    total = sum(values)
    last = collector._last_cpu
    collector._last_cpu = (total, int(idle))
    if last is None:
        return 0.0
    total_delta = total - last[0]
    idle_delta = idle - last[1]
    if total_delta <= 0:
        return 0.0
    return max(0.0, min(100.0, (1 - idle_delta / total_delta) * 100))


def _proc_loadavg() -> tuple[float, float, float]:  # pragma: no cover
    try:
        load1, load5, load15 = os.getloadavg()
        return float(load1), float(load5), float(load15)
    except (OSError, AttributeError):
        return 0.0, 0.0, 0.0


def _proc_meminfo() -> tuple[int, int]:  # pragma: no cover
    total = avail = 0
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, _, rest = line.partition(":")
            value = int(rest.split()[0])
            if key == "MemTotal":
                total = value
            elif key == "MemAvailable":
                avail = value
    except (OSError, ValueError, IndexError):
        return 0, 0
    return total, avail


def _proc_net_dev() -> dict[str, tuple[int, int]]:  # pragma: no cover
    counters: dict[str, tuple[int, int]] = {}
    try:
        lines = Path("/proc/net/dev").read_text(encoding="ascii").splitlines()[2:]
    except OSError:
        return counters
    for line in lines:
        name, _, rest = line.partition(":")
        fields = rest.split()
        if len(fields) >= 9:
            try:
                counters[name.strip()] = (int(fields[0]), int(fields[8]))
            except ValueError:
                continue
    return counters


def collect_status(collector: StatusCollector, *, interval_ms: int = 1000) -> dict[str, Any]:
    """便捷函数，主要供测试使用。"""

    return collector.collect(interval_ms=interval_ms)


def flatten(payload: dict[str, Any]) -> dict[str, Any]:
    """把嵌套状态展开成点号键，方便前端或测试断言。"""

    out: dict[str, Any] = {}

    def walk(prefix: str, node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(f"{prefix}.{key}" if prefix else str(key), value)
        else:
            out[prefix] = node

    walk("", payload)
    return out


__all__ = ["StatusCollector", "collect_status", "flatten", "asdict"]
