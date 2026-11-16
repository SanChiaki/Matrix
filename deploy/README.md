# 部署 Matrix

`compose.yml` 起两个容器：

- `matrix`：应用本身，只在 compose 内部网络监听 8765，不对宿主机发布端口。
- `caddy`：唯一对外入口，开 80（跳转）和 443，提供 HTTPS，并在 Matrix 自己的登录前面再加一层 basic auth。`/internal/*` 对外一律返回 404。

## 第一次部署

```bash
git clone https://github.com/SanChiaki/Matrix.git /opt/matrix && cd /opt/matrix/deploy
docker run --rm caddy:2.10 caddy hash-password --plaintext '<basic auth 密码>'   # 生成哈希
cp env.example .env && chmod 600 .env   # 填 MATRIX_BASIC_HASH（每个 $ 写成 $$）；需要时改 MATRIX_SITE / MATRIX_BASIC_USER
docker compose up -d --build
```

服务器的安全组要放行 TCP 80 和 443。

临时关掉 basic auth：在 `.env` 里写 `MATRIX_BASIC_AUTH=off`，然后执行 `docker compose up -d`。关掉时不需要 `MATRIX_BASIC_HASH`，可以不填；只认 `on` / `off`，写成别的值 Caddy 会起不来，不会悄悄放开。关掉之后只剩 Matrix 自己的简易登录（没有 CSRF 防护、没有限流、令牌不过期），而 DSH 能在服务器上执行命令，所以这种状态只适合短时间的演示。

## 配置模型凭据（DSH）

凭据不进镜像，也不进仓库。以下两种方式选一种：

- 在 `.env` 里写 `DEEPSEEK_API_KEY=...`，然后执行 `docker compose up -d` 让它生效。
- 在管理后台的“模型”页添加供应商（协议、地址、密钥；密钥加密存在数据卷的 `vault/` 里，每轮只交给那一个 dsh 进程）。

**2026-11-16 起的破坏性变化**：云侧每个执行单元（席位、委托）用自己的 dsh 状态目录（`/data/dsh-state/`）当 HOME，
共用的 `dsh-home` 卷（`/home/matrix/.dsh`）不再给云侧运行用（[docs/cloud-runtime.md](../docs/cloud-runtime.md) 5.4、第 8 节阶段 0）。
**进容器用 dsh 自己登录或配置、存在 `dsh-home` 里的凭据与默认供应商从此失效**：升级前把要用的供应商在“模型”页重配一遍，
或改用 `.env` 里的 `DEEPSEEK_API_KEY`。只能用账号登录、给不出地址加密钥的供应商，云侧不能再用。
`dsh-home` 卷先别删：里面的旧会话在每个执行单元第一次运行时被拷进它自己的状态目录（原处不删，回退时还能用），之后就用不到了。

## 预置模型（可选）

在数据卷里放一份 `/data/models.json`，云侧 Agent 就只能从里面选模型；改这个文件即生效，不用重启。删掉文件回到自由填写。
`provider` 必须是管理后台“模型”页里配的供应商，或 dsh 自带、只靠环境变量就能用的（如 `deepseek-official` 配 `DEEPSEEK_API_KEY`）；
dsh 自己登录过、存在共用 `~/.dsh` 里的供应商云侧不再读。

```json
{
  "default": "flash",
  "models": [
    {"id": "flash", "label": "DeepSeek Flash", "provider": "deepseek-official", "model": "deepseek-flash"},
    {"id": "opus", "label": "Claude Opus", "provider": "hi-code-claude", "model": "claude-opus-5-5", "reasoning_effort": "high"}
  ]
}
```

从列表里删掉一个模型，用它的 Agent 下一轮起改用 `default`。文件写坏时服务沿用上一份并在日志里写原因。

