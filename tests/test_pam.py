"""PAM 绑定的纯单元测试（不依赖真实 libpam / Linux）。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "portal"))

from app import pam  # noqa: E402


def _no_libpam(*args, **kwargs):  # noqa: ANN002, ANN003
    raise AssertionError("不应在参数校验阶段加载 libpam")


def test_empty_password_denied(monkeypatch):
    monkeypatch.setattr(pam, "_load_libpam", _no_libpam)
    result = pam.authenticate("tris", "")
    assert result.outcome is pam.AuthOutcome.DENIED


def test_empty_username_denied(monkeypatch):
    monkeypatch.setattr(pam, "_load_libpam", _no_libpam)
    result = pam.authenticate("", "secret")
    assert result.outcome is pam.AuthOutcome.DENIED


def test_nul_byte_rejected(monkeypatch):
    monkeypatch.setattr(pam, "_load_libpam", _no_libpam)
    assert pam.authenticate("tris", "abc\x00def").outcome is pam.AuthOutcome.DENIED
    assert pam.authenticate("tri\x00s", "secret").outcome is pam.AuthOutcome.DENIED


def test_windows_unavailable(monkeypatch):
    monkeypatch.setattr(pam.os, "name", "nt")
    result = pam.authenticate("tris", "secret")
    assert result.outcome is pam.AuthOutcome.UNAVAILABLE


@pytest.mark.parametrize(
    "code,expected",
    [
        (pam.PAM_AUTH_ERR, pam.AuthOutcome.DENIED),
        (pam.PAM_MAXTRIES, pam.AuthOutcome.DENIED),
        (pam.PAM_PERM_DENIED, pam.AuthOutcome.DENIED),
        (pam.PAM_USER_UNKNOWN, pam.AuthOutcome.DENIED),
        (pam.PAM_AUTHTOK_ERR, pam.AuthOutcome.DENIED),
        (pam.PAM_NEW_AUTHTOK_REQD, pam.AuthOutcome.DENIED),
        (pam.PAM_ACCT_EXPIRED, pam.AuthOutcome.DENIED),
        (pam.PAM_CRED_INSUFFICIENT, pam.AuthOutcome.DENIED),
        (pam.PAM_AUTHINFO_UNAVAIL, pam.AuthOutcome.UNAVAILABLE),
        (pam.PAM_SYSTEM_ERR, pam.AuthOutcome.UNAVAILABLE),
        (pam.PAM_BUF_ERR, pam.AuthOutcome.UNAVAILABLE),
        (pam.PAM_CONV_ERR, pam.AuthOutcome.UNAVAILABLE),
        (pam.PAM_ABORT, pam.AuthOutcome.UNAVAILABLE),
        (pam.PAM_SUCCESS, pam.AuthOutcome.OK),
        (999, pam.AuthOutcome.ERROR),
    ],
)
def test_classify_mapping(code: int, expected: pam.AuthOutcome):
    assert pam._classify(None, None, code).outcome is expected


def test_missing_libpam(monkeypatch):
    monkeypatch.setattr(pam.os, "name", "posix")
    monkeypatch.setattr(pam, "_load_libpam", lambda: None)
    result = pam.authenticate("tris", "secret")
    assert result.outcome is pam.AuthOutcome.UNAVAILABLE
    assert "libpam" in result.message


def test_availability_false_when_libpam_missing(monkeypatch):
    monkeypatch.setattr(pam, "_load_libpam", lambda: None)
    assert pam.availability("tris") is False


def test_availability_true_on_denied(monkeypatch):
    monkeypatch.setattr(
        pam,
        "authenticate",
        lambda username, password, **kw: pam.PamResult(pam.AuthOutcome.DENIED),
    )
    assert pam.availability("tris") is True


def test_availability_false_on_unavailable(monkeypatch):
    monkeypatch.setattr(
        pam,
        "authenticate",
        lambda username, password, **kw: pam.PamResult(pam.AuthOutcome.UNAVAILABLE),
    )
    assert pam.availability("tris") is False


@pytest.mark.skipif(os.name == "nt", reason="仅 POSIX 加载 libpam")
def test_posix_bad_password_is_not_ok(monkeypatch):
    """Inject the native boundary; ordinary tests must not invoke host PAM."""
    class DeniedPam:
        def pam_start(self, *args):
            return pam.PAM_AUTH_ERR

        def pam_strerror(self, *args):
            return b"denied"

    monkeypatch.setattr(pam, "_load_libpam", DeniedPam)
    result = pam.authenticate("cloudhost-nonexistent-user", "not-a-real-password")
    assert result.outcome is not pam.AuthOutcome.OK
