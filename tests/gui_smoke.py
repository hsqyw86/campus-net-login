"""界面冒烟测试：把设置向导真的建出来，检查控件与布局是否正常。

不做登录，只验证界面能构建、能取到值、关键逻辑（字段映射 / 明文检查）正常。
运行：python tests/gui_smoke.py
"""

from __future__ import annotations

import os
import sys
import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from gui.setup_app import NO_FIELD, SetupApp  # noqa: E402

SAMPLE_CURL = (
    "curl 'http://10.21.1.1/eportal/InterFace.do?method=login' \\\n"
    "  -H 'Accept: */*' \\\n"
    "  -H 'Content-Type: application/x-www-form-urlencoded; charset=UTF-8' \\\n"
    "  --data-raw 'userId=20230001&password=5f4dcc3b5aa765d61d8327deb882cf99&service=default' \\\n"
    "  --compressed"
)


def main() -> int:
    root = tk.Tk()
    app = SetupApp(root)
    root.update_idletasks()
    root.update()

    problems: list[str] = []

    width = root.winfo_width()
    height = root.winfo_height()
    if width < 700 or height < 600:
        problems.append(f"窗口尺寸异常：{width}x{height}")

    widgets = 0
    stack = [root]
    while stack:
        node = stack.pop()
        widgets += 1
        stack.extend(node.winfo_children())
    if widgets < 60:
        problems.append(f"控件数量偏少，可能界面没建全：{widgets}")

    def walk(node):
        for child in node.winfo_children():
            yield child
            yield from walk(child)

    # 布局检查：显示出来的控件都要有实际尺寸，内容不能超出窗口
    zero = [
        f"{w.winfo_class()}"
        for w in walk(root)
        if w.winfo_ismapped() and (w.winfo_width() < 2 or w.winfo_height() < 2)
    ]
    if zero:
        problems.append(f"有控件尺寸为 0：{zero[:6]}")
    if app.log_text.winfo_height() < 40:
        problems.append(f"日志区太矮，可能被挤没了：{app.log_text.winfo_height()}")
    content_bottom = max(
        (w.winfo_rooty() + w.winfo_height() for w in walk(root) if w.winfo_ismapped()), default=0
    )
    window_bottom = root.winfo_rooty() + root.winfo_height()
    if content_bottom > window_bottom + 2:
        problems.append(f"内容超出窗口底部 {content_bottom - window_bottom}px，会被裁掉")

    # 1) 粘贴 cURL 后应当自动填好并替换占位符
    app.curl_text.insert("1.0", SAMPLE_CURL)
    app.on_parse_curl()
    root.update()
    if "method=login" not in app.url_var.get():
        problems.append(f"URL 没填进界面：{app.url_var.get()}")
    body = app.body_text.get("1.0", "end")
    if "{password}" not in body:
        problems.append(f"密码没被替换成占位符：{body.strip()}")
    if "{username}" not in body:
        problems.append(f"账号没被替换成占位符：{body.strip()}")
    if app.pw_tpl_var.get() != "{md5}":
        problems.append(f"32 位密码应自动选 md5，实际：{app.pw_tpl_var.get()}")

    # 2) 收集配置应当成功
    app.user_var.set("20230001")
    app.pass_var.set("password")
    try:
        cfg = app.collect_config()
    except ValueError as exc:
        problems.append(f"collect_config 失败：{exc}")
        cfg = {}
    if cfg:
        if cfg["login"]["fields"].get("userId") != "{username}":
            problems.append(f"账号字段不对：{cfg['login']['fields']}")
        if cfg["login"]["password_template"] != "{md5}":
            problems.append("密码模板没保存下来")

        # 3) 故意把明文密码塞回请求体，应当被拦住
        app.body_text.delete("1.0", "end")
        app.body_text.insert("1.0", "userId=20230001\npassword=password")
        try:
            app.collect_config()
        except ValueError as exc:
            if "明文" not in str(exc):
                problems.append(f"明文检查提示不对：{exc}")
        else:
            problems.append("请求体里留着明文密码，居然没被拦住")

    root.destroy()

    if problems:
        print("界面冒烟测试发现问题：")
        for item in problems:
            print(f"  - {item}")
        return 1
    print(f"界面冒烟测试通过：窗口 {width}x{height}，控件 {widgets} 个，cURL 导入 / 字段映射 / 明文拦截均正常")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
