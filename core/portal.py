"""通用 Portal（网页）认证引擎。

思路：不绑定任何厂商，把「一次登录」拆成三段可描述的东西 ——
  1. preflight：先访问一个地址（通常是被劫持后的跳转页），从 URL/响应里拿到
     userip、token、wlanacname 等本次登录必需的参数；
  2. login：按模板拼出请求（URL / 头 / 表单字段），把 {username}、{password}
     以及上一步拿到的参数填进去发出去；
  3. expect：按「JSON 字段」或「关键字」判断成功还是失败，必要时再用联网探测复核。
"""

from __future__ import annotations

import base64
import hashlib
import http.cookiejar
import importlib.util
import json
import logging
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from . import net, paths

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_DROP_HEADERS = {"host", "content-length", "connection", "accept-encoding", "transfer-encoding", "expect"}
_SECRET_VARS = {
    "username", "user", "account", "userid", "user_id", "name",
    "password", "password_raw", "pwd", "passwd",
    "md5", "md5_upper", "sha1", "base64", "urlquote",
}
# 跨域跳转时不该带过去的头（浏览器同样会丢掉这些）
_CROSS_ORIGIN_DROP_HEADERS = {"authorization", "cookie", "proxy-authorization"}

logger = logging.getLogger("campusnet.portal")

_hook_module = None
_hook_loaded = False


def load_hook():
    """加载程序目录下的 custom_hook.py（可选）。

    有些学校（深澜、锐捷等）在网页 JS 里对密码做了 AES/RSA 之类的加密，
    纯配置写不出来，就在 custom_hook.py 里实现 transform_password()：
        def transform_password(username, password, variables) -> str
    也可以实现 judge(status, body, expect) -> (bool|None, str) 覆盖成功判定。
    """
    global _hook_module, _hook_loaded
    if _hook_loaded:
        return _hook_module
    _hook_loaded = True
    if not os.path.exists(paths.HOOK_FILE):
        return None
    try:
        spec = importlib.util.spec_from_file_location("campusnet_custom_hook", paths.HOOK_FILE)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _hook_module = module
        logger.info("已加载自定义钩子：%s", paths.HOOK_FILE)
    except Exception:  # noqa: BLE001 - 钩子坏了不能拖垮整个登录
        logger.exception("加载 custom_hook.py 失败，已忽略")
        _hook_module = None
    return _hook_module


class LoginError(Exception):
    """登录过程中出错（配置、占位符、网络不可达等）。"""


class ConfigProblem(LoginError):
    """配置本身有问题（占位符写错、地址为空……）。重试一百次也一样，立即停。"""


@dataclass
class RenderedRequest:
    """一条已经渲染好、可以直接发出去的请求。"""

    method: str
    url: str
    headers: dict = field(default_factory=dict)
    body: bytes | None = None
    body_text: str = ""

    def preview(self, mask: bool = True) -> str:
        lines = [f"{self.method} {self.url}"]
        for key, value in self.headers.items():
            if mask and key.lower() in ("cookie", "authorization"):
                value = _mask_secret(value)
            lines.append(f"{key}: {value}")
        if self.body_text:
            lines.append("")
            lines.append(_mask_secret(self.body_text) if mask else self.body_text)
        return "\n".join(lines)


@dataclass
class LoginResult:
    ok: bool = False
    message: str = ""
    detail: str = ""
    status: int = 0
    url: str = ""
    body: str = ""
    attempts: int = 0
    elapsed: float = 0.0
    request_preview: str = ""
    verified: bool | None = None
    definitive: bool = False  # 服务器给过明确答复（如密码错误），重试没意义还会锁账号
    config_error: bool = False  # 配置本身有问题（不是网络、不是账号）


def _mask_secret(text: str, limit: int = 400) -> str:
    text = re.sub(r"(password|passwd|pwd)=([^&\s\"]+)", r"\1=***", text, flags=re.I)
    text = re.sub(r'"(password|passwd|pwd)"\s*:\s*"[^"]*"', r'"\1":"***"', text, flags=re.I)
    text = re.sub(r"(JSESSIONID|SESSION|token)=([^;&\s]+)", r"\1=***", text, flags=re.I)
    return text[:limit]


# ------------------------------------------------------------------ 模板渲染

