"""网络探测：本机 IP、端口可达性、是否已经能上外网。

这里刻意不使用任何第三方库，也不需要管理员权限。
"""

from __future__ import annotations

import gzip
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass

# 用来判断“是否已联网”的探测地址：
# 生成 204 的探测接口，未认证时会返回 200/302（被劫持），已联网时返回 204。
DEFAULT_PROBE_URL = "http://connect.rom.miui.com/generate_204"
DEFAULT_PROBE_STATUS = 204

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass
class ProbeResult:
    """一次 HTTP 探测的结果。"""

    status: int = 0
    final_url: str = ""
    headers: dict = None
    body: str = ""
    error: str = ""

    def __post_init__(self):
        if self.headers is None:
            self.headers = {}

    @property
    def ok(self) -> bool:
        return self.error == "" and self.status > 0


def local_ip() -> str:
    """获取本机在局域网里的 IP（不真的发包）。"""
    for target in ("223.5.5.5", "114.114.114.114", "8.8.8.8", "192.168.1.1"):
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.4)
            sock.connect((target, 53))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        except OSError:
            continue
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:  # pragma: no cover
                    pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


def local_mac() -> str:
    """取本机 MAC（取不到时返回空串，部分认证系统需要）。"""
    node = None
    try:
        import uuid

        node = uuid.getnode()
    except Exception:  # pragma: no cover - 极少数环境会抛异常
        return ""
    if not node:
        return ""
    if (node >> 40) & 0x01:
        # uuid.getnode() 取不到真实网卡时会随机造一个（最低位组播位为 1），
        # 这种值发出去只会让认证失败得更莫名，宁可当作取不到。
        return ""
    raw = f"{node:012x}"
    return ":".join(raw[i : i + 2] for i in range(0, 12, 2))


def _ssl_context(insecure: bool = False) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _decode_body(raw: bytes, encoding: str) -> bytes:
    encoding = (encoding or "").lower()
    if "gzip" in encoding:
        try:
            return gzip.decompress(raw)
        except OSError:
            return raw
    if "deflate" in encoding:
        try:
            return zlib.decompress(raw)
        except zlib.error:
            try:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
            except zlib.error:
                return raw
    return raw


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """拦下自动跳转，让调用方自己看 3xx 和 Location。"""

    def redirect_request(self, *args, **kwargs):  # noqa: D102
        return None


def http_probe(
    url: str,
    *,
    timeout: float = 8.0,
    method: str = "GET",
    headers: dict | None = None,
    data: bytes | None = None,
    insecure: bool = False,
    follow_redirects: bool = True,
    charsets: tuple = ("utf-8", "gb18030"),
    max_body: int = 256 * 1024,
) -> ProbeResult:
    """发一个 HTTP(S) 请求并返回结构化的结果（不抛异常）。"""
    result = ProbeResult(final_url=url)
    req_headers = {"User-Agent": _UA, "Accept-Encoding": "identity"}
    if headers:
        req_headers.update({k: v for k, v in headers.items() if v is not None})
    try:
        req = urllib.request.Request(url, data=data, method=method, headers=req_headers)
    except ValueError as exc:
        result.error = f"请求地址不合法: {exc}"
        return result

    handlers: list = [urllib.request.HTTPSHandler(context=_ssl_context(insecure))]
    if not follow_redirects:
        handlers.append(NoRedirect())
    opener = urllib.request.build_opener(*handlers)

    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read(max_body)
            result.status = resp.status
            result.final_url = resp.geturl()
            result.headers = {k.lower(): v for k, v in resp.headers.items()}
            raw = _decode_body(raw, result.headers.get("content-encoding", ""))
            result.body = _to_text(raw, charsets, result.headers.get("content-type", ""))
    except urllib.error.HTTPError as exc:  # 4xx/5xx：也要读 body，认证系统常用 4xx 表示失败
        result.status = exc.code
        result.final_url = getattr(exc, "url", url)
        try:
            raw = exc.read(max_body)
            result.headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
            raw = _decode_body(raw, result.headers.get("content-encoding", ""))
            result.body = _to_text(raw, charsets, result.headers.get("content-type", ""))
        except Exception:  # pragma: no cover
            pass
    except urllib.error.URLError as exc:
        result.error = f"无法连接: {getattr(exc, 'reason', exc)}"
    except (socket.timeout, TimeoutError):
        result.error = "请求超时"
    except Exception as exc:  # noqa: BLE001 - 探测失败不应中断主流程
        result.error = f"{type(exc).__name__}: {exc}"
    return result


def _to_text(raw: bytes, charsets: tuple, content_type: str) -> str:
    candidates = []
    lowered = (content_type or "").lower()
    if "charset=" in lowered:
        candidates.append(lowered.split("charset=", 1)[1].split(";")[0].strip())
    candidates.extend(charsets)
    for enc in candidates:
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def tcp_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    """TCP 端口能否连上（用于等待校园网网关就绪）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def url_host_port(url: str) -> tuple[str, int]:
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    if parsed.port:
        port = parsed.port
    else:
        port = 443 if parsed.scheme.lower() == "https" else 80
    return host, port


def portal_reachable(url: str, timeout: float = 2.0) -> bool:
    """认证页面所在主机是否可以连通（不管认证过没有）。"""
    host, port = url_host_port(url)
    if not host:
        return False
    return tcp_reachable(host, port, timeout)


def online(url: str = DEFAULT_PROBE_URL, expect_status: int = DEFAULT_PROBE_STATUS, timeout: float = 6.0):
    """是否已经可以正常上网。返回 (是否在线, 说明)。"""
    # 不跟跳转：未认证时探测地址通常会被 302 到认证页，
    # 那个 Location 本身就是最有用的诊断信息（跟过去只会拿到一张登录页 HTML）。
    res = http_probe(url, timeout=timeout, insecure=url.startswith("https://"), follow_redirects=False)
    if not res.ok:
        return False, res.error or "探测失败"
    if res.status == expect_status:
        return True, "已联网"
    # 被劫持到认证页时，服务器通常返回 200 + HTML，或被 302 到 portal
    location = res.headers.get("location", "")
    if res.status in (301, 302, 303, 307, 308):
        return False, f"被重定向到认证页: {location or '(没有 Location)'}"
    return False, f"探测地址返回 HTTP {res.status}（未联网）"


def wait_for(condition, timeout: float = 60.0, interval: float = 2.0, on_tick=None) -> bool:
    """轮询等待某个条件成立。"""
    deadline = time.time() + max(0.0, timeout)
    while True:
        if condition():
            return True
        if time.time() >= deadline:
            return False
        if on_tick:
            try:
                on_tick()
            except Exception:  # pragma: no cover
                pass
        time.sleep(interval)
