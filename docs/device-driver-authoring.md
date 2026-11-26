# 接业务系统自己的设备端 Agent：一键连接与外部驱动

给要把自家的设备端 Agent（装在用户电脑上的程序）接进 Matrix 的业务系统：写驱动的人、业务系统后端、部署方。
做完之后，用户在业务系统里点一下“连接到 Matrix”，他电脑上的连接器就配好对、连上来，Matrix 里多一个绑在这台电脑上的 Agent，
全程不用输配对码，也不用去 Matrix 网页里建 Agent。

- 仓库里的是通用的部分：一次性配对令牌（第二节）、外部驱动的接口与加载（第三、四节）、把驱动打进独立程序（第五节）。
  你们 Agent 的驱动留在内网，不进这个仓库。
- 样板：[examples/device-driver](../examples/device-driver)（一个虚构的 `demo-agent` 命令的驱动，带一个假 Agent 和测试）。
- 设备侧的整体设计见 [device-runtimes.md](device-runtimes.md)（第 2.5 节设备码配对、第 2.8 节一次性令牌、第 3 节能力与权限）。
  改了驱动接口（`matrix/connector/drivers/base.py`）、外部驱动的加载约定或打包方式，要同步改这份文档与样板。

## 一、整体流程

```
业务系统前端        业务系统后端                     Matrix                      用户电脑
  点“连接” ──→  POST /api/devices/enroll  ──→  签一次性令牌（10 分钟、单次）
                (X-Matrix-Key + X-Matrix-User)  ←── {code}
                把 code 交给自家的设备端程序（它自己的通道）  ─────────────→  matrix-connect setup <服务器> --code <code>
                                                POST /api/devices/enroll/redeem  ←──  {code, hostname, harnesses}
                                                建设备 + 绑在它上面的 Agent  ──→  设备令牌（只交付这一次）
                                                /api/devices/connect（WebSocket）←──  连接器照常连上、一直在线
```

Matrix 给 Agent 派活时，连接器把这一轮交给**驱动**，驱动调起你们的 Agent、把它的输出报告回来。驱动就是第三节要写的东西。

## 二、一次性配对令牌

### 签令牌（业务系统后端）

```bash
curl -X POST https://matrix.corp.example/api/devices/enroll \
     -H "X-Matrix-Key: $BIZ_MATRIX_KEY" -H "X-Matrix-User: 8f3a2c01" -H "X-Matrix-User-Email: wang@corp.example" \
     -H "Content-Type: application/json" \
     -d '{"name": "王工的电脑",
          "agent": {"name": "报销助手", "description": "在这台电脑上处理报销单据", "harness": "biz-agent"}}'
# → {"code": "…43 个字符…", "expires_in": 600}
```

- 鉴权就是普通的 `/api/*` 鉴权：已登录的网页可以签，业务系统后端用临时方案的共享密钥替用户签
  （[login-driver-authoring.md](login-driver-authoring.md) 第九节），以后换成第八节也一样。**签令牌的人就是设备和 Agent 的所有者。**
- `name`：设备名；不写就用连接器报告的（主机名）。
- `agent`：可选。兑换时建好、绑到这台设备上。字段同 `POST /api/agents`（`name`、`description`、`capabilities`、`authority`、
  `chat_enabled`、`timeout_s`、`max_delegations`），另加 `harness`：设备上的哪个驱动，不写 = 设备的默认（第一个检测到的）。
  `harness` 必须是服务器认得的（第四节），否则签的时候就 400。
- 令牌高熵、10 分钟有效、只能兑换一次；每人同时最多 16 条没用过的。只在服务器内存里：服务重启后没用过的作废，重新签一条即可。

### 交给设备

用你们自己的设备端程序已有的通道（它和业务系统后端之间本来就有的连接、推送，或本机自定义协议的深链），
把 `code` 和 Matrix 的地址交过去，然后由它运行：

```bash
matrix-connect setup https://matrix.corp.example --code <code>      # 配对 + 装成后台服务；加 --no-service 只配对
```

连接器检测本机装了哪些驱动，凭令牌换到设备令牌，写进本机配置（0600），装成登录后自动运行的后台服务。
之后和用设备码配对的设备完全一样：在设备页能看到、能吊销，断线自动重连。