def render(template, variables: dict, where: str = "配置") -> str:
    """把 {name} 替换成变量值；遇到不认识的变量直接报错，避免发出半截请求。"""
    if template is None:
        return ""
    text = str(template)

    def repl(match: re.Match) -> str:
        key = match.group(1)
        if key not in variables:
            known = ", ".join(sorted(variables))
            raise ConfigProblem(
                f"{where} 里的占位符 {{{key}}} 不认识。\n可用的变量有：{known}"
            )
        value = variables[key]
        return "" if value is None else str(value)

    return _PLACEHOLDER.sub(repl, text)


def build_variables(cfg: dict, username: str, password: str) -> dict:
    """准备所有可用变量（账号、密码各种变形、本机信息等）。"""
    md5 = hashlib.md5(password.encode("utf-8")).hexdigest()
    sha1 = hashlib.sha1(password.encode("utf-8")).hexdigest()
    b64 = base64.b64encode(password.encode("utf-8")).decode("ascii")
    ip = net.local_ip()
    mac = net.local_mac()
    now = time.time()
    variables = {
        # 账号
        "username": username,
        "user": username,
        "account": username,
        "userid": username,
        "user_id": username,
        "name": username,
        # 密码：先放明文，稍后按 password_template 改写
        "password": password,
        "password_raw": password,
        "pwd": password,
        "passwd": password,
        # 常见变形
        "md5": md5,
        "md5_upper": md5.upper(),
        "sha1": sha1,
        "base64": b64,
        "urlquote": urllib.parse.quote(password, safe=""),
        # 本机信息
        "ip": ip,
        "local_ip": ip,
        "userip": ip,
        "wlanuserip": ip,
        "mac": mac,
        "hostname": socket.gethostname(),
        # 时间
        "timestamp": str(int(now * 1000)),
        "timestamp_s": str(int(now)),
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    tpl = (cfg.get("login") or {}).get("password_template") or "{password}"
    variables["password"] = render(tpl, variables, "login.password_template")

    hook = load_hook()
    if hook is not None and hasattr(hook, "transform_password"):
        try:
            variables["password"] = str(hook.transform_password(username, password, dict(variables)))
        except Exception as exc:  # noqa: BLE001
            raise ConfigProblem(f"custom_hook.transform_password 执行出错：{exc}") from exc
    return variables


# ------------------------------------------------------------------ 会话

class _FakeResponse:
    """给 http.cookiejar 用的最小响应对象（手工跟跳转时收 Cookie）。"""

    def __init__(self, headers):
        self._headers = headers

    def info(self):
        return self._headers


def _origin(url: str) -> tuple[str, str, int | None]:
    """scheme + host + port，用来判断是不是跨域跳转。"""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return ("", "", None)
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme.lower() == "https" else 80
    return (parsed.scheme.lower(), (parsed.hostname or "").lower(), port)


class Session:
    """带 Cookie 的 HTTP 会话，登录前后的请求共享同一个会话。

    手动跟跳转（不交给 urllib），原因有三：
      1. 每一跳都能顺手把 Set-Cookie 收进 cookie jar，避免中途丢会话；
      2. 能完整记录跳转链，从 Location 里捞 userip / token 之类的参数；
      3. 跳转行为可控（303 改成 GET，307 保留 POST），和浏览器一致。
    """

    def __init__(self, insecure: bool = False, max_redirects: int = 8):
        self.jar = http.cookiejar.CookieJar()
        self.history: list[tuple[int, str]] = []
        self.max_redirects = max_redirects
        self.last_url = ""  # 最后一次真正访问到的地址（做 Referer 用）
        self._insecure = insecure
        self._opener = self._build()

    def _build(self):
        handlers = [
            urllib.request.HTTPCookieProcessor(self.jar),
            net.NoRedirect(),
            urllib.request.HTTPSHandler(context=net._ssl_context(self._insecure)),
        ]
        return urllib.request.build_opener(*handlers)

    def _absorb_cookies(self, headers, url: str) -> None:
        if not headers:
            return
        try:
            self.jar.extract_cookies(_FakeResponse(headers), urllib.request.Request(url))
        except Exception:  # pragma: no cover - Cookie 收不上不影响主流程
            pass

    def _fill(self, result: net.ProbeResult, status: int, headers, raw: bytes, final_url: str) -> None:
        result.status = status
        result.final_url = final_url
        self.last_url = final_url
        result.headers = {k.lower(): v for k, v in (headers.items() if headers else [])}
        raw = net._decode_body(raw, result.headers.get("content-encoding", ""))
        result.body = net._to_text(raw, ("utf-8", "gb18030"), result.headers.get("content-type", ""))

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        body: bytes | None = None,
        timeout: float = 10.0,
    ) -> net.ProbeResult:
        result = net.ProbeResult(final_url=url)
        base_headers = {"Accept-Encoding": "identity"}
        base_headers.update({k: v for k, v in (headers or {}).items() if v is not None})
        current_url = url
        current_method = method.upper()
        current_body = body

        for hop in range(self.max_redirects + 1):
            try:
                req = urllib.request.Request(
                    current_url, data=current_body, method=current_method, headers=base_headers
                )
                with self._opener.open(req, timeout=timeout) as resp:
                    raw = resp.read(256 * 1024)
                    self._absorb_cookies(resp.headers, current_url)
                    self._fill(result, resp.status, resp.headers, raw, resp.geturl())
                    return result
            except urllib.error.HTTPError as exc:
                headers_msg = exc.headers
                try:
                    raw = exc.read(256 * 1024)
                except Exception:  # pragma: no cover
                    raw = b""
                self._absorb_cookies(headers_msg, current_url)
                self._fill(result, exc.code, headers_msg, raw, current_url)

                location = (headers_msg.get("Location") if headers_msg else None) or ""
                if exc.code in (301, 302, 303, 307, 308) and location and hop < self.max_redirects:
                    next_url = urllib.parse.urljoin(current_url, location.strip())
                    self.history.append((int(exc.code), next_url))
                    if exc.code == 303 or (exc.code in (301, 302) and current_method != "GET"):
                        current_method = "GET"
                        current_body = None
                        base_headers = {k: v for k, v in base_headers.items() if k.lower() != "content-type"}
                    if _origin(next_url) != _origin(current_url):
                        # 网关跳到另一个域名时，别把 Cookie/Authorization 这些带过去
                        base_headers = {
                            k: v for k, v in base_headers.items() if k.lower() not in _CROSS_ORIGIN_DROP_HEADERS
                        }
                    current_url = next_url
                    continue
                return result
            except urllib.error.URLError as exc:
                result.error = f"无法连接 {current_url}：{getattr(exc, 'reason', exc)}"
                return result
            except TimeoutError:
                result.error = f"请求超时：{current_url}"
                return result
            except OSError as exc:
                result.error = f"网络错误：{exc}"
                return result

        result.error = f"跳转次数超过 {self.max_redirects} 次，已停止（认证页面可能在循环跳转）"
        return result