更方便的是在管理后台（`/admin` 的“模型”页）编辑，改的是同一份文件。后台还能配**平台自己的供应商**（OpenAI / Anthropic 兼容的服务：
协议、地址、密钥），不用进容器改 dsh：密钥加密存在 `matrix-data` 卷的 `vault/` 里，`models.json` 里只有协议和地址。
保管密钥（`MATRIX_VAULT_KEY` 或数据卷里的 `vault.key`）丢了，就要在后台重新填一遍各供应商的密钥。

供应商可以带 `compat`：dsh `llm-pi-ai` 的协议兼容开关，原样交给 dsh。非标准的 OpenAI 兼容端点（如 glm 系列）不配它时每轮都是
`INVALID_REQUEST: 400`。后台供应商表单里的“兼容开关”就是它，写在文件里是：

```json
{"id": "modelada", "name": "ModelAda", "api": "openai-completions", "base_url": "https://model.example.internal/v1",
 "compat": {"thinkingFormat": "zai", "supportsReasoningEffort": true, "maxTokensField": "max_tokens",
            "supportsDeveloperRole": false, "supportsStore": false}}
```

Matrix 只查形状；键名写错时那一轮 dsh 会报出是哪个键、能用的有哪些。

## 内网抓取白名单（可选）

dsh 自带的 `web_fetch` 只抓公网地址，内网的域名和 IP 一律拒绝，也没有白名单可配。要让云侧 Agent 抓内网页面，
在数据卷里放一份 `/data/fetch.json`；改这个文件即生效，不用重启。删掉文件回到 dsh 自带的 `web_fetch`。

```json
{
  "allow": ["wiki.corp.example", "*.svc.internal", "10.20.0.0/16", "192.168.1.8"],
  "public": true,
  "replace_builtin": true
}
```

- `allow`：能抓的内网目标。域名含子域名（`*.x` 与 `x` 一样），也可以写 IP 或网段。
- `public`：也抓公网地址（默认是）。
- `replace_builtin`：关掉 dsh 自带的 `web_fetch`（默认是），只留 Matrix 的 `mcp__matrix_web__fetch`，`web_search` 不受影响。
  两个都在时模型常挑自带的那个，在内网地址上碰壁，所以默认关掉。

只对云侧、有 `web.research` 权限的 Agent 生效。内网服务用自签证书时，见下面的「内网 CA / 自签证书」。
升级 dsh 后跑 `matrix-dsh-check`，`web-fetch` 一项确认自带的 `web_fetch` 仍然关得掉。

## 内网连接器（可选）

部署方目录 `/data/connectors.json` 里的连接器地址本来就可以是内网、可以用 `http://`。用户自己加的**自定义连接器**
（我的连接器、项目里写的 MCP 配置）默认只能连 `https://` 公网地址；要让它们连内网的 MCP 服务，在同一个文件顶层写 `allow`：

```json
{
  "connectors": [],
  "allow": ["mcp.corp.example", "*.svc.internal", "10.20.0.0/16"]
}
```

- 写法同 `fetch.json` 的 `allow`：域名含子域名，也可以写 IP 或网段。写进来的主机可以是内网地址、可以用 `http://`。
- 改了即生效；删掉一项后，已经连好的连接下一次调用就连不上。
- 连接直连校验过的地址、不跟重定向、不走代理。内网服务用自签证书时，见下面的「内网 CA / 自签证书」。

环境里配了代理（`HTTPS_PROXY` / `HTTP_PROXY`）时，目录里的连接器默认**内网地址直连、其余走代理**，不用为内网服务配 `NO_PROXY`。
内网指：`allow` 里的主机、写成非公网 IP 的、服务机本机解析出非公网地址的（本机解析不了的域名交给代理）。要改就写 `proxy`：

```json
{
  "proxy": "auto",
  "connectors": [
    {"id": "wiki", "name": "内部 Wiki", "url": "http://wiki-mcp.corp:8080/mcp", "proxy": "direct"}
  ]
}
```

| 值 | 做法 |
|---|---|
| `auto`（默认） | 内网地址直连，其余照环境变量（`NO_PROXY` 照常生效） |
| `env` | 全照环境变量，和以前一样（内网地址要自己写进 `NO_PROXY`） |
| `direct` | 一律直连，不走代理 |