- `code` 不要写进日志，也不要长期存。它只能用一次，泄露了的影响只在 10 分钟内、只能被换一次；
  而设备令牌是长期有效的，所以**不要**让业务系统后端直接调 `POST /api/devices` 拿设备令牌再转交（那条路能跑通，但长期凭据会经过后端和中间通道）。
- 指定的 `harness` 这台电脑上没有时，兑换返回 400 并说明它报告的是什么；令牌不作废，装好你们的 Agent 后用同一条令牌再跑一次即可。
- 不想经过命令行时，设备端程序也可以直接调 `POST /api/devices/enroll/redeem`
  `{"code", "name", "hostname", "harnesses"}`，拿回 `{token, device_id, device_name, email, agent_id}`，
  再按 `matrix/connector/config.py` 的格式写配置、自己拉起 `matrix-connect run`。一般没必要。

## 三、写驱动

驱动是一个 Python 类，放在你们自己的包里（照样板复制一份改）。接口在 `matrix/connector/drivers/base.py`：

```python
class BizAgentDriver:
    name = "biz-agent"                      # harness 名：2–40 位小写字母、数字、-、_，字母开头；定了就不要改

    def __init__(self, *, bin_path=None, bridge_url=""): ...
    def capabilities(self) -> RuntimeCapabilities: ...   # 能强制哪些权限（如实报告）
    def available(self) -> bool: ...                     # 本机装没装你们的 Agent
    def services(self) -> list[str]: ...                 # 它里面用户配置的外部服务名；没有就 []
    def run(self, spec: RunSpec, ctx: RunCtx, cancel: threading.Event) -> RunOutcome: ...
```

- **构造**：连接器按关键字参数 `bin_path`（`--bin` 给的路径，没给是 `None`）、`bridge_url` 构造外部驱动。
  `bridge_url` 是本机网络工具桥（MCP）的地址：你们的 Agent 能接 MCP 时把它交过去，就能用 Matrix 的网络工具
  （`delegate`、`send_file` 等，以 `spec.tool_names` 为准），做法参考 `drivers/claude.py`。接不了就不用管。
- **`run`**：一轮一次。`spec` 里有 `prompt`、`workdir`（这个 Agent 在本机的工作目录，材料已经下发到里面）、
  `session_id`（上一轮你们返回的会话 id，用来续接；没有就是新会话）、`timeout_s`、`authority`、`tool_names`。
  - 过程用 `ctx.emit({"type": "text", "text": …})` 实时报告，Matrix 里边跑边显示。
  - 拉起子进程的，用 `guard(proc, spec.timeout_s, cancel)` 看住它：超时或人终止时连接器会结束它。
  - 返回 `RunOutcome(final_text=最终答复, session_id=…, tool_calls=[…])`；失败返回 `ok=False` 和 `error`，
    `error` 以 `TIMEOUT:` / `SERVER …` 等开头时网络按 `runtime.ERROR_CLASSES` 归类（超时会续接一次收尾）。
- **`capabilities`**：如实报告。你们的 Agent 管不住的权限（写文件、跑命令、联网……）写 `unavailable`：
  网络会把绑在它上面的 Agent 的有效权限收窄掉，并在界面上说明，而不是假装它被限制住了（device-runtimes.md 第 3 节）。
  `approval` 写 `none`（运行中不会回来问人）；能续接会话就 `session_resume=True`。
- **不要**在驱动里读写连接器的配置和令牌、自己连 Matrix、自己批准什么：驱动只负责“把这一轮交给本机的 Agent，再把结果交回来”。

## 四、让连接器和服务器认它

两边都要配，缺一边 Agent 都绑不上。

**连接器**：驱动包在 `pyproject.toml` 里声明 entry point，装进 matrix-connect 所在的环境：

```toml
[project.entry-points."matrix.connect_drivers"]
biz-agent = "biz_matrix_driver.driver:BizAgentDriver"
```