# ------------------------------------------------------------------ 组装请求

def _render_headers(raw: dict | None, variables: dict) -> dict:
    headers: dict = {}
    for key, value in (raw or {}).items():
        name = render(key, variables, "login.headers")
        if name.lower() in _DROP_HEADERS:
            continue
        headers[name] = render(value, variables, "login.headers")
    return headers


_ALREADY_ENCODED_VARS = {"urlquote"}


def _maybe_escape(key: str, value: str, escape_form: bool) -> str:
    """表单体里的占位符按「一个字段值」来编码，和 urlencode 分支保持一致。

    以前只编码「密码类」变量，结果 {pwd} / {token} 里的 & = + 会把一个字段劈成两个。
    {urlquote} 本身就是编码后的值，再编码一次就成了双重编码。
    """
    if escape_form and key not in _ALREADY_ENCODED_VARS:
        return urllib.parse.quote(value, safe="")
    return value


def build_request(cfg: dict, variables: dict, *, escape_form: bool | None = None) -> RenderedRequest:
    login = cfg.get("login") or {}
    method = (login.get("method") or "POST").upper()
    if escape_form is None:
        escape_form = bool(login.get("escape_form", True))

    url = render(login.get("url", ""), variables, "login.url").strip()
    if not url:
        raise ConfigProblem("配置里没有登录地址（login.url 为空）。")
    headers = _render_headers(login.get("headers"), variables)
    ctype = (login.get("content_type") or "form").lower()
    fields = login.get("fields") or {}
    body_tpl = login.get("body") or ""

    body_text = ""
    if fields:
        rendered = {}
        for key, value in fields.items():
            rendered[render(key, variables, "login.fields")] = render(value, variables, "login.fields")
        if ctype == "json":
            body_text = json.dumps(rendered, ensure_ascii=False)
            headers.setdefault("Content-Type", "application/json; charset=UTF-8")
        else:
            body_text = urllib.parse.urlencode(rendered)
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
    elif body_tpl:
        # JSON 体里不该做 URL 编码（那会把密码改成 x%26y 发出去），
        # 只有表单体才需要；fields 分支的 json.dumps 本来也不编码。
        if ctype == "json":
            body_text = render(body_tpl, variables, "login.body")
            headers.setdefault("Content-Type", "application/json; charset=UTF-8")
        elif escape_form:
            body_text = _PLACEHOLDER.sub(
                lambda m: _maybe_escape(m.group(1), render(m.group(0), variables, "login.body"), True),
                body_tpl,
            )
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
        else:
            body_text = render(body_tpl, variables, "login.body")
            if ctype == "form":
                headers.setdefault("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")

    if method == "GET":
        if body_text:
            url = url + ("&" if "?" in url else "?") + body_text
        body = None
        body_text_out = ""
    else:
        body = body_text.encode("utf-8") if body_text else None
        body_text_out = body_text
        if body is not None and not any(k.lower() == "content-type" for k in headers):
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"

    return RenderedRequest(method=method, url=url, headers=headers, body=body, body_text=body_text_out)


