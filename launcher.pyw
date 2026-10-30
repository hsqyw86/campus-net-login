"""双击启动器（用 pythonw 运行，不会弹黑框）。

流程：
    配置/账号齐全 → 已经联网就静默退出 → 否则登录
    登录成功 → 右下角飘一条小提示（--quiet 时不提示）
    登录失败 → 弹窗告知原因，并提供「打开设置向导」按钮
    没有配置 → 直接打开设置向导
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from core import config as config_mod, log_setup, paths, runner, secrets  # noqa: E402

APP_TITLE = "校园网一键登录"
_MUTEX_NAME = "Local\\CampusNetAutoLogin_SingleInstance"
_mutex_handle = None
log = logging.getLogger("campusnet")


def setup_logging() -> None:
    # 和命令行共用一份，带 1MB 轮转——开机自启走的是这里，日志不能只增不减
    log_setup.setup_logging(quiet=True)


def acquire_single_instance() -> bool:
    """避免开机自启和手动双击同时跑，弹出两个窗口。"""
    global _mutex_handle
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        _mutex_handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
        if not _mutex_handle:
            return True
        return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS
    except Exception:  # pragma: no cover
        return True


# ------------------------------------------------------------------ 界面提示

def toast(text: str, ms: int = 2400) -> None:
    """右下角一个无边框小提示，几秒后自动消失。"""
    try:
        import tkinter as tk

        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        bg = "#111827"
        frame = tk.Frame(root, bg=bg, bd=0)
        frame.pack(fill="both", expand=True)
        tk.Label(
            frame,
            text=text,
            fg="#e5e7eb",
            bg=bg,
            font=("Microsoft YaHei UI", 10),
            padx=20,
            pady=12,
            justify="left",
        ).pack()
        root.update_idletasks()
        width, height = root.winfo_width(), root.winfo_height()
        screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"+{screen_w - width - 28}+{screen_h - height - 72}")
        root.after(ms, root.destroy)
        root.mainloop()
    except Exception:  # pragma: no cover - 提示失败不影响主流程
        log.debug("提示窗显示失败", exc_info=True)


def ask_open_setup(message: str, detail: str = "") -> bool:
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        text = message
        if detail:
            text += "\n\n" + detail.strip()[:600]
        answer = messagebox.askyesno(APP_TITLE, text + "\n\n要打开设置向导检查一下吗？", parent=root)
        root.destroy()
        return bool(answer)
    except Exception:  # pragma: no cover
        log.debug("弹窗失败", exc_info=True)
        return False


def warning(text: str) -> None:
    """只告知、不给按钮的提示框；tkinter 起不来时退回系统 MessageBox。"""
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        messagebox.showwarning(APP_TITLE, text, parent=root)
        root.destroy()
        return
    except Exception:  # pragma: no cover
        log.debug("提示框失败，改用 MessageBox", exc_info=True)
    try:  # pragma: no cover
        ctypes.windll.user32.MessageBoxW(None, text, APP_TITLE, 0x30)
    except Exception:
        pass


def open_setup() -> None:
    script = os.path.join(paths.ROOT, "setup.pyw")
    if not os.path.exists(script):
        # 这里不能问「要打开设置向导吗」——向导本身就是找不到的那个东西
        warning(f"找不到设置向导：\n{script}\n\n请确认程序文件完整。")
        return
    try:
        subprocess.Popen(
            [paths.PYTHONW_EXE, script],
            cwd=paths.ROOT,
            close_fds=True,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
        )
    except OSError as exc:
        log.error("打开设置向导失败：%s", exc)
        warning(f"打开设置向导失败：\n{exc}")


# ------------------------------------------------------------------ 主流程

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--wait", action="store_true", help="等校园网就绪再登录")
    parser.add_argument("--quiet", action="store_true", help="不显示成功提示（开机自启用）")
    parser.add_argument("--force", action="store_true", help="即使已联网也重新登录")
    parser.add_argument("--no-verify", action="store_true", help="登录后不做联网探测")
    parser.add_argument("--setup", action="store_true", help="直接打开设置向导")
    args, _ = parser.parse_known_args(argv or sys.argv[1:])

    setup_logging()
    log.info("==== 启动器开始（参数：%s）====", " ".join(argv or sys.argv[1:]))

    if not acquire_single_instance():
        log.info("已有实例在运行，本次退出")
        return 0

    if args.setup or not config_mod.exists():
        log.info("打开设置向导")
        open_setup()
        return 0

    try:
        cfg = config_mod.load()
    except config_mod.ConfigError as exc:
        log.warning("配置有问题：%s", exc)
        if ask_open_setup(f"配置有问题：\n{exc}"):
            open_setup()
        return 2

    try:
        creds = secrets.load()
    except secrets.SecretError as exc:
        log.warning("账号读取失败：%s", exc)
        if ask_open_setup(f"读取保存的账号失败：\n{exc}"):
            open_setup()
        return 2

    if not creds.complete:
        log.info("还没有保存账号，打开设置向导")
        if ask_open_setup("还没有保存账号密码，先设置一下吧。"):
            open_setup()
        return 2

    outcome = runner.run_login(
        cfg,
        creds.username,
        creds.password,
        wait=args.wait,
        force=args.force,
        verify=False if args.no_verify else None,
        log=log,
    )

    if outcome.code == 0:
        log.info("结果：%s", outcome.message)
        ui = cfg.get("ui")
        ui = ui if isinstance(ui, dict) else {}
        if not outcome.already_online and not args.quiet and ui.get("notify_on_success", True):
            elapsed = outcome.result.elapsed if outcome.result else 0.0
            toast(f"✓ 校园网已连接（{elapsed:.1f}s）")
        return 0

    log.warning("结果：%s", outcome.message)
    ui = cfg.get("ui")
    ui = ui if isinstance(ui, dict) else {}
    notify = ui.get("notify_on_failure", True)
    if outcome.code == 3:
        # 不在校园网内 / 没连 Wi-Fi：开机时很常见，不打扰用户
        return 3
    if notify:
        # 先给方向，再给原始返回：多数人看不懂 HTML，但看得懂「账号密码不对，重新存一次」
        detail = outcome.hint or (outcome.result.detail if outcome.result else "")
        if ask_open_setup(f"校园网登录失败：\n{outcome.message}", detail):
            open_setup()
    return outcome.code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:  # pragma: no cover - 启动器绝不能因为异常留下痕迹
        log.exception("启动器异常")
        raise SystemExit(1)
