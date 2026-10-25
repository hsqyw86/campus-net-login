"""账号密码的本地加密存储（Windows DPAPI，无需密码库）。

原理：调用 Windows 自带的 CryptProtectData，用「当前 Windows 用户」的密钥加密。
加密后的文件只有同一台机器、同一个 Windows 账号才能解开；
把 credentials.dat 拷到别的电脑 / 别的账号上会直接解密失败。

不使用 pywin32，所以不需要 pip 安装任何东西。
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import time
from ctypes import wintypes
from dataclasses import dataclass

from . import paths

# 附加熵：让别的程序就算拿到密文也不容易直接解开
_ENTROPY = b"campus-net-login/v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class SecretError(Exception):
    """加密/解密失败。"""


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


class _CRYPTPROTECT_PROMPTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("dwPromptFlags", wintypes.DWORD),
        ("hwndApp", wintypes.HWND),
        ("szPrompt", wintypes.LPCWSTR),
    ]


_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _blob(data: bytes) -> _DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _blob_bytes(blob: _DATA_BLOB) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def _protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise SecretError("当前系统不是 Windows，无法使用 DPAPI 加密存储。")
    in_blob = _blob(data)
    entropy = _blob(_ENTROPY)
    out_blob = _DATA_BLOB()
    ok = _crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        None,
        ctypes.byref(entropy),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise SecretError(f"加密失败（Windows 错误码 {ctypes.get_last_error()}）")
    try:
        return _blob_bytes(out_blob)
    finally:
        _kernel32.LocalFree(out_blob.pbData)


def _unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise SecretError("当前系统不是 Windows，无法使用 DPAPI 解密。")
    in_blob = _blob(data)
    entropy = _blob(_ENTROPY)
    out_blob = _DATA_BLOB()
    ok = _crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        ctypes.byref(entropy),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise SecretError(
            "解密失败：密文不是本机本账号加密的，或文件已损坏（可重新运行设置向导保存一次）。"
        )
    try:
        return _blob_bytes(out_blob)
    finally:
        _kernel32.LocalFree(out_blob.pbData)


@dataclass
class Credentials:
    username: str = ""
    password: str = ""
    saved_at: str = ""

    @property
    def complete(self) -> bool:
        return bool(self.username) and bool(self.password)


def exists(path: str | None = None) -> bool:
    return os.path.exists(path or paths.CREDENTIAL_FILE)


def save(username: str, password: str, path: str | None = None) -> str:
    path = path or paths.CREDENTIAL_FILE
    payload = json.dumps({"username": username, "password": password}, ensure_ascii=False).encode("utf-8")
    blob = base64.b64encode(_protect(payload)).decode("ascii")
    doc = {
        "format": "dpapi-v1",
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "blob": blob,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def load(path: str | None = None) -> Credentials:
    path = path or paths.CREDENTIAL_FILE
    if not os.path.exists(path):
        return Credentials()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise SecretError(f"读取账号文件失败：{exc}") from exc
    if doc.get("format") != "dpapi-v1":
        raise SecretError("账号文件格式不认识，请重新运行设置向导保存一次。")
    try:
        payload = json.loads(_unprotect(base64.b64decode(doc.get("blob", ""))).decode("utf-8"))
    except (json.JSONDecodeError, ValueError) as exc:
        raise SecretError(f"账号文件内容损坏：{exc}") from exc
    return Credentials(
        username=payload.get("username", ""),
        password=payload.get("password", ""),
        saved_at=doc.get("saved_at", ""),
    )


def delete(path: str | None = None) -> bool:
    path = path or paths.CREDENTIAL_FILE
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
