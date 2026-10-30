"""端到端 + 单元测试（不需要真的校园网）。

运行：
    python tests/run_tests.py
会在本机随机端口起一个假认证服务器（tests/mock_portal.py），
把预跳转取参、Cookie 会话、密码加密、成功/失败判定、CLI 退出码都跑一遍。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import random
import re
import shutil
import string
import subprocess
import sys
import traceback
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 让测试不要写到项目根目录的真实配置文件 / 状态文件
os.environ.setdefault("CAMPUSNET_STATE", os.path.join(HERE, "_test_state.json"))

from core import (  # noqa: E402
    autostart,
    config as config_mod,
    curlparse,
    doctor,
    importcurl,
    paths,
    portal,
    runner,
    secrets,
)
from tests.mock_portal import ACCOUNTS, MockPortal  # noqa: E402

USERNAME = "20230001"
PASSWORD = "S3cret!pwd"
TEST_STATE_FILE = os.environ["CAMPUSNET_STATE"]
RESULTS: list[tuple[str, bool, str]] = []


class Skipped(Exception):
    """本机环境不具备条件，跳过（不算失败）。"""


def check(name: str, func):
    try:
        func()
    except Skipped as exc:
        RESULTS.append((name, True, f"跳过：{exc}"))
        print(f"[SKIP] {name}\n       跳过：{exc}")
    except Exception as exc:  # noqa: BLE001
        RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
        print(f"[FAIL] {name}\n       {type(exc).__name__}: {exc}")
        traceback.print_exc(limit=3)
    else:
        RESULTS.append((name, True, ""))
        print(f"[ OK ] {name}")


def expect(condition, message: str):
    if not condition:
        raise AssertionError(message)


@contextlib.contextmanager
def tmpdir(prefix: str = "tmp_"):
    """测试用临时目录（用 os.makedirs 创建，兼容受限环境）。"""
    name = prefix + "".join(random.choice(string.ascii_lowercase) for _ in range(8))
    path = os.path.join(HERE, name)
    os.makedirs(path, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


# ------------------------------------------------------------------ 配置样本

def make_cfg(base: str, *, with_preflight: bool = True, with_token: bool = True) -> dict:
    fields = {"userId": "{username}", "password": "{password}"}
    if with_token:
        fields["token"] = "{token}"
    raw = {
        "profile_name": "测试",
        "preflight": {"url": base + "/"} if with_preflight else {"url": ""},
        "login": {
            "method": "POST",
            "url": base + "/eportal/login",
            "content_type": "form",
            "fields": fields,
            "password_template": "{md5}",
        },
        "expect": {"json_path": "result", "json_equals": "success"},
        "verify": {"enabled": True, "url": base + "/generate_204", "expect_status": 204, "timeout": 4},
        "retry": {"attempts": 1, "interval": 0},
    }
    return config_mod.normalize(raw)


# ------------------------------------------------------------------ 单元测试

def test_curl_bash():
    text = (
        "curl 'http://10.21.1.1/eportal/InterFace.do?method=login' \\\n"
        "  -H 'Accept: */*' \\\n"
        "  -H 'Content-Type: application/x-www-form-urlencoded; charset=UTF-8' \\\n"
        "  -H 'Content-Length: 99' \\\n"
        "  --data-raw 'userId=20230001&password=abc123&service=default&queryString=userip%3D10.20.30.40' \\\n"
        "  --compressed"
    )
    req = curlparse.parse_curl(text)
    expect(req.url.endswith("method=login"), f"URL 解析错：{req.url}")
    expect(req.method == "POST", f"方法错：{req.method}")
    expect(req.content_type == "form", f"类型错：{req.content_type}")
    expect("content-length" not in {k.lower() for k in req.headers}, "Content-Length 应该被丢掉")
    expect(req.headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded"), "Content-Type 丢了")
    values = req.field_values()
    expect(values["userId"] == "20230001", f"字段值错：{values}")
    guess = req.guess_fields()
    expect(guess.get("username") == "userId", f"没猜出账号字段：{guess}")
    expect(guess.get("password") == "password", f"没猜出密码字段：{guess}")


def test_curl_cmd():
    text = (
        'curl ^"http://10.21.1.1/eportal/login^" ^\n'
        '  -H ^"Content-Type: application/x-www-form-urlencoded^" ^\n'
        '  --data-raw ^"userId=20230001^&password=xyz^&service=default^"'
    )
    req = curlparse.parse_curl(text)
    expect(req.method == "POST", f"方法错：{req.method}")
    values = req.field_values()
    expect(values.get("userId") == "20230001", f"字段值错：{values}")
    expect(values.get("service") == "default", f"字段值错：{values}")


def test_curl_json():
    text = (
        "curl 'https://portal.example.edu/api/login' \\\n"
        "  -H 'Content-Type: application/json' \\\n"
        "  --data-raw '{\"account\":\"u1\",\"pwd\":\"p1\",\"nas\":\"x\"}'"
    )
    req = curlparse.parse_curl(text)
    expect(req.content_type == "json", f"类型错：{req.content_type}")
    expect(req.guess_fields().get("username") == "account", "JSON 账号字段没猜出来")
    expect(req.guess_fields().get("password") == "pwd", "JSON 密码字段没猜出来")


def test_render_unknown_placeholder():
    cfg = make_cfg("http://127.0.0.1:1")
    variables = portal.build_variables(cfg, "u", "p")
    try:
        portal.render("{nope}", variables, "测试")
    except portal.LoginError as exc:
        expect("nope" in str(exc), "报错信息没提到具体占位符")
    else:
        raise AssertionError("未知占位符居然没报错")


def test_password_template():
    cfg = make_cfg("http://127.0.0.1:1")
    variables = portal.build_variables(cfg, "u", "p")
    expect(variables["password"] == hashlib.md5(b"p").hexdigest(), "md5 密码模板没生效")
    expect(variables["password_raw"] == "p", "password_raw 应该保留明文")

    cfg2 = config_mod.normalize({"login": {"url": "http://x/", "body": "a=1", "password_template": "{sha1}"}})
    variables2 = portal.build_variables(cfg2, "u", "p")
    expect(variables2["password"] == hashlib.sha1(b"p").hexdigest(), "sha1 密码模板没生效")


def test_judge_rules():
    cfg = make_cfg("http://127.0.0.1:1")
    ok, msg = portal.judge(cfg, 200, json.dumps({"result": "success", "msg": "认证成功"}))
    expect(ok is True, f"JSON 成功判定错：{ok} {msg}")
    ok, msg = portal.judge(cfg, 200, json.dumps({"result": "fail", "msg": "密码错误"}))
    expect(ok is False, f"JSON 失败判定错：{ok} {msg}")
    expect("密码错误" in msg, f"没提取出服务器提示：{msg}")

    cfg_kw = config_mod.normalize({"login": {"url": "http://x/", "body": "a=1"}, "expect": {"json_path": ""}})
    ok, _ = portal.judge(cfg_kw, 200, "var r={result:'success'};")
    expect(ok is True, "关键字成功判定错")
    ok, _ = portal.judge(cfg_kw, 200, "<p>密码错误，请重试</p>")
    expect(ok is False, "关键字失败判定错")


def test_form_encoding():
    cfg = config_mod.normalize(
        {
            "login": {
                "url": "http://x/login",
                "content_type": "form",
                "fields": {"userId": "{username}", "password": "{password}"},
            }
        }
    )
    req = portal.build_request(cfg, portal.build_variables(cfg, "u&1", "p&=+"))
    body = req.body_text
    parsed = dict(urllib.parse.parse_qsl(body, keep_blank_values=True))
    expect(parsed["userId"] == "u&1", f"账号里的 & 没被正确编码：{body}")
    expect(parsed["password"] == "p&=+", f"密码里的特殊字符没被正确编码：{body}")


def test_body_template_escaping():
    cfg = config_mod.normalize(
        {"login": {"url": "http://x/login", "content_type": "form", "body": "user={username}&pwd={password}"}}
    )
    req = portal.build_request(cfg, portal.build_variables(cfg, "a b", "x&y"))
    parsed = dict(urllib.parse.parse_qsl(req.body_text, keep_blank_values=True))
    expect(parsed.get("user") == "a b", f"原始模板里的账号没编码：{req.body_text}")
    expect(parsed.get("pwd") == "x&y", f"原始模板里的密码没编码：{req.body_text}")


def test_preview_masks_password():
    cfg = config_mod.normalize(
        {
            "login": {
                "url": "http://x/login",
                "content_type": "form",
                "fields": {"userId": "{username}", "password": "{password}"},
            }
        }
    )
    req = portal.build_request(cfg, portal.build_variables(cfg, "u1", "TopSecret123"))
    preview = req.preview()
    expect("TopSecret123" not in preview, f"预览里泄露了明文密码：{preview}")
    expect("u1" in preview, "预览应该能看到账号")


def test_dpapi_roundtrip():
    with tmpdir() as tmp:
        path = os.path.join(tmp, "cred.dat")
        secrets.save("20230001", "p@ss&word!", path=path)
        loaded = secrets.load(path)
        expect(loaded.username == "20230001", "账号回读不一致")
        expect(loaded.password == "p@ss&word!", "密码回读不一致")
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
        expect("p@ss&word!" not in raw, "密文里出现了明文密码")
        expect("20230001" not in raw, "密文里出现了明文账号")
        expect(json.loads(raw)["format"] == "dpapi-v1", "格式标记不对")


def test_config_validation():
    problems = config_mod.validate(config_mod.normalize({}), strict=False)
    expect(any("登录地址" in p for p in problems), f"空配置居然没报缺地址：{problems}")
    try:
        config_mod.validate(config_mod.normalize({}), strict=True)
    except config_mod.ConfigError:
        pass
    else:
        raise AssertionError("strict 模式没抛异常")


def test_config_bom_and_gbk():
    """记事本存成 UTF-8 带 BOM 要能读；存成 GBK 要给能看懂的提示。"""
    with tmpdir() as tmp, MockPortal() as srv:
        cfg = make_cfg(srv.base_url)
        bom_path = os.path.join(tmp, "bom.json")
        with open(bom_path, "w", encoding="utf-8-sig") as fh:  # 模拟记事本「UTF-8 带 BOM」
            json.dump(cfg, fh, ensure_ascii=False)
        loaded = config_mod.load(bom_path)
        expect(loaded["login"]["url"] == cfg["login"]["url"], "带 BOM 的配置读出来不对")

        gbk_path = os.path.join(tmp, "gbk.json")
        with open(gbk_path, "w", encoding="gbk") as fh:  # 模拟存成 ANSI/GBK
            json.dump(cfg, fh, ensure_ascii=False)
        try:
            config_mod.load(gbk_path)
        except config_mod.ConfigError as exc:
            expect("UTF-8" in str(exc), f"编码错误提示不清楚：{exc}")
        else:
            raise AssertionError("GBK 文件应该报编码错误")


def test_secret_alias_escaping():
    """密码别名 pwd / passwd 也必须做 URL 编码，否则含 & 的密码会被截断。"""
    cfg = config_mod.normalize(
        {"login": {"url": "http://x/login", "content_type": "form", "body": "user={username}&pwd={pwd}&passwd={passwd}"}}
    )
    req = portal.build_request(cfg, portal.build_variables(cfg, "a b", "x&y"))
    parsed = dict(urllib.parse.parse_qsl(req.body_text, keep_blank_values=True))
    expect(parsed.get("pwd") == "x&y", f"pwd 别名没编码，密码会被截断：{req.body_text}")
    expect(parsed.get("passwd") == "x&y", f"passwd 别名没编码，密码会被截断：{req.body_text}")


def test_json_body_keeps_password_raw():
    """JSON 请求体里密码不能被 URL 编码（x&y 不该变成 x%26y）。"""
    cfg = config_mod.normalize(
        {
            "login": {
                "url": "http://x/api/login",
                "content_type": "json",
                "fields": {"userId": "{username}", "password": "{password}"},
            }
        }
    )
    req = portal.build_request(cfg, portal.build_variables(cfg, "u1", "x&y"))
    payload = json.loads(req.body_text)
    expect(payload["password"] == "x&y", f"JSON 里密码被错误编码：{req.body_text}")
    expect(payload["userId"] == "u1", f"JSON 里账号不对：{req.body_text}")


def test_config_type_errors():
    """配置文件结构写错时要给能看懂的提示，而不是 AttributeError。"""
    with tmpdir() as tmp:
        bad_list = os.path.join(tmp, "list.json")
        with open(bad_list, "w", encoding="utf-8") as fh:
            fh.write("[]")
        try:
            config_mod.load(bad_list)
        except config_mod.ConfigError as exc:
            expect("对象" in str(exc) or "dict" in str(exc).lower(), f"提示不清楚：{exc}")
        else:
            raise AssertionError("顶层是数组时应该报 ConfigError")

        bad_login = os.path.join(tmp, "str.json")
        with open(bad_login, "w", encoding="utf-8") as fh:
            fh.write('{"login": "http://x/login"}')
        try:
            config_mod.load(bad_login)
        except config_mod.ConfigError:
            pass
        else:
            raise AssertionError("login 写成字符串时应该报 ConfigError")

        problems = config_mod.validate(
            config_mod.normalize({"login": {"url": "http://x/"}, "retry": {"attempts": "两次"}}), strict=False
        )
        expect(any("retry.attempts" in p for p in problems), f"没校验出 attempts 类型错：{problems}")


def test_credentials_bad_shape():
    """credentials.dat 被改成合法 JSON 但结构不对时，抛 SecretError 而不是 AttributeError。"""
    with tmpdir() as tmp:
        path = os.path.join(tmp, "cred.dat")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("[]")
        try:
            secrets.load(path)
        except secrets.SecretError:
            pass
        else:
            raise AssertionError("结构不对的凭据文件应该报 SecretError")


def test_definitive_failure_not_retried():
    """服务器已经明确说「密码错误」时不要重试——重试只会把账号锁死。"""
    with MockPortal() as srv:
        cfg = make_cfg(srv.base_url)
        cfg["retry"] = {"attempts": 3, "interval": 0.05}
        result = portal.login(cfg, USERNAME, "wrong-pass")
        expect(not result.ok, "错误密码居然成功")
        expect(result.attempts == 1, f"确定性失败不该重试，实际试了 {result.attempts} 次")
        posts = [r for r in srv.requests if r[0] == "POST"]
        expect(len(posts) == 1, f"服务端收到 {len(posts)} 次登录请求，应当只有 1 次")


def test_autostart_command():
    cmd = autostart.build_command(r"C:\py\pythonw.exe", r"C:\app\launcher.pyw")
    expect(cmd == r'"C:\py\pythonw.exe" "C:\app\launcher.pyw" --wait --quiet', f"自启命令不对：{cmd}")


def test_autostart_state():
    expect(isinstance(autostart.is_enabled(), bool), "is_enabled 应返回 bool")
    text = autostart.describe()
    expect("开机自动登录" in text, f"describe 输出不对：{text}")


def _read_lnk_fields(lnk: str, folder: str) -> str:
    """用 cscript 读回 .lnk 的目标/参数/工作目录。

    脚本按 UTF-16LE+BOM 存盘、结果也存成 UTF-16，绕开控制台代码页：
    英文系统上中文内容不会被转成「????」。
    """
    out_file = os.path.join(folder, "_lnk_readback.txt")
    vbs = os.path.join(folder, "_lnk_readback.vbs")
    quote = lambda text: text.replace('"', '""')  # noqa: E731
    script = (
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'Set s = sh.CreateShortcut("{quote(lnk)}")\r\n'
        'Set fso = CreateObject("Scripting.FileSystemObject")\r\n'
        f'Set f = fso.CreateTextFile("{quote(out_file)}", True, True)\r\n'
        "f.WriteLine s.TargetPath\r\n"
        "f.WriteLine s.Arguments\r\n"
        "f.WriteLine s.WorkingDirectory\r\n"
        "f.Close\r\n"
    )
    with open(vbs, "w", encoding="utf-16") as fh:
        fh.write(script)
    cscript = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cscript.exe")
    subprocess.run([cscript, "//nologo", "//B", vbs], capture_output=True, timeout=60)
    with open(out_file, encoding="utf-16") as fh:
        return fh.read()


def test_shortcut_creation():
    """真的生成一个 .lnk，再读回来核对目标与参数。

    WScript.Shell 保存快捷方式时会走系统 ANSI 代码页：在英文系统（如 GitHub
    Actions 的 cp437 runner）上，非 ASCII 的快捷方式名会被写成「????.lnk」而失败。
    这是 WSH 的限制，不是本项目的 bug（中文 Windows 上一切正常），所以：
    先用中文名试，失败就退回 ASCII 名验证机制本身，两者都失败才算跳过。
    """
    with tmpdir() as tmp:
        try:
            lnk = autostart.create_desktop_shortcut(name="冒烟测试", folder=tmp)
        except OSError as exc:
            print(f"       中文快捷方式名不被本机 WSH 接受（{exc}），改用 ASCII 名重试")
            try:
                lnk = autostart.create_desktop_shortcut(name="campus-net-smoke", folder=tmp)
            except OSError as exc2:
                raise Skipped(f"本机 WScript.Shell 无法创建快捷方式（{exc2}）") from exc2
            expect(os.path.exists(lnk), f"快捷方式没生成：{lnk}")
            return  # 机制可用；中文名受系统代码页限制，见上面的提示
        expect(os.path.exists(lnk), f"快捷方式没生成：{lnk}")
        out = _read_lnk_fields(lnk, tmp).strip()
        expect("wscript.exe" in out.lower(), f"快捷方式目标不对：{out}")
        if "一键登录.vbs" not in out and "?" in out:
            # WSH 在英文系统上把非 ASCII 路径存成 ?（系统代码页限制，不是本项目逻辑问题）
            raise Skipped("本机 WScript.Shell 无法保存中文路径参数（非中文系统），已生成 .lnk 但内容不可验证")
        expect("一键登录.vbs" in out, f"快捷方式参数没指向启动器：{out}")
        expect(ROOT.lower() in out.lower(), f"工作目录不对：{out}")


def test_vbs_launchers_utf16():
    """两个 .vbs 必须是 UTF-16LE+BOM，并且指向正确的入口。

    这两条是踩过的坑：WSH 用 ANSI 读就会把中文全部读花；
    入口指错了（比如 一键登录.vbs 误加 --wait）会让开机自启多等一轮。
    """
    cases = {
        "一键登录.vbs": "launcher.pyw",
        "设置向导.vbs": "setup.pyw",
    }
    for name, entry in cases.items():
        path = os.path.join(ROOT, name)
        expect(os.path.exists(path), f"缺少启动器：{name}")
        with open(path, "rb") as fh:
            raw = fh.read()
        expect(raw[:2] == b"\xff\xfe", f"{name} 不是 UTF-16LE BOM 开头（前两字节 {raw[:2]!r}），中文会乱码")
        text = raw[2:].decode("utf-16-le")
        expect(entry in text, f"{name} 没有调用 {entry}")
        head = raw[2:].decode("utf-16-le").lower()
        if name == "一键登录.vbs":
            expect("--wait" not in head.split("launcher.pyw")[1][:80], "一键登录.vbs 不该带 --wait（那是开机自启用的）")


def test_stdlib_only():
    """守住「零依赖」这条底线：源码里只能 import 标准库（和项目内模块）。"""
    allowed_local = {"core", "gui", "tests", "custom_hook", "login", "launcher", "setup"}
    stdlib = set(sys.stdlib_module_names)
    pattern = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)
    offenders: list[str] = []
    for folder in ("", "core", "gui", "tests"):
        base = os.path.join(ROOT, folder)
        for filename in sorted(os.listdir(base)):
            if not filename.endswith((".py", ".pyw")):
                continue
            full = os.path.join(base, filename)
            with open(full, encoding="utf-8", errors="replace") as fh:
                source = fh.read()
            for module in pattern.findall(source):
                if module in stdlib or module in allowed_local:
                    continue
                offenders.append(f"{os.path.join(folder, filename)}: {module}")
    expect(not offenders, "引入了第三方依赖（本工具要求零依赖）：" + "；".join(offenders))


def test_gui_imports():
    import gui.setup_app  # noqa: F401  只验证能导入（不创建窗口）


HOOK_SOURCE = '''
import hashlib


def transform_password(username, password, variables):
    return hashlib.md5((password + "|salt").encode()).hexdigest()


def judge(status, body, expect):
    return ('"code":0' in body, "钩子判定")
'''


def test_custom_hook():
    """自定义钩子：密码变形 + 自定义判定（子进程隔离，避免污染其他测试）。"""
    with tmpdir() as tmp:
        hook = os.path.join(tmp, "custom_hook.py")
        with open(hook, "w", encoding="utf-8") as fh:
            fh.write(HOOK_SOURCE)
        script = (
            "import sys, hashlib\n"
            f"sys.path.insert(0, r'{ROOT}')\n"
            "from core import config as cm, portal\n"
            "cfg = cm.normalize({'login': {'url': 'http://x/', 'body': 'a=1', 'password_template': '{md5}',"
            " 'fields': {'p': '{password}'}}})\n"
            "variables = portal.build_variables(cfg, 'u', 'p')\n"
            "print(variables['password'])\n"
            "print('EXPECT', hashlib.md5(b'p|salt').hexdigest())\n"
            "ok, msg = portal.judge(cfg, 200, '{\"code\":0}')\n"
            "print('JUDGE', ok, msg)\n"
        )
        env = dict(os.environ)
        env.update({"CAMPUSNET_HOOK": hook, "PYTHONIOENCODING": "utf-8"})
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=120,
        )
        out = proc.stdout or ""
        expect(proc.returncode == 0, f"钩子测试子进程失败：{proc.stderr[:400]}")
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        expect(lines and lines[0] != "", f"没拿到密码：{out}")
        expected = [line.split()[1] for line in lines if line.startswith("EXPECT")][0]
        expect(lines[0] == expected, f"钩子没生效：得到 {lines[0]}，应为 {expected}")
        expect("JUDGE True" in out, f"钩子 judge 没生效：{out}")


# ------------------------------------------------------------------ 端到端

def test_e2e_success():
    with MockPortal() as srv:
        cfg = make_cfg(srv.base_url)
        result = portal.login(cfg, USERNAME, PASSWORD)
        expect(result.ok, f"应当登录成功，实际：{result.message}")
        expect(srv.logged_in, "服务器没收到成功登录")
        expect(result.request_preview.startswith("POST"), "预览内容不对")
        posts = [r for r in srv.requests if r[0] == "POST"]
        expect(posts, "没有发出 POST 请求")
        cookie = posts[0][2].get("Cookie", "")
        expect("JSESSIONID" in cookie, f"登录请求没带上会话 Cookie：{cookie}")


def test_e2e_wrong_password():
    with MockPortal() as srv:
        cfg = make_cfg(srv.base_url)
        result = portal.login(cfg, USERNAME, "wrong-pass")
        expect(not result.ok, "错误密码居然登录成功")
        expect("密码错误" in result.message, f"没把服务器提示带出来：{result.message}")


def test_e2e_unknown_user():
    with MockPortal() as srv:
        cfg = make_cfg(srv.base_url)
        result = portal.login(cfg, "99999999", PASSWORD)
        expect(not result.ok, "不存在的账号居然登录成功")
        expect("用户名不存在" in result.message, f"提示不对：{result.message}")


def test_e2e_needs_preflight():
    """不做预跳转 → 没有会话 Cookie / token，应该失败。"""
    with MockPortal() as srv:
        cfg = make_cfg(srv.base_url, with_preflight=False, with_token=False)
        result = portal.login(cfg, USERNAME, PASSWORD)
        expect(not result.ok, "缺少预跳转居然成功了")
        expect("会话" in result.message or "token" in result.message, f"提示不对：{result.message}")


def test_e2e_retry_then_success():
    with MockPortal() as srv:
        cfg = make_cfg(srv.base_url)
        cfg["retry"] = {"attempts": 3, "interval": 0.1}
        result = portal.login(cfg, USERNAME, PASSWORD)
        expect(result.ok and result.attempts == 1, f"第一次就该成功：{result.attempts} 次")


def test_failure_hint():
    # 提示只在「能确定是哪一类问题」时才给，猜错方向比没有更坏
    expect("账号密码" in portal._diagnose(status=403), "403 应提示账号密码/动态参数")
    expect("连不上" in portal._diagnose(error="connection refused"), "连接失败应提示先看 Wi-Fi")
    expect("还没网" in portal._diagnose(verify_why="超时"), "复核失败应提示 Cookie/放行")
    expect(portal._diagnose() == "", "什么信息都没有时不该瞎猜")
    html = "<html><body><p>密码错误，请重试</p></body></html>"
    hint = portal._diagnose(status=200, body=html)
    expect("密码错误" in hint and "<" not in hint, f"应把 HTML 剥成一句人话：{hint!r}")


def test_retry_backoff():
    """重试要退避、要有上限：网关多半在限流，固定间隔连撞没意义。"""
    with MockPortal() as srv:
        cfg = make_cfg(srv.base_url)
        # 预跳转正常，但登录地址打到一个没人听的端口 → 每次都是连接失败
        cfg["login"]["url"] = "http://127.0.0.1:9/eportal/login"
        cfg["retry"] = {"attempts": 4, "interval": 0.5, "max_interval": 1.0}
        waits: list[float] = []
        real_sleep = portal.time.sleep
        portal.time.sleep = lambda s: waits.append(s)  # type: ignore[assignment]
        try:
            result = portal.login(cfg, USERNAME, PASSWORD, verify=False)
        finally:
            portal.time.sleep = real_sleep  # type: ignore[assignment]
    expect(not result.ok, "连不上的地址不该被判成功")
    expect(len(waits) == 3, f"4 次尝试之间应该只等 3 次，实际 {len(waits)} 次")
    expect(waits[1] > waits[0], f"第二次等待应更长（指数退避）：{waits}")
    expect(max(waits) <= 1.0 * 1.15, f"等待不该超过 max_interval×抖动上限：{waits}")
    expect(result.hint, "连接失败时应该给出提示")


def test_wrong_password_has_hint():
    with MockPortal() as srv:
        cfg = make_cfg(srv.base_url)
        result = portal.login(cfg, USERNAME, "wrong-password", verify=False)
    expect(not result.ok and result.definitive, "密码错应当是明确失败")
    expect(result.hint, "密码错时应给一句人话提示")


def test_cli_log_tail():
    import login as cli  # 命令行入口就在项目根目录

    class Args:
        lines = 5

    with tmpdir() as tmp:
        path = os.path.join(tmp, "login.log")
        with open(path, "w", encoding="utf-8") as fh:
            for i in range(50):
                fh.write(f"第 {i} 行\n")
        original = paths.LOG_FILE
        paths.LOG_FILE = path
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                code = cli.cmd_log(Args())
        finally:
            paths.LOG_FILE = original
    text = out.getvalue()
    expect(code == 0, f"log 命令应正常退出：{code}")
    expect("第 49 行" in text and "第 45 行" in text, f"应打印最后 5 行：{text[-200:]}")
    expect("第 44 行" not in text, "不该多打印一行")


IMPORT_CURL = """curl 'http://10.21.1.1/eportal/InterFace.do?method=login' \\
  -H 'Content-Type: application/x-www-form-urlencoded; charset=UTF-8' \\
  -H 'Referer: http://10.21.1.1/eportal/index.jsp?userip=10.20.30.40&token=abc' \\
  -H 'Cookie: JSESSIONID=deadbeef' \\
  --data-raw 'userId=20230001&password=Plain!Pass1&service=default&token=abc'"""

IMPORT_CURL_JSON = """curl 'http://10.21.1.1/api/login' \\
  -H 'Content-Type: application/json' \\
  --data-raw '{"stuNo":"20230001","pwd":"Plain!Pass1","nas":"AC"}'"""


def test_import_curl_placeholders():
    report = importcurl.from_text(IMPORT_CURL)
    fields = report.cfg["login"]["fields"]
    expect(fields.get("userId") == "{username}", f"账号字段应换成占位符：{fields}")
    expect(fields.get("password") == "{password}", f"密码字段应换成占位符：{fields}")
    expect(fields.get("service") == "default", f"其它字段应原样保留：{fields}")
    blob = json.dumps(report.cfg, ensure_ascii=False)
    expect("Plain!Pass1" not in blob, "配置里不能出现明文密码")
    expect("20230001" not in blob, "配置里不能出现明文账号")
    expect(report.username == "20230001" and report.password == "Plain!Pass1", "凭据应被单独取出")
    expect(report.cfg["preflight"]["url"].startswith("http://10.21.1.1/"), "应从 Referer 推出登录前跳转")
    expect("Cookie" not in report.cfg["login"]["headers"], "Cookie 不该写进配置（会话由预跳转重建）")
    expect("***" in report.masked() and "Plain" not in report.masked(), "摘要里密码必须打码")


def test_import_curl_json_body():
    report = importcurl.from_text(IMPORT_CURL_JSON)
    expect(report.cfg["login"]["content_type"] == "json", f"应识别成 JSON：{report.cfg['login']['content_type']}")
    fields = report.cfg["login"]["fields"]
    expect(fields.get("stuNo") == "{username}", f"JSON 里的账号字段也应换掉：{fields}")
    expect(fields.get("pwd") == "{password}", f"JSON 里的密码字段也应换掉：{fields}")
    expect(report.password == "Plain!Pass1", "凭据应被取出")


def test_import_curl_hash_password():
    text = IMPORT_CURL.replace("Plain!Pass1", "e10adc3949ba59abbe56e057f20f883e")
    report = importcurl.from_text(text)
    expect(report.password_is_hash, "32 位 hex 应识别成 md5 并原样保存")
    expect(any("md5" in note for note in report.notes), f"应提示是哈希值：{report.notes}")


def test_import_curl_rejects_garbage():
    for bad in ("", "随便一段话", "https://example.com/only-url-not-cur l"):
        try:
            importcurl.from_text(bad)
        except curlparse.CurlParseError:
            continue
        if bad.startswith("https://example"):
            continue  # 只粘网址是允许的
        raise AssertionError(f"这段不该被当成登录请求：{bad!r}")


def test_import_curl_cli_preview():
    """CLI 的 import-curl 在预览模式下不能落盘，也不能打印密码。"""
    import login as cli

    with tmpdir() as tmp:
        path = os.path.join(tmp, "curl.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(IMPORT_CURL)
        out = io.StringIO()

        class Args:
            file = path
            save = False
            config = os.path.join(tmp, "config.json")

        with contextlib.redirect_stdout(out):
            code = cli.cmd_import_curl(Args())
        expect(code == 0, f"预览应成功：{code}")
        text = out.getvalue()
        expect("Plain!Pass1" not in text, f"输出里不能出现明文密码：{text[-200:]}")
        expect(not os.path.exists(Args.config), "预览模式不该写配置")


def test_clipboard_reader_is_safe():
    """读剪贴板不能崩进程：非 Windows 要报错而不是崩，Windows 上要返回 str。

    这条是回归测试——之前 ctypes 没声明 restype，64 位下 HANDLE 被截断成 32 位，
    GlobalLock 直接访问冲突（exit 0xC0000005）。
    """
    if os.name != "nt":
        try:
            importcurl.read_clipboard()
        except RuntimeError:
            return
        raise AssertionError("非 Windows 上读剪贴板应抛 RuntimeError")
    text = importcurl.read_clipboard()
    expect(isinstance(text, str), f"应返回字符串，实际 {type(text)}")


def test_runner_skips_when_online():
    with MockPortal() as srv:
        cfg = make_cfg(srv.base_url)
        outcome = runner.run_login(cfg, USERNAME, PASSWORD)
        expect(outcome.code == 0 and outcome.already_online, f"已联网时应当跳过：{outcome}")
        expect(not srv.logged_in, "已联网却还是发了登录请求")


def test_runner_login_with_force():
    with MockPortal() as srv:
        cfg = make_cfg(srv.base_url)
        outcome = runner.run_login(cfg, USERNAME, PASSWORD, force=True)
        expect(outcome.code == 0, f"强制登录应该成功：{outcome.message}")


# ------------------------------------------------------------------ CLI

def test_cli_end_to_end():
    with MockPortal() as srv, tmpdir() as tmp:
        cfg = make_cfg(srv.base_url)
        cfg_path = os.path.join(tmp, "config.json")
        cred_path = os.path.join(tmp, "credentials.dat")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False)
        secrets.save(USERNAME, PASSWORD, path=cred_path)

        env = dict(os.environ)
        env.update(
            {
                "CAMPUSNET_CONFIG": cfg_path,
                "CAMPUSNET_CREDENTIALS": cred_path,
                "CAMPUSNET_STATE": os.path.join(tmp, "state.json"),
                "PYTHONIOENCODING": "utf-8",
            }
        )
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "login.py"), "--config", cfg_path, "login", "--force"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=120,
        )
        expect(proc.returncode == 0, f"CLI 退出码 {proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}")
        expect("成功" in (proc.stdout or ""), f"CLI 输出里没有成功提示：{proc.stdout}")


def test_cli_check():
    with MockPortal() as srv, tmpdir() as tmp:
        cfg = make_cfg(srv.base_url)
        cfg_path = os.path.join(tmp, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False)
        env = dict(os.environ)
        env.update(
            {
                "CAMPUSNET_CONFIG": cfg_path,
                "CAMPUSNET_CREDENTIALS": os.path.join(tmp, "none.dat"),
                "CAMPUSNET_STATE": os.path.join(tmp, "state.json"),
                "PYTHONIOENCODING": "utf-8",
            }
        )
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "login.py"), "--config", cfg_path, "check"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=120,
        )
        expect(proc.returncode == 0, f"check 退出码 {proc.returncode}: {proc.stdout} {proc.stderr}")
        expect("登录地址" in proc.stdout, f"check 输出不全：{proc.stdout}")


# ------------------------------------------------------------------ 自检

def test_doctor_all_ok():
    with MockPortal() as srv, tmpdir() as tmp:
        cfg_path = os.path.join(tmp, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(make_cfg(srv.base_url), fh, ensure_ascii=False)
        checks = doctor.run(cfg_path, probe=True)
        titles = [c.title for c in checks]
        expect("Python" in titles and "配置文件" in titles, f"自检项不全：{titles}")
        expect(all(c.status in (doctor.OK, doctor.BAD, doctor.INFO) for c in checks), "出现了未知状态")
        by_title = {c.title: c for c in checks}
        expect(by_title["Python"].status == doctor.OK, "Python 检查不该失败")
        expect(by_title["配置文件"].status == doctor.OK, f"配置读取失败：{by_title['配置文件'].detail}")
        # 配置里用了 {token}，要靠预跳转才拿得到 —— 自检应该自动跑一次预跳转而不是误报
        expect(by_title["请求模板渲染"].status == doctor.OK, f"模板渲染失败：{by_title['请求模板渲染'].detail}")
        expect("POST" in by_title["请求模板渲染"].detail, "模板渲染结果里没有请求方法")
        expect(by_title["联网探测"].status in (doctor.OK, doctor.INFO), "联网探测不该判为失败")
        expect(doctor.problems(checks) == [c for c in checks if c.status == doctor.BAD], "problems 过滤不对")
        expect("✓" in by_title["Python"].line(), "输出行缺对勾")


def test_doctor_offline_preflight():
    """认证页连不上时，不该把配置判成错的（否则用户会被误导）。"""
    with tmpdir() as tmp:
        cfg_path = os.path.join(tmp, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(make_cfg("http://127.0.0.1:9"), fh, ensure_ascii=False)  # 该端口没人监听
        checks = doctor.run(cfg_path, probe=True)
        by_title = {c.title: c for c in checks}
        expect(by_title["请求模板渲染"].status == doctor.INFO, f"离线时模板检查应为提示项：{by_title['请求模板渲染'].detail}")
        expect("配置文件" not in [c.title for c in doctor.problems(checks)], "配置本身没问题，不该报错")


def test_doctor_missing_config():
    checks = doctor.run(os.path.join(HERE, "no_such_config.json"), probe=False)
    bad = doctor.problems(checks)
    titles = [c.title for c in bad]
    expect("配置文件" in titles, f"缺配置时没报出来：{titles}")
    expect("请求模板渲染" in titles, "配置不可用时模板检查应标记为问题")
    for item in bad:
        expect(item.title in doctor.HINT, f"问题项 {item.title} 没有对应的处理建议")
        expect("✗" in item.line(), "问题项输出缺叉号")


# ------------------------------------------------------------------ 入口

def main() -> int:
    tests = [
        ("cURL 解析（bash 风格）", test_curl_bash),
        ("cURL 解析（cmd 风格）", test_curl_cmd),
        ("cURL 解析（JSON 请求体）", test_curl_json),
        ("占位符报错提示", test_render_unknown_placeholder),
        ("密码模板 md5 / sha1", test_password_template),
        ("成功失败判定", test_judge_rules),
        ("表单字段编码", test_form_encoding),
        ("原始模板参数编码", test_body_template_escaping),
        ("预览不泄露密码", test_preview_masks_password),
        ("密码别名也编码", test_secret_alias_escaping),
        ("JSON 请求体不编码密码", test_json_body_keeps_password_raw),
        ("配置结构写错给提示", test_config_type_errors),
        ("凭据文件结构容错", test_credentials_bad_shape),
        ("确定性失败不重试", test_definitive_failure_not_retried),
        ("账号密码 DPAPI 加密", test_dpapi_roundtrip),
        ("配置校验", test_config_validation),
        ("配置：BOM / 编码容错", test_config_bom_and_gbk),
        ("开机自启命令", test_autostart_command),
        ("开机自启状态读取", test_autostart_state),
        ("启动器编码与入口", test_vbs_launchers_utf16),
        ("零依赖：只用标准库", test_stdlib_only),
        ("桌面快捷方式生成", test_shortcut_creation),
        ("界面模块可导入", test_gui_imports),
        ("自定义钩子", test_custom_hook),
        ("端到端：登录成功", test_e2e_success),
        ("端到端：密码错误", test_e2e_wrong_password),
        ("端到端：账号不存在", test_e2e_unknown_user),
        ("端到端：缺少预跳转会失败", test_e2e_needs_preflight),
        ("端到端：重试配置", test_e2e_retry_then_success),
        ("失败提示：只说确定的方向", test_failure_hint),
        ("重试：指数退避与上限", test_retry_backoff),
        ("失败提示：密码错给人话", test_wrong_password_has_hint),
        ("CLI：log 看日志尾部", test_cli_log_tail),
        ("导入：cURL 自动分离账号密码", test_import_curl_placeholders),
        ("导入：JSON 请求体", test_import_curl_json_body),
        ("导入：哈希密码原样保存", test_import_curl_hash_password),
        ("导入：垃圾输入要拒绝", test_import_curl_rejects_garbage),
        ("导入：CLI 预览不落盘不打码", test_import_curl_cli_preview),
        ("导入：读剪贴板不崩进程", test_clipboard_reader_is_safe),
        ("流程：已联网时跳过", test_runner_skips_when_online),
        ("流程：强制登录", test_runner_login_with_force),
        ("CLI：login 退出码", test_cli_end_to_end),
        ("CLI：check 输出", test_cli_check),
        ("自检：全部正常", test_doctor_all_ok),
        ("自检：认证页连不上时只提示", test_doctor_offline_preflight),
        ("自检：缺少配置时报问题", test_doctor_missing_config),
    ]
    print(f"Python {sys.version.split()[0]}  项目目录 {ROOT}\n")
    try:
        for name, func in tests:
            check(name, func)
    finally:
        try:
            os.remove(TEST_STATE_FILE)
        except OSError:
            pass

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    skipped = sum(1 for _, ok, detail in RESULTS if ok and detail.startswith("跳过"))
    failed = len(RESULTS) - passed
    tail = f"，跳过 {skipped}" if skipped else ""
    print("\n" + "=" * 56)
    print(f"通过 {passed - skipped} / {len(RESULTS)}" + tail + (f"，失败 {failed}" if failed else "，全部通过 ✓"))
    if failed:
        print("\n失败项：")
        for name, ok, err in RESULTS:
            if not ok:
                print(f"  - {name}: {err}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