# ------------------------------------------------------------------ 判定

def _json_get(payload, path: str):
    cur = payload
    for part in str(path).split("."):
        if not part:
            continue
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                raise KeyError(part)
            continue
        if not isinstance(cur, dict) or part not in cur:
            raise KeyError(part)
        cur = cur[part]
    return cur


def extract_message(body: str, limit: int = 160) -> str:
    """从响应里尽量抠一句人话出来，用于提示用户。"""
    text = (body or "").strip()
    if not text:
        return ""
    if text.startswith(("{", "[")):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            for key in ("msg", "message", "errmsg", "error", "info", "result", "res", "ret", "reason", "detail"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:limit]
                if isinstance(value, dict):
                    for sub in ("msg", "message", "info"):
                        sv = value.get(sub)
                        if isinstance(sv, str) and sv.strip():
                            return sv.strip()[:limit]
    if text.startswith("<"):
        stripped = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.S | re.I)
        stripped = re.sub(r"<[^>]+>", " ", stripped)
        stripped = re.sub(r"\s+", " ", stripped).strip()
        return stripped[:limit]
    return text[:limit]


def judge(cfg: dict, status: int, body: str) -> tuple[bool | None, str]:
    """返回 (结论, 说明)。结论为 None 表示拿不准，交给联网探测复核。"""
    expect = cfg.get("expect") or {}
    message = extract_message(body)
    path = (expect.get("json_path") or "").strip()

    hook = load_hook()
    if hook is not None and hasattr(hook, "judge"):
        try:
            verdict = hook.judge(status, body, expect)
            if isinstance(verdict, tuple) and len(verdict) == 2:
                return bool(verdict[0]) if verdict[0] is not None else None, str(verdict[1] or message)
        except Exception:  # noqa: BLE001
            logger.exception("custom_hook.judge 执行失败，改用内置判定")

    if path:
        try:
            payload = json.loads(body or "")
        except json.JSONDecodeError:
            return None, message or "响应不是合法 JSON，无法按 JSON 规则判定"
        try:
            value = _json_get(payload, path)
        except (KeyError, TypeError):
            return False, message or f"响应里没有字段 {path}"
        expected = expect.get("json_equals", "")
        if expected is None or expected == "":
            ok = bool(value) and str(value).lower() not in ("0", "false", "fail", "failed", "error")
        else:
            ok = str(value).strip().lower() == str(expected).strip().lower()
        return ok, message or f"{path} = {value}"

    text = body or ""
    for keyword in expect.get("failure_keywords") or []:
        if keyword and keyword in text:
            return False, message or f"命中失败关键字「{keyword}」"
    for keyword in expect.get("success_keywords") or []:
        if keyword and keyword in text:
            return True, message or f"命中成功关键字「{keyword}」"

    try:
        status_ok = [int(s) for s in (expect.get("status_ok") or [200])]
    except (TypeError, ValueError):
        status_ok = [200]
    if status in status_ok:
        return None, message or f"HTTP {status}（未匹配关键字，将用联网探测确认）"
    return False, message or f"HTTP {status}，且未匹配成功关键字"


