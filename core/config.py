"""配置文件读写与校验。

配置里只放「怎么登录」，不放密码；密码保存在 DPAPI 加密的 credentials.dat。
"""

from __future__ import annotations

import copy
import json
import os

from . import paths


class ConfigError(Exception):
    """配置有问题（缺字段、占位符写错等）。"""


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

DEFAULT_CONFIG: dict = {
    "version": 1,
    "profile_name": "校园网",
    # 登录前先访问一个地址，从跳转后的 URL / 响应里取出 userip、token 等参数
    "preflight": {
        "url": "",
        "method": "GET",
        "headers": {},
        "body": "",
        "timeout": 8,
        "follow_redirects": True,
        "capture_query": True,
        "capture_regex": [],  # [{"name": "token", "pattern": "token=([0-9a-f]+)", "from": "url"}]
    },
    "login": {
        "method": "POST",
        "url": "",
        # form = application/x-www-form-urlencoded；json = application/json；raw = 原样发送
        "content_type": "form",
        # 二选一：fields（字典，自动拼装/编码）或 body（原样模板字符串）
        "fields": {},
        "body": "",
        "headers": {},
        # 密码怎么变形，默认明文。例："{md5}"、"{MD5}" 之类按学校要求自行填写
        "password_template": "{password}",
        "insecure_tls": False,
        "timeout": 10,
    },
    "expect": {
        # 优先使用 JSON 判定（留空则不启用）
        "json_path": "",
        "json_equals": "",
        "success_keywords": ["success", "登录成功", "认证成功", "认证通过", "login_ok"],
        "failure_keywords": ["密码错误", "认证失败", "登录失败", "用户名或密码", "账号或密码", "已欠费", "被禁用"],
        "status_ok": [200],
    },
    "verify": {
        "enabled": True,
        "url": "http://connect.rom.miui.com/generate_204",
        "expect_status": 204,
        "timeout": 6,
    },
    "retry": {"attempts": 2, "interval": 2.0},
    "network": {"wait_timeout": 90, "wait_interval": 2.0},
    "ui": {"notify_on_success": False, "notify_on_failure": True},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def default_config() -> dict:
    return copy.deepcopy(DEFAULT_CONFIG)


def normalize(raw: dict) -> dict:
    """把用户配置补齐成完整结构，兼容旧版本缺字段。"""
    return _deep_merge(DEFAULT_CONFIG, raw or {})


def config_path() -> str:
    return paths.CONFIG_FILE


def exists() -> bool:
    return os.path.exists(paths.CONFIG_FILE)


def load(path: str | None = None) -> dict:
    path = path or paths.CONFIG_FILE
    if not os.path.exists(path):
        raise ConfigError(f"找不到配置文件：{paths.rel(path)}（请先运行「设置向导」）")
    try:
        # utf-8-sig：记事本存成「UTF-8 带 BOM」时也能读，避免莫名其妙的 JSON 报错
        with open(path, "r", encoding="utf-8-sig") as fh:
            raw = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 JSON：{exc}（第 {exc.lineno} 行第 {exc.colno} 列附近）") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError("配置文件编码不对：请用 UTF-8 保存（记事本里选「UTF-8」而不是 ANSI）") from exc
    except OSError as exc:
        raise ConfigError(f"读取配置失败：{exc}") from exc
    cfg = normalize(raw)
    validate(cfg)
    return cfg


def save(cfg: dict, path: str | None = None) -> str:
    path = path or paths.CONFIG_FILE
    data = normalize(cfg)
    validate(data, strict=False)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def validate(cfg: dict, strict: bool = True) -> list[str]:
    """返回问题列表；strict=True 时发现问题直接抛 ConfigError。"""
    problems: list[str] = []
    login = cfg.get("login") or {}
    url = (login.get("url") or "").strip()
    if not url:
        problems.append("未填写登录地址（login.url）")
    elif not (url.startswith("http://") or url.startswith("https://")):
        problems.append("登录地址必须以 http:// 或 https:// 开头")
    method = (login.get("method") or "POST").upper()
    if method not in ("GET", "POST", "PUT", "PATCH"):
        problems.append(f"不支持的请求方法：{method}")

    pre = cfg.get("preflight") or {}
    pre_url = (pre.get("url") or "").strip()
    if pre_url and not (pre_url.startswith("http://") or pre_url.startswith("https://")):
        problems.append("登录前跳转地址必须以 http:// 或 https:// 开头")

    content_type = (login.get("content_type") or "form").lower()
    if content_type not in ("form", "json", "raw"):
        problems.append("content_type 只能是 form / json / raw")
    fields = login.get("fields") or {}
    body = (login.get("body") or "").strip()
    if not fields and not body and method != "GET":
        problems.append("未填写请求体（login.fields 或 login.body 至少填一个）")

    retry = cfg.get("retry") or {}
    try:
        if int(retry.get("attempts", 2)) < 1:
            problems.append("retry.attempts 至少为 1")
    except (TypeError, ValueError):
        problems.append("retry.attempts 必须是数字")

    if problems and strict:
        raise ConfigError("；".join(problems))
    return problems


# ---------------------------------------------------------------- 运行状态

def load_state() -> dict:
    try:
        with open(paths.STATE_FILE, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(**kwargs) -> None:
    state = load_state()
    state.update(kwargs)
    try:
        tmp = paths.STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, paths.STATE_FILE)
    except OSError:
        pass
