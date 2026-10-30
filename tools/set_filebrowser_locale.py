#!/usr/bin/env python3
"""Set File Browser's default locale to English without changing other settings."""

import json
import sys
from pathlib import Path


def set_default_locale(config_path: Path) -> None:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("File Browser configuration must be a JSON object")
    defaults = config.setdefault("defaults", {})
    if not isinstance(defaults, dict):
        raise ValueError("File Browser configuration has an invalid defaults section")
    defaults["locale"] = "en"
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(f"Usage: {sys.argv[0]} FILEBROWSER_CONFIG")
    set_default_locale(Path(sys.argv[1]))