# ------------------------------------------------------------------ 预跳转

def _capture_from_query(url: str) -> dict:
    try:
        query = urllib.parse.urlsplit(url).query
    except ValueError:
        return {}
    return {k: v for k, v in urllib.parse.parse_qsl(query, keep_blank_values=True)}


def preflight_variables(cfg: dict, username: str = "", password: str = "") -> dict:
    """跑一次「登录前跳转」，返回补齐了 token/userip 等参数的变量表（自检、预览用）。

    与真正登录时完全同一套预跳转逻辑，所以自检能验证到「参数到底取不取得到」。
    """
    variables = build_variables(cfg, username, password)
    session = Session(insecure=bool((cfg.get("login") or {}).get("insecure_tls")))
    _preflight(cfg, session, variables)
    return variables


def _preflight(cfg: dict, session: Session, variables: dict) -> dict:
    pre = cfg.get("preflight") or {}
    raw_url = (pre.get("url") or "").strip()
    if not raw_url:
        return {}

    url = render(raw_url, variables, "preflight.url")
    method = (pre.get("method") or "GET").upper()
    headers = _render_headers(pre.get("headers"), variables)
    body_tpl = pre.get("body") or ""
    body_text = render(body_tpl, variables, "preflight.body") if body_tpl else ""
    if body_text and method == "GET":
        # GET 没有请求体，参数只能挂在查询串上；以前这里会静默丢掉整段 body
        url = url + ("&" if "?" in url else "?") + body_text
        body_text = ""
    body = body_text.encode("utf-8") if body_text else None
    timeout = float(pre.get("timeout") or 8)
    if not pre.get("follow_redirects", True):
        session.max_redirects = 0

    logger.info("preflight: %s %s", method, _mask_secret(url))
    res = session.request(method, url, headers=headers, body=body, timeout=timeout)
    if res.error:
        raise LoginError(f"访问登录前的跳转地址失败：{res.error}")

    captured: dict = {}
    if pre.get("capture_query", True):
        captured.update(_capture_from_query(res.final_url))
        for code, location in session.history:
            captured.update({k: v for k, v in _capture_from_query(location).items() if k not in captured})

    for rule in pre.get("capture_regex") or []:
        name = (rule or {}).get("name")
        pattern = (rule or {}).get("pattern")
        if not name or not pattern:
            continue
        source = res.final_url if (rule.get("from") or "url").lower() == "url" else res.body
        match = re.search(pattern, source or "")
        if match:
            captured[name] = match.group(1) if match.groups() else match.group(0)

    captured = {k: v for k, v in captured.items() if v}
    if captured:
        variables.update(captured)
        logger.info("preflight 取到参数: %s", ", ".join(captured))
    return captured


# ------------------------------------------------------------------ 主流程

def _attempt(cfg: dict, variables: dict, session: Session, *, verify: bool) -> LoginResult:
    login = cfg.get("login") or {}

    request = build_request(cfg, variables)
    if login.get("auto_referer", True) and not any(k.lower() == "referer" for k in request.headers):
        # 用「上一跳真正落地的地址」当 Referer；没有跳转时退回预跳转地址。
        ref = session.last_url or (cfg.get("preflight") or {}).get("url") or ""
        if ref:
            try:
                request.headers["Referer"] = render(ref, variables, "preflight.url")
            except LoginError as exc:
                logger.debug("Referer 渲染失败，已跳过：%s", exc)
    logger.info("login: %s %s", request.method, _mask_secret(request.url))
    res = session.request(
        request.method,
        request.url,
        headers=request.headers,
        body=request.body,
        timeout=float(login.get("timeout") or 10),
    )
    result = LoginResult(
        status=res.status,
        url=request.url,
        body=res.body,
        request_preview=request.preview(),
    )
    if res.error:
        result.ok = False
        result.message = res.error
        return result

    verdict, message = judge(cfg, res.status, res.body)
    result.message = message

    if verdict is False:
        result.ok = False
        result.definitive = True  # 密码错 / 已欠费这类，服务器已经说清了，别再连发
        result.detail = _mask_secret(res.body, 600)
        return result

    if verify:
        online, why = verify_online(cfg)
        result.verified = online
        if online:
            result.ok = True
            result.message = message if verdict else "认证成功"
        else:
            result.ok = False
            result.message = f"{message}（但联网检测没通过：{why}）"
            result.detail = _mask_secret(res.body, 600)
        return result

    result.ok = True if verdict is None else verdict
    result.message = message
    result.detail = _mask_secret(res.body, 600)
    return result