顶层的对所有目录连接器生效，单个连接器写的 `proxy` 盖过顶层的；改了即生效。用户自己加的自定义连接器从不走代理，不受它影响。

## 出网代理（可选）

服务器只能经代理出网时，代理写在 `deploy/.env` 里（compose 用 `env_file` 交给 matrix 容器），写完执行 `docker compose up -d`
重建容器（只 `restart` 不会重读 `.env`）：

```
HTTPS_PROXY=http://proxy.corp.example:3128
HTTP_PROXY=http://proxy.corp.example:3128
NO_PROXY=localhost,127.0.0.1,.corp.example
```

不用 Docker 时，在启动 `matrix` 的那个 shell 或 systemd 单元的 `Environment=` 里设同样的变量，重启服务生效。
Matrix 的网页抓取、连接器和远程插件服务都读这几个变量。连接器与远程插件服务另有 `proxy` 开关，
默认 `auto`：内网地址直连、不用写进 `NO_PROXY`。目录连接器的在 `connectors.json` 里改（见上面「内网连接器」）；
远程插件服务的在管理后台「插件」页 →「远程插件服务走不走代理」改（存在 `/data/plugin-policy.json` 的 `proxy`），改了即生效。

## 内网 CA / 自签证书

内网的模型供应商、抓取目标用自签 CA 时，把根 CA（PEM，扩展名 `.crt`）放进 `deploy/ca-certificates/`，然后
`docker compose up -d --build`：构建时加进镜像的系统证书库（构建后面几步的 npm / PyPI 下载也用它）。

不同客户端读证书的方式不一样，排查时别被 curl / pip 误导：

| 谁 | 用什么 | 读系统证书库 / `SSL_CERT_FILE` |
|---|---|---|
| dsh（连模型供应商、自带的 `web_fetch`） | Node | 默认不读，只信 Node 自带的 CA 包。镜像里设了 `NODE_USE_SYSTEM_CA=1` 才读系统库；**`SSL_CERT_FILE` 对它无效** |
| uv（构建时装 Python 依赖） | Rust | 默认不读，只信自带的 CA 包。镜像里设了 `UV_SYSTEM_CERTS=1` 才读系统库（报 `invalid peer certificate: UnknownIssuer` 就是它） |
| Matrix 的抓取（`mcp__matrix_web__fetch`）、云端连接器、pip | Python（OpenSSL） | 读系统库，也认 `SSL_CERT_FILE` |
| curl、openssl | OpenSSL | 读系统库 |

所以 curl 能通、dsh 报 `TRANSPORT: Connection error` 时，多半是 Node 不信这张 CA。确认：

```bash
docker compose exec matrix node -e 'require("https").get(process.argv[1], r => console.log("OK", r.statusCode)).on("error", e => console.log("ERR", e.code || e.message))' https://<模型地址>/v1
```

`SELF_SIGNED_CERT_IN_CHAIN` / `UNABLE_TO_GET_ISSUER_CERT_LOCALLY` 就是证书没进 Node。不想重建镜像时，也可以把 CA 挂进容器、
在 `.env` 里写 `NODE_EXTRA_CA_CERTS=/path/to/ca.pem`（在 Node 自带的 CA 包之外追加，只对 dsh 生效）。

## 内网 / 无公网构建

构建要访问 apt（deb.debian.org）、NodeSource、npm、PyPI。只有内网镜像时在 `.env` 里写（都空着 = 官方源）：

| 变量 | 作用 |
|---|---|
| `MATRIX_APT_MIRROR` | Debian 镜像根地址（其下有 `debian`、`debian-security`），替换 deb.debian.org |
| `MATRIX_NODE_TARBALL` | Node 官方二进制包（`node-v24.*-linux-x64.tar.xz`）的地址，代替 NodeSource（内网通常只镜像 nodejs.org） |
| `MATRIX_NPM_REGISTRY` | 装 dsh 用的 npm 源 |
| `MATRIX_PYPI_INDEX` | 装 uv 与 Python 依赖用的 PyPI 源 |

