"""把「检查是否已联网 → 等网关就绪 → 登录 → 记录状态」串成一条流程。

命令行 login.py 和图形界面的双击启动器都调用这里，保证两边行为一致。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from . import config as config_mod, net, portal


@dataclass
class Outcome:
    code: int          # 0 成功/已联网，1 登录失败，2 配置或账号问题，3 网络未就绪
    message: str
    result: portal.LoginResult | None = None
    already_online: bool = False


def _verify_settings(cfg: dict) -> tuple[str, int]:
    verify = cfg.get("verify") or {}
    url = verify.get("url") or net.DEFAULT_PROBE_URL
    try:
        expect = int(verify.get("expect_status") or 204)
    except (TypeError, ValueError):
        expect = 204
    return url, expect


def is_online(cfg: dict, timeout: float = 6.0) -> tuple[bool, str]:
    verify = cfg.get("verify") or {}
    if not verify.get("enabled", True):
        return False, "未启用联网探测，无法判断是否已联网"
    url, expect = _verify_settings(cfg)
    return net.online(url, expect, timeout)


def run_login(
    cfg: dict,
    username: str,
    password: str,
    *,
    wait: bool = False,
    force: bool = False,
    verify: bool | None = None,
    log: logging.Logger | None = None,
) -> Outcome:
    log = log or logging.getLogger("campusnet")

    if not force:
        online, why = is_online(cfg)
        if online:
            log.info("当前%s，跳过登录", why)
            config_mod.save_state(
                last_check=time.strftime("%Y-%m-%d %H:%M:%S"),
                last_ok=True,
                last_message=why,
                already_online=True,
            )
            return Outcome(code=0, message=f"当前{why}，无需登录", already_online=True)

    if wait:
        login_url = (cfg.get("login") or {}).get("url") or ""
        network = cfg.get("network") or {}
        timeout = float(network.get("wait_timeout") or 90)
        interval = float(network.get("wait_interval") or 2.0)
        if login_url and not net.portal_reachable(login_url):
            log.info("等待校园网网关就绪（最多 %.0f 秒）", timeout)
            ready = net.wait_for(lambda: net.portal_reachable(login_url, 1.5), timeout, interval)
            if not ready:
                message = "连不上校园网网关：可能还没连上 Wi-Fi，或不在校园网内"
                log.warning(message)
                config_mod.save_state(
                    last_check=time.strftime("%Y-%m-%d %H:%M:%S"),
                    last_ok=False,
                    last_message=message,
                )
                return Outcome(code=3, message=message)

    result = portal.login(cfg, username, password, verify=verify, log=log)
    config_mod.save_state(
        last_check=time.strftime("%Y-%m-%d %H:%M:%S"),
        last_ok=bool(result.ok),
        last_message=result.message,
        last_attempts=result.attempts,
        last_elapsed=round(result.elapsed, 2),
        already_online=False,
    )
    if result.ok:
        return Outcome(code=0, message=result.message, result=result)
    return Outcome(code=1, message=result.message, result=result)
