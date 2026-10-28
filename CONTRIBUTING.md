# 参与这个项目

欢迎提 Issue 和 PR。这个工具只做一件事：把浏览器里那次成功的登录，照原样再发一遍。

## 硬约束（PR 请守住）

- **只用 Python 标准库。** 不引入任何第三方依赖——这是"双击即用、不用 pip"的前提。
- **支持 Python 3.10+，Windows 10/11。** CI 会在这四个版本上跑：3.10 / 3.11 / 3.12 / 3.13。
- **不绑定厂商。** 新认证系统优先用 `config.json` 或 `custom_hook.py` 支持，而不是往核心里塞 if。

## 改完自己先跑

```
python tests\run_tests.py        # 应输出「通过 36 / 36，全部通过」
python tests\gui_smoke.py        # 界面冒烟，应输出「界面冒烟测试通过」
python login.py doctor --no-network
```

`tests/mock_portal.py` 是一个本地假认证服务器（会返回 `userip`/`token` 并用 JSESSIONID 校验），
大部分改动都能在这里验证，不需要真连校园网。

## 提 Issue 时带什么

- `python login.py doctor` 的输出（账号会打码，密码不会出现）
- 学校的认证系统大概属于哪家：深澜 / 锐捷 / H3C / 华为 / 城市热点 / 认不出
- 登录请求的**结构**，值全部换成 `x`：

  ```
  curl 'http://x/eportal/portal/login?userip=x&wlanacname=x' -X POST \
    -H 'Content-Type: application/x-www-form-urlencoded' \
    --data-raw 'userId=x&password=x&token=x'
  ```

- 失败时 `logs/login.log` 的最后几行

**不要贴真实密码、真实 token、真实学号或工号。** 仓库开了 secret scanning，但那是兜底，不是许可。

## 提交 PR

- 一次 PR 做一件事，说明里写清"原来的行为 / 现在的行为 / 为什么"
- 新增行为要有测试；改界面的话跑一遍 `tests\gui_smoke.py`
- 不要提交 `config.json`、`credentials.dat`、`logs/`、`state.json`、`python_path.txt`（都已在 `.gitignore` 里）
- 截图里的账号请用示例号（如 `20230001`）
