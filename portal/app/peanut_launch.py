"""Fixed SDK launcher, executed only by cloudhost-peanut.service."""

import os
import platform
import sys
import subprocess

from .peanut_control import adapter_evidence
from .peanut_credentials import SDK_FILE, PeanutError, inspect_sdk, read_credentials


def main() -> None:
    try:
        if platform.machine() not in {"aarch64", "arm64"} or inspect_sdk() != "installed":
            raise PeanutError("sdk_incompatible")
        credentials = read_credentials()
        evidence = adapter_evidence()
        result = subprocess.run(
            ["/lib/ld-linux-aarch64.so.1", "--list", str(SDK_FILE)],
            capture_output=True, timeout=5, check=False,
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
        )
        if result.returncode:
            raise PeanutError("runtime_dependencies_missing")
    except PeanutError as exc:
        print("PHTunnel preflight: " + exc.reason, file=sys.stderr)
        raise SystemExit(78) from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        print("PHTunnel preflight: runtime_dependencies_unavailable", file=sys.stderr)
        raise SystemExit(78) from exc
    os.umask(0o077)
    argv = [
        str(SDK_FILE), "--appid=" + credentials.app_id, "--appkey=" + credentials.app_key,
        "--config=/var/lib/cloudhost-peanut/sdk/phtunnel.json",
        "--pid=/var/lib/cloudhost-peanut/sdk/phtunnel.pid",
        "--log=/var/log/cloudhost-peanut/phtunnel.log",
        "--forward=127.0.0.1:18080", "--noqr",
    ]
    if evidence.get("rpc_isolated"):
        argv += ["--rpc", "--rpcport=16062"]
    os.execve(str(SDK_FILE), argv, {"PATH": "/usr/bin:/bin", "LANG": "C", "HOME": "/var/lib/cloudhost-peanut/sdk"})


if __name__ == "__main__":
    main()
