"""把浏览器「Copy as cURL」的内容解析成配置。

支持 Chrome / Edge 开发者工具里的：
  * Copy as cURL (bash)   —— 推荐
  * Copy as cURL (cmd)
  * Copy as cURL (powershell)
以及自己手写的 curl 命令。

这样用户不用一个字段一个字段地抄请求，粘一段命令就行。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlsplit

# 这些头由程序自己生成，回放时会造成麻烦
_DROP_HEADERS = {
    "host",
    "content-length",
    "connection",
    "accept-encoding",
    "transfer-encoding",
    "expect",
    ":authority",
    ":method",
    ":path",
    ":scheme",
}

_VALUE_FLAGS = {
    "-x",
    "--request",
    "-h",
    "--header",
    "-d",
    "--data",
    "--data-raw",
    "--data-binary",
    "--data-ascii",
    "-b",
    "--cookie",
    "-a",
    "--user-agent",
    "-e",
    "--referer",
    "--url",
    "--connect-timeout",
    "-m",
    "--max-time",
    "-o",
    "--output",
    "--compressed",
    "--json",
    "-u",
    "--user",
}


@dataclass
class CurlRequest:
    url: str = ""
    method: str = "POST"
    headers: dict = field(default_factory=dict)
    body: str = ""
    content_type: str = "form"
    cookies: str = ""
    warnings: list = field(default_factory=list)

    @property
    def host(self) -> str:
        try:
            return urlsplit(self.url).netloc
        except ValueError:
            return ""

    def field_names(self) -> list[str]:
        """请求体里有哪些字段名（form 拆 key=value，json 取顶层键）。"""
        if not self.body:
            return []
        if self.content_type == "json":
            try:
                payload = json.loads(self.body)
            except json.JSONDecodeError:
                return []
            return list(payload.keys()) if isinstance(payload, dict) else []
        return [key for key, _ in parse_form_pairs(self.body)]

    def field_values(self) -> dict:
        if self.content_type == "json":
            try:
                payload = json.loads(self.body)
            except json.JSONDecodeError:
                return {}
            return {k: ("" if v is None else str(v)) for k, v in payload.items()} if isinstance(payload, dict) else {}
        return dict(parse_form_pairs(self.body))

    def guess_fields(self) -> dict:
        """猜「哪个字段是账号 / 密码」，只给建议。"""
        guess: dict = {}
        for key in self.field_names():
            low = key.lower()
            if "pass" in low or "pwd" in low:
                guess.setdefault("password", key)
            elif any(tag in low for tag in ("user", "account", "login", "name", "id", "no")):
                guess.setdefault("username", key)
        # 常见组合：账号字段名里同时含 id/name 时才认，避免把 uuid 之类当账号
        if "username" in guess and guess["username"].lower() in ("uuid", "deviceid", "sessionid"):
            guess.pop("username")
        return guess


def parse_form_pairs(body: str) -> list[tuple[str, str]]:
    """解析 a=1&b=2 形式的请求体，失败返回空列表。"""
    if not body:
        return []
    try:
        return parse_qsl(body, keep_blank_values=True)
    except ValueError:
        return []


class CurlParseError(Exception):
    """解析不出来。"""


def _preprocess(text: str) -> str:
    text = text.strip()
    # cmd 风格（Chrome 的 "Copy as cURL (cmd)" 会把引号写成 ^"，& 写成 ^&）
    cmd_style = '^"' in text
    # 先把三种续行符统一掉：cmd 的 ^、bash 的 \、powershell 的 `
    text = re.sub(r"\^\r?\n", " ", text)
    text = re.sub(r"\\\r?\n", " ", text)
    text = re.sub(r"`\r?\n", " ", text)
    if cmd_style:
        text = text.replace('^"', '"')
        text = re.sub(r"\^([&|<>^()\"'!])", r"\1", text)
    return text.strip()


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if ch == "'":
            j = text.find("'", i + 1)
            if j < 0:
                j = n
            tokens.append(text[i + 1 : j])
            i = j + 1
            continue
        if ch == '"':
            buf: list[str] = []
            i += 1
            while i < n:
                cur = text[i]
                if cur == "\\" and i + 1 < n and text[i + 1] in '"\\$`':
                    buf.append(text[i + 1])
                    i += 2
                    continue
                if cur == '"':
                    i += 1
                    break
                buf.append(cur)
                i += 1
            tokens.append("".join(buf))
            continue
        if ch == "$" and i + 1 < n and text[i + 1] == "'":
            i += 2
            buf = []
            mapping = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"'}
            while i < n:
                cur = text[i]
                if cur == "\\" and i + 1 < n and text[i + 1] in mapping:
                    buf.append(mapping[text[i + 1]])
                    i += 2
                    continue
                if cur == "'":
                    i += 1
                    break
                buf.append(cur)
                i += 1
            tokens.append("".join(buf))
            continue
        buf = []
        while i < n and text[i] not in " \t\r\n":
            if text[i] == "\\" and i + 1 < n and text[i + 1] in "'\"\\ ":
                buf.append(text[i + 1])
                i += 2
                continue
            buf.append(text[i])
            i += 1
        tokens.append("".join(buf))
    return tokens


def _split_headers(raw_headers: list[str]) -> dict:
    headers: dict = {}
    for item in raw_headers:
        if ":" not in item:
            continue
        name, value = item.split(":", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        if name.lower() in _DROP_HEADERS:
            continue
        headers[name] = value
    return headers


def parse_curl(text: str) -> CurlRequest:
    if not text or not text.strip():
        raise CurlParseError("粘贴内容为空。")
    cleaned = _preprocess(text)
    if not cleaned.lower().lstrip().startswith(("curl", "curl.exe")):
        # 允许用户只粘 URL
        if cleaned.lower().startswith(("http://", "https://")):
            return CurlRequest(url=cleaned, method="GET", content_type="raw")
        raise CurlParseError("看起来不是 curl 命令。请在开发者工具里右键请求 → Copy → Copy as cURL。")

    tokens = _tokenize(cleaned)
    if tokens:
        tokens[0] = tokens[0].lower().replace("curl.exe", "curl")

    req = CurlRequest()
    url = ""
    raw_headers: list[str] = []
    data_parts: list[str] = []
    method = ""
    cookies = ""

    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("-") and "=" in tok and tok.split("=", 1)[0] in _VALUE_FLAGS:
            flag, value = tok.split("=", 1)
            tok = flag
            tokens.insert(i + 1, value)
            tok_len = 1
        else:
            tok_len = 1

        lower = tok.lower()
        if lower in ("-x", "--request"):
            method = tokens[i + 1].upper()
            i += 2
            continue
        if lower in ("-h", "--header"):
            raw_headers.append(tokens[i + 1])
            i += 2
            continue
        if lower in ("-d", "--data", "--data-raw", "--data-binary", "--data-ascii", "--json"):
            data_parts.append(tokens[i + 1])
            if lower == "--json" and "content-type" not in {k.lower() for k in raw_headers}:
                raw_headers.append("Content-Type: application/json")
            i += 2
            continue
        if lower in ("-b", "--cookie"):
            cookies = tokens[i + 1]
            i += 2
            continue
        if lower in ("-a", "--user-agent"):
            raw_headers.append(f"User-Agent: {tokens[i + 1]}")
            i += 2
            continue
        if lower in ("-e", "--referer"):
            raw_headers.append(f"Referer: {tokens[i + 1]}")
            i += 2
            continue
        if lower == "--url":
            url = tokens[i + 1]
            i += 2
            continue
        if lower in ("-u", "--user"):
            req.warnings.append("请求里带了 -u 用户名密码，已忽略（请在上方单独填写账号密码）。")
            i += 2
            continue
        if lower in ("-o", "--output", "--connect-timeout", "-m", "--max-time"):
            i += 2
            continue
        if lower == "--compressed":
            i += 1
            continue
        if tok.startswith("-") and not tok.startswith("--"):
            # 未知的短选项，跳过
            i += 1
            continue
        if tok.startswith("--"):
            i += 1
            continue
        # 位置参数 = URL
        if not url:
            url = tok
        i += 1
        _ = tok_len

    if not url:
        raise CurlParseError("没有解析出请求地址（URL）。")

    req.url = url
    req.headers = _split_headers(raw_headers)
    req.cookies = cookies
    if cookies:
        req.headers.setdefault("Cookie", cookies)
    req.body = "".join(data_parts)

    if method:
        req.method = method
    elif req.body:
        req.method = "POST"
    else:
        req.method = "GET"

    ct = ""
    for name, value in req.headers.items():
        if name.lower() == "content-type":
            ct = value.lower()
    if "json" in ct:
        req.content_type = "json"
    elif not ct and req.body.strip().startswith(("{", "[")):
        req.content_type = "json"
    elif ct and "x-www-form-urlencoded" in ct:
        req.content_type = "form"
    else:
        req.content_type = "form" if req.body and "=" in req.body else "raw"
    if not req.body:
        req.content_type = "raw"

    # 提示：如果请求体本身就是 GET 的 query，转成 body 更直观
    if req.method == "GET":
        split = urlsplit(req.url)
        if split.query:
            req.warnings.append("这是 GET 请求，参数已经带在 URL 里了。")
    return req
