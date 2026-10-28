"""自检：把「这台机器现在能不能登录」拆成一条条检查项。

命令行 `python login.py doctor` 和设置向导里的「自检」按钮共用这套逻辑，
输出格式也统一，方便用户截图 / 复制求助。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from . import autostart, config as config_mod, net, paths, portal, secrets

OK = "ok"
BAD = "bad"
INFO = "info"

MARK = {OK: "✓ ", BAD: "✗ ", INFO: ""}

# 自检没过时给一句「那该怎么办」，CLI 与设置向导共用
HINT = {
    "DPAPI 加密存取": "只支持 Windows；若 credentials.dat 是别的电脑/别的 Windows 账号存的，删掉后重新保存一次密码",
    "配置文件": "打开设置向导，把登录请求粘贴进去后点「保存设置」",
    "请求模板渲染": "占位符写错了：报错里已列出全部可用变量，按它改",
    "已保存账号": "在设置向导 ③ 里填账号密码，点「保存设置」",
    "联网探测": "没联网通常是还没认证；如果本来就能上网，可以忽略这条",
}


@dataclass
class Check:
    """一条检查结果。

    status 只有三种：ok（正常）、bad（有问题，会影响登录）、info（仅供参考，不计入失败）。
    """

    title: str
    status: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status != BAD

    def line(self) -> str:
        return f"{self.title}：{MARK.get(self.status, '')}{self.detail}"


def _check_python() -> Check:
    return Check("Python", OK, f"{sys.version.split()[0]}  ({sys.executable})")


def _check_dpapi() -> Check:
    probe = os.path.join(paths.LOG_DIR, "_selftest.dat")
    try:
        paths.ensure_log_dir()
        secrets.save("自检账号", "自检密码", path=probe)
        back = secrets.load(probe)
        good = back.username == "自检账号" and back.password == "自检密码"
        return Check("DPAPI 加密存取", OK if good else BAD, "正常" if good else "回读不一致")
    except Exception as exc:  # noqa: BLE001 - 自检要吃掉所有异常
        return Check("DPAPI 加密存取", BAD, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass


def _check_config(config_path: str | None) -> tuple[Check, dict | None]:
    try:
        cfg = config_mod.load(config_path)
    except config_mod.ConfigError as exc:
        return Check("配置文件", BAD, str(exc)), None
    problems = config_mod.validate(cfg, strict=False)
    if problems:
        return Check("配置文件", BAD, "；".join(problems)), cfg
    return Check("配置文件", OK, paths.rel(paths.CONFIG_FILE)), cfg


def _check_template(cfg: dict | None, probe: bool) -> Check:
    if cfg is None:
        return Check("请求模板渲染", BAD, "跳过（配置不可用）")
    preflight_url = ((cfg.get("preflight") or {}).get("url") or "").strip()
    try:
        variables = portal.build_variables(cfg, "test", "test")
        rendered = portal.build_request(cfg, variables)
    except Exception as exc:  # noqa: BLE001
        # 常见情况：请求体里用了 {token} / {userip}，这些要先访问认证页才拿得到
        if not preflight_url:
            return Check("请求模板渲染", BAD, f"{type(exc).__name__}: {exc}")
        if not probe:
            return Check("请求模板渲染", INFO, "需要联网预跳转才能检查（已跳过）")
        try:
            variables = portal.preflight_variables(cfg, "test", "test")
            rendered = portal.build_request(cfg, variables)
        except Exception as exc2:  # noqa: BLE001
            return Check("请求模板渲染", INFO, f"未能验证：{exc2}")
    return Check("请求模板渲染", OK, f"{rendered.method} {rendered.url[:80]}")


def _check_credentials() -> Check:
    try:
        creds = secrets.load()
    except secrets.SecretError as exc:
        return Check("已保存账号", BAD, str(exc))
    if not creds.complete:
        return Check("已保存账号", BAD, "未保存（打开设置向导填一次即可）")
    return Check("已保存账号", OK, secrets.mask_username(creds.username))


def _check_network(probe: bool, cfg: dict | None = None) -> Check:
    if not probe:
        return Check("联网探测", INFO, "已跳过")
    if cfg is not None:
        # 用配置里的探测地址，和「登录后复核」「login.py check」保持同一口径
        settings = portal.verify_settings(cfg)
        if not settings["enabled"]:
            return Check("联网探测", INFO, "配置里关掉了联网探测")
        online, why = net.online(settings["url"], settings["expect_status"], settings["timeout"])
    else:
        online, why = net.online()
    return Check("联网探测", OK if online else INFO, why)


def run(config_path: str | None = None, *, probe: bool = True, verbose: bool = True) -> list[Check]:
    """跑完所有检查，返回结果列表（顺序固定，方便对比两次自检）。"""
    checks: list[Check] = [_check_python(), _check_dpapi()]

    cfg_check, cfg = _check_config(config_path)
    checks.append(cfg_check)
    checks.append(_check_template(cfg, probe))
    checks.append(_check_credentials())
    checks.append(_check_network(probe, cfg))

    checks.append(Check("开机自启", INFO, "已开启" if autostart.is_enabled() else "未开启"))
    if verbose:
        checks.append(Check("日志文件", INFO, str(paths.LOG_FILE)))
    return checks


def problems(checks: list[Check]) -> list[Check]:
    return [c for c in checks if c.status == BAD]
