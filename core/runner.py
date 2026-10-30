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
    hint: str = ""     # 失败时的一句「接下来该做什么」，来自 portal._diagnose


def _verify_settings(cfg: dict) -> tuple[str, int]:
    settings = portal.verify_settings(cfg)
    return settings["url"], settings["expect_status"]


def is_online(cfg: dict, timeout: float = 6.0) -> tuple[bool, str]:
    settings = portal.verify_settings(cfg)
    if not settings["enabled"]:
        return False, "未启用联网探测，无法判断是否已联网"
    return net.online(settings["url"], settings["expect_status"], timeout, source_ip=campus_source_ip(cfg))


def campus_source_ip(cfg: dict) -> str:
    """去网关会走哪块网卡的 IP。

    开着 VPN/TUN 时默认路由是虚拟网卡（如 198.18.0.1），
    从那里探测外网永远是「已联网」，校园网其实没认证——所以要用网关侧的地址发探测。
    """
    gateway = ""
    explicit = str(((cfg.get("verify") or {}).get("source_ip") or "")).strip()
    if explicit:
        # 手填的地址要是已经不在本机（换了网段、DHCP 变了），用它发探测只会一律失败，
        # 那比不填更糟：退回自动挑网卡。
        if explicit in net.local_ips():
            return explicit
    for key in ("preflight", "login"):
        host = net.url_host_port((cfg.get(key) or {}).get("url") or "")[0]
        if host and not host.startswith("{"):
            gateway = host
            break
    if not gateway:
        return ""
    return net.campus_ip(gateway)


def _probe_target(cfg: dict, username: str, password: str) -> str:
    """挑一个能直接做 TCP 探测的地址。

    以前直接拿 login.url 的**模板**去连，地址里带 {userip} 这种占位符时
    hostname 就是 "{userip}"，永远连不上，白等 90 秒再报「可能没连上 Wi-Fi」。
    """
    candidates = []
    login_url = (cfg.get("login") or {}).get("url") or ""
    preflight_url = (cfg.get("preflight") or {}).get("url") or ""
    if login_url or preflight_url:
        try:
            variables = portal.build_variables(cfg, username, password)
            for raw in (login_url, preflight_url):
                if not raw:
                    continue
                try:
                    candidates.append(portal.render(raw, variables, "login.url"))
                except portal.LoginError:
                    continue
        except portal.LoginError:
            candidates.extend([login_url, preflight_url])
    for url in candidates:
        if url and "{" not in url and "}" not in url and net.url_host_port(url)[0]:
            return url
    return ""


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
        login_url = _probe_target(cfg, username, password)
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
                return Outcome(
                    code=3,
                    message=message,
                    hint="先连上校园 Wi-Fi 或插好网线；如果本来就连着，可能是网关临时不通，过几分钟再双击一次。",
                )

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
    if result.config_error:
        return Outcome(
            code=2,
            message=result.message,
            result=result,
            hint="这是设置里的问题，不是账号问题：双击「设置向导」，重新粘贴一次登录请求再保存。",
        )
    return Outcome(code=1, message=result.message, result=result, hint=result.hint)
