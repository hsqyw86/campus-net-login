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
    "ui": {"notify_on_success": True, "notify_on_failure": True},
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
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件最外层必须是 {{...}} 对象，但现在是一个 {type(raw).__name__}")
    return _deep_merge(DEFAULT_CONFIG, raw)


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
    try:
        paths.write_json_atomic(path, data)
    except OSError as exc:
        raise ConfigError(f"写入配置失败：{exc}") from exc
    return path


_SECTIONS = ("preflight", "login", "expect", "verify", "retry", "network", "ui")


def _as_dict(cfg: dict, name: str, problems: list[str]) -> dict:
    """取一个配置段；类型不对就记一条问题并返回空字典（不让后面 .get 崩掉）。"""
    value = cfg.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        problems.append(f"{name} 必须是一个 {{...}} 对象，现在是 {type(value).__name__}")
        return {}
    return value


def _as_number(section: dict, key: str, label: str, problems: list[str], *, minimum=None) -> float | None:
    if key not in section or section[key] in ("", None):
        return None
    try:
        number = float(section[key])
    except (TypeError, ValueError):
        problems.append(f"{label} 必须是数字（现在写的是 {section[key]!r}）")
        return None
    if minimum is not None and number < minimum:
        problems.append(f"{label} 不能小于 {minimum}")
        return None
    return number


def validate(cfg: dict, strict: bool = True) -> list[str]:
    """返回问题列表；strict=True 时发现问题直接抛 ConfigError。"""
    problems: list[str] = []
    if not isinstance(cfg, dict):
        problems.append("配置最外层必须是对象")
        if strict:
            raise ConfigError("；".join(problems))
        return problems

    for name in _SECTIONS:
        _as_dict(cfg, name, problems)

    login = _as_dict(cfg, "login", problems)
    fields = login.get("fields") or {}
    if not isinstance(fields, dict):
        problems.append("login.fields 必须是一个 {...} 对象")
        fields = {}
    body = login.get("body") or ""
    if not isinstance(body, str):
        problems.append("login.body 必须是字符串")
        body = ""
    url = str(login.get("url") or "").strip()
    if not url:
        problems.append("未填写登录地址（login.url）")
    elif not (url.startswith("http://") or url.startswith("https://")):
        problems.append("登录地址必须以 http:// 或 https:// 开头")
    method = str(login.get("method") or "POST").upper()
    if method not in ("GET", "POST", "PUT", "PATCH"):
        problems.append(f"不支持的请求方法：{method}")

    pre = _as_dict(cfg, "preflight", problems)
    pre_url = str(pre.get("url") or "").strip()
    if pre_url and not (pre_url.startswith("http://") or pre_url.startswith("https://")):
        problems.append("登录前跳转地址必须以 http:// 或 https:// 开头")

    content_type = str(login.get("content_type") or "form").lower()
    if content_type not in ("form", "json", "raw"):
        problems.append("content_type 只能是 form / json / raw")
    if not fields and not body.strip() and method != "GET":
        problems.append("未填写请求体（login.fields 或 login.body 至少填一个）")

    retry = _as_dict(cfg, "retry", problems)
    _as_number(retry, "attempts", "retry.attempts", problems, minimum=1)
    _as_number(retry, "interval", "retry.interval", problems, minimum=0)

    network = _as_dict(cfg, "network", problems)
    _as_number(network, "wait_timeout", "network.wait_timeout", problems, minimum=0)
    _as_number(network, "wait_interval", "network.wait_interval", problems, minimum=0)

    verify = _as_dict(cfg, "verify", problems)
    _as_number(verify, "timeout", "verify.timeout", problems, minimum=0)
    _as_number(verify, "expect_status", "verify.expect_status", problems, minimum=100)

    # 超时写错（比如 "10s"）会在真正发请求那一刻炸成 ValueError，
    # 提前在这里拦住，用户看到的才是人话。
    _as_number(login, "timeout", "login.timeout", problems, minimum=0)
    _as_number(pre, "timeout", "preflight.timeout", problems, minimum=0)

    if problems and strict:
        raise ConfigError("；".join(problems))
    return problems


# ---------------------------------------------------------------- 运行状态

def load_state() -> dict:
    try:
        with open(paths.STATE_FILE, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(**kwargs) -> None:
    state = load_state()
    state.update(kwargs)
    try:
        paths.write_json_atomic(paths.STATE_FILE, state)
    except OSError:
        pass