- 外部驱动排在内置的（claude-code、codex、dsh）前面：电脑上装了你们的 Agent，它就是这台设备的默认 harness。
- 和内置驱动重名、加载出错的跳过，不影响连接器启动；`matrix-connect status` 会列出没加载上的和原因。
- 从源码装的连接器：`uv tool install <Matrix 仓库> --with <你们的驱动包>`。用免依赖的独立程序的，见第五节。

**服务器**：部署方用 `MATRIX_HARNESSES`（或 `matrix --harnesses`）告诉 Matrix 有这个 harness，以及界面上显示的名字：

```bash
MATRIX_HARNESSES="biz-agent=报销助手"          # 多个用逗号分隔；显示名可省
```

服务器不认的 harness，连接器报告了也会在配对时被过滤掉，签令牌时指定它会 400。内置的三个不能被覆盖。

## 五、把驱动打进独立程序

用户电脑上一般装的是免依赖的独立程序（device-runtimes.md 第 2.7 节），它看不到电脑上别处装的 Python 包，
所以要把驱动包打进去，出一份你们自己的安装包：

```bash
# 在 Matrix 仓库根目录；你们的驱动包在 ../biz-matrix-driver
MATRIX_CONNECT_EXTRA_DRIVERS=biz-matrix-driver \
  uv run --with pyinstaller --with ../biz-matrix-driver pyinstaller packaging/matrix-connect.spec --noconfirm
dist/matrix-connect/matrix-connect --help         # --driver 的选项里应该有 biz-agent
```

`MATRIX_CONNECT_EXTRA_DRIVERS` 写驱动包的**发行包名**（`pyproject.toml` 的 `project.name`），多个用逗号分隔。
配方会带上包的元数据（entry point 靠它发现）并收齐 entry point 指向的顶层包。

打好后按 `matrix-connect-<系统>-<架构>.tar.gz|zip`（Windows 是 zip）加 `.sha256` 的名字放进服务器的
`MATRIX_CONNECT_DIST` 目录：设备页上的安装命令、`/connect/install.sh` 和 `install.ps1` 就会下载你们这份，不再转去 GitHub。
包的目录结构照 `.github/workflows/connect-release.yml` 里打包那一步。

## 六、本地测

```bash
uv run pytest -q examples/device-driver/tests            # 样板自己的测试（在 Matrix 仓库根目录）
```

走一遍完整流程（三个终端；样板用假 Agent，换成你们的驱动和 Agent 同理）：

```bash
MATRIX_HARNESSES="demo-agent=演示助手" uv run --with-editable examples/device-driver matrix --runtime scripted
```

```bash
# 在网页里注册一个账号，然后用浏览器的开发者工具或 curl（带登录 cookie）签令牌：
curl -b <cookie> -X POST http://127.0.0.1:8765/api/devices/enroll -H 'Content-Type: application/json' \
     -d '{"agent": {"name": "演示助手", "description": "演示", "harness": "demo-agent"}}'
```

```bash
# 假 Agent 要在 PATH 上叫 demo-agent：样板的测试里有现成的包装（tests/test_demo_device_driver.py 的 fake_bin）
uv run --with-editable examples/device-driver matrix-connect setup http://127.0.0.1:8765 --code <code> --no-service
uv run --with-editable examples/device-driver matrix-connect run
```

在 Matrix 里和“演示助手”说话，回复应当是“已处理：<你说的话>”。

## 七、验收

- [ ] 业务系统里点“连接”，用户电脑上不用输任何东西；设备页出现这台电脑，harness 里有你们的驱动，在线。
- [ ] 自动建的 Agent 所有者是这个用户、绑在这台设备和你们的 harness 上；和它说话能得到你们 Agent 的回复，过程边跑边显示。
- [ ] 第二轮能续接上一轮的会话（`session_id`）。
- [ ] 人在 Matrix 里终止、超过 Agent 的超时，你们的 Agent 进程被结束。
- [ ] 电脑关机 / 断网时，发给 Agent 的消息排队等待，恢复后照常处理（不算失败）。
- [ ] 同一条 `code` 第二次兑换失败；超过 10 分钟兑换失败。
- [ ] 设备页吊销这台设备后，连接器连不上。
- [ ] macOS / Windows 上用你们打的独立程序各装一次。
