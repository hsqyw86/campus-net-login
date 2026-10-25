"""本地假的校园网认证服务器（只用于测试，不联网）。

它模仿了一类很常见的 Portal 认证流程：
  1. 访问 http://host/ 会被 302 到 /login.html?userip=..&token=..，并下发会话 Cookie；
  2. 真正登录要 POST /eportal/login，必须带上第 1 步拿到的 token 和会话 Cookie；
  3. 返回 JSON：{"result": "success|fail", "msg": "..."}。

启动方式：
    python tests/mock_portal.py 8089
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ACCOUNTS = {"20230001": "S3cret!pwd"}
TOKEN = "T0K3N-abc123"
SESSION_COOKIE = "JSESSIONID=mock-session-42"
CLIENT_IP = "10.20.30.40"


class Handler(BaseHTTPRequestHandler):
    server_version = "MockPortal/1.0"

    def log_message(self, fmt, *args):  # 静音
        pass

    # ---------------------------------------------------------------- 工具
    def _send(self, code: int, body: str = "", ctype: str = "application/json; charset=utf-8", headers=None):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if data:
            self.wfile.write(data)
        self.server.requests.append((self.command, self.path, dict(self.headers)))

    def _has_session(self) -> bool:
        return "JSESSIONID=mock-session-42" in (self.headers.get("Cookie") or "")

    def _json(self, payload: dict, code: int = 200):
        self._send(code, json.dumps(payload, ensure_ascii=False))

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/generate_204":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            self.server.requests.append((self.command, self.path, dict(self.headers)))
            return
        if path in ("/", "/portal"):
            location = f"/login.html?userip={CLIENT_IP}&wlanacname=AC-TEST&token={TOKEN}"
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Set-Cookie", f"{SESSION_COOKIE}; Path=/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            self.server.requests.append((self.command, self.path, dict(self.headers)))
            return
        if path == "/login.html":
            self._send(200, "<html><body>请输入账号密码</body></html>", "text/html; charset=utf-8")
            return
        if path == "/stats":
            self._json(
                {
                    "logged_in": bool(self.server.logged_in),
                    "requests": len(self.server.requests),
                    "last": [f"{m} {p}" for m, p, _ in self.server.requests[-5:]],
                }
            )
            return
        self._send(404, json.dumps({"result": "fail", "msg": "not found"}))

    # ---------------------------------------------------------------- POST
    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        form = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

        if path != "/eportal/login":
            self._json({"result": "fail", "msg": "not found"}, 404)
            return
        if not self._has_session():
            self._json({"result": "fail", "msg": "无会话，请先打开认证页面"})
            return
        if form.get("token") != TOKEN:
            self._json({"result": "fail", "msg": "token 无效或已过期"})
            return

        user = form.get("userId", "")
        password = form.get("password", "")
        expected = hashlib.md5(ACCOUNTS.get(user, "").encode()).hexdigest()
        if user in ACCOUNTS and password == expected:
            self.server.logged_in = True
            self._json({"result": "success", "msg": "认证成功，欢迎使用校园网"})
        elif user in ACCOUNTS:
            self._json({"result": "fail", "msg": "密码错误"})
        else:
            self._json({"result": "fail", "msg": "用户名不存在"})


class MockPortal:
    """上下文管理器：自动选端口、起线程、退出时关闭。"""

    def __init__(self, port: int = 0):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.requests = []
        self.httpd.logged_in = False
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def requests(self):
        return self.httpd.requests

    @property
    def logged_in(self) -> bool:
        return self.httpd.logged_in

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        return False


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8089
    server = MockPortal(port)
    print(f"假认证服务器已启动：{server.base_url}")
    print(f"  账号 20230001 / 密码 S3cret!pwd（密码按 md5 提交）")
    print("  Ctrl+C 退出")
    with server:
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            print("\n已停止")
