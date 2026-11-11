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

凭据只放在 `dsh-home` 卷里（容器内的 `/home/matrix/.dsh`），不进镜像，也不进仓库。以下两种方式选一种：

- 在 `.env` 里写 `DEEPSEEK_API_KEY=...`，然后执行 `docker compose up -d` 让它生效。
- 进容器用 dsh 自己的方式登录或配置：`docker compose exec matrix dsh headless --help`。

## 预置模型（可选）

在数据卷里放一份 `/data/models.json`，云侧 Agent 就只能从里面选模型；改这个文件即生效，不用重启。删掉文件回到自由填写。
`provider` 必须是 dsh 里已配置的。

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
uv run matrix-dsh-check --dsh-bin /tmp/dsh-new/node_modules/.bin/dsh --live    # 实际跑：回复、续接、技能、委托、连接器、沙箱审批
```

`--live` 要 dsh 能调模型：本机单独装的 dsh 可能解析不了桌面 App 存的凭据，用账号登录的加 `--model deepseek-account/deepseek-flash`；
都不行就只在本机跑静态检查，实际检查留到容器里。

静态检查专门盯 dsh 的一个行为：覆盖层里写了不存在的插件 id 时 dsh 只警告、照常运行。被按权限禁用的插件在新版本里改了名，
就会在没有权限时重新可用，所以“找不到”算失败；新出现的 `tool-*` 插件给提醒，确认要不要按权限禁用（`runtime.AUTHORITY_PLUGINS` /
`ALWAYS_DISABLED`）再加进 `dsh_check.KNOWN_TOOLS`。

服务器上更新前先备份 `dsh-home`（新版本可能改会话格式），更新后在容器里再跑一次：

```bash
docker run --rm -v matrix_dsh-home:/d -v "$PWD":/b alpine tar czf /b/dsh-home.tgz -C /d .
cd /opt/matrix && git pull && cd deploy && docker compose up -d --build
docker compose exec matrix matrix-dsh-check --live
```

不想等仓库改版本号、临时换一个版本：`docker compose build --build-arg DSH_VERSION=<版本> && docker compose up -d`
（下次不带参数构建会回到 Dockerfile 里的版本）。有问题退回：把版本号改回去重新构建，必要时用备份恢复 `dsh-home`。

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

dsh 在 Linux 上用 bwrap 做沙箱。bwrap 起不来时 dsh 不会拒绝执行，而是退到一种更弱的方式：实测工作目录以外仍有能写的地方（如 `/tmp`），`matrix-dsh-check` 的 `sandbox` 一项会变红（[docs/cloud-runtime.md](../docs/cloud-runtime.md) 第 11 节）。所以下面两个安全选项不能去掉。bwrap 要在容器里创建命名空间，所以 `matrix` 容器设置了 `seccomp=unconfined` 和 `systempaths=unconfined`，同时用 `cap_drop: ALL` 去掉了全部特权，并以非 root 用户运行。这样会削弱容器自身的隔离，换来的是 Agent 执行的命令受到文件级的限制。

沙箱只限制写，不限制读，见 [docs/reference/security.md](../docs/reference/security.md) 的“已知风险”。

## 数据与备份

- `matrix-data` 卷存放 Matrix 的全部状态。
- `dsh-home` 卷存放 dsh 的会话和凭据。

备份示例：

```bash
docker run --rm -v matrix_matrix-data:/d -v "$PWD":/b alpine tar czf /b/matrix-data.tgz -C /d .
```
