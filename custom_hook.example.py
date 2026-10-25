"""custom_hook.py 示例 —— 复制成 custom_hook.py 才会生效。

绝大多数学校用「粘贴登录请求 + 配置」就够了，用不到这个文件。
只有两种情况需要它：

  1. 网页 JS 对密码做了特殊加密（深澜 / 锐捷 / 城市热点的部分版本），
     抓到的密码字段是一串看不出规律的密文，而又不能仅用 md5/base64 复现；
  2. 登录成功的响应长得太奇怪，关键字和 JSON 规则都判定不了。

用法：
    copy custom_hook.example.py custom_hook.py
    然后按下面的函数签名改。

注意：本文件是纯 Python，请不要把明文密码写进来。
"""

import base64
import hashlib


def transform_password(username: str, password: str, variables: dict) -> str:
    """把明文密码变成学校要的那串东西。

    参数：
        username  —— 账号
        password  —— 明文密码（就是你在设置向导里填的那个）
        variables —— 目前已经算好的所有变量（ip、timestamp、md5 等），只读用

    返回：要放进请求里的密码字符串
    """
    # 例 2：AES 加密（需要额外装库：pip install pycryptodome）
    #
    # from Crypto.Cipher import AES
    # from Crypto.Util.Padding import pad
    # key = bytes.fromhex("在网页 JS 里找到的 key")
    # iv = b"0000000000000000"
    # cipher = AES.new(key, AES.MODE_CBC, iv)
    # data = pad(password.encode("utf-8"), 16)
    # return base64.b64encode(cipher.encrypt(data)).decode()

    # 例 3：不需要任何加密，直接返回明文
    # return password

    # 例 1：md5(密码 + 固定盐) —— 有些学校就是这么干的
    salt = "请换成网页 JS 里的盐值"
    _ = base64  # 备用，删掉也行
    return hashlib.md5((password + salt).encode("utf-8")).hexdigest()


def judge(status: int, body: str, expect: dict):
    """可选：自己判定成功/失败，返回 (是否成功, 说明)。

    返回 None 表示「拿不准」，程序会再去做联网探测复核。
    不实现这个函数就用配置里的成功/失败关键字和 JSON 规则。
    """
    if '"code":0' in body or '"code": 0' in body:
        return True, "code=0，登录成功"
    if "用户名或密码" in body:
        return False, "账号或密码不对"
    return None, ""
