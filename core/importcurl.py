"""把浏览器里「Copy as cURL」出来的一条请求，变成能用的配置 + 加密凭据。

为什么做这个：手动抄地址、抄字段、认哪个是密码，是整件事里最容易出错的十分钟。
复制一条 cURL，剩下的都该由程序做——包括把密码从配置里摘出来，单独存进 DPAPI。

安全边界：本模块从不打印密码；写进 config.json 的只有 `{username}` / `{password}` 占位符。
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass, field

from . import config as config_mod, curlparse, net, secrets

USER_PLACEHOLDER = "{username}"
PASS_PLACEHOLDER = "{password}"

_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")
_HEX40 = re.compile(r"^[0-9a-fA-F]{40}$")
_DROP_HEADERS = {
    "host", "content-length", "connection", "accept-encoding", "transfer-encoding",
    "expect", "cookie", "referer", "origin", "user-agent",
}


@dataclass
class ImportReport:
    """导入的结果：配置、凭据、以及给人看的说明。"""

    cfg: dict = field(default_factory=dict)
    username: str = ""
    password: str = ""
    user_field: str = ""
    pass_field: str = ""
    password_is_hash: bool = False
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def masked(self) -> str:
        name = secrets.mask_username(self.username) if self.username else "（没认出来）"
        if self.password:
            pw = "***" if not self.password_is_hash else "***（已是哈希值）"
        else:
            pw = "（没认出来）"
        return f"账号：{name}　密码：{pw}"


def read_clipboard() -> str:
    """读 Windows 剪贴板里的文字（ctypes，不需要第三方库）。"""
    import ctypes

    CF_UNICODETEXT = 13
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    if not user32.OpenClipboard(None):
        raise RuntimeError("打不开剪贴板：可能被别的程序占用，稍后再试")
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        kernel32.GlobalLock.restype = ctypes.c_void_p
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.c_wchar_p(ptr).value or ""
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _json_fields(body: str) -> dict | None:
    """JSON 请求体 → 扁平 dict（只处理一层，够 Portal 用了）。"""
    try:
        doc = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(doc, dict):
        return None
    flat: dict = {}
    for key, value in doc.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            flat[str(key)] = "" if value is None else str(value)
        else:
            return None
    return flat


def _guess_from_names(names, values: dict) -> tuple[str, str]:
    """在字段名里认账号和密码（认不出就返回空串，绝不瞎猜）。"""
    user = ""
    password = ""
    for key in names:
        low = key.lower()
        if not password and ("pass" in low or "pwd" in low or low in ("p", "pw")):
            password = key
        elif not user and any(
            tag in low for tag in ("user", "account", "stu", "card", "phone", "mobile", "name", "no", "id")
        ):
            if low not in ("uuid", "deviceid", "sessionid", "wlanacname", "wlanacip"):
                user = key
    # 账号字段的值通常像学号/手机号；密码字段的值通常更长且含符号
    if not user:
        for key, value in values.items():
            if key in (password,):
                continue
            if re.fullmatch(r"[0-9]{6,20}", value or ""):
                user = key
                break
    return user, password


def from_text(text: str, base: dict | None = None) -> ImportReport:
    """解析一条 cURL（bash / cmd / powershell 都行），产出配置和凭据。"""
    if not (text or "").strip():
        raise curlparse.CurlParseError("剪贴板是空的：先在浏览器里右键那条请求 → Copy → Copy as cURL (bash)")
    req = curlparse.parse_curl(text)
    if not req.url:
        raise curlparse.CurlParseError("没解析出请求地址，这条可能不是登录请求")

    report = ImportReport()
    report.notes.extend(req.warnings or [])

    values: dict = {}
    if req.content_type == "json":
        parsed = _json_fields(req.body)
        if parsed is not None:
            values = parsed
        else:
            report.warnings.append("请求体是 JSON，但不是简单的键值结构，可能需要手动补字段")
    if not values:
        values = req.field_values()

    user_field, pass_field = _guess_from_names(list(values), values)
    report.user_field = user_field
    report.pass_field = pass_field
    report.username = values.get(user_field, "") if user_field else ""
    report.password = values.get(pass_field, "") if pass_field else ""

    if not report.password:
        report.warnings.append("没认出密码字段：请手动在「请求内容」里把密码位置写成 {password}")
    if not report.username:
        report.warnings.append("没认出账号字段：请手动把账号位置写成 {username}")
    if report.password and _HEX32.match(report.password):
        report.password_is_hash = True
        report.notes.append("这条请求里的密码已经是 md5：我原样保存、原样发送，以后改了密码要重新抓一次")
    elif report.password and _HEX40.match(report.password):
        report.password_is_hash = True
        report.notes.append("这条请求里的密码已经是 sha1：我原样保存、原样发送，以后改了密码要重新抓一次")

    cfg = config_mod.normalize(base or {})
    login = cfg["login"]
    login["url"] = req.url
    login["method"] = req.method or "POST"
    login["content_type"] = req.content_type if req.content_type in ("form", "json", "raw") else "form"
    login["headers"] = {
        k: v for k, v in (req.headers or {}).items() if k.lower() not in _DROP_HEADERS
    }

    rendered: dict = {}
    for key, value in values.items():
        if key == user_field:
            rendered[key] = USER_PLACEHOLDER
        elif key == pass_field:
            rendered[key] = PASS_PLACEHOLDER
        else:
            rendered[key] = value
    if rendered:
        login["fields"] = rendered
        login["body"] = ""
    elif req.body:
        # 认不出字段结构：退回原始文本，只把密码值换掉
        body = req.body
        if report.password:
            body = body.replace(report.password, PASS_PLACEHOLDER)
        if report.username:
            body = body.replace(report.username, USER_PLACEHOLDER)
        login["body"] = body
        login["fields"] = {}

    # 登录前跳转：Referer 往往就是被劫持的那一页，里面有 userip/token
    referer = ""
    for key, value in (req.headers or {}).items():
        if key.lower() == "referer":
            referer = value
            break
    if referer:
        cfg["preflight"]["url"] = referer
        report.notes.append(f"用请求里的 Referer 当「登录前跳转」：{referer}")
    elif not cfg["preflight"].get("url"):
        parts = urllib.parse.urlsplit(req.url)
        if parts.scheme and parts.netloc:
            cfg["preflight"]["url"] = f"{parts.scheme}://{parts.netloc}/"
            report.notes.append("自动用网关根地址当「登录前跳转」：如果登录后提示缺参数，改成认证页地址")

    host = parts.hostname if (parts := urllib.parse.urlsplit(req.url)) else ""
    if host and not net.url_host_port(req.url)[0]:
        report.warnings.append(f"地址里认不出主机名：{req.url}")

    if "cookie" in {k.lower() for k in (req.headers or {})}:
        report.notes.append("请求里的 Cookie 已忽略：登录前会重新访问一次跳转页，拿新的会话")

    report.cfg = cfg
    return report


def apply(report: ImportReport, config_path: str | None = None) -> tuple[str, str]:
    """把导入结果落盘：配置写成 JSON，账号密码进 DPAPI。返回两个文件路径。"""
    problems = config_mod.validate(report.cfg, strict=False)
    if problems:
        raise config_mod.ConfigError("导入的配置还不能用：\n  - " + "\n  - ".join(problems))
    cfg_path = config_mod.save(report.cfg, config_path)
    cred_path = ""
    if report.username or report.password:
        cred_path = secrets.save(report.username, report.password)
    return cfg_path, cred_path
