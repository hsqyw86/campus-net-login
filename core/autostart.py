"""开机自动登录 & 快捷方式。

优先用注册表 HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run：
  * 不需要管理员权限
  * 设置向导里一个开关就能关掉，不往「启动」文件夹塞文件
如果注册表写不进去（个别被组策略锁死的机器），自动退回到
「启动」文件夹里放一个快捷方式，效果一样。
"""

from __future__ import annotations

import os
import subprocess
import winreg

from . import paths

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
SHORTCUT_NAME = "校园网一键登录"
STARTUP_LNK_NAME = "校园网一键登录（开机自动）"


# ------------------------------------------------------------------ 命令

def build_command(pythonw: str | None = None, launcher: str | None = None) -> str:
    pythonw = pythonw or paths.PYTHONW_EXE
    launcher = launcher or os.path.join(paths.ROOT, "launcher.pyw")
    return f'"{pythonw}" "{launcher}" --wait --quiet'


# ------------------------------------------------------------------ 注册表

def _reg_set(command: str) -> None:
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, paths.RUN_KEY_NAME, 0, winreg.REG_SZ, command)


def _reg_delete() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, paths.RUN_KEY_NAME)
            return True
    except (FileNotFoundError, OSError):
        return False


def current_command() -> str:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_QUERY_VALUE) as key:
            value, _ = winreg.QueryValueEx(key, paths.RUN_KEY_NAME)
            return str(value)
    except OSError:
        return ""


# ------------------------------------------------------------------ 快捷方式

def desktop_dir() -> str:
    """桌面路径（兼容 OneDrive 重定向过的桌面）。"""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "Desktop")
            if value and os.path.isdir(value):
                return value
    except OSError:
        pass
    for candidate in (
        os.path.join(os.path.expanduser("~"), "Desktop"),
        os.path.join(os.path.expanduser("~"), "OneDrive", "Desktop"),
    ):
        if os.path.isdir(candidate):
            return candidate
    return os.path.join(os.path.expanduser("~"), "Desktop")


def startup_dir() -> str:
    appdata = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def _vbs_quote(text: str) -> str:
    return text.replace('"', '""')


def _make_lnk(lnk_path: str, target: str, arguments: str, workdir: str, description: str, icon: str | None = None) -> str:
    """用系统自带的 WScript.Shell 生成 .lnk（不依赖 pywin32）。

    写法是「临时 .vbs + cscript」，脚本按 UTF-16LE+BOM 存盘：
    WSH 认这个编码，所以中文快捷方式名在英文系统、任意控制台代码页下都不会变成「????.lnk」。
    """
    icon = icon or target
    script = (
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'Set lnk = sh.CreateShortcut("{_vbs_quote(lnk_path)}")\r\n'
        f'lnk.TargetPath = "{_vbs_quote(target)}"\r\n'
        f'lnk.Arguments = "{_vbs_quote(arguments)}"\r\n'
        f'lnk.WorkingDirectory = "{_vbs_quote(workdir)}"\r\n'
        f'lnk.IconLocation = "{_vbs_quote(icon)},0"\r\n'
        f'lnk.Description = "{_vbs_quote(description)}"\r\n'
        "lnk.WindowStyle = 7\r\n"
        "lnk.Save\r\n"
    )
    folder = os.path.dirname(lnk_path) or "."
    vbs = os.path.join(folder, f"_mklnk_{os.getpid()}.vbs")
    cscript = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cscript.exe")
    try:
        with open(vbs, "w", encoding="utf-16") as fh:
            fh.write(script)
        proc = subprocess.run(
            [cscript, "//nologo", vbs],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=60,
        )
    except subprocess.SubprocessError as exc:
        # TimeoutExpired 不是 OSError 子类，漏掉它上面那段友好报错就整个失效
        raise OSError(f"创建快捷方式失败：{type(exc).__name__}: {exc}；目标 {lnk_path!r}") from exc
    finally:
        try:
            os.remove(vbs)
        except OSError:
            pass
    if proc.returncode != 0 or not os.path.exists(lnk_path):
        detail = " ".join(filter(None, [(proc.stderr or "").strip(), (proc.stdout or "").strip()]))
        try:
            seen = sorted(os.listdir(folder))[:8]
        except OSError:
            seen = []
        raise OSError(
            f"创建快捷方式失败：返回码 {proc.returncode}，{detail[:600] or '无输出'}；"
            f"目标 {lnk_path!r}；目录里现有 {seen}"
        )
    return lnk_path


