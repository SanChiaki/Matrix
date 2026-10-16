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

## 更新

```bash
cd /opt/matrix && git pull && cd deploy && docker compose up -d --build
```

## 证书

`MATRIX_SITE` 默认是服务器 IP。裸 IP 拿不到公开证书，Caddy 会用自己的内部 CA 签发，浏览器会提示证书不受信任，属于正常现象。改成域名（`MATRIX_SITE=matrix.example.com`，并把 DNS 指向服务器）之后，Caddy 会自动申请正式证书，这时要去掉 `Caddyfile` 里的 `local_certs` 和 `tls internal`。

## 沙箱

dsh 在 Linux 上用 bwrap 做沙箱。如果没有可用的沙箱，它会 fail closed，也就是拒绝执行命令，而不是不加限制地执行。bwrap 要在容器里创建命名空间，所以 `matrix` 容器设置了 `seccomp=unconfined` 和 `systempaths=unconfined`，同时用 `cap_drop: ALL` 去掉了全部特权，并以非 root 用户运行。这样会削弱容器自身的隔离，换来的是 Agent 执行的命令受到文件级的限制。

沙箱只限制写，不限制读，见 AGENTS.md 的“已知风险”。

## 数据与备份

- `matrix-data` 卷存放 Matrix 的全部状态。
- `dsh-home` 卷存放 dsh 的会话和凭据。

备份示例：

```bash
docker run --rm -v matrix_matrix-data:/d -v "$PWD":/b alpine tar czf /b/matrix-data.tgz -C /d .
```
