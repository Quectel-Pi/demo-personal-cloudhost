#!/usr/bin/env python3
"""Apply only the verified official File Browser branding options to its DB.

Run as the service user while File Browser is stopped (BoltDB is exclusive).
Never resets the database or modifies user permissions/files.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


SUPPORTED_VERSION = "2.31.2"


def apply_branding(binary: Path, config: Path, database: Path, assets: Path) -> None:
    version = subprocess.run(
        [str(binary), "version"], check=True, capture_output=True, text=True
    ).stdout.strip()
    if not re.search(r"\bv?" + re.escape(SUPPORTED_VERSION) + r"(?:/|\s|$)", version):
        raise ValueError(f"Branding not verified for {version!r}; expected {SUPPORTED_VERSION}")
    if not (assets / "custom.css").is_file():
        raise ValueError("Official branding asset custom.css is missing")
    subprocess.run(
        [
            str(binary), "--config", str(config), "--database", str(database),
            "config", "set", "--branding.name", "Pi M2 · Personal Cloud",
            "--branding.files", str(assets), "--branding.theme", "dark",
            "--branding.disableExternal", "--branding.color", "#66d9ef",
        ],
        check=True, capture_output=True, text=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("binary", "config", "database", "assets"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    args = parser.parse_args()
    apply_branding(args.binary, args.config, args.database, args.assets)


if __name__ == "__main__":
    main()
