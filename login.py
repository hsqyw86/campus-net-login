"""校园网一键登录 —— 命令行入口。

常用：
    python login.py login            # 登录一次
    python login.py login --wait     # 等校园网就绪再登录（开机自启用）
    python login.py check            # 查看联网状态 / 配置是否完整
    python login.py preview          # 只显示将要发出的请求（密码打码）
    python login.py set-credentials  # 交互式保存账号密码（DPAPI 加密）
    python login.py autostart on|off|status
    python login.py doctor           # 自检：加密、配置、网络

退出码：0 成功（或已联网），1 登录失败，2 配置/账号问题，3 网络未就绪
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import subprocess
import sys
import time

try:  # Windows 控制台默认 GBK，遇到特殊字符不要整个崩掉
    sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(errors="replace")  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from core import (  # noqa: E402
    __version__,
    autostart,
    config as config_mod,
    doctor,
    log_setup,
    net,
    paths,
    portal,
    runner,
    secrets,
)

log = logging.getLogger("campusnet")


def setup_logging(quiet: bool = False) -> None:
    log_setup.setup_logging(quiet=quiet)


def _need_config(path: str | None):
    try:
        return config_mod.load(path)
    except config_mod.ConfigError as exc:
        print(f"[配置有误] {exc}")
        print("请先运行「设置向导」：双击 设置向导.vbs（或运行 python setup.pyw）")
        raise SystemExit(2)


def _need_credentials():
    try:
        creds = secrets.load()
    except secrets.SecretError as exc:
        print(f"[账号读取失败] {exc}")
        raise SystemExit(2)
    if not creds.complete:
        print("[没有保存账号] 请先运行：  python login.py set-credentials   或打开设置向导")
        raise SystemExit(2)
    return creds


# ------------------------------------------------------------------ 子命令

def cmd_login(args) -> int:
    cfg = _need_config(args.config)
    creds = _need_credentials()
    verify = False if args.no_verify else None

    outcome = runner.run_login(
        cfg, creds.username, creds.password, wait=args.wait, force=args.force, verify=verify, log=log
    )
    if outcome.code == 0:
        if outcome.already_online or outcome.result is None:
            print(outcome.message)
        else:
            print(f"✓ {outcome.message}（第 {outcome.result.attempts} 次尝试，耗时 {outcome.result.elapsed:.1f}s）")
        return 0
    print(f"✗ {outcome.message}")
    if outcome.result and outcome.result.detail:
        log.debug("响应内容：%s", outcome.result.detail)
    if outcome.code == 3:
        print("  看起来现在不在校园网里（或没连上 Wi-Fi）。")
    return outcome.code


def cmd_check(args) -> int:
    print(f"程序目录：{paths.ROOT}")
    cfg = None
    config_ok = True
    try:
        cfg = config_mod.load(args.config)
        print(f"配置文件：{paths.rel(paths.CONFIG_FILE)} ✓")
        print(f"登录地址：{(cfg.get('login') or {}).get('url')}")
        if (cfg.get("preflight") or {}).get("url"):
            print(f"预跳转地址：{cfg['preflight']['url']}")
    except config_mod.ConfigError as exc:
        config_ok = False
        print(f"配置文件：✗ {exc}")
    try:
        creds = secrets.load()
        if creds.complete:
            print(f"已保存账号：{secrets.mask_username(creds.username)}（保存于 {creds.saved_at or '未知时间'}）✓")
        else:
            print("已保存账号：✗ 未保存")
    except secrets.SecretError as exc:
        print(f"已保存账号：✗ {exc}")

    settings = portal.verify_settings(cfg or {})
    online, why = net.online(settings["url"], settings["expect_status"], settings["timeout"])
    print(f"当前联网：{'✓ ' if online else '✗ '}{why}")
    print(f"本机 IP：{net.local_ip()}")
    print(f"开机自动登录：{'已开启' if autostart.is_enabled() else '未开启'}")
    state = config_mod.load_state()
    if state.get("last_check"):
        print(f"上次尝试：{state['last_check']}  {'成功' if state.get('last_ok') else '失败'}  {state.get('last_message', '')}")
    # 退出码要和文件开头那三行说明对得上：2 = 配置有问题，3 = 网络未就绪
    if not config_ok:
        return 2
    return 0 if online else 3


def cmd_preview(args) -> int:
    cfg = _need_config(args.config)
    creds = _need_credentials()
    result = portal.login(cfg, creds.username, creds.password, dry_run=True, log=log)
    print("===== 将要发出的请求（密码已打码）=====")
    print(result.request_preview or result.message)
    return 0


def cmd_set_credentials(args) -> int:
    try:
        old = secrets.load()
    except secrets.SecretError:
        old = secrets.Credentials()
    default_user = old.username
    prompt = "账号（学号/工号/手机号）"
    if default_user:
        prompt += f" [{default_user}]"
    try:
        username = input(prompt + "：").strip() or default_user
        if not username:
            print("账号不能为空。")
            return 2
        password = getpass.getpass("密码（输入时不显示）：")
        if not password:
            print("密码不能为空。")
            return 2
        again = getpass.getpass("再输一次确认：")
    except (EOFError, KeyboardInterrupt):
        print("\n需要在这个窗口里交互输入。也可以直接双击「设置向导」用图形界面填写。")
        return 2
    if password != again:
        print("两次输入不一致，未保存。")
        return 2
    try:
        secrets.save(username, password)
    except secrets.SecretError as exc:
        print(f"保存失败：{exc}")
        return 2
    print(f"✓ 已加密保存到 {paths.rel(paths.CREDENTIAL_FILE)}（只有当前 Windows 账号能解密）")
    return 0


def cmd_autostart(args) -> int:
    action = args.action
    if action == "on":
        try:
            where = autostart.enable(paths.PYTHONW_EXE, os.path.join(paths.ROOT, "launcher.pyw"))
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"✗ 开启失败：{exc}")
            return 1
        print(f"✓ 已开启开机自动登录\n  {where}")
        return 0
    if action == "off":
        removed = autostart.disable()
        if removed:
            print("✓ 已关闭开机自动登录（清理了：")
            for item in removed:
                print(f"    {item}")
            print("）")
        else:
            print("✓ 开机自动登录本来就没开启")
        return 0
    print(autostart.describe())
    return 0


def cmd_doctor(args) -> int:
    print("=== 自检 ===")
    checks = doctor.run(args.config, probe=not args.no_network)
    for item in checks:
        print(item.line())
    bad = doctor.problems(checks)
    if bad:
        print(f"=== 自检发现问题（{len(bad)} 项，见上） ===")
        return 1
    print("=== 自检通过 ===")
    return 0


# ------------------------------------------------------------------ 入口

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="login.py", description="校园网一键登录（通用 Portal 认证）", add_help=True
    )
    parser.add_argument("--version", action="version", version=f"campus-net-login {__version__}")
    parser.add_argument("--config", help="指定配置文件（默认项目目录下的 config.json）")
    parser.add_argument("--quiet", action="store_true", help="不打印过程信息（只写日志）")
    sub = parser.add_subparsers(dest="command")

    p_login = sub.add_parser("login", help="执行登录")
    p_login.add_argument("--wait", action="store_true", help="先等校园网可达再登录")
    p_login.add_argument("--no-verify", action="store_true", help="登录后不做联网探测")
    p_login.add_argument("--force", action="store_true", help="即使已经联网也强制重新登录")
    p_login.set_defaults(func=cmd_login, force=False, wait=False, no_verify=False)

    sub.add_parser("check", help="查看状态").set_defaults(func=cmd_check)
    sub.add_parser("preview", help="预览将发出的请求").set_defaults(func=cmd_preview)
    sub.add_parser("set-credentials", help="交互式保存账号密码").set_defaults(func=cmd_set_credentials)
    p_doctor = sub.add_parser("doctor", help="自检（加密 / 配置 / 模板 / 网络）")
    p_doctor.add_argument("--no-network", action="store_true", help="跳过联网探测")
    p_doctor.set_defaults(func=cmd_doctor, no_network=False)

    p_auto = sub.add_parser("autostart", help="开机自动登录开关")
    p_auto.add_argument("action", choices=["on", "off", "status"])
    p_auto.set_defaults(func=cmd_autostart)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        args.command = "login"
        args.func = cmd_login
        args.wait = False
        args.no_verify = False
        args.force = False
    setup_logging(quiet=args.quiet or args.command != "login")
    try:
        return int(args.func(args))
    except SystemExit as exc:
        return int(exc.code or 0)
    except KeyboardInterrupt:
        print("\n已取消。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