def verify_settings(cfg: dict) -> dict:
    """统一解析 verify 段（探测地址 / 期望状态码 / 超时 / 是否启用）。

    runner 里的「是否已联网」和这里的「登录后复核」必须用同一套参数，
    否则会出现自检说已联网、登录后复核说不通的怪事。
    """
    raw = cfg.get("verify") or {}
    try:
        expect = int(raw.get("expect_status") or net.DEFAULT_PROBE_STATUS)
    except (TypeError, ValueError):
        expect = net.DEFAULT_PROBE_STATUS
    try:
        timeout = float(raw.get("timeout") or 6)
    except (TypeError, ValueError):
        timeout = 6.0
    return {
        "enabled": bool(raw.get("enabled", True)),
        "url": raw.get("url") or net.DEFAULT_PROBE_URL,
        "expect_status": expect,
        "timeout": timeout,
    }


def verify_online(cfg: dict) -> tuple[bool, str]:
    settings = verify_settings(cfg)
    if not settings["enabled"]:
        return True, "已跳过联网检测"
    return net.online(settings["url"], settings["expect_status"], settings["timeout"])


def login(
    cfg: dict,
    username: str,
    password: str,
    *,
    dry_run: bool = False,
    verify: bool | None = None,
    log: logging.Logger | None = None,
) -> LoginResult:
    """执行一次完整登录（含预跳转、重试）。"""
    log = log or logger
    started = time.time()
    try:
        variables = build_variables(cfg, username, password)
    except ConfigProblem as exc:
        return LoginResult(ok=False, message=str(exc), definitive=True, config_error=True)
    do_verify = verify_settings(cfg)["enabled"]
    if verify is not None:
        do_verify = verify

    retry = cfg.get("retry") or {}
    try:
        attempts = max(1, int(retry.get("attempts") or 1))
    except (TypeError, ValueError):
        attempts = 1
    interval = float(retry.get("interval") or 0)

    last = LoginResult(ok=False, message="未执行")
    for index in range(1, attempts + 1):
        session = Session(insecure=bool((cfg.get("login") or {}).get("insecure_tls")))
        try:
            _preflight(cfg, session, variables)
            if dry_run:
                request = build_request(cfg, variables)
                return LoginResult(
                    ok=True,
                    message="仅预览，未真正发送",
                    request_preview=request.preview(),
                    attempts=index,
                    elapsed=time.time() - started,
                )
            last = _attempt(cfg, variables, session, verify=do_verify)
        except ConfigProblem as exc:
            # 配置写错了，重试一百次也是同一个错，直接给结论
            last = LoginResult(
                ok=False, message=str(exc), attempts=index, definitive=True, config_error=True
            )
        except LoginError as exc:
            last = LoginResult(ok=False, message=str(exc), attempts=index)
        except Exception as exc:  # noqa: BLE001 - 兜底，避免一键登录弹崩溃框
            log.exception("登录过程异常")
            last = LoginResult(ok=False, message=f"{type(exc).__name__}: {exc}", attempts=index)

        last.attempts = index
        last.elapsed = time.time() - started
        if last.ok:
            log.info("第 %s 次尝试登录成功：%s", index, last.message)
            return last
        log.warning("第 %s 次尝试失败：%s", index, last.message)
        if last.definitive:
            log.info("服务器已明确拒绝，不再重试")
            return last
        if index < attempts and interval > 0:
            time.sleep(interval)

    return last
