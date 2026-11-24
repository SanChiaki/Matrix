# 演示 SSO 的登录驱动包：公司统一登录接入的样板

“演示 SSO”是虚构的票据式统一登录（不是 OAuth2，协议写在 `matrix_login_demo/driver.py` 开头）：
登录后带一张一次性票据跳回来，应用的服务器拿票据去核实是谁。

公司的登录是 OAuth2 / OIDC 时多半不用写驱动，在 `login.json` 里配内置的 `oauth2` 就行；配不出来时复制这个目录，
照 [docs/login-driver-authoring.md](../../docs/login-driver-authoring.md) 改：换掉 `driver.py` 里标着「协议」的三处，改包名与 entry point。

```bash
uv run pytest -q examples/login-driver/tests       # 驱动自己的测试（在 Matrix 仓库根目录）
```

在浏览器里走一遍（两个终端）：

```bash
uv run --with-editable examples/login-driver python -m matrix_login_demo.devserver --port 9300
```

```bash
DEMO_SSO_KEY=demo-secret uv run --with-editable examples/login-driver matrix --runtime scripted --login examples/login-driver/login.json
```

打开 `http://127.0.0.1:8765`，点“用演示 SSO 登录”，在演示 SSO 的页面上选一个账号。

| 文件 | 是什么 |
| --- | --- |
| `pyproject.toml` | 包与 entry point `matrix.login_drivers`（Matrix 按它发现驱动） |
| `matrix_login_demo/driver.py` | 驱动：登录页地址、回调里的票据、向身份系统核实 |
| `matrix_login_demo/devserver.py` | 本地的演示 SSO：测试用，也能在浏览器里点 |
| `login.json` | 配这个驱动的登录配置（密钥经环境变量 `DEMO_SSO_KEY`） |
| `tests/test_demo_login_driver.py` | 结构检查与登录测试（Matrix 的 `uv run pytest` 也会跑它） |