设了 `MATRIX_PYPI_INDEX` 时镜像改用 `uv sync --no-dev`（不带 `--frozen`）：`--frozen` 直接用 `uv.lock` 里写死的
`files.pythonhosted.org` 下载地址，不看 index 配置，换了源也照样去公网；不 frozen 时从配置的源重新解析，已锁定的版本优先。
（自己改 Dockerfile 换源时同理，`UV_DEFAULT_INDEX` 加 `--frozen` 不生效。）

`MATRIX_PYPI_INDEX` 可以是 http 源：镜像给 pip 加 `--trusted-host`（不加时 pip 只打一行 WARNING 就跳过这个源，接着报
“找不到 uv”，像是源里没有这个包），也给 uv 设上 `UV_INSECURE_HOST`。这两个开关对 https 源会关掉证书校验，所以只在 http 源上加；
https 源的自签 CA 放进 `deploy/ca-certificates/`。

构建最后会确认 `dsh` 在 PATH 上（Node 装在别的前缀时补一个链接），找不到就构建失败，不会等到 Agent 第一轮才报 `FileNotFoundError`。

**非标准的 OpenAI 兼容端点**（glm 等）每轮都报 `INVALID_REQUEST: 400` 时，多半要在后台的供应商上配“兼容开关”
（dsh `llm-pi-ai` 的 `compat`，见上面「预置模型」）。

## IM 入口（可选）

企业微信是内置的：在后台 `/admin#im` 添加部署方的机器人，或让每个人在“助理与定时 → 在 IM 里找助理”接自己的（[docs/reference/im.md](../docs/reference/im.md)）。
连接是从容器出站的长连接，不用开端口；私有化部署的企业微信在 `.env` 里写 `MATRIX_WECOM_URL`。

企业内部 IM 按 [docs/im-driver-authoring.md](../docs/im-driver-authoring.md) 写成驱动包，在 `.env` 里写 `MATRIX_IM_DRIVERS`
（pip 安装参数，空格隔开，例如 `acme-im==1.2.0` 或 `git+https://git.corp.example/im/acme-im.git@v1.2.0`）和驱动要的环境变量，
然后 `docker compose up -d --build`：构建镜像时装进去。后台“IM 机器人”页首没有“有 IM 驱动没加载上”就是装好了。
只支持一个 Matrix 进程：同一个机器人两条连接会互相顶掉。

## 更新

```bash
cd /opt/matrix && git pull && cd deploy && docker compose up -d --build
```

从改名前（代码包 `agentnet`）的版本升级：镜像里的环境变量已改为 `MATRIX_DATA`、`MATRIX_HOST`，数据卷不变。
`.env` 里自己加过的 `AGENTNET_*`（`DATA`、`HOST`、`MODELS`、`CONNECTORS`、`PLUGINS`、`VAULT_KEY`）在新名字没设时照样生效
（`matrix/compat.py`），方便时改成 `MATRIX_*`。已配对的电脑升级连接器后运行一次 `matrix-connect install`（不重新配对），
见 [docs/reference/device-runtimes.md](../docs/reference/device-runtimes.md)。

## 升级 dsh

云端 dsh 的版本钉在 [Dockerfile](Dockerfile) 的 `ARG DSH_VERSION`，`git pull` 不会顺带升级它。仓库里改这个版本号时，
先在本机对新版本跑一遍冒烟检查（`matrix-dsh-check`，见 `matrix/dsh_check.py`）：

```bash
npm install --prefix /tmp/dsh-new @deepseek-ai/dsh@<版本>
uv run matrix-dsh-check --dsh-bin /tmp/dsh-new/node_modules/.bin/dsh           # 静态：插件 id、覆盖层能加载，不调模型
uv run matrix-dsh-check --dsh-bin /tmp/dsh-new/node_modules/.bin/dsh --live    # 实际跑：回复、续接、技能、委托、连接器、工作目录以外写不进、提问
```

