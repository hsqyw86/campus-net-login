"""统一的路径与日志位置。

所有文件都放在程序目录下，整个文件夹可以随意移动 / 拷到 U 盘。
"""

from __future__ import annotations

import json
import os
import sys

# 程序根目录（login.py 所在目录）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CONFIG_FILE = os.environ.get("CAMPUSNET_CONFIG") or os.path.join(ROOT, "config.json")
EXAMPLE_CONFIG_FILE = os.path.join(ROOT, "config.example.json")
CREDENTIAL_FILE = os.environ.get("CAMPUSNET_CREDENTIALS") or os.path.join(ROOT, "credentials.dat")
STATE_FILE = os.environ.get("CAMPUSNET_STATE") or os.path.join(ROOT, "state.json")
HOOK_FILE = os.environ.get("CAMPUSNET_HOOK") or os.path.join(ROOT, "custom_hook.py")
LOG_DIR = os.path.join(ROOT, "logs")
LOG_FILE = os.path.join(LOG_DIR, "login.log")

PYTHON_EXE = sys.executable
PYTHONW_EXE = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
if not os.path.exists(PYTHONW_EXE):
    PYTHONW_EXE = sys.executable

APP_NAME = "校园网一键登录"
RUN_KEY_NAME = "CampusNetAutoLogin"  # 写入 HKCU\...\Run 的值名


def ensure_log_dir() -> str:
    os.makedirs(LOG_DIR, exist_ok=True)
    return LOG_DIR


def rel(path: str) -> str:
    """把绝对路径转成相对程序目录的短路径，用于日志显示。"""
    try:
        return os.path.relpath(path, ROOT)
    except ValueError:
        return path


def show(path: str) -> str:
    """给人看的路径：在程序目录里就显示相对路径，在外面的（如临时文件）就原样显示。"""
    if not path:
        return ""
    full = os.path.abspath(path)
    root = os.path.abspath(ROOT)
    try:
        inside = os.path.commonpath([full, root]) == root
    except ValueError:  # 不同盘符
        inside = False
    return rel(full) if inside else full


def write_json_atomic(path: str, data, *, indent: int = 2) -> str:
    """先写临时文件再替换，避免写到一半断电/崩溃留下半个 JSON。

    正常写入会被杀软、同步盘占用而失败，所以调用方自己决定要不要吞掉 OSError。
    """
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=indent)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:
        # 临时文件要是留下垃圾就顺手清掉，别占着目录
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise
    return path


def write_python_hint() -> str:
    """把当前 pythonw.exe 路径写给 .vbs 启动器用（找不到 Python 时的兜底）。"""
    hint = os.path.join(ROOT, "python_path.txt")
    try:
        with open(hint, "w", encoding="utf-8") as fh:
            fh.write(PYTHONW_EXE + "\n")
    except OSError:
        pass
    return hint
