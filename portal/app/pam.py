"""PAM 密码校验（POSIX，ctypes 直接绑定 libpam，不新增 pip 依赖）。

重要约束：非 root 进程只能校验「自己」的密码。pam_unix 对非 root 调用者会走
setgid 的 unix_chkpwd，而该 helper 只接受与调用者 uid 同名的用户；否则返回
PAM_AUTHINFO_UNAVAIL。因此 system_user 必须等于 portal 服务的运行用户。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import secrets
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)

SERVICE_NAME = "cloudhost"

PAM_SUCCESS = 0
PAM_SYSTEM_ERR = 4
PAM_BUF_ERR = 5
PAM_PERM_DENIED = 6
PAM_AUTH_ERR = 7
PAM_CRED_INSUFFICIENT = 8
PAM_AUTHINFO_UNAVAIL = 9
PAM_USER_UNKNOWN = 10
PAM_MAXTRIES = 11
PAM_NEW_AUTHTOK_REQD = 12
PAM_ACCT_EXPIRED = 13
PAM_CONV_ERR = 19
PAM_AUTHTOK_ERR = 20
PAM_ABORT = 26

PAM_PROMPT_ECHO_OFF = 1
PAM_PROMPT_ECHO_ON = 2
PAM_ERROR_MSG = 3
PAM_TEXT_INFO = 4

_DENIED_CODES = {
    PAM_AUTH_ERR,
    PAM_MAXTRIES,
    PAM_PERM_DENIED,
    PAM_USER_UNKNOWN,
    PAM_AUTHTOK_ERR,
    PAM_NEW_AUTHTOK_REQD,
    PAM_ACCT_EXPIRED,
    PAM_CRED_INSUFFICIENT,
}
_UNAVAILABLE_CODES = {
    PAM_AUTHINFO_UNAVAIL,
    PAM_SYSTEM_ERR,
    PAM_BUF_ERR,
    PAM_CONV_ERR,
    PAM_ABORT,
}


class AuthOutcome(str, Enum):
    OK = "ok"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


@dataclass(frozen=True)
class PamResult:
    outcome: AuthOutcome
    code: int = -1
    message: str = ""


class _PamMessage(ctypes.Structure):
    _fields_ = [("msg_style", ctypes.c_int), ("msg", ctypes.c_char_p)]


class _PamResponse(ctypes.Structure):
    _fields_ = [("resp", ctypes.c_char_p), ("resp_retcode", ctypes.c_int)]


_CONV_FUNC = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.c_int,
    ctypes.POINTER(ctypes.POINTER(_PamMessage)),
    ctypes.POINTER(ctypes.POINTER(_PamResponse)),
    ctypes.c_void_p,
)


class _PamConv(ctypes.Structure):
    _fields_ = [("conv", _CONV_FUNC), ("appdata_ptr", ctypes.c_void_p)]


def _load_libc() -> ctypes.CDLL | None:
    name = ctypes.util.find_library("c")
    for candidate in (name, "libc.so.6", "libc.so"):
        if not candidate:
            continue
        try:
            lib = ctypes.CDLL(candidate, use_errno=True)
        except OSError:
            continue
        lib.malloc.restype = ctypes.c_void_p
        lib.malloc.argtypes = [ctypes.c_size_t]
        lib.calloc.restype = ctypes.c_void_p
        lib.calloc.argtypes = [ctypes.c_size_t, ctypes.c_size_t]
        return lib
    return None


_LIBC = _load_libc() if os.name != "nt" else None


def _malloc_str(text: str) -> int | None:
    """分配 NUL 结尾的缓冲区，交给 libpam free。返回地址（0 表示失败）。"""

    if _LIBC is None:
        return None
    raw = text.encode("utf-8") + b"\0"
    buf = _LIBC.malloc(len(raw))
    if not buf:
        return None
    ctypes.memmove(buf, raw, len(raw))
    return buf


class _Conversation:
    """一次认证专用的 PAM conversation；密码只在本次调用的缓冲区里出现。"""

    def __init__(self, password: str) -> None:
        self._password = password
        self._callback = _CONV_FUNC(self._handle)
        self._struct = _PamConv(self._callback, None)

    @property
    def struct(self) -> _PamConv:
        return self._struct

    def _handle(self, num_msg, msg, resp, appdata):  # noqa: ANN001 - ctypes 回调
        if _LIBC is None or num_msg <= 0 or not msg or not resp:
            return PAM_CONV_ERR
        size = ctypes.sizeof(_PamResponse) * num_msg
        pointer = _LIBC.calloc(1, size)
        if not pointer:
            return PAM_BUF_ERR
        responses = ctypes.cast(pointer, ctypes.POINTER(_PamResponse))
        for index in range(num_msg):
            style = msg[index].contents.msg_style
            if style in (PAM_PROMPT_ECHO_OFF, PAM_PROMPT_ECHO_ON):
                buffer = _malloc_str(self._password)
                if not buffer:
                    return PAM_BUF_ERR
                responses[index].resp = ctypes.cast(ctypes.c_void_p(buffer), ctypes.c_char_p)
            # ERROR_MSG / TEXT_INFO：resp 必须保持 NULL
            responses[index].resp_retcode = 0
        resp[0] = responses
        return PAM_SUCCESS


def _load_libpam() -> ctypes.CDLL | None:
    for candidate in (ctypes.util.find_library("pam"), "libpam.so.0", "libpam.so"):
        if not candidate:
            continue
        try:
            lib = ctypes.CDLL(candidate)
        except OSError:
            continue
        lib.pam_start.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.POINTER(_PamConv),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        lib.pam_start.restype = ctypes.c_int
        lib.pam_authenticate.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.pam_authenticate.restype = ctypes.c_int
        lib.pam_acct_mgmt.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.pam_acct_mgmt.restype = ctypes.c_int
        lib.pam_end.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.pam_end.restype = ctypes.c_int
        lib.pam_strerror.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.pam_strerror.restype = ctypes.c_char_p
        return lib
    return None


def _describe(lib, pamh, code: int) -> str:  # noqa: ANN001
    try:
        raw = lib.pam_strerror(pamh, code)
        return raw.decode("utf-8", "replace") if raw else f"PAM error {code}"
    except Exception:  # noqa: BLE001
        return f"PAM error {code}"


def _classify(lib, pamh, code: int) -> PamResult:  # noqa: ANN001
    if code == PAM_SUCCESS:
        return PamResult(AuthOutcome.OK)
    if code in _DENIED_CODES:
        return PamResult(AuthOutcome.DENIED, code, _describe(lib, pamh, code))
    if code in _UNAVAILABLE_CODES:
        return PamResult(AuthOutcome.UNAVAILABLE, code, _describe(lib, pamh, code))
    return PamResult(AuthOutcome.ERROR, code, _describe(lib, pamh, code))


def authenticate(
    username: str, password: str, *, service: str = SERVICE_NAME
) -> PamResult:
    """用 PAM 校验系统密码。不要记录 username/password 之外的敏感信息。"""

    if not username or not password:
        return PamResult(AuthOutcome.DENIED, message="empty credential")
    if "\x00" in username or "\x00" in password:
        return PamResult(AuthOutcome.DENIED, message="invalid credential")
    if os.name == "nt":
        return PamResult(AuthOutcome.UNAVAILABLE, message="PAM only available on POSIX")

    lib = _load_libpam()
    if lib is None:
        return PamResult(AuthOutcome.UNAVAILABLE, message="libpam not found")

    conversation = _Conversation(password)
    pamh = ctypes.c_void_p()
    code = lib.pam_start(
        service.encode("utf-8"),
        username.encode("utf-8"),
        ctypes.byref(conversation.struct),
        ctypes.byref(pamh),
    )
    if code != PAM_SUCCESS:
        return PamResult(AuthOutcome.ERROR, code, f"pam_start failed: {code}")

    last = code
    try:
        last = lib.pam_authenticate(pamh, 0)
        if last != PAM_SUCCESS:
            return _classify(lib, pamh, last)
        last = lib.pam_acct_mgmt(pamh, 0)
        if last != PAM_SUCCESS:
            return _classify(lib, pamh, last)
        return PamResult(AuthOutcome.OK)
    finally:
        # pam_end 会 free 掉 conversation 分配给它的响应缓冲区
        lib.pam_end(pamh, last)


def availability(username: str) -> bool:
    """安装探测：用随机密码判断 PAM 链路是否可用（不校验真实密码）。

    DENIED/OK 都说明 pam_unix + helper 工作正常；UNAVAILABLE 说明
    NoNewPrivileges / setgid / /etc/shadow 权限有问题。
    """

    probe = secrets.token_urlsafe(24)
    result = authenticate(username, probe)
    return result.outcome in (AuthOutcome.DENIED, AuthOutcome.OK)
