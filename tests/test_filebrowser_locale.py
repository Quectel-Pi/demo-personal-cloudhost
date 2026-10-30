import json
from pathlib import Path

import pytest

from set_filebrowser_locale import set_default_locale

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SOURCE = (PROJECT_ROOT / "install.sh").read_text(encoding="utf-8")


def test_set_default_locale_preserves_other_filebrowser_settings(tmp_path: Path):
    config_path = tmp_path / "filebrowser.json"
    original = {
        "port": 8081,
        "database": "/var/lib/cloudhost/filebrowser/filebrowser.db",
        "noauth": True,
        "defaults": {"locale": "zh-cn", "viewMode": "mosaic"},
    }
    config_path.write_text(json.dumps(original), encoding="utf-8")

    set_default_locale(config_path)

    updated = json.loads(config_path.read_text(encoding="utf-8"))
    assert updated["defaults"]["locale"] == "en"
    assert updated["defaults"]["viewMode"] == "mosaic"
    assert {key: updated[key] for key in ("port", "database", "noauth")} == {
        key: original[key] for key in ("port", "database", "noauth")
    }


def test_set_default_locale_creates_defaults_section(tmp_path: Path):
    config_path = tmp_path / "filebrowser.json"
    config_path.write_text('{"noauth": true}\n', encoding="utf-8")

    set_default_locale(config_path)

    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "noauth": True,
        "defaults": {"locale": "en"},
    }


def test_set_default_locale_rejects_invalid_defaults(tmp_path: Path):
    config_path = tmp_path / "filebrowser.json"
    config_path.write_text('{"defaults": []}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="invalid defaults"):
        set_default_locale(config_path)


def test_installer_sets_shared_user_locale_while_filebrowser_is_stopped():
    stop_position = INSTALL_SOURCE.index('systemctl stop cloudhost-files.service')
    update_position = INSTALL_SOURCE.index("users update 1 --locale en")
    restart_position = INSTALL_SOURCE.index(
        "systemctl restart cloudhost-files.service ||\n\t\t\twarn",
        update_position,
    )

    assert stop_position < update_position < restart_position
    assert "--config \"${ETC_DIR}/filebrowser.json\"" in INSTALL_SOURCE
    assert "--database \"${STATE_DIR}/filebrowser/filebrowser.db\"" in INSTALL_SOURCE
    assert "set_filebrowser_locale.py" in INSTALL_SOURCE