def create_desktop_shortcut(name: str = SHORTCUT_NAME, folder: str | None = None) -> str:
    """在桌面创建指向「一键登录.vbs」的快捷方式，返回 .lnk 路径。

    中文名在中文 Windows 上没问题；个别英文系统（WScript.Shell 走 ANSI 代码页）
    存不了非 ASCII 文件名，这时自动退回 ASCII 名，不至于让功能整个失败。
    """
    target_vbs = os.path.join(paths.ROOT, "一键登录.vbs")
    if not os.path.exists(target_vbs):
        raise FileNotFoundError(f"找不到启动器：{target_vbs}")
    wscript = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "wscript.exe")
    folder = folder or desktop_dir()
    try:
        return _make_lnk(
            os.path.join(folder, f"{name}.lnk"),
            wscript,
            f'"{target_vbs}"',
            paths.ROOT,
            "双击登录校园网",
            icon=paths.PYTHONW_EXE,
        )
    except OSError:
        if name.isascii():
            raise
        return _make_lnk(
            os.path.join(folder, "CampusNetLogin.lnk"),
            wscript,
            f'"{target_vbs}"',
            paths.ROOT,
            "CampusNet one-click login",
            icon=paths.PYTHONW_EXE,
        )


# ------------------------------------------------------------------ 开机自启

def startup_lnk_path() -> str:
    return os.path.join(startup_dir(), f"{STARTUP_LNK_NAME}.lnk")


def enable(pythonw: str | None = None, launcher: str | None = None) -> str:
    """开启开机自动登录，返回人话说明（写到哪里了）。"""
    command = build_command(pythonw, launcher)
    try:
        _reg_set(command)
        return f"已写入注册表：HKCU\\{RUN_KEY}\\{paths.RUN_KEY_NAME}"
    except OSError as exc:
        reg_error = exc

    # 注册表被限制时，退化到「启动」文件夹
    try:
        os.makedirs(startup_dir(), exist_ok=True)
        _make_lnk(
            startup_lnk_path(),
            pythonw or paths.PYTHONW_EXE,
            f'"{launcher or os.path.join(paths.ROOT, "launcher.pyw")}" --wait --quiet',
            paths.ROOT,
            "开机自动登录校园网",
        )
        return f"已放入启动文件夹：{startup_lnk_path()}"
    except OSError:
        raise OSError(
            f"注册表和启动文件夹都写不进去（{reg_error}）。"
            "可以手动把「一键登录.vbs」的快捷方式放进 开始菜单→启动 文件夹。"
        ) from reg_error


def disable() -> list[str]:
    """关闭开机自动登录，返回被清掉的项目。"""
    removed: list[str] = []
    if _reg_delete():
        removed.append(f"HKCU\\{RUN_KEY}\\{paths.RUN_KEY_NAME}")
    lnk = startup_lnk_path()
    if os.path.exists(lnk):
        try:
            os.remove(lnk)
            removed.append(lnk)
        except OSError:
            pass
    return removed


def is_enabled() -> bool:
    return bool(current_command()) or os.path.exists(startup_lnk_path())


def describe() -> str:
    lines = []
    command = current_command()
    if command:
        lines.append("开机自动登录：已开启（注册表）")
        lines.append(f"  HKCU\\{RUN_KEY}\\{paths.RUN_KEY_NAME}")
        lines.append(f"  命令：{command}")
    if os.path.exists(startup_lnk_path()):
        lines.append("开机自动登录：已开启（启动文件夹）")
        lines.append(f"  {startup_lnk_path()}")
    if not lines:
        return "开机自动登录：未开启"
    return "\n".join(lines)