`--live` 要 dsh 能调模型：本机单独装的 dsh 可能解析不了桌面 App 存的凭据，用账号登录的加 `--model deepseek-account/deepseek-flash`；
都不行就只在本机跑静态检查，实际检查留到容器里。

静态检查专门盯 dsh 的一个行为：覆盖层里写了不存在的插件 id 时 dsh 只警告、照常运行。被按权限禁用的插件在新版本里改了名，
就会在没有权限时重新可用，所以“找不到”算失败；新出现的 `tool-*` 插件给提醒，确认要不要按权限禁用（`runtime.AUTHORITY_PLUGINS` /
`ALWAYS_DISABLED`）再加进 `dsh_check.KNOWN_TOOLS`。

服务器上更新前先备份 `matrix-data`（新版本可能改会话格式；云侧的 dsh 会话在 `/data/dsh-state/`），更新后在容器里再跑一次：

```bash
docker run --rm -v matrix_matrix-data:/d -v "$PWD":/b alpine tar czf /b/matrix-data.tgz -C /d .
cd /opt/matrix && git pull && cd deploy && docker compose up -d --build
docker compose exec matrix matrix-dsh-check --live
```

不想等仓库改版本号、临时换一个版本：`docker compose build --build-arg DSH_VERSION=<版本> && docker compose up -d`
（下次不带参数构建会回到 Dockerfile 里的版本）。有问题退回：把版本号改回去重新构建，必要时用备份恢复 `matrix-data`。

## 证书

由 `deploy/.env` 里的 `MATRIX_TLS_ISSUER` 决定：

- `internal`（默认）：Caddy 自己的内部 CA。适合只有裸 IP 的情况，浏览器会提示证书不受信任，属于正常现象。
- `acme`：Let's Encrypt 正式证书，浏览器直接信任。要求 `MATRIX_SITE` 是能从公网解析到这台服务器的域名，且 80 / 443 对外开放。

