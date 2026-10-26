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

临时关掉 basic auth：在 `.env` 里写 `MATRIX_BASIC_AUTH=off`，然后执行 `docker compose up -d`。关掉之后只剩 Matrix 自己的简易登录（没有 CSRF 防护、没有限流、令牌不过期），而 DSH 能在服务器上执行命令，所以这种状态只适合短时间的演示。

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

dsh 在 Linux 上用 bwrap 做沙箱。如果没有可用的沙箱，它会 fail closed，也就是拒绝执行命令，而不是不加限制地执行。bwrap 要在容器里创建命名空间，所以 `matrix` 容器设置了 `seccomp=unconfined` 和 `systempaths=unconfined`，同时用 `cap_drop: ALL` 去掉了全部特权，并以非 root 用户运行。这样会削弱容器自身的隔离，换来的是 Agent 执行的命令受到文件级的限制。

沙箱只限制写，不限制读，见 [docs/reference/security.md](../docs/reference/security.md) 的“已知风险”。

## 数据与备份

- `matrix-data` 卷存放 Matrix 的全部状态。
- `dsh-home` 卷存放 dsh 的会话和凭据。

备份示例：

```bash
docker run --rm -v matrix_matrix-data:/d -v "$PWD":/b alpine tar czf /b/matrix-data.tgz -C /d .
```
