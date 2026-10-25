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

from core import autostart, config as config_mod, net, paths, portal, runner, secrets  # noqa: E402

log = logging.getLogger("campusnet")


def setup_logging(quiet: bool = False) -> None:
    paths.ensure_log_dir()
    try:
        if os.path.exists(paths.LOG_FILE) and os.path.getsize(paths.LOG_FILE) > 1024 * 1024:
            os.replace(paths.LOG_FILE, paths.LOG_FILE + ".1")
    except OSError:
        pass

    root = logging.getLogger("campusnet")
    root.setLevel(logging.DEBUG)
    if not any(isinstance(h, logging.FileHandler) for h in root.handlers):
        fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S")
        fh = logging.FileHandler(paths.LOG_FILE, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    if not quiet and not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in root.handlers):
        sh = logging.StreamHandler(sys.stdout)
        sh.setLevel(logging.INFO)
        sh.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(sh)


def _need_config(path: str | None):
    try:
        return config_mod.load(path)
    except config_mod.ConfigError as exc:
        print(f"[配置有误] {exc}")
        print("请先运行「设置向导」，或在项目目录运行：  python 设置向导.pyw")
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
    return outcome.code




def cmd_check(args) -> int:
    print(f"程序目录：{paths.ROOT}")
    cfg = None
    try:
        cfg = config_mod.load(args.config)
        print(f"配置文件：{paths.rel(paths.CONFIG_FILE)} ✓")
        print(f"登录地址：{(cfg.get('login') or {}).get('url')}")
        if (cfg.get("preflight") or {}).get("url"):
            print(f"预跳转地址：{cfg['preflight']['url']}")
    except config_mod.ConfigError as exc:
        print(f"配置文件：✗ {exc}")
    try:
        creds = secrets.load()
        if creds.complete:
            masked = creds.username[:2] + "*" * max(1, len(creds.username) - 2)
            print(f"已保存账号：{masked}（保存于 {creds.saved_at or '未知时间'}）✓")
        else:
            print("已保存账号：✗ 未保存")
    except secrets.SecretError as exc:
        print(f"已保存账号：✗ {exc}")

    url = ((cfg or {}).get("verify") or {}).get("url") or net.DEFAULT_PROBE_URL
    expect = int((((cfg or {}).get("verify") or {}).get("expect_status")) or 204)
    online, why = net.online(url, expect, 6)
    print(f"当前联网：{'✓ ' if online else '✗ '}{why}")
    print(f"本机 IP：{net.local_ip()}")
    print(f"开机自动登录：{'已开启' if autostart.is_enabled() else '未开启'}")
    state = config_mod.load_state()
    if state.get("last_check"):
        print(f"上次尝试：{state['last_check']}  {'成功' if state.get('last_ok') else '失败'}  {state.get('last_message', '')}")
    return 0 if online else 1


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
        except OSError as exc:
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
    ok = True
    print("=== 自检 ===")
    print(f"Python：{sys.version.split()[0]}  ({sys.executable})")

    try:
        secrets.save("自检账号", "自检密码", path=os.path.join(paths.LOG_DIR, "_selftest.dat"))
        back = secrets.load(os.path.join(paths.LOG_DIR, "_selftest.dat"))
        good = back.username == "自检账号" and back.password == "自检密码"
        os.remove(os.path.join(paths.LOG_DIR, "_selftest.dat"))
        print(f"DPAPI 加密存取：{'✓ 正常' if good else '✗ 回读不一致'}")
        ok = ok and good
    except Exception as exc:  # noqa: BLE001
        print(f"DPAPI 加密存取：✗ {type(exc).__name__}: {exc}")
        ok = False

    try:
        cfg = config_mod.load(args.config)
        problems = config_mod.validate(cfg, strict=False)
        print(f"配置文件：{'✓ 完整' if not problems else '✗ ' + '；'.join(problems)}")
        ok = ok and not problems
        rendered = portal.build_request(cfg, portal.build_variables(cfg, "test", "test"))
        print(f"请求模板渲染：✓ {rendered.method} {rendered.url[:80]}")
    except Exception as exc:  # noqa: BLE001
        print(f"请求模板渲染：✗ {type(exc).__name__}: {exc}")
        ok = False

    try:
        creds = secrets.load()
        print(f"已保存账号：{'✓ ' + creds.username[:2] + '***' if creds.complete else '✗ 未保存'}")
    except Exception as exc:  # noqa: BLE001
        print(f"已保存账号：✗ {exc}")

    online, why = net.online()
    print(f"联网探测：{'✓ ' if online else '✗ '}{why}")
    print(f"开机自启：{'已开启' if autostart.is_enabled() else '未开启'}")
    print(f"日志文件：{paths.LOG_FILE}")
    print("=== 自检%s ===" % ("通过" if ok else "发现问题（见上）"))
    return 0 if ok else 1


# ------------------------------------------------------------------ 入口

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="login.py", description="校园网一键登录（通用 Portal 认证）", add_help=True
    )
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
    sub.add_parser("doctor", help="自检").set_defaults(func=cmd_doctor)

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
