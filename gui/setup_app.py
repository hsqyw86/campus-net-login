"""设置向导（tkinter 图形界面）。

目标：让「不知道学校用什么认证系统」的人也能配好 ——
把浏览器开发者工具里那条登录请求「Copy as cURL」粘进来，点两下就行。
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time

import tkinter as tk
from tkinter import messagebox, ttk

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from core import (  # noqa: E402
    autostart,
    config as config_mod,
    curlparse,
    doctor as doctor_mod,
    net,
    paths,
    portal,
    secrets,
)

APP_TITLE = "校园网一键登录 · 设置向导"
FONT = ("Microsoft YaHei UI", 9)
FONT_BOLD = ("Microsoft YaHei UI", 9, "bold")
FONT_TITLE = ("Microsoft YaHei UI", 13, "bold")
FONT_MONO = ("Consolas", 9)

NO_FIELD = "（不使用）"
_PLACEHOLDER_RE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")


def _enable_dpi() -> None:
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


class SetupApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.testing = False
        self.cfg = self._load_config()
        self.old_creds = self._load_creds()
        self._pending_warning = getattr(self, "_pending_warning", "")
        paths.write_python_hint()

        root.title(APP_TITLE)
        # 按屏幕大小自适应，1366x768 的小本也能完整显示
        screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
        win_w = min(940, max(800, screen_w - 240))
        win_h = min(880, max(620, screen_h - 140))
        root.geometry(f"{win_w}x{win_h}+{max(0, (screen_w - win_w) // 2)}+{max(0, (screen_h - win_h) // 3)}")
        root.minsize(800, 600)
        self._build_style()
        self._build_ui()
        self._load_into_ui(self.cfg)
        self._refresh_autostart_state()
        self._poll_log()

        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------ 初始化数据

    def _load_config(self) -> dict:
        try:
            return config_mod.load()
        except config_mod.ConfigError as exc:
            self._pending_warning = str(exc)
            cfg = config_mod.default_config()
            return cfg

    def _load_creds(self):
        try:
            return secrets.load()
        except secrets.SecretError:
            return secrets.Credentials()

    # ------------------------------------------------------------ 界面搭建

    def _build_style(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure(".", font=FONT)
        style.configure("Title.TLabel", font=FONT_TITLE)
        style.configure("Hint.TLabel", foreground="#6b7280", font=("Microsoft YaHei UI", 8))
        style.configure("Ok.TLabel", foreground="#047857", font=FONT_BOLD)
        style.configure("Err.TLabel", foreground="#b91c1c", font=FONT_BOLD)
        style.configure("Run.TLabel", foreground="#1d4ed8", font=FONT_BOLD)
        self.root.option_add("*Font", FONT)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=(14, 12, 14, 10))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="校园网一键登录", style="Title.TLabel").pack(side="left")
        ttk.Label(
            header,
            text="  通用 Portal 认证 · 配置一次，以后双击即登录",
            style="Hint.TLabel",
        ).pack(side="left", pady=(6, 0))
        self.status_label = ttk.Label(header, text="", style="Hint.TLabel")
        self.status_label.pack(side="right", pady=(6, 0))

        # ---------- 步骤 1：粘贴请求
        box1 = ttk.LabelFrame(outer, text=" ① 粘贴登录请求（最简单的方式） ", padding=10)
        box1.grid(row=1, column=0, sticky="ew", pady=(10, 6))
        box1.columnconfigure(0, weight=1)
        ttk.Label(
            box1,
            text=(
                "浏览器里打开校园网登录页 → F12 打开开发者工具 → Network(网络) → 手动登录一次 → "
                "在列表里找到登录那条请求（一般是 login / InterFace.do / wlanuserip 之类）→ "
                "右键 → Copy → Copy as cURL，然后粘贴到下面。"
            ),
            style="Hint.TLabel",
            wraplength=860,
            justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        self.curl_text = tk.Text(box1, height=5, font=FONT_MONO, wrap="none", undo=True)
        self.curl_text.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 6))
        bar1 = ttk.Frame(box1)
        bar1.grid(row=2, column=0, columnspan=2, sticky="ew")
        ttk.Button(bar1, text="解析并填入 ↓", command=self.on_parse_curl).pack(side="left")
        ttk.Button(bar1, text="清空", command=lambda: self.curl_text.delete("1.0", "end")).pack(side="left", padx=6)
        ttk.Label(
            bar1,
            text="也可以完全跳过这一步，直接在下面手动填写。",
            style="Hint.TLabel",
        ).pack(side="left", padx=10)

        # ---------- 步骤 2：请求内容
        box2 = ttk.LabelFrame(outer, text=" ② 请求内容 ", padding=10)
        box2.grid(row=2, column=0, sticky="ew", pady=6)
        box2.columnconfigure(1, weight=1)

        row = 0
        ttk.Label(box2, text="登录地址").grid(row=row, column=0, sticky="w", pady=2)
        self.url_var = tk.StringVar()
        ttk.Entry(box2, textvariable=self.url_var, font=FONT_MONO).grid(row=row, column=1, sticky="ew", padx=(8, 8))
        ttk.Label(box2, text="方法").grid(row=row, column=2, sticky="e")
        self.method_var = tk.StringVar(value="POST")
        ttk.Combobox(
            box2, textvariable=self.method_var, values=["POST", "GET"], width=7, state="readonly"
        ).grid(row=row, column=3, sticky="w", padx=(4, 0))

        row += 1
        ttk.Label(box2, text="登录前跳转").grid(row=row, column=0, sticky="w", pady=2)
        self.preflight_var = tk.StringVar()
        ttk.Entry(box2, textvariable=self.preflight_var, font=FONT_MONO).grid(
            row=row, column=1, columnspan=3, sticky="ew", padx=(8, 0)
        )
        ttk.Label(
            box2,
            text="选填：登录前先访问的地址，用来从跳转里取 userip / token 等参数",
            style="Hint.TLabel",
        ).grid(row=row + 1, column=1, columnspan=3, sticky="w", padx=(8, 0))

        row += 2
        ttk.Label(box2, text="请求体格式").grid(row=row, column=0, sticky="w", pady=(6, 2))
        self.ctype_var = tk.StringVar(value="form")
        ctype_frame = ttk.Frame(box2)
        ctype_frame.grid(row=row, column=1, columnspan=3, sticky="w", padx=(8, 0))
        for text, value in (("表单 form", "form"), ("JSON", "json"), ("原始文本", "raw")):
            ttk.Radiobutton(
                ctype_frame, text=text, value=value, variable=self.ctype_var, command=self._update_body_hint
            ).pack(side="left", padx=(0, 14))

        row += 1
        ttk.Label(box2, text="请求体").grid(row=row, column=0, sticky="nw", pady=(6, 2))
        self.body_text = tk.Text(box2, height=6, font=FONT_MONO, wrap="none", undo=True)
        self.body_text.grid(row=row, column=1, columnspan=2, sticky="ew", padx=(8, 8))
        body_scroll = ttk.Scrollbar(box2, orient="vertical", command=self.body_text.yview)
        body_scroll.grid(row=row, column=3, sticky="ns")
        self.body_text.configure(yscrollcommand=body_scroll.set)

        row += 1
        self.body_hint = ttk.Label(box2, text="", style="Hint.TLabel")
        self.body_hint.grid(row=row, column=1, columnspan=3, sticky="w", padx=(8, 0), pady=(0, 4))

        row += 1
        ttk.Label(box2, text="账号字段").grid(row=row, column=0, sticky="w", pady=2)
        self.user_field_var = tk.StringVar(value=NO_FIELD)
        self.user_field_box = ttk.Combobox(box2, textvariable=self.user_field_var, values=[NO_FIELD], width=22, state="readonly")
        self.user_field_box.grid(row=row, column=1, sticky="w", padx=(8, 8))
        self.user_field_box.bind("<<ComboboxSelected>>", lambda _e: self._apply_field_mapping())
        ttk.Label(box2, text="密码字段").grid(row=row, column=2, sticky="e")
        self.pass_field_var = tk.StringVar(value=NO_FIELD)
        self.pass_field_box = ttk.Combobox(box2, textvariable=self.pass_field_var, values=[NO_FIELD], width=18, state="readonly")
        self.pass_field_box.grid(row=row, column=3, sticky="w", padx=(4, 0))
        self.pass_field_box.bind("<<ComboboxSelected>>", lambda _e: self._apply_field_mapping())

        row += 1
        ttk.Label(box2, text="密码处理").grid(row=row, column=0, sticky="w", pady=(6, 2))
        self.pw_tpl_var = tk.StringVar(value="{password}")
        ttk.Combobox(
            box2,
            textvariable=self.pw_tpl_var,
            values=["{password}", "{md5}", "{md5_upper}", "{base64}", "{sha1}"],
            width=22,
        ).grid(row=row, column=1, sticky="w", padx=(8, 8))
        ttk.Label(
            box2,
            text="明文提交就用 {password}；如果抓到的密码是 32 位十六进制串，用 {md5}",
            style="Hint.TLabel",
        ).grid(row=row, column=2, columnspan=2, sticky="w")

        row += 1
        ttk.Label(box2, text="成功关键字").grid(row=row, column=0, sticky="w", pady=(6, 2))
        self.ok_kw_var = tk.StringVar()
        ttk.Entry(box2, textvariable=self.ok_kw_var, font=FONT_MONO).grid(
            row=row, column=1, sticky="ew", padx=(8, 8)
        )
        row += 1
        ttk.Label(box2, text="失败关键字").grid(row=row, column=0, sticky="w", pady=2)
        self.fail_kw_var = tk.StringVar()
        ttk.Entry(box2, textvariable=self.fail_kw_var, font=FONT_MONO).grid(
            row=row, column=1, sticky="ew", padx=(8, 8)
        )
        row += 1
        ttk.Label(box2, text="JSON 判定").grid(row=row, column=0, sticky="w", pady=2)
        json_frame = ttk.Frame(box2)
        json_frame.grid(row=row, column=1, columnspan=3, sticky="w", padx=(8, 0))
        self.json_path_var = tk.StringVar()
        self.json_eq_var = tk.StringVar()
        ttk.Entry(json_frame, textvariable=self.json_path_var, width=20, font=FONT_MONO).pack(side="left")
        ttk.Label(json_frame, text=" = ").pack(side="left")
        ttk.Entry(json_frame, textvariable=self.json_eq_var, width=18, font=FONT_MONO).pack(side="left")
        ttk.Label(
            json_frame,
            text="  选填，例：result = success（比关键字更准）",
            style="Hint.TLabel",
        ).pack(side="left")

        # ---------- 步骤 3：账号
        box3 = ttk.LabelFrame(outer, text=" ③ 账号密码 ", padding=10)
        box3.grid(row=3, column=0, sticky="ew", pady=6)
        box3.columnconfigure(1, weight=1)
        box3.columnconfigure(3, weight=1)
        ttk.Label(box3, text="账号").grid(row=0, column=0, sticky="w")
        self.user_var = tk.StringVar()
        ttk.Entry(box3, textvariable=self.user_var, font=FONT_MONO).grid(row=0, column=1, sticky="ew", padx=(8, 16))
        ttk.Label(box3, text="密码").grid(row=0, column=2, sticky="w")
        self.pass_var = tk.StringVar()
        ttk.Entry(box3, textvariable=self.pass_var, show="●", font=FONT_MONO).grid(
            row=0, column=3, sticky="ew", padx=(8, 0)
        )
        ttk.Label(
            box3,
            text="密码用 Windows DPAPI 加密保存在 credentials.dat，只有当前 Windows 账号能解开。",
            style="Hint.TLabel",
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))

        # ---------- 操作按钮
        actions = ttk.Frame(outer)
        actions.grid(row=4, column=0, sticky="ew", pady=(8, 4))
        self.test_btn = ttk.Button(actions, text="测试登录", command=self.on_test)
        self.test_btn.pack(side="left")
        ttk.Button(actions, text="保存设置", command=self.on_save).pack(side="left", padx=6)
        ttk.Button(actions, text="创建桌面快捷方式", command=self.on_shortcut).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="检查网络", command=self.on_check_network).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="自检", command=self.on_doctor).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="查看日志", command=self.on_open_log).pack(side="left")

        self.autostart_var = tk.BooleanVar(value=False)
        self.autostart_check = ttk.Checkbutton(
            actions, text="开机自动登录", variable=self.autostart_var, command=self.on_toggle_autostart
        )
        self.autostart_check.pack(side="right")

        # ---------- 日志
        box4 = ttk.LabelFrame(outer, text=" 运行信息 ", padding=8)
        box4.grid(row=5, column=0, sticky="nsew")
        outer.rowconfigure(5, weight=1)
        box4.columnconfigure(0, weight=1)
        box4.rowconfigure(0, weight=1)
        self.log_text = tk.Text(box4, height=9, font=FONT_MONO, wrap="word", state="disabled", bg="#f8fafc")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(box4, orient="vertical", command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)
        for tag, color in (("ok", "#047857"), ("err", "#b91c1c"), ("info", "#334155"), ("warn", "#b45309")):
            self.log_text.tag_configure(tag, foreground=color)

        self._update_body_hint()
        warning = getattr(self, "_pending_warning", "")
        if warning:
            self.log(f"提示：{warning}", "warn")
            self.log("下面这些是默认值，请先粘贴登录请求或手动填写。", "warn")
        else:
            self.log("已读取现有配置，可以直接测试登录。", "info")
        if self.old_creds.complete:
            self.status_label.configure(text=f"已保存账号：{self.old_creds.username}", style="Hint.TLabel")

    # ------------------------------------------------------------ 配置 <-> 界面

    def _load_into_ui(self, cfg: dict) -> None:
        login = cfg.get("login") or {}
        expect = cfg.get("expect") or {}
        self.url_var.set(login.get("url", ""))
        self.method_var.set((login.get("method") or "POST").upper())
        self.preflight_var.set((cfg.get("preflight") or {}).get("url", ""))
        self.ctype_var.set((login.get("content_type") or "form").lower())
        self.pw_tpl_var.set(login.get("password_template") or "{password}")
        self.ok_kw_var.set("|".join(expect.get("success_keywords") or []))
        self.fail_kw_var.set("|".join(expect.get("failure_keywords") or []))
        self.json_path_var.set(expect.get("json_path") or "")
        self.json_eq_var.set(expect.get("json_equals") or "")

        fields = login.get("fields") or {}
        body = login.get("body") or ""
        self.body_text.delete("1.0", "end")
        if fields:
            self.body_text.insert("1.0", "\n".join(f"{k}={v}" for k, v in fields.items()))
        elif body:
            self.body_text.insert("1.0", body)
        self.user_var.set(self.old_creds.username)
        self._refresh_field_choices()
        self._update_body_hint()

    def _update_body_hint(self) -> None:
        ctype = self.ctype_var.get()
        if ctype == "raw":
            hint = "原样发送（可含 {username} {password} 占位符），一般用于非表单格式"
        elif ctype == "json":
            hint = "每行一个 key=value，保存时自动转成 JSON"
        else:
            hint = "每行一个 key=value，例如：userId={username}&password={password}（保存时自动编码）"
        self.body_hint.configure(text=hint)

    def _body_lines(self) -> list[str]:
        return [line for line in self.body_text.get("1.0", "end").splitlines() if line.strip()]

    def _parse_fields(self) -> dict:
        fields: dict = {}
        for line in self._body_lines():
            if line.lstrip().startswith("#"):
                continue
            if "=" not in line:
                raise ValueError(f"这一行看不懂（缺少 =）：{line}")
            key, value = line.split("=", 1)
            key = key.strip()
            if key:
                fields[key] = value.strip()
        return fields

    def _refresh_field_choices(self) -> None:
        names = [NO_FIELD]
        try:
            names.extend(self._parse_fields().keys())
        except ValueError:
            pass
        self.user_field_box.configure(values=names)
        self.pass_field_box.configure(values=names)
        if self.user_field_var.get() not in names:
            self.user_field_var.set(NO_FIELD)
        if self.pass_field_var.get() not in names:
            self.pass_field_var.set(NO_FIELD)

    def _apply_field_mapping(self) -> None:
        """把选中的字段值替换成 {username} / {password} 占位符。"""
        if self.ctype_var.get() == "raw":
            return
        try:
            fields = self._parse_fields()
        except ValueError as exc:
            self.log(str(exc), "err")
            return
        user_key = self.user_field_var.get()
        pass_key = self.pass_field_var.get()
        changed = False
        if user_key != NO_FIELD:
            fields[user_key] = "{username}"
            changed = True
        if pass_key != NO_FIELD:
            fields[pass_key] = "{password}"
            changed = True
        if not changed:
            return
        self.body_text.delete("1.0", "end")
        self.body_text.insert("1.0", "\n".join(f"{k}={v}" for k, v in fields.items()))

    def _assert_no_plaintext(self, fields: dict, body_raw: str) -> None:
        """防止把明文账号密码直接写进配置文件（密码应该只在加密文件里）。"""
        password = self.pass_var.get()
        username = self.user_var.get().strip()
        blobs = [str(v) for v in fields.values()]
        if body_raw:
            blobs.append(body_raw)
        for blob in blobs:
            # 占位符本身（如 {password}）当然不能算明文
            plain = _PLACEHOLDER_RE.sub("", blob)
            if password and len(password) >= 3 and password in plain:
                raise ValueError(
                    "请求体里还留着明文密码。请在「密码字段」下拉框选中密码那一项，让它变成 {password}。"
                )
            if username and len(username) >= 3 and username in plain:
                raise ValueError(
                    "请求体里还留着明文账号。请在「账号字段」下拉框选中账号那一项，让它变成 {username}。"
                )

    def collect_config(self) -> dict:
        """把界面内容整理成配置 dict（不保存）。"""
        cfg = config_mod.normalize(self.cfg)
        ctype = self.ctype_var.get()
        url = self.url_var.get().strip()
        if not url:
            raise ValueError("请填写登录地址（就是刚才那条请求的 URL）。")
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("登录地址必须以 http:// 或 https:// 开头。")

        cfg["login"] = dict(cfg.get("login") or {})
        cfg["login"].update(
            {
                "url": url,
                "method": self.method_var.get().upper(),
                "content_type": ctype,
                "password_template": self.pw_tpl_var.get().strip() or "{password}",
            }
        )
        if ctype == "raw":
            body_raw = self.body_text.get("1.0", "end").strip()
            fields: dict = {}
            self._assert_no_plaintext(fields, body_raw)
            cfg["login"]["fields"] = {}
            cfg["login"]["body"] = body_raw
        else:
            fields = self._parse_fields()
            self._assert_no_plaintext(fields, "")
            cfg["login"]["fields"] = fields
            cfg["login"]["body"] = ""

        cfg["preflight"] = dict(cfg.get("preflight") or {})
        cfg["preflight"]["url"] = self.preflight_var.get().strip()

        cfg["expect"] = dict(cfg.get("expect") or {})
        cfg["expect"]["success_keywords"] = [x.strip() for x in self.ok_kw_var.get().split("|") if x.strip()]
        cfg["expect"]["failure_keywords"] = [x.strip() for x in self.fail_kw_var.get().split("|") if x.strip()]
        cfg["expect"]["json_path"] = self.json_path_var.get().strip()
        cfg["expect"]["json_equals"] = self.json_eq_var.get().strip()

        problems = config_mod.validate(cfg, strict=False)
        if problems:
            raise ValueError(problems[0])
        return cfg

    # ------------------------------------------------------------ 日志

    def log(self, message: str, tag: str = "info") -> None:
        self.log_queue.put((message, tag))

    def _poll_log(self) -> None:
        try:
            while True:
                message, tag = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", message.rstrip() + "\n", tag)
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        finally:
            self.root.after(120, self._poll_log)

    # ------------------------------------------------------------ 动作

    def on_parse_curl(self) -> None:
        text = self.curl_text.get("1.0", "end").strip()
        try:
            req = curlparse.parse_curl(text)
        except curlparse.CurlParseError as exc:
            self.log(f"解析失败：{exc}", "err")
            messagebox.showwarning(APP_TITLE, str(exc))
            return

        self.url_var.set(req.url)
        self.method_var.set(req.method)
        self.ctype_var.set(req.content_type)
        self._update_body_hint()

        fields: dict = req.field_values()

        self.body_text.delete("1.0", "end")
        if fields:
            self.body_text.insert("1.0", "\n".join(f"{k}={v}" for k, v in fields.items()))
        elif req.body:
            self.body_text.insert("1.0", req.body)

        # 猜账号 / 密码字段
        guess = req.guess_fields()
        self._refresh_field_choices()
        if guess.get("username"):
            self.user_field_var.set(guess["username"])
        if guess.get("password"):
            self.pass_field_var.set(guess["password"])

        # 密码看起来像 md5 就提示改密码模板
        pw_value = fields.get(guess.get("password", ""), "")
        if len(pw_value) == 32 and all(c in "0123456789abcdefABCDEF" for c in pw_value):
            self.pw_tpl_var.set("{md5}")
            self.log("抓到 32 位十六进制密码，已把「密码处理」设为 {md5}（若学校用 JS 加密则需另配）", "warn")
        else:
            self.pw_tpl_var.set("{password}")

        self._apply_field_mapping()

        if fields and (not guess.get("username") or not guess.get("password")):
            self.log(
                "没自动认出账号/密码字段：请在「账号字段 / 密码字段」下拉框里手动选一下，"
                "选完会自动替换成 {username} / {password}。",
                "warn",
            )

        self.log(f"✓ 已解析：{req.method} {req.host}", "ok")
        if req.body:
            self.log(f"  请求体字段：{', '.join(fields) if fields else '(无法拆成字段，已按原文保留)'}")
        if req.warnings:
            for warn in req.warnings:
                self.log(f"  注意：{warn}", "warn")
        if not self.preflight_var.get().strip():
            self.log("  如果登录失败且响应缺少 userip/token 之类参数，把「登录前跳转」填成认证页地址再试。", "info")
        self.log("  下一步：填好 ③ 里的账号密码，点「测试登录」。", "info")

    def on_check_network(self) -> None:
        def work():
            ip = net.local_ip()
            online, why = net.online()
            self.log(f"本机 IP：{ip}", "info")
            self.log(f"联网状态：{why}", "ok" if online else "warn")
            try:
                cfg = self.collect_config()
                target = cfg["login"]["url"]
                if net.portal_reachable(target):
                    self.log(f"认证服务器可达：{target}", "ok")
                else:
                    self.log(f"认证服务器不可达：{target}（可能不在校园网内 / 未连 Wi-Fi）", "err")
            except ValueError:
                pass

        threading.Thread(target=work, daemon=True).start()

    def on_doctor(self) -> None:
        """跑一遍自检，把每条结果打到「运行信息」里（与 login.py doctor 同一套逻辑）。"""
        if getattr(self, "_doctor_running", False):
            return
        self._doctor_running = True
        self.log("——————————————————")
        self.log("开始自检：加密存储 / 配置 / 请求模板 / 网络…", "info")

        def work():
            try:
                checks = doctor_mod.run(probe=True)
            except Exception as exc:  # noqa: BLE001
                self.log(f"✗ 自检出错：{type(exc).__name__}: {exc}", "err")
                return
            finally:
                self._doctor_running = False
            bad = doctor_mod.problems(checks)
            for item in checks:
                tag = {"ok": "ok", "bad": "err", "info": "info"}.get(item.status, "info")
                self.log(item.line(), tag)
            if bad:
                self.log(f"自检发现 {len(bad)} 个问题（见上），对应处理办法：", "warn")
                for item in bad:
                    self.log(f"  · {item.title}：{doctor_mod.HINT.get(item.title, '见 README 的「故障排查」')}", "warn")
            else:
                self.log("✓ 自检通过：这台机器上可以直接登录。", "ok")

        threading.Thread(target=work, daemon=True).start()

    def _gather_credentials(self) -> tuple[str, str] | None:
        username = self.user_var.get().strip()
        password = self.pass_var.get()
        if not username:
            messagebox.showwarning(APP_TITLE, "请填写账号。")
            return None
        if not password:
            if self.old_creds.complete and self.old_creds.username == username:
                password = self.old_creds.password
                self.log("密码留空，沿用已保存的密码。", "info")
            else:
                messagebox.showwarning(APP_TITLE, "请填写密码。")
                return None
        return username, password

    def on_test(self) -> None:
        if self.testing:
            return
        try:
            cfg = self.collect_config()
        except ValueError as exc:
            messagebox.showwarning(APP_TITLE, str(exc))
            self.log(f"配置不完整：{exc}", "err")
            return
        creds = self._gather_credentials()
        if not creds:
            return
        username, password = creds

        self.testing = True
        self.test_btn.configure(state="disabled", text="测试中…")
        self.status_label.configure(text="正在登录…", style="Run.TLabel")
        self.log("——————————————————")
        self.log(f"开始测试登录：{cfg['login']['method']} {cfg['login']['url']}", "info")

        def work():
            started = time.time()
            try:
                result = portal.login(cfg, username, password)
                if result.ok:
                    self.log(f"✓ 登录成功：{result.message}", "ok")
                    self.log(f"  耗时 {time.time() - started:.1f}s，尝试 {result.attempts} 次", "info")
                    self.root.after(0, lambda: self.status_label.configure(text="✓ 登录成功", style="Ok.TLabel"))
                else:
                    self.log(f"✗ 登录失败：{result.message}", "err")
                    if result.body:
                        snippet = portal.extract_message(result.body) or result.body[:300]
                        self.log(f"  服务器返回：{snippet}", "warn")
                    self.log("  排查建议：确认账号密码；确认成功/失败关键字；必要时补「登录前跳转」地址。", "info")
                    self.root.after(0, lambda: self.status_label.configure(text="✗ 登录失败", style="Err.TLabel"))
            except Exception as exc:  # noqa: BLE001
                self.log(f"✗ 出错：{type(exc).__name__}: {exc}", "err")
                self.root.after(0, lambda: self.status_label.configure(text="✗ 出错", style="Err.TLabel"))
            finally:
                def done():
                    self.testing = False
                    self.test_btn.configure(state="normal", text="测试登录")
                self.root.after(0, done)

        threading.Thread(target=work, daemon=True).start()

    def on_save(self) -> None:
        try:
            cfg = self.collect_config()
        except ValueError as exc:
            messagebox.showwarning(APP_TITLE, str(exc))
            return
        creds = self._gather_credentials()
        if not creds:
            return
        username, password = creds
        try:
            path = config_mod.save(cfg)
            secrets.save(username, password)
        except (OSError, secrets.SecretError) as exc:
            messagebox.showerror(APP_TITLE, f"保存失败：{exc}")
            self.log(f"✗ 保存失败：{exc}", "err")
            return
        self.cfg = cfg
        self.old_creds = secrets.Credentials(username=username, password=password)
        self.pass_var.set("")
        self.status_label.configure(text=f"✓ 已保存，账号 {username}", style="Ok.TLabel")
        self.log(f"✓ 配置已保存：{paths.rel(path)}", "ok")
        self.log(f"✓ 账号密码已加密保存：{paths.rel(paths.CREDENTIAL_FILE)}", "ok")
        self.log("现在可以双击桌面上的「校园网一键登录」了。", "info")

    def on_shortcut(self) -> None:
        try:
            lnk = autostart.create_desktop_shortcut()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_TITLE, f"创建快捷方式失败：{exc}")
            self.log(f"✗ 创建快捷方式失败：{exc}", "err")
            return
        self.log(f"✓ 已在桌面创建快捷方式：{lnk}", "ok")

    def _refresh_autostart_state(self) -> None:
        enabled = autostart.is_enabled()
        self.autostart_var.set(enabled)
        self.log(f"开机自动登录：{'已开启' if enabled else '未开启'}", "info")

    def on_toggle_autostart(self) -> None:
        want = self.autostart_var.get()
        try:
            if want:
                where = autostart.enable()
                self.log("✓ 已开启开机自动登录（登录 Windows 后自动尝试，最多等 90 秒网络）", "ok")
                self.log(f"  {where}")
            else:
                removed = autostart.disable()
                self.log("✓ 已关闭开机自动登录", "ok")
                for item in removed:
                    self.log(f"  已清理：{item}")
        except OSError as exc:
            self.autostart_var.set(not want)
            messagebox.showerror(APP_TITLE, f"修改开机自启失败：{exc}")
            self.log(f"✗ 修改开机自启失败：{exc}", "err")

    def on_open_log(self) -> None:
        paths.ensure_log_dir()
        if not os.path.exists(paths.LOG_FILE):
            self.log("还没有日志文件（登录一次后才会生成）。", "warn")
            return
        try:
            os.startfile(paths.LOG_FILE)  # type: ignore[attr-defined]
        except OSError:
            subprocess.Popen(["notepad.exe", paths.LOG_FILE])

    def _on_close(self) -> None:
        self.root.destroy()


def main() -> int:
    _enable_dpi()
    root = tk.Tk()
    SetupApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
