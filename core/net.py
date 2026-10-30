"""网络探测：本机 IP、端口可达性、是否已经能上外网。

这里刻意不使用任何第三方库，也不需要管理员权限。
"""

from __future__ import annotations

import gzip
import http.client
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
DEFAULT_PROBE_URL = "https://connect.rom.miui.com/generate_204"
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


def local_ip(target: str = "") -> str:
    """获取本机 IP（不真的发包）。

    给了 target 就取「去往这个地址时会用哪块网卡的 IP」——
    开着代理/VPN（比如 TUN 网卡）时，默认路由给出的可能是 198.18.x.x 这种
    虚拟网卡，用它探测外网会永远显示「已联网」，校园网其实没认证。
    """
    targets = (target,) if target else ("223.5.5.5", "114.114.114.114", "8.8.8.8", "192.168.1.1")
    for host in targets:
        if not host:
            continue
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.4)
            sock.connect((host, 53))
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
    if target:
        return ""
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "127.0.0.1"


def local_ips() -> list[str]:
    """本机所有 IPv4 地址（含 VPN/TUN 虚拟网卡）。"""
    found: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in found:
                found.append(ip)
    except OSError:
        pass
    try:
        primary = socket.gethostbyname(socket.gethostname())
    except OSError:
        primary = ""
    if primary and primary not in found:
        found.insert(0, primary)
    return [ip for ip in found if ip and not ip.startswith("127.")]


def _is_virtual_ip(ip: str) -> bool:
    """VPN/TUN 常用 198.18.0.0/15（fake-ip），链路本地是 169.254.x.x。"""
    if ip.startswith(("169.254.", "198.18.", "198.19.")):
        return True
    return False


def _is_loopback_host(host: str) -> bool:
    text = (host or "").strip().lower()
    return text in ("localhost", "::1") or text.startswith("127.")


def _candidate_ips() -> list[str]:
    """本机地址，真网卡排在虚拟网卡（VPN/TUN）前面。"""
    all_ips = local_ips()
    return [ip for ip in all_ips if not _is_virtual_ip(ip)] + [
        ip for ip in all_ips if _is_virtual_ip(ip)
    ]


def campus_ip(gateway: str, port: int = 80, timeout: float = 0.8) -> str:
    """挑一块能直达网关的网卡 IP。

    开着 VPN/TUN 时默认路由是虚拟网卡，从它探测外网永远是「已联网」。
    这里挨个用本机地址去连网关，能连上的那个才是校园网那块网卡。

    网关是本机地址（自测、代理、本地模拟服务器）时不挑网卡：绑源地址只会
    把本来能通的请求弄断，而那种情况也不存在 VPN 抢路由的问题。
    """
    if not gateway or _is_loopback_host(gateway):
        return ""
    # 先去试真实网卡：TUN 网卡往往「什么都连得通」，先试它必然选中它。
    ordered = _candidate_ips()
    for ip in ordered:
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.bind((ip, 0))
            sock.connect((gateway, port))
            return ip
        except OSError:
            continue
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:  # pragma: no cover
                    pass
    for ip in ordered:
        if not _is_virtual_ip(ip):
            return ip
    return ""


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


class _SourceHandler(urllib.request.HTTPHandler):
    """从指定本机 IP 发请求：让包走校园网那块网卡，而不是 VPN/TUN 虚拟网卡。"""

    def __init__(self, source_ip: str):
        super().__init__()
        self.source_ip = source_ip

    def http_open(self, req):
        def build(host, **kwargs):
            kwargs.pop("timeout", None)
            return http.client.HTTPConnection(
                host, timeout=_DEFAULT_HTTP_TIMEOUT, source_address=(self.source_ip, 0)
            )

        return self.do_open(build, req)


_DEFAULT_HTTP_TIMEOUT = 10.0


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
    source_ip: str = "",
) -> ProbeResult:
    """发一个 HTTP(S) 请求并返回结构化的结果（不抛异常）。

    一律不走系统代理：判断「校园网认证过没有」不能被用户的代理软件骗过去。
    source_ip 非空时从这个本机地址发（用于避开 VPN/TUN 网卡）。
    """
    result = ProbeResult(final_url=url)
    req_headers = {"User-Agent": _UA, "Accept-Encoding": "identity"}
    if headers:
        req_headers.update({k: v for k, v in headers.items() if v is not None})
    try:
        req = urllib.request.Request(url, data=data, method=method, headers=req_headers)
    except ValueError as exc:
        result.error = f"请求地址不合法: {exc}"
        return result

    handlers: list = [
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=_ssl_context(insecure)),
    ]
    if source_ip:
        handlers.append(_SourceHandler(source_ip))
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


def online(
    url: str = DEFAULT_PROBE_URL,
    expect_status: int = DEFAULT_PROBE_STATUS,
    timeout: float = 6.0,
    source_ip: str = "",
):
    """是否已经可以正常上网。返回 (是否在线, 说明)。"""
    # 不跟跳转：未认证时探测地址通常会被 302 到认证页，
    # 那个 Location 本身就是最有用的诊断信息（跟过去只会拿到一张登录页 HTML）。
    res = http_probe(
        url,
        timeout=timeout,
        insecure=url.startswith("https://"),
        follow_redirects=False,
        source_ip=source_ip,
    )
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
