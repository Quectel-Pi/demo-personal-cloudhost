import os
import shutil
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = PROJECT_ROOT / "install.sh"
BASH = shutil.which("bash")


@pytest.mark.skipif(not BASH or os.name == "nt", reason="requires a native Bash environment")
def test_install_help_and_invalid_option_are_english_and_non_destructive():
    environment = os.environ.copy()
    environment.update({"LC_ALL": "C", "LANG": "C"})

    syntax = subprocess.run(
        [BASH, "-n", str(INSTALL_SCRIPT)],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert syntax.returncode == 0, syntax.stderr

    help_result = subprocess.run(
        [BASH, str(INSTALL_SCRIPT), "--help"],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert "Options:" in help_result.stdout
    assert "Service account" in help_result.stdout
    assert "系统依赖" not in help_result.stdout

    invalid_result = subprocess.run(
        [BASH, str(INSTALL_SCRIPT), "--unknown-option"],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert invalid_result.returncode == 1
    assert "Unknown option: --unknown-option" in invalid_result.stderr
