"""设置向导（tkinter 图形界面）。

目标：让「不知道学校用什么认证系统」的人也能配好 ——
把浏览器开发者工具里那条登录请求「Copy as cURL」粘进来，点两下就行。
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
import traceback

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
from core import __version__ as APP_VERSION  # noqa: E402

APP_TITLE = "校园网一键登录 · 设置向导"
FONT_FAMILY = "Microsoft YaHei UI"
FONT = (FONT_FAMILY, 9)
FONT_SMALL = (FONT_FAMILY, 8)
FONT_BOLD = (FONT_FAMILY, 9, "bold")
FONT_SECTION = (FONT_FAMILY, 10, "bold")
FONT_TITLE = (FONT_FAMILY, 16, "bold")
FONT_SUB = (FONT_FAMILY, 9)
FONT_MONO = ("Consolas", 9)
FONT_PILL = (FONT_FAMILY, 8, "bold")

# 颜色写死，不跟系统主题走：vista / 经典 / 高对比度主题下，界面都长同一个样子。
# 底色偏蓝一点点、主色亮一点点 —— 看上去没那么「办公软件」，但对比度仍然够。
COLOR = {
    "bg": "#f0f6ff",
    "card": "#ffffff",
    "border": "#dbe6f5",
    "text": "#1f2933",
    "muted": "#6b7280",
    "accent": "#2f86f6",
    "accent_dark": "#1d6fdc",
    "accent_soft": "#e6f0ff",
    "ok": "#12a150",
    "warn": "#c2760a",
    "err": "#dc3f3f",
    "header": "#122a4d",
    "header_muted": "#9fb8d8",
    "bar": "#e8f1fd",
    # 大肥鱼的配色
    "fish": "#5aa9f7",
    "fish_dark": "#2f7fd6",
    "fish_light": "#cfe8ff",
    "fish_belly": "#eaf5ff",
    "blush": "#ffb3c1",
}
STATUS_BG = {
    "idle": "#475569",
    "run": COLOR["accent"],
    "ok": COLOR["ok"],
    "warn": COLOR["warn"],
    "err": COLOR["err"],
}
# 界面状态 → 鱼的脸色
MOOD_BY_STATUS = {"idle": "idle", "run": "busy", "ok": "happy", "warn": "warn", "err": "sad"}
SHORTCUT_HINT = "快捷键：Ctrl+Enter 测试登录 · Ctrl+S 保存 · F5 检查网络"

NO_FIELD = "（不使用）"
_PLACEHOLDER_RE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")


def _enable_dpi() -> None:
    """让 Windows 别把界面按 96dpi 画完再拉伸（否则字号会糊）。"""
    try:
        import ctypes

        # 2 = 每显示器 DPI 感知（多屏、缩放 150% 时不糊），老系统不支持就退回 1
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


class FishMascot(tk.Canvas):
    """大肥鱼 —— 一条用画布现画的胖鱼，不加载任何图片文件。

    项目是零依赖的，鱼也得零依赖：所有线条都是 create_oval / create_polygon 拼出来的。
    它顺便还是状态灯：
      干活中（busy）  → 张嘴吐泡泡，尾巴摆得快
      成功（happy）  → 眯眼笑，旁边冒两颗小星星
      失败（sad）    → 眼睛打叉，头上挂一滴汗
      提示（warn）   → 抿嘴
    """

    DESIGN = 64.0
    MOODS = ("idle", "busy", "happy", "sad", "warn")

    def __init__(
        self,
        master,
        size: int = 64,
        bg: str = COLOR["header"],
        animate: bool = True,
        halo: str = "",
    ) -> None:
        super().__init__(master, width=size, height=size, bg=bg, highlightthickness=0, bd=0)
        self._scale = size / self.DESIGN
        self._mood = "idle"
        self._phase = 0.0
        self._blink_left = 0
        # 头几秒先不眨眼：截图、演示的时候正好闭着眼会很奇怪
        self._blink_at = random.randint(60, 110)
        self._running = bool(animate)
        self._job = None
        self._halo = halo
        self._draw()
        self._apply_mood()
        if self._running:
            self._schedule()

    # ---------------------------------------------------------------- 画元素

    def _p(self, x: float, y: float):
        return (x * self._scale, y * self._scale)

    def _box(self, x1: float, y1: float, x2: float, y2: float):
        return (x1 * self._scale, y1 * self._scale, x2 * self._scale, y2 * self._scale)

    def _line(self, points, width: float = 2.0, smooth: bool = True, **kw):
        flat = []
        for x, y in points:
            flat.extend(self._p(x, y))
        return self.create_line(
            *flat, width=max(1.0, width * self._scale), capstyle="round", smooth=smooth, **kw
        )

    def _draw(self) -> None:
        # 太小的尺寸（页脚那条）只画轮廓 + 眼睛，否则细节糊成一团
        simple = self._scale < 0.45
        edge = max(1.0, 1.2 * self._scale)

        if self._halo:
            self.create_oval(*self._box(2, 2, 62, 62), fill=self._halo, outline="")

        # 泡泡先画，压在鱼身后面
        self._bubbles = [
            self.create_oval(0, 0, 0, 0, outline="", fill="#8fc2ff", state="hidden") for _ in range(3)
        ]
        # 尾巴：摆动时只动两个尖角
        self._tail = self.create_polygon(
            *self._p(14, 34), *self._p(6, 26), *self._p(6, 42),
            fill=COLOR["fish_dark"], outline="",
        )
        # 背鳍
        self.create_polygon(
            *self._p(26, 21), *self._p(32, 12), *self._p(39, 22),
            fill=COLOR["fish_dark"], outline="",
        )
        # 身子 + 肚皮 + 背上一点高光
        self.create_oval(
            *self._box(11, 18, 51, 50), fill=COLOR["fish"], outline=COLOR["fish_dark"], width=edge
        )
        if not simple:
            self.create_oval(*self._box(17, 33, 47, 49), fill=COLOR["fish_belly"], outline="")
            self._fin = self.create_oval(
                *self._box(21, 32, 32, 42), fill=COLOR["fish_light"], outline=""
            )
            self.create_oval(*self._box(26, 35, 33, 40), fill=COLOR["blush"], outline="")
            self._highlight = self._line([(25, 26), (33, 23)], width=2.4, fill="#9fd0ff")
        else:
            self._fin = None
            self._highlight = None

        # 眼睛三套：普通 / 眯眼笑 / 打叉
        self._eye_white = self.create_oval(*self._box(35, 24, 47, 36), fill="#ffffff", outline="")
        self._pupil = self.create_oval(*self._box(40, 27, 46, 33), fill="#16324f", outline="")
        self._shine = self.create_oval(*self._box(41.4, 28.2, 43.8, 30.6), fill="#ffffff", outline="")
        self._eye_closed = self._line([(37, 30), (41, 26), (46, 30)], width=2.2, fill="#16324f", state="hidden")
        self._eye_x1 = self._line([(38, 26), (45, 34)], width=2.2, fill="#16324f", state="hidden")
        self._eye_x2 = self._line([(45, 26), (38, 34)], width=2.2, fill="#16324f", state="hidden")

        # 嘴四套：微笑 / 张嘴 / 抿嘴 / 撇嘴
        self._mouth_smile = self._line([(41, 38), (45, 41), (49, 38)], width=1.8, fill="#16324f")
        self._mouth_open = self.create_oval(*self._box(43, 38, 49, 42), fill="#16324f", outline="", state="hidden")
        self._mouth_flat = self._line([(42, 39), (49, 39)], width=2.0, fill="#16324f", state="hidden")
        self._mouth_frown = self._line([(42, 42), (46, 39), (50, 42)], width=1.8, fill="#16324f", state="hidden")

        # 失败时那滴汗
        self._sweat = self.create_oval(*self._box(50, 15, 55, 23), fill="#bfe3ff", outline="", state="hidden")
        # 成功时的两颗小星星
        self._stars = [
            self.create_text(
                *self._p(53, 16),
                text="✦",
                fill="#ffd76a",
                font=(FONT_FAMILY, max(6, int(10 * self._scale))),
                state="hidden",
            ),
            self.create_text(
                *self._p(57, 26),
                text="✦",
                fill="#ffd76a",
                font=(FONT_FAMILY, max(5, int(7 * self._scale))),
                state="hidden",
            ),
        ]

    # ---------------------------------------------------------------- 表情

    def set_mood(self, mood: str) -> None:
        self._mood = mood if mood in self.MOODS else "idle"
        self._apply_mood()

    @property
    def mood(self) -> str:
        return self._mood

    def _apply_mood(self) -> None:
        happy = self._mood == "happy"
        sad = self._mood == "sad"
        busy = self._mood == "busy"
        flat = self._mood == "warn"

        normal_eye = not (happy or sad)
        for item in (self._eye_white, self._pupil, self._shine):
            self.itemconfigure(item, state="normal" if normal_eye else "hidden")
        self.itemconfigure(self._eye_closed, state="normal" if happy else "hidden")
        for item in (self._eye_x1, self._eye_x2):
            self.itemconfigure(item, state="normal" if sad else "hidden")

        self.itemconfigure(self._mouth_open, state="normal" if busy else "hidden")
        self.itemconfigure(self._mouth_flat, state="normal" if flat else "hidden")
        self.itemconfigure(self._mouth_frown, state="normal" if sad else "hidden")
        self.itemconfigure(
            self._mouth_smile, state="normal" if (not busy and not flat and not sad) else "hidden"
        )
        if happy:
            # 笑的时候嘴再张一点点
            self.coords(self._mouth_smile, *self._p(40, 37), *self._p(45, 42), *self._p(50, 37))

        self.itemconfigure(self._sweat, state="normal" if sad else "hidden")
        for item in self._stars:
            self.itemconfigure(item, state="normal" if happy else "hidden")

    # ---------------------------------------------------------------- 动

    def _schedule(self) -> None:
        if not self._running:
            return
        self._job = self.after(90, self._tick)

    def _tick(self) -> None:
        self._job = None
        try:
            self._step()
        except tk.TclError:  # 窗口已经销毁
            return
        self._schedule()

    def _step(self) -> None:
        """推进一帧：摆尾、抖鳍、吐泡、偶尔眨眼。"""
        self._phase += 1.0
        speed = 0.62 if self._mood == "busy" else 0.34

        wag = math.sin(self._phase * speed) * 4.0
        self.coords(self._tail, *self._p(14, 34), *self._p(6, 26 + wag), *self._p(6, 42 + wag))

        if self._fin is not None:
            fin = math.sin(self._phase * speed * 1.7) * 1.1
            self.coords(self._fin, *self._box(21, 32 + fin, 32, 42 + fin))

        # 眨眼：普通表情下每 2~5 秒眨一次，闭眼 3 帧
        if self._blink_left > 0:
            self._blink_left -= 1
            if self._blink_left == 0:
                self._apply_mood()
        elif self._phase >= self._blink_at and self._mood in ("idle", "busy"):
            self._blink_left = 3
            self._blink_at = self._phase + random.randint(18, 46)
            for item in (self._eye_white, self._pupil, self._shine):
                self.itemconfigure(item, state="hidden")
            self.itemconfigure(self._eye_closed, state="normal")

        # 泡泡：从嘴边冒出来，斜着往上飘
        if self._mood == "sad":
            for item in self._bubbles:
                self.itemconfigure(item, state="hidden")
            return
        rate = 0.022 if self._mood == "busy" else 0.007
        for k, item in enumerate(self._bubbles):
            t = (self._phase * rate + k / float(len(self._bubbles))) % 1.0
            if t > 0.94:
                self.itemconfigure(item, state="hidden")
                continue
            x = 50 + t * 11.0
            y = 31 - t * 25.0
            r = 2.2 - t * 1.3
            self.coords(item, *self._box(x - r, y - r, x + r, y + r))
            self.itemconfigure(item, state="normal")

    def destroy(self) -> None:  # pragma: no cover - 只在关窗口时走到
        self._running = False
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None
        super().destroy()


class SetupApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.testing = False
        self.cfg = self._load_config()
        self.old_creds = self._load_creds()
        paths.write_python_hint()

        root.title(APP_TITLE)
        # 按屏幕大小自适应，1366x768 的小本也能完整显示
        screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
        win_w = min(940, max(800, screen_w - 240))
        win_h = min(940, max(620, screen_h - 120))
        root.geometry(f"{win_w}x{win_h}+{max(0, (screen_w - win_w) // 2)}+{max(0, (screen_h - win_h) // 3)}")
        root.minsize(800, 600)
        self._build_style()
        self._build_ui()
        self._load_into_ui(self.cfg)
        self._refresh_autostart_state()
        self._poll_log()
        self.log("大肥鱼在这儿。把浏览器里那条登录请求粘到第①步，剩下的我来。", "info")

        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------ 初始化数据

    def _load_config(self) -> dict:
        self._pending_warning = ""
        try:
            return config_mod.load()
        except config_mod.ConfigError as exc:
            self._pending_warning = str(exc)
            return config_mod.default_config()

    def _load_creds(self):
        try:
            return secrets.load()
        except secrets.SecretError:
            return secrets.Credentials()

    # ------------------------------------------------------------ 界面搭建

    def _build_style(self) -> None:
        """换成 clam 主题再自己配色。

        系统主题（vista / xpnative）在不同 Windows 版本上长得不一样，颜色也大多不让改；
        clam 朴素但完全可配，至少每个人打开看到的是同一张脸。
        """
        self.root.configure(bg=COLOR["bg"])
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:  # pragma: no cover
            pass

        style.configure(".", font=FONT, background=COLOR["bg"], foreground=COLOR["text"])
        style.configure("TFrame", background=COLOR["bg"])
        style.configure("Card.TFrame", background=COLOR["card"])
        style.configure("TLabel", background=COLOR["bg"], foreground=COLOR["text"])
        style.configure("Card.TLabel", background=COLOR["card"], foreground=COLOR["text"])
        style.configure("Hint.TLabel", background=COLOR["bg"], foreground=COLOR["muted"], font=FONT_SMALL)
        style.configure("CardHint.TLabel", background=COLOR["card"], foreground=COLOR["muted"], font=FONT_SMALL)

        style.configure(
            "Card.TLabelframe",
            background=COLOR["card"],
            bordercolor=COLOR["border"],
            lightcolor=COLOR["border"],
            darkcolor=COLOR["border"],
            relief="solid",
            borderwidth=1,
        )
        style.configure(
            "Card.TLabelframe.Label",
            background=COLOR["card"],
            foreground=COLOR["text"],
            font=FONT_SECTION,
        )

        style.configure(
            "TEntry",
            fieldbackground=COLOR["card"],
            bordercolor=COLOR["border"],
            lightcolor=COLOR["border"],
            darkcolor=COLOR["border"],
            insertcolor=COLOR["text"],
            padding=(6, 3),
        )
        style.map(
            "TEntry",
            bordercolor=[("focus", COLOR["accent"])],
            lightcolor=[("focus", COLOR["accent"])],
            darkcolor=[("focus", COLOR["accent"])],
        )
        style.configure(
            "TCombobox",
            fieldbackground=COLOR["card"],
            background=COLOR["card"],
            bordercolor=COLOR["border"],
            lightcolor=COLOR["border"],
            darkcolor=COLOR["border"],
            arrowcolor=COLOR["text"],
            padding=(4, 2),
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", COLOR["card"])],
            bordercolor=[("focus", COLOR["accent"])],
            lightcolor=[("focus", COLOR["accent"])],
            darkcolor=[("focus", COLOR["accent"])],
        )

        # 按钮分三级：普通（浅灰）、Primary（蓝，主操作）、Save（绿，收尾动作）、Mini（卡片里的小按钮）
        style.configure(
            "TButton",
            background="#e8ecf2",
            foreground=COLOR["text"],
            bordercolor=COLOR["border"],
            lightcolor=COLOR["border"],
            darkcolor=COLOR["border"],
            padding=(12, 5),
            relief="flat",
        )
        style.map(
            "TButton",
            background=[("disabled", "#eef1f5"), ("pressed", "#d3dae5"), ("active", "#dde3ec")],
            foreground=[("disabled", "#9aa3af")],
        )
        style.configure(
            "Primary.TButton",
            background=COLOR["accent"],
            foreground="#ffffff",
            bordercolor=COLOR["accent_dark"],
            lightcolor=COLOR["accent"],
            darkcolor=COLOR["accent_dark"],
            font=FONT_BOLD,
        )
        style.map(
            "Primary.TButton",
            background=[("disabled", "#9db9f0"), ("pressed", COLOR["accent_dark"]), ("active", COLOR["accent_dark"])],
            foreground=[("disabled", "#f2f6ff")],
        )
        style.configure(
            "Save.TButton",
            background=COLOR["ok"],
            foreground="#ffffff",
            bordercolor="#0b6240",
            lightcolor=COLOR["ok"],
            darkcolor="#0b6240",
            font=FONT_BOLD,
        )
        style.map(
            "Save.TButton",
            background=[("disabled", "#8fb8a6"), ("pressed", "#0b6240"), ("active", "#0b6240")],
            foreground=[("disabled", "#f2f7f4")],
        )
        style.configure(
            "Mini.TButton",
            background=COLOR["card"],
            foreground=COLOR["accent_dark"],
            bordercolor=COLOR["border"],
            lightcolor=COLOR["border"],
            darkcolor=COLOR["border"],
            padding=(8, 2),
            font=FONT_SMALL,
        )
        style.map("Mini.TButton", background=[("active", "#eef2ff"), ("pressed", "#e0e7ff")])

        style.configure("Card.TCheckbutton", background=COLOR["card"], foreground=COLOR["text"])
        style.map("Card.TCheckbutton", background=[("active", COLOR["card"])])
        style.configure("TCheckbutton", background=COLOR["bg"], foreground=COLOR["text"])
        style.map("TCheckbutton", background=[("active", COLOR["bg"])])
        style.configure("Card.TRadiobutton", background=COLOR["card"], foreground=COLOR["text"])
        style.map("Card.TRadiobutton", background=[("active", COLOR["card"])])

        style.configure(
            "Header.Horizontal.TProgressbar",
            background=COLOR["accent"],
            troughcolor=COLOR["header"],
            bordercolor=COLOR["header"],
            lightcolor=COLOR["accent"],
            darkcolor=COLOR["accent"],
            thickness=6,
        )
        for orient in ("Vertical", "Horizontal"):
            style.configure(
                f"{orient}.TScrollbar",
                background="#cbd5e1",
                troughcolor=COLOR["card"],
                bordercolor=COLOR["card"],
                arrowcolor="#64748b",
            )

    # ------------------------------------------------------------ 界面搭建

    def _build_ui(self) -> None:
        self._progress_on = False
        self._build_header()

        outer = ttk.Frame(self.root, padding=(16, 12, 16, 10))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)  # 三步表单：可滚动，窗口变高时优先给它
        outer.rowconfigure(2, weight=0)  # 运行信息：固定高度，别抢表单的地方

        content = self._build_scroll_area(outer, row=0)
        self._build_step1(content, row=0)
        self._build_step3(content, row=1)  # 账号密码：填完 cURL 就要用，放在前面
        self._build_step2(content, row=2)  # 请求细节：多数人不用动，放最后
        self._build_actions(outer, row=1)
        self._build_log(outer, row=2)
        self._build_statusbar()
        self._bind_shortcuts()

        warning = getattr(self, "_pending_warning", "")
        if warning:
            self.log(f"提示：{warning}", "warn")
            self.log("下面这些是默认值，请先粘贴登录请求或手动填写。", "warn")
            self._set_status("需要配置", "warn")
        else:
            self.log("已读取现有配置，可以直接测试登录。", "info")
        if self.old_creds.complete:
            self._set_status(f"账号 {secrets.mask_username(self.old_creds.username)}", "idle")

    def _build_scroll_area(self, parent, row: int) -> ttk.Frame:
        """把三步表单放进可滚动的画布。

        1366x768 的本子上，按原来的排法进度条和日志区会被挤没；
        滚动之后窗口小不小都不影响，滚一下就能看到全部内容。
        """
        holder = ttk.Frame(parent)
        holder.grid(row=row, column=0, sticky="nsew", pady=(0, 4))
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)

        self._canvas = tk.Canvas(holder, bg=COLOR["bg"], highlightthickness=0, borderwidth=0)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(holder, orient="vertical", command=self._canvas.yview)
        bar.grid(row=0, column=1, sticky="ns")

        content = ttk.Frame(self._canvas)
        content.columnconfigure(0, weight=1)
        window = self._canvas.create_window((0, 0), window=content, anchor="nw")

        # 「下面还有内容」的角标：用 place 浮在上面，不占布局，也就不会自己把自己顶出来
        self._scroll_hint = tk.Label(
            holder,
            text=" 下面还有内容 ",
            font=FONT_SMALL,
            bg=COLOR["card"],
            fg=COLOR["muted"],
            highlightbackground=COLOR["border"],
            highlightthickness=1,
            padx=6,
            pady=1,
        )

        def on_scroll(first, last) -> None:
            bar.set(first, last)
            self._update_scroll_hint(float(first), float(last), content)

        self._canvas.configure(yscrollcommand=on_scroll)

        def sync(_event=None) -> None:
            self._canvas.itemconfigure(window, width=self._canvas.winfo_width())
            self._canvas.configure(scrollregion=self._canvas.bbox("all"))
            tall = content.winfo_reqheight() > self._canvas.winfo_height() + 2
            if tall:
                bar.grid()
            else:
                bar.grid_remove()
            self._update_scroll_hint(*self._canvas.yview(), content)

        content.bind("<Configure>", sync)
        self._canvas.bind("<Configure>", sync)
        # 只有指针在表单上时才接管滚轮，鼠标停在日志框里滚的还是日志
        self._canvas.bind("<Enter>", lambda _e: self._canvas.bind_all("<MouseWheel>", self._on_wheel))
        self._canvas.bind("<Leave>", lambda _e: self._canvas.unbind_all("<MouseWheel>"))
        return content

    def _update_scroll_hint(self, first: float, last: float, content) -> None:
        hint = getattr(self, "_scroll_hint", None)
        if hint is None:
            return
        more = content.winfo_reqheight() > self._canvas.winfo_height() + 2 and last < 0.999
        if more:
            hint.place(relx=1.0, rely=1.0, x=-22, y=-10, anchor="se")
        else:
            hint.place_forget()

    def _on_wheel(self, event) -> None:
        delta = -1 if getattr(event, "delta", 0) > 0 else 1
        self._canvas.yview_scroll(delta * 2, "units")

    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg=COLOR["header"])
        header.pack(side="top", fill="x")
        inner = tk.Frame(header, bg=COLOR["header"])
        inner.pack(fill="x", padx=16, pady=(10, 8))

        # 左边这条胖鱼是这个界面的吉祥物，也是状态指示灯
        self.mascot = FishMascot(inner, size=70, bg=COLOR["header"], halo="#1b3c68")
        self.mascot.pack(side="left", padx=(0, 12))

        text_box = tk.Frame(inner, bg=COLOR["header"])
        text_box.pack(side="left", pady=(4, 0))
        tk.Label(
            text_box, text="校园网一键登录", font=FONT_TITLE, bg=COLOR["header"], fg="#ffffff"
        ).pack(anchor="w")
        tk.Label(
            text_box,
            text="大肥鱼带你进校园网 · 配置一次，以后双击就登录",
            font=FONT_SUB,
            bg=COLOR["header"],
            fg=COLOR["header_muted"],
        ).pack(anchor="w", pady=(1, 0))

        self.status_label = tk.Label(
            inner, text="准备就绪", font=FONT_PILL, bg=STATUS_BG["idle"], fg="#ffffff", padx=10, pady=3
        )
        self.status_label.pack(side="right")
        self.progress = ttk.Progressbar(inner, mode="indeterminate", length=88, style="Header.Horizontal.TProgressbar")

    def _card(self, parent, title: str, row: int, pady=(0, 8)) -> ttk.LabelFrame:
        card = ttk.LabelFrame(parent, text=title, style="Card.TLabelframe", padding=(12, 8, 12, 12))
        card.grid(row=row, column=0, sticky="ew", pady=pady)
        return card

    def _text_area(self, parent, height: int, wrap: str = "none") -> tuple[tk.Frame, tk.Text]:
        """带 1px 边框和双向滚动条的文本区。

        直接给 tk.Text 套一层底框当边框：highlightthickness 在聚焦时会叠色，
        而且横向滚动条以前根本没有，长 URL 只能靠键盘挪。
        """
        shell = tk.Frame(parent, bg=COLOR["border"])
        text = tk.Text(
            shell,
            height=height,
            font=FONT_MONO,
            wrap=wrap,
            undo=True,
            relief="flat",
            borderwidth=0,
            bg=COLOR["card"],
            fg=COLOR["text"],
            insertbackground=COLOR["text"],
            padx=8,
            pady=6,
            selectbackground="#c7ddff",
            selectforeground=COLOR["text"],
        )
        text.grid(row=0, column=0, sticky="nsew", padx=(1, 0), pady=(1, 0))
        vbar = ttk.Scrollbar(shell, orient="vertical", command=text.yview)
        vbar.grid(row=0, column=1, sticky="ns", padx=(0, 1), pady=(1, 0))
        hbar = ttk.Scrollbar(shell, orient="horizontal", command=text.xview)
        hbar.grid(row=1, column=0, sticky="ew", padx=(1, 0), pady=(0, 1))
        text.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
        shell.rowconfigure(0, weight=1)
        shell.columnconfigure(0, weight=1)
        return shell, text

    def _build_step1(self, parent, row: int) -> None:
        card = self._card(parent, "① 粘贴登录请求（最快）", row)
        card.columnconfigure(0, weight=1)
        ttk.Label(
            card,
            text=(
                "浏览器打开校园网登录页 → F12 → Network(网络) → 手动登录一次 → "
                "在列表里找到登录那条请求（一般叫 login / InterFace.do / wlanuserip 之类）→ "
                "右键 → Copy → Copy as cURL，然后粘到下面。"
            ),
            style="CardHint.TLabel",
            wraplength=820,
            justify="left",
        ).grid(row=0, column=0, sticky="w")

        shell, self.curl_text = self._text_area(card, height=4)
        shell.grid(row=1, column=0, sticky="ew", pady=(8, 8))

        bar = ttk.Frame(card, style="Card.TFrame")
        bar.grid(row=2, column=0, sticky="ew")
        ttk.Button(bar, text="粘贴并解析", style="Primary.TButton", command=self.on_paste_clipboard).pack(side="left")
        ttk.Button(bar, text="重新解析", command=self.on_parse_curl).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="清空", command=lambda: self.curl_text.delete("1.0", "end")).pack(side="left", padx=(8, 0))
        ttk.Label(
            bar, text="也可以完全跳过这一步，直接在下面手填。", style="CardHint.TLabel"
        ).pack(side="left", padx=12)

    def _build_step2(self, parent, row: int) -> None:
        card = self._card(parent, "③ 请求内容（一般不用改，粘贴 cURL 时自动填好）", row)
        card.columnconfigure(1, weight=1)
        card.columnconfigure(3, weight=1)

        row = 0
        ttk.Label(card, text="登录地址", style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=2)
        self.url_var = tk.StringVar()
        ttk.Entry(card, textvariable=self.url_var, font=FONT_MONO).grid(row=row, column=1, columnspan=2, sticky="ew", padx=(8, 8))
        method_frame = ttk.Frame(card, style="Card.TFrame")
        method_frame.grid(row=row, column=3, sticky="e")
        ttk.Label(method_frame, text="方法", style="Card.TLabel").pack(side="left", padx=(0, 4))
        self.method_var = tk.StringVar(value="POST")
        ttk.Combobox(
            method_frame, textvariable=self.method_var, values=["POST", "GET"], width=7, state="readonly"
        ).pack(side="left")

        row += 1
        ttk.Label(card, text="登录前跳转", style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=(4, 2))
        self.preflight_var = tk.StringVar()
        ttk.Entry(card, textvariable=self.preflight_var, font=FONT_MONO).grid(
            row=row, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=(4, 0)
        )
        ttk.Label(
            card,
            text="选填：登录前先访问的地址，用来从跳转里取 userip / token 等参数",
            style="CardHint.TLabel",
        ).grid(row=row + 1, column=1, columnspan=3, sticky="w", padx=(8, 0))

        row += 2
        ttk.Label(card, text="请求体格式", style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=(6, 2))
        ctype_frame = ttk.Frame(card, style="Card.TFrame")
        ctype_frame.grid(row=row, column=1, columnspan=3, sticky="w", padx=(8, 0))
        self.ctype_var = tk.StringVar(value="form")
        for text, value in (("表单 form", "form"), ("JSON", "json"), ("原始文本", "raw")):
            ttk.Radiobutton(
                ctype_frame,
                text=text,
                value=value,
                variable=self.ctype_var,
                style="Card.TRadiobutton",
                command=self._update_body_hint,
            ).pack(side="left", padx=(0, 14))

        row += 1
        ttk.Label(card, text="请求体", style="Card.TLabel").grid(row=row, column=0, sticky="nw", pady=(6, 2))
        body_shell, self.body_text = self._text_area(card, height=4)
        body_shell.grid(row=row, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=(6, 0))
        # 手填请求体时下拉框要跟着变，否则「账号字段」里永远只有「（不使用）」
        self.body_text.bind("<<Modified>>", self._on_body_modified)

        row += 1
        self.body_hint = ttk.Label(card, text="", style="CardHint.TLabel")
        self.body_hint.grid(row=row, column=1, columnspan=3, sticky="w", padx=(8, 0), pady=(4, 4))

        row += 1
        ttk.Label(card, text="账号字段", style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=2)
        self.user_field_var = tk.StringVar(value=NO_FIELD)
        self.user_field_box = ttk.Combobox(
            card, textvariable=self.user_field_var, values=[NO_FIELD], width=20, state="readonly"
        )
        self.user_field_box.grid(row=row, column=1, sticky="w", padx=(8, 8))
        self.user_field_box.bind("<<ComboboxSelected>>", lambda _e: self._apply_field_mapping())
        ttk.Label(card, text="密码字段", style="Card.TLabel").grid(row=row, column=2, sticky="e")
        self.pass_field_var = tk.StringVar(value=NO_FIELD)
        self.pass_field_box = ttk.Combobox(
            card, textvariable=self.pass_field_var, values=[NO_FIELD], width=18, state="readonly"
        )
        self.pass_field_box.grid(row=row, column=3, sticky="w", padx=(4, 0))
        self.pass_field_box.bind("<<ComboboxSelected>>", lambda _e: self._apply_field_mapping())

        row += 1
        ttk.Label(card, text="密码处理", style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=(6, 2))
        self.pw_tpl_var = tk.StringVar(value="{password}")
        ttk.Combobox(
            card,
            textvariable=self.pw_tpl_var,
            values=["{password}", "{md5}", "{md5_upper}", "{base64}", "{sha1}"],
            width=20,
        ).grid(row=row, column=1, sticky="w", padx=(8, 8), pady=(6, 0))
        ttk.Label(
            card,
            text="明文提交用 {password}；抓到的若是 32 位十六进制串，用 {md5}",
            style="CardHint.TLabel",
        ).grid(row=row, column=2, columnspan=2, sticky="w", pady=(6, 0))

        row += 1
        ttk.Label(card, text="成功关键字", style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=(6, 2))
        self.ok_kw_var = tk.StringVar()
        ttk.Entry(card, textvariable=self.ok_kw_var, font=FONT_MONO).grid(
            row=row, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=(6, 0)
        )
        row += 1
        ttk.Label(card, text="失败关键字", style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=2)
        self.fail_kw_var = tk.StringVar()
        ttk.Entry(card, textvariable=self.fail_kw_var, font=FONT_MONO).grid(
            row=row, column=1, columnspan=3, sticky="ew", padx=(8, 0)
        )
        row += 1
        ttk.Label(card, text="JSON 判定", style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=2)
        json_frame = ttk.Frame(card, style="Card.TFrame")
        json_frame.grid(row=row, column=1, columnspan=3, sticky="w", padx=(8, 0))
        self.json_path_var = tk.StringVar()
        self.json_eq_var = tk.StringVar()
        ttk.Entry(json_frame, textvariable=self.json_path_var, width=18, font=FONT_MONO).pack(side="left")
        ttk.Label(json_frame, text=" = ", style="Card.TLabel").pack(side="left")
        ttk.Entry(json_frame, textvariable=self.json_eq_var, width=16, font=FONT_MONO).pack(side="left")
        ttk.Label(
            json_frame, text="  选填，例：result = success（比关键字更准）", style="CardHint.TLabel"
        ).pack(side="left")

    def _build_step3(self, parent, row: int) -> None:
        card = self._card(parent, "② 账号密码（加密存在本机）", row)
        card.columnconfigure(1, weight=1)
        card.columnconfigure(3, weight=1)
        ttk.Label(card, text="账号", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        self.user_var = tk.StringVar()
        self.user_entry = ttk.Entry(card, textvariable=self.user_var, font=FONT_MONO)
        self.user_entry.grid(row=0, column=1, sticky="ew", padx=(8, 16))

        ttk.Label(card, text="密码", style="Card.TLabel").grid(row=0, column=2, sticky="w")
        self.pass_var = tk.StringVar()
        self.pass_entry = ttk.Entry(card, textvariable=self.pass_var, show="●", font=FONT_MONO)
        self.pass_entry.grid(row=0, column=3, sticky="ew", padx=(8, 0))

        self.show_pass_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            card,
            text="显示",
            variable=self.show_pass_var,
            style="Card.TCheckbutton",
            command=self._toggle_password,
        ).grid(row=0, column=4, sticky="w", padx=(8, 0))

        self.cred_hint = ttk.Label(
            card,
            text="密码用 Windows DPAPI 加密保存在 credentials.dat，只有当前 Windows 账号能解开；"
            "配置里只会有 {username} / {password} 占位符。",
            style="CardHint.TLabel",
            wraplength=820,
            justify="left",
        )
        self.cred_hint.grid(row=1, column=0, columnspan=5, sticky="w", pady=(8, 0))

    def _build_actions(self, parent, row: int) -> None:
        box = ttk.Frame(parent)
        box.grid(row=row, column=0, sticky="ew", pady=(4, 6))
        box.columnconfigure(0, weight=1)

        left = ttk.Frame(box)
        left.grid(row=0, column=0, sticky="w")
        self.test_btn = ttk.Button(left, text="测试登录", style="Primary.TButton", command=self.on_test)
        self.test_btn.pack(side="left")
        self.save_btn = ttk.Button(left, text="保存设置", style="Save.TButton", command=self.on_save)
        self.save_btn.pack(side="left", padx=(8, 0))
        for text, cmd in (
            ("创建桌面快捷方式", self.on_shortcut),
            ("检查网络", self.on_check_network),
            ("自检", self.on_doctor),
        ):
            ttk.Button(left, text=text, command=cmd).pack(side="left", padx=(8, 0))

        right = ttk.Frame(box)
        right.grid(row=0, column=1, sticky="e")
        self.autostart_var = tk.BooleanVar(value=False)
        # 用 tk 的复选框：clam 主题那个 indicator 勾选后画的是个叉，看着像「没选」
        self.autostart_check = tk.Checkbutton(
            right,
            text="开机自动登录",
            variable=self.autostart_var,
            command=self.on_toggle_autostart,
            font=FONT,
            bg=COLOR["bg"],
            activebackground=COLOR["bg"],
            fg=COLOR["text"],
            activeforeground=COLOR["text"],
            selectcolor="#ffffff",
            bd=0,
            highlightthickness=0,
            cursor="hand2",
        )
        self.autostart_check.pack(side="right")

        ttk.Label(box, text=SHORTCUT_HINT, style="Hint.TLabel").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(6, 0)
        )

    def _build_log(self, parent, row: int) -> None:
        card = self._card(parent, "运行信息", row, pady=(4, 0))
        card.columnconfigure(0, weight=1)
        card.rowconfigure(1, weight=1)

        tools = ttk.Frame(card, style="Card.TFrame")
        tools.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(
            tools, text="出问题时把这里的内容整段贴给我，一眼就能看出卡在哪一步。", style="CardHint.TLabel"
        ).pack(side="left")
        ttk.Button(tools, text="复制全部", style="Mini.TButton", command=self.on_copy_log).pack(side="right")
        ttk.Button(tools, text="清空", style="Mini.TButton", command=self.on_clear_log).pack(side="right", padx=(0, 6))
        ttk.Button(tools, text="打开日志文件", style="Mini.TButton", command=self.on_open_log).pack(
            side="right", padx=(0, 6)
        )

        log_shell = tk.Frame(card, bg=COLOR["border"])
        log_shell.grid(row=1, column=0, sticky="nsew")
        self.log_text = tk.Text(
            log_shell,
            height=7,
            font=FONT_MONO,
            wrap="word",
            state="disabled",
            relief="flat",
            borderwidth=0,
            bg="#fbfcfe",
            fg=COLOR["text"],
            padx=8,
            pady=6,
        )
        self.log_text.grid(row=0, column=0, sticky="nsew", padx=(1, 0), pady=(1, 1))
        scroll = ttk.Scrollbar(log_shell, orient="vertical", command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns", padx=(0, 1), pady=(1, 1))
        self.log_text.configure(yscrollcommand=scroll.set)
        log_shell.rowconfigure(0, weight=1)
        log_shell.columnconfigure(0, weight=1)
        for tag, color in (("ok", COLOR["ok"]), ("err", COLOR["err"]), ("info", "#334155"), ("warn", COLOR["warn"])):
            self.log_text.tag_configure(tag, foreground=color)

    def _build_statusbar(self) -> None:
        bar = tk.Frame(self.root, bg=COLOR["bar"])
        bar.pack(side="bottom", fill="x")
        tk.Frame(bar, bg=COLOR["border"], height=2).pack(side="top", fill="x")
        inner = tk.Frame(bar, bg=COLOR["bar"])
        inner.pack(fill="x", padx=16, pady=5)
        # 角落里一条不会动的小鱼，签名用
        self.footer_fish = FishMascot(inner, size=18, bg=COLOR["bar"], animate=False)
        self.footer_fish.pack(side="left", padx=(0, 6))
        self.footer_left = tk.Label(
            inner,
            text=f"大肥鱼 · 配置文件：{paths.rel(paths.CONFIG_FILE)}",
            font=FONT_SMALL,
            bg=COLOR["bar"],
            fg=COLOR["muted"],
        )
        self.footer_left.pack(side="left")
        self.footer_right = tk.Label(
            inner,
            text=f"双击「一键登录.vbs」即可登录 · v{APP_VERSION}",
            font=FONT_SMALL,
            bg=COLOR["bar"],
            fg=COLOR["muted"],
        )
        self.footer_right.pack(side="right")

    def _bind_shortcuts(self) -> None:
        self.root.bind("<Control-Return>", self._shortcut(self.on_test))
        self.root.bind("<Control-s>", self._shortcut(self.on_save))
        self.root.bind("<F5>", self._shortcut(self.on_check_network))

    @staticmethod
    def _shortcut(func):
        def handler(_event=None):
            func()
            return "break"

        return handler

    # ------------------------------------------------------------ 状态与忙碌

    def _set_status(self, text: str, kind: str = "idle") -> None:
        self.status_label.configure(text=text, bg=STATUS_BG.get(kind, STATUS_BG["idle"]))
        mascot = getattr(self, "mascot", None)
        if mascot is not None:
            mood = MOOD_BY_STATUS.get(kind)
            if mood:
                mascot.set_mood(mood)
        if kind in ("run",):
            self._busy(True)
        elif kind in ("ok", "err", "warn", "idle"):
            self._busy(False)

    def _busy(self, on: bool) -> None:
        """顶部那条细进度条：只在真的在干活时出现，静止的界面不该有东西在动。"""
        if on and not self._progress_on:
            self.progress.pack(side="right", padx=(0, 10))
            self.progress.start(14)
            self._progress_on = True
        elif not on and self._progress_on:
            self.progress.stop()
            self.progress.pack_forget()
            self._progress_on = False

    def _toggle_password(self) -> None:
        self.pass_entry.configure(show="" if self.show_pass_var.get() else "●")

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
        self._update_cred_hint()

    def _update_cred_hint(self) -> None:
        """已经存过密码就直说，省得用户以为要重打一遍。"""
        base = "密码用 Windows DPAPI 加密保存在 credentials.dat，只有当前 Windows 账号能解开；配置里只会有 {username} / {password} 占位符。"
        if getattr(self, "cred_hint", None) is None:
            return
        if self.old_creds.complete:
            self.cred_hint.configure(
                text=f"已保存过 {secrets.mask_username(self.old_creds.username)} 的密码，密码框留空就沿用它。"
                "密码用 DPAPI 加密存在 credentials.dat，配置里只有占位符。"
            )
        else:
            self.cred_hint.configure(text=base)

    def _on_body_modified(self, _event=None) -> None:
        """请求体一改就刷新字段下拉框（用 <<Modified>> 标志去重，避免递归触发）。"""
        try:
            if not self.body_text.edit_modified():
                return
            self.body_text.edit_modified(False)
        except Exception:
            pass
        self._refresh_field_choices()

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

    def on_paste_clipboard(self) -> None:
        """把剪贴板里的内容填进第 ① 步。

        复制 cURL 之后最常见的卡点是「不知道往哪贴」，所以直接给一个按钮，
        贴进来之后顺手就解析——省掉一次多余的点击。
        """
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            self.log("剪贴板里没有文字（可能是空的，或者复制的是图片）", "warn")
            messagebox.showinfo(APP_TITLE, "剪贴板里没有文字。\n先在浏览器开发者工具里右键登录请求 → Copy as cURL。")
            return
        text = (text or "").strip()
        if not text:
            messagebox.showinfo(APP_TITLE, "剪贴板是空的。")
            return
        if not text.lower().lstrip().startswith("curl"):
            # 允许直接贴一整段浏览器复制的内容，但提示一句，避免贴错东西
            self.log("剪贴板内容看起来不是 curl 命令，仍然填进去了，请自行确认", "warn")
        self.curl_text.delete("1.0", "end")
        self.curl_text.insert("1.0", text)
        self.log("已从剪贴板填入，正在解析……")
        self.on_parse_curl()

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
        self._set_status("请求已识别", "ok")
        # 解析完人手一般就在键盘上，直接跳到第一个还空着的输入框，省一次 Tab
        if not self.user_var.get().strip():
            self.user_entry.focus_set()
        elif not self.pass_var.get() and not self.old_creds.complete:
            self.pass_entry.focus_set()

    def on_check_network(self) -> None:
        # 先在主线程把界面内容读出来。Tk 变量不是线程安全的，
        # 以前在后台线程里调 collect_config()，在 pythonw 下会静默失败，
        # 症状就是「认证服务器可达」那行永远不出现。
        try:
            cfg = self.collect_config()
            target = cfg["login"]["url"]
        except ValueError as exc:
            self.log(f"配置还不完整，先跳过认证服务器检查：{exc}", "warn")
            target = ""

        self._set_status("检查网络…", "run")

        def work():
            try:
                ip = net.local_ip()
                online, why = net.online()
                self.log(f"本机 IP：{ip}", "info")
                self.log(f"联网状态：{why}", "ok" if online else "warn")
                if not target:
                    self.root.after(0, lambda: self._set_status("网络已检查", "idle"))
                    return
                if net.portal_reachable(target):
                    self.log(f"认证服务器可达：{target}", "ok")
                    self.root.after(0, lambda: self._set_status("网关可达", "ok"))
                else:
                    self.log(f"认证服务器不可达：{target}（可能不在校园网内 / 未连 Wi-Fi）", "err")
                    self.root.after(0, lambda: self._set_status("网关不可达", "err"))
            except Exception as exc:  # noqa: BLE001
                self.log(f"✗ 检查网络出错：{type(exc).__name__}: {exc}", "err")
                self.root.after(0, lambda: self._set_status("检查出错", "err"))

        threading.Thread(target=work, daemon=True).start()

    def on_doctor(self) -> None:
        """跑一遍自检，把每条结果打到「运行信息」里（与 login.py doctor 同一套逻辑）。"""
        if getattr(self, "_doctor_running", False):
            return
        self._doctor_running = True
        self._set_status("自检中…", "run")
        self.log("——————————————————")
        self.log("开始自检：加密存储 / 配置 / 请求模板 / 网络…", "info")

        def work():
            try:
                checks = doctor_mod.run(probe=True)
            except Exception as exc:  # noqa: BLE001
                self.log(f"✗ 自检出错：{type(exc).__name__}: {exc}", "err")
                self.root.after(0, lambda: self._set_status("自检出错", "err"))
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
                self.root.after(0, lambda: self._set_status(f"自检 {len(bad)} 个问题", "warn"))
            else:
                self.log("✓ 自检通过：这台机器上可以直接登录。", "ok")
                self.root.after(0, lambda: self._set_status("自检通过", "ok"))

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
        self._set_status("正在登录…", "run")
        self.log("——————————————————")
        self.log(f"开始测试登录：{cfg['login']['method']} {cfg['login']['url']}", "info")

        def work():
            started = time.time()
            try:
                result = portal.login(cfg, username, password)
                if result.ok:
                    self.log(f"✓ 登录成功：{result.message}", "ok")
                    self.log(f"  耗时 {time.time() - started:.1f}s，尝试 {result.attempts} 次", "info")
                    self.root.after(0, lambda: self._set_status("登录成功", "ok"))
                else:
                    self.log(f"✗ 登录失败：{result.message}", "err")
                    if result.hint:
                        # 先给方向：多数人看不懂 HTML，但看得懂「账号密码不对」
                        self.log(f"  提示：{result.hint}", "warn")
                    snippet = (result.detail or "").strip()
                    if snippet:
                        # detail 里密码已经打过码，body 是原文，别往界面上贴
                        self.log(f"  服务器返回：{snippet[:600]}", "warn")
                    self.root.after(0, lambda: self._set_status("登录失败", "err"))
            except Exception as exc:  # noqa: BLE001
                self.log(f"✗ 出错：{type(exc).__name__}: {exc}", "err")
                self.root.after(0, lambda: self._set_status("出错了", "err"))
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
        # 先存凭据再存配置：反过来的话，凭据写失败会留下「新配置 + 旧密码」的半保存状态
        try:
            secrets.save(username, password)
        except (OSError, secrets.SecretError) as exc:
            messagebox.showerror(APP_TITLE, f"账号密码保存失败，设置没有保存：{exc}")
            self.log(f"✗ 账号密码保存失败：{exc}", "err")
            return
        try:
            path = config_mod.save(cfg)
        except (OSError, ValueError) as exc:
            messagebox.showerror(APP_TITLE, f"保存失败：{exc}")
            self.log(f"✗ 保存失败：{exc}", "err")
            return
        self.cfg = cfg
        self.old_creds = secrets.Credentials(username=username, password=password)
        self.pass_var.set("")
        self._update_cred_hint()
        self._set_status(f"已保存 · {secrets.mask_username(username)}", "ok")
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

    def on_copy_log(self) -> None:
        """把「运行信息」整段复制走：出问题时要贴给我看的就是这一段。"""
        text = self.log_text.get("1.0", "end").strip()
        if not text:
            self.log("运行信息还是空的。", "warn")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.log(f"✓ 已复制 {len(text)} 个字符到剪贴板。", "ok")

    def on_clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _on_close(self) -> None:
        self.root.destroy()


def _fatal(message: str, detail: str = "") -> None:
    """pythonw 下没有控制台，异常必须弹出来，否则用户只看到「双击没反应」。"""
    text = message + (("\n\n" + detail.strip()[:800]) if detail else "")
    try:
        from tkinter import messagebox

        messagebox.showerror(APP_TITLE, text)
        return
    except Exception:  # pragma: no cover
        pass
    try:  # pragma: no cover
        ctypes.windll.user32.MessageBoxW(None, text, APP_TITLE, 0x10)
    except Exception:
        pass


def main() -> int:
    try:
        _enable_dpi()
        root = tk.Tk()
    except Exception as exc:  # noqa: BLE001  # pragma: no cover
        _fatal(f"设置向导启动失败：{type(exc).__name__}: {exc}")
        return 1
    try:
        SetupApp(root)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        try:
            root.destroy()
        except Exception:
            pass
        _fatal(f"设置向导初始化失败：{type(exc).__name__}: {exc}", traceback.format_exc())
        return 1
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