没有自己的域名时可以用 [nip.io](https://nip.io)：它是通配 DNS，`47-88-79-27.nip.io` 直接解析到 `47.88.79.27`，不转发流量、不签证书。

```bash
MATRIX_SITE=47-88-79-27.nip.io
MATRIX_TLS_ISSUER=acme
```

改完执行 `docker compose up -d --force-recreate caddy`。nip.io 是多人共用的域名，Let's Encrypt 按注册域名限流，签发失败时 Caddy 日志会写原因；可以改用 sslip.io，或者换成自己的域名。

## 端侧连接器

用户在自己的电脑上把 Claude Code、Codex 或 dsh 接进 Matrix：在「设备」页签发一个令牌，然后在本机运行

```bash
uvx --from git+https://github.com/SanChiaki/Matrix.git matrix-connect \
  --url wss://<MATRIX_SITE>/api/devices/connect --token <令牌> --driver claude-code
```

- 连接器**只发出站** WebSocket，不开入站端口；Caddy 的 `reverse_proxy` 自动处理升级，不需要额外配置。
- 令牌可随时在「设备」页吊销；吊销后连接立即断开，绑在该设备上的 Agent 进入等待。
- 如果开着 basic auth（`MATRIX_BASIC_AUTH=on`），连接器还要带上 basic auth 凭据，否则握手会被 Caddy 拒绝；
  简化起见可以在演示时关掉 basic auth，只依赖 Matrix 自己的登录。
- 设备离线不是故障：任务进入 `waiting`，不消耗重试，连接器重连后继续。

## 沙箱

### 云侧读隔离（外层沙箱）

云侧 dsh 每一轮外面再包一层 bwrap（[docs/cloud-runtime.md](../docs/cloud-runtime.md) 第 8 节阶段 0）：Agent 的命令只看得到系统目录、
Python 环境与 Matrix 代码、dsh 与 node（只读），这个席位或委托的工作目录和它自己的 dsh 状态目录（读写）；数据卷的其余部分、
别的执行单元、`vault.key`、`dsh-home` 在里面不存在，也看不到沙箱之外的进程。网络仍共享（要回调 Matrix、调模型）。开关是 `.env` 里的：

| `MATRIX_CLOUD_ISOLATION` | 外层可用 | 外层不可用 |
| --- | --- | --- |
| `preferred`（默认） | 套上 | 照旧跑，启动日志、管理后台“模型”页与 Agent 页写明“没有读隔离” |
| `required` | 套上 | 云侧运行一律拒绝，失败说明里写原因（端侧不受影响） |
| `off` | 不套 | 不套 |

写错的值按 `required` 处理。多用户部署、接真实账号的建议写 `required`。现在的 compose（`seccomp=unconfined`、`systempaths=unconfined`、
非 root）下外层可用，已实测（部署镜像里 `matrix-dsh-check --live` 全部通过，含 `read-isolation`、`proc-isolation`）；
去掉这两项后外层与内层都起不来。是否生效看 `docker compose logs matrix` 开头的“云侧读隔离”一行，或跑 `matrix-dsh-check`。
外层不设内存与 CPU 上限（Node 预留的虚拟地址空间很大，设地址空间上限 dsh 会起不来）；配额要等阶段 1 的 cgroup。

### 云侧执行面：沙箱方式（试用，默认关）

阶段 1a（[docs/cloud-runtime.md](../docs/cloud-runtime.md) 第 8 节“阶段 1a 的实现”）：每个席位或委托一个沙箱，沙箱里是端侧客户端的同一套代码，
连回 Matrix 的 `/api/sandbox/connect` 领活；只读材料按清单 + 缓存下发，产物与 dsh 会话每轮结束存进检查点。打开方法：`.env` 里写

```
MATRIX_CLOUD_SANDBOX=local          # off（默认）= 照旧；local = 每个执行单元一个本机子进程沙箱（外层 bwrap）
# 可选：
MATRIX_SANDBOX_MAX=28               # 全局同时在的沙箱数，默认按 (内存 − 2 GB) / 512 MB 估
MATRIX_SANDBOX_PER_USER=4           # 每个用户同时在的沙箱数
MATRIX_SANDBOX_IDLE=600             # 空闲多少秒停沙箱（目录保留）
MATRIX_SANDBOX_RETAIN=604800        # 执行单元结束后多少秒销毁沙箱目录（检查点留着）
MATRIX_CHECKPOINT_LIMIT=1000000000  # 每个执行单元的检查点最多存多少字节的产物，超出的只记清单
MATRIX_SANDBOX_ROOT=                # 执行面目录树，默认 /data/exec
```

或命令行 `matrix --sandbox local`。要求与现在的外层沙箱一样（Linux、bwrap 能在容器里建命名空间，compose 现在的安全选项下可用）；
起不来时云侧运行失败并写明原因，不会悄悄退回不隔离的子进程。满了的时候委托等待、席位排队，不算失败。

- 打开后第一次运行的执行单元会把旧工作目录的产物、阶段 0 状态目录（或 `dsh-home`）里的 dsh 会话导入检查点，原目录不删，会话续得上。
- 新增的数据：`/data/exec/`（沙箱的工作目录与状态，可以不备份，能从检查点重建）、`/data/checkpoints/` 与 `/data/content/`（检查点，**要备份**）、
  `/data/run-logs/`（dsh 事件）。
- 没做的：配额（local 后端没有 cgroup 配额，docker 后端有，见下一节）、批量建检查点的 `matrix migrate workdirs` 与导出回退。回退就是关掉开关：旧方式照旧用原来的工作目录，
  沙箱方式期间新写的产物只在检查点里。
- 升级 dsh 时加跑 `matrix-dsh-check --live --sandbox local`。

### 云侧执行面：docker 后端（每个执行单元一个容器）

阶段 1b（[docs/cloud-runtime.md](../docs/cloud-runtime.md) 第 8 节“阶段 1b 的实现”）：每个席位或委托一个容器，生产用 gVisor（`runsc`）。
容器由沙箱管理器 `sandboxd` 创建，它是唯一挂 `docker.sock` 的服务；Matrix 自己不再跑 dsh，也就不再需要放开 seccomp 与 `/proc` 遮挡。

**1. 装 gVisor**（宿主上，Debian / Ubuntu；其他发行版见 <https://gvisor.dev/docs/user_guide/install/>）：

```bash
curl -fsSL https://gvisor.dev/archive.key | sudo gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" | sudo tee /etc/apt/sources.list.d/gvisor.list
sudo apt-get update && sudo apt-get install -y runsc
sudo runsc install && sudo systemctl restart docker
docker info --format '{{json .Runtimes}}'    # 里面要有 "runsc"
```

**2. 写管理器配置并构建镜像**（在 `deploy/` 下）：

```bash
cp sandboxd.example.json sandboxd.json       # runtime 默认 runsc；镜像名、配额、沙箱里固定的环境（出网代理、CA）在这里改
docker compose -f compose.yml -f compose.sandbox.yml --profile build build
```

`sandboxd.json` 里能改的：`namespace`（部署名：容器、卷的名字与标签都带它，管理器只认自己名下的沙箱；同一个 Docker 上跑几套 Matrix 时各写各的）、`image`、`runtime`（`runsc` / `insecure-runc`）、`network`、`connect_url`（沙箱客户端连回来的地址，和
`.env` 的 `MATRIX_SANDBOX_GATEWAY` 一致）、`workdir_prefixes`、`cpus` / `memory_mb` / `pids` 与对应的 `max_*`、`tmp_mb`、`env`（不能放密钥，
名字带 KEY / TOKEN / SECRET / PASSWORD 的会被拒）。镜像、命令、挂载、特权都只能在这里定，Matrix 服务发来的请求改不了。
内网构建的参数与主镜像相同，另有 `DOCKER_CLI_TARBALL`（管理器镜像里 docker 命令行的静态包地址）。

**3. 打开**：

```bash
docker compose -f compose.yml -f compose.sandbox.yml up -d
docker compose -f compose.yml -f compose.sandbox.yml logs sandboxd | head    # 第二行写着隔离等级
```

叠加文件会把 `matrix` 改成 `MATRIX_CLOUD_SANDBOX=docker`、挂上管理器的 socket 目录、在沙箱网络网关（默认 `172.31.255.1`）上开放 8765、
安全选项改成只有 `no-new-privileges`。沙箱网络 `matrix-sandbox`（默认 `172.31.255.0/24`）和机器上已有的网络冲突时，在 `.env` 里改
`MATRIX_SANDBOX_SUBNET` / `MATRIX_SANDBOX_GATEWAY`，同时改 `sandboxd.json` 的 `connect_url`。

- **`insecure-runc`**：没装 gVisor 时可以先用 runc 试（`sandboxd.json` 写 `"runtime": "insecure-runc"`）。这一档沙箱容器与宿主共用内核，
  而且为了让 dsh 的内层 bwrap 能建命名空间，沙箱容器放开了 seccomp 与 `/proc` 遮挡（不放开 dsh 会悄悄退到更弱的沙箱）。
  管理后台、Agent 页、启动日志会一直写“未达到生产隔离”。**不要用在多用户部署上**。
- 配置的运行时不在（比如写了 `runsc` 但没装）：云侧运行一律拒绝并写明原因，不退回 runc。
- 沙箱经沙箱网络直接出网；服务器只能经代理出网时，代理地址要是沙箱网络能到的（不能是宿主的 127.0.0.1），写进 `sandboxd.json` 的 `env`。
- 配额：CPU、内存（不给 swap）、进程数由 cgroup 管；**卷没有大小上限**（Docker 的 local 卷做不到）。
- 数据：沙箱的卷（`matrix-sbx-*`）只是缓存，能从检查点重建，不用备份；`/data/checkpoints/` 与 `/data/content/` 要备份。
- 升级 dsh：重建沙箱镜像（上面第 2 步），再跑 `deploy/isolation-check` 的 `t8`，或在宿主上
  `matrix-dsh-check --live --sandbox docker --listen <网关> --port <端口> --root <管理器允许的前缀下的目录>`。

**回退**：`docker compose -f compose.yml up -d`（不带叠加文件），`matrix` 回到在自己容器里跑 dsh 的方式（`compose.yml` 里的安全选项照旧）。
沙箱方式期间新写的产物只在检查点里（`/data/checkpoints/` + `/data/content/`）。留下的沙箱容器与卷：
`docker ps -aq --filter label=dev.matrix.ns=matrix | xargs -r docker rm -f`、`docker volume ls -q --filter label=dev.matrix.ns=matrix | xargs -r docker volume rm`（`matrix` 换成 `sandboxd.json` 的 `namespace`）。

**排错**

| 现象 | 处理 |
| --- | --- |
| 云侧运行失败，原因以 `ISOLATION` 开头 | 管理器配置的运行时不在：`docker info` 里没有 `runsc`，按第 1 步装好并重启 Docker |
| 失败原因是“连不上沙箱管理器” | `sandboxd` 没起来或 socket 卷没挂上：`docker compose … logs sandboxd` |
| 失败原因是“沙箱的客户端没有连上” | 沙箱连不回 Matrix：`connect_url` 与网关、端口不一致，或防火墙拦了沙箱网络到网关的 8765；`docker logs <沙箱容器>` 看客户端的报错 |
| 失败原因是“沙箱容器起不来” / “工作目录卷准备不了” | 镜像名不对或没构建（第 2 步）；`docker images | grep matrix-sandbox` |
| Agent 的命令报 Permission denied、dsh 起不来 | 用的是旧的沙箱镜像或改过 `tmp_mb` 之外的模板：重建镜像；`/tmp` 必须可执行（模板里已是 `exec`） |
| `matrix-dsh-check` 的 `isolation` 说“不在 dsh 的内层 bwrap 里” | runc 档下沙箱容器没放开 seccomp / `/proc` 遮挡（模板已带）；或宿主的 AppArmor 拦了用户命名空间：改用 gVisor |

### dsh 自己的沙箱

dsh 在 Linux 上用 bwrap 做沙箱。bwrap 起不来时 dsh 不会拒绝执行，而是退到一种更弱的方式：实测工作目录以外仍有能写的地方（如 `/tmp`），`matrix-dsh-check` 的 `sandbox` 一项会变红（[docs/cloud-runtime.md](../docs/cloud-runtime.md) 第 11 节）。所以下面两个安全选项不能去掉。bwrap 要在容器里创建命名空间，所以 `matrix` 容器设置了 `seccomp=unconfined` 和 `systempaths=unconfined`，同时用 `cap_drop: ALL` 去掉了全部特权，并以非 root 用户运行。这样会削弱容器自身的隔离，换来的是 Agent 执行的命令受到文件级的限制。

dsh 自己的沙箱只限制写，不限制读；读由上面的外层管。外层只做到“文件读隔离”：内核与网络仍共用、没有 cgroup 配额、模型密钥仍在 dsh 进程的环境变量里，
见 [docs/reference/security.md](../docs/reference/security.md) 的“已知风险”。

## 数据与备份

- `matrix-data` 卷存放 Matrix 的全部状态，包括云侧每个执行单元的 dsh 会话（`/data/dsh-state/`）。
- `dsh-home` 卷是升级前共用的 dsh 会话和凭据：云侧不再用它，只在旧会话第一次被拷走前要留着（凭据见「配置模型凭据」）。

备份示例：

```bash
docker run --rm -v matrix_matrix-data:/d -v "$PWD":/b alpine tar czf /b/matrix-data.tgz -C /d .
```
