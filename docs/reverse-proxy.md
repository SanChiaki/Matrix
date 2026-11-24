# 对外暴露 Matrix：反向代理

> 入口见 [AGENTS.md](../AGENTS.md)。部署本身见 [deploy/README.md](../deploy/README.md)，安全边界见 [reference/security.md](reference/security.md)。

Matrix 只负责应用本身：`deploy/compose.yml` 只起 `matrix` 一个容器，端口默认只发布到宿主机的 `127.0.0.1:8765`。
怎么对外暴露（HTTPS、域名、证书、要不要在 Matrix 的登录前面再加一层认证）由部署方用自己环境里现成的方式解决：
Caddy、nginx、公司的网关、负载均衡都可以。下面第一节是**不管用哪种都要满足的要求**，第二节是一份能直接用的 Caddy 示例。

## 一、对反向代理的要求

| 要求 | 为什么 |
|---|---|
| **对外只给 HTTPS** | Matrix 自己只说 HTTP，登录 cookie 和令牌不能明文过公网 |
| **`/internal/*` 对外一律拒绝**（如返回 404） | 这些是容器里 dsh、MCP 服务回调用的（`/internal/tools`、`/internal/approval`、`/internal/question`、`/internal/connectors/*`），Matrix 只凭执行上下文令牌识别，不看来源地址，挡在外面是代理的事 |
| **SSE 不缓冲**：`/api/events`、`/svc/*` 的流式回复 | 缓冲了网页收不到实时事件，外部应用的流要等到结束才一次性到 |
| **读超时放宽到 15 分钟以上** | `/svc/…/chat` 一次服务调用可能跑很久（[open-api-integration.md](open-api-integration.md)） |
| **WebSocket 升级**：`/api/devices/connect` | 端侧连接器（`matrix-connect`）经它出站连进来 |
| **请求体上限不低于 100 MB** | 项目资料单个文件最多 100 MB（`models.FILE_LIMIT`），上传是一个请求体 |
| 加了额外的认证层（basic auth、SSO 网关）时，**放行 `/mcp/remote`、`/mcp/remote/*`、`/open/v1/*`、`/svc/*`** | 外部 Agent 平台回调（不变量 28）、外部应用（不变量 30）带的是 Matrix 签的密钥 / 应用令牌，过不了你的认证层，由 Matrix 自己鉴权 |
| 用公司账号登录（[login-driver-authoring.md](login-driver-authoring.md)）时：原样传 `Host`，`/api/auth/sso/*` 照常转发 | 发起与回调是普通的浏览器请求，不用单独加规则；回调地址按 `login.json` 的 `public_url` 拼，要和代理对外的地址一致 |
| 加了额外的认证层时，端侧连接器也要能过 | `/api/devices/connect` 照常经过认证层；连接器要带上那一层的凭据，或者也放行这条路径（它本来就凭设备令牌鉴权） |

代理和 Matrix 在同一台机器上时转到 `http://127.0.0.1:8765`；代理也跑在容器里时，让它加入 Matrix 的 compose 网络
`matrix_internal`，转到 `http://matrix:8765`，这时 `compose.yml` 里发布到宿主机的那一行可以删掉。
不要把 `MATRIX_BIND` 改成 `0.0.0.0` 直接对外：上面的要求一条都不满足。

## 二、示例：Caddy（HTTPS + basic auth）

这是 Matrix 最早部署时用的方式：一个单独的 Caddy 容器做唯一对外入口，开 80（跳转）与 443，签证书，在 Matrix 自己的简易登录
（没有 CSRF 防护、没有限流、令牌不过期）前面再加一层 basic auth。它和 Matrix 是两个 compose 项目，放在仓库外面的一个目录里，
例如 `/opt/matrix-edge/`，里面三个文件。

`compose.yml`：

```yaml
# 对外入口：加入 Matrix 的 compose 网络，用 matrix:8765 访问它
name: matrix-edge

services:
  caddy:
    image: caddy:2.10
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    environment:
      MATRIX_SITE: ${MATRIX_SITE:?在 .env 里设置 MATRIX_SITE（服务器 IP 或域名）}
      MATRIX_TLS_ISSUER: ${MATRIX_TLS_ISSUER:-internal}   # acme = Let's Encrypt（MATRIX_SITE 须是域名）
      MATRIX_BASIC_AUTH: ${MATRIX_BASIC_AUTH:-on}         # off = 关掉 basic auth（不安全）
      MATRIX_BASIC_USER: ${MATRIX_BASIC_USER:-matrix}
      MATRIX_BASIC_HASH: ${MATRIX_BASIC_HASH:-}           # caddy hash-password 生成；basic auth 开着时必填
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy-data:/data
      - caddy-config:/config
    networks: [matrix]

volumes:
  caddy-data:
  caddy-config:

networks:
  matrix:
    external: true
    name: matrix_internal
```

`Caddyfile`：

```caddyfile
# 唯一对外入口。证书签发方由 MATRIX_TLS_ISSUER 决定：
# - internal（默认）：Caddy 自己的内部 CA，适合裸 IP（浏览器会提示不受信任）；
# - acme：Let's Encrypt 正式证书，MATRIX_SITE 必须是能从公网解析到本机的域名，且 80 / 443 对外开放。
{
	# 按 IP 访问时客户端不发 SNI，Caddy 需要知道默认用哪张证书
	default_sni {$MATRIX_SITE}
}

# 外部平台回调（/mcp/remote，旧版 SSE 的消息地址在 /mcp/remote/messages）与外部应用（/open/v1/*、/svc/*）
# 由 Matrix 凭密钥 / 令牌鉴权，过不了 basic auth，放行；其余路径都要 basic auth。
# 片段只在被 import 时才解析：MATRIX_BASIC_AUTH=off 时不需要 MATRIX_BASIC_HASH。
(basic-auth-on) {
	@protected not path /mcp/remote /mcp/remote/* /open/v1/* /svc/*
	basic_auth @protected {
		{$MATRIX_BASIC_USER:matrix} {$MATRIX_BASIC_HASH}
	}
}
(basic-auth-off) {
}

{$MATRIX_SITE} {
	# 显式写 tls：default_sni 才会生成“空 SNI 也用这张证书”的连接策略
	tls {
		issuer {$MATRIX_TLS_ISSUER:internal}
	}

	# 写错（不是 on / off）时 Caddy 起不来，不会悄悄放开
	import basic-auth-{$MATRIX_BASIC_AUTH:on}

	# /internal/* 只给容器里的 dsh / MCP 回调用，不对外
	@internal path /internal/*
	respond @internal 404

	header {
		Strict-Transport-Security "max-age=31536000"
		X-Content-Type-Options "nosniff"
		X-Frame-Options "DENY"
		Referrer-Policy "no-referrer"
		-Server
	}

	# WebSocket（/api/devices/connect）的升级 reverse_proxy 自动处理；Caddy 默认不限请求体大小
	reverse_proxy matrix:8765 {
		# SSE（/api/events、/svc/*）不缓冲
		flush_interval -1
	}
}

# 其它 Host（例如 127.0.0.1、别的域名）：不转给 Matrix
:443 {
	tls internal
	respond 404
}
```

`.env`（`chmod 600`，不要提交到任何仓库）：

```bash
# 对外站点：服务器 IP 或域名
MATRIX_SITE=your-server-ip-or-domain
# 证书：internal = 内部 CA（裸 IP 用，浏览器会警告）；acme = Let's Encrypt（MATRIX_SITE 须是域名）
MATRIX_TLS_ISSUER=internal
# basic auth：用户名与 caddy hash-password 生成的 bcrypt 哈希。哈希里的每个 $ 写成 $$（compose 会插值 $）
MATRIX_BASIC_AUTH=on
MATRIX_BASIC_USER=matrix
MATRIX_BASIC_HASH=
```

启动（先起 Matrix，`matrix_internal` 网络才存在）：

```bash
docker run --rm caddy:2.10 caddy hash-password --plaintext '<basic auth 密码>'   # 生成哈希，填进 .env
cd /opt/matrix-edge && docker compose up -d
```

服务器的安全组放行 TCP 80 和 443。这时 Matrix 已经经 Caddy 对外，`deploy/compose.yml` 里发布到宿主机 127.0.0.1 的那一行可以留着
（只在本机能访问，方便排查），也可以删掉。

### 证书

由 `MATRIX_TLS_ISSUER` 决定：

- `internal`（默认）：Caddy 自己的内部 CA。适合只有裸 IP 的情况，浏览器会提示证书不受信任，属于正常现象。
- `acme`：Let's Encrypt 正式证书，浏览器直接信任。要求 `MATRIX_SITE` 是能从公网解析到这台服务器的域名，且 80 / 443 对外开放。

没有自己的域名时可以用 [nip.io](https://nip.io)：它是通配 DNS，`47-88-79-27.nip.io` 直接解析到 `47.88.79.27`，不转发流量、不签证书。

```bash
MATRIX_SITE=47-88-79-27.nip.io
MATRIX_TLS_ISSUER=acme
```

改完执行 `docker compose up -d --force-recreate caddy`。nip.io 是多人共用的域名，Let's Encrypt 按注册域名限流，签发失败时 Caddy 日志会写原因；
可以改用 sslip.io，或者换成自己的域名。

### 临时关掉 basic auth

在 `.env` 里写 `MATRIX_BASIC_AUTH=off`，然后 `docker compose up -d`。关掉时不需要 `MATRIX_BASIC_HASH`；只认 `on` / `off`，写成别的值
Caddy 会起不来，不会悄悄放开。关掉之后只剩 Matrix 自己的简易登录，而 dsh 能在服务器上执行命令，所以只适合短时间的演示。

开着 basic auth 时，端侧连接器握手 `/api/devices/connect` 也要过这一层：连接器要带上 basic auth 凭据，否则握手会被 Caddy 拒绝；
也可以把这条路径加进上面 `@protected` 的放行列表（它凭设备令牌鉴权）。

### 从旧的部署迁移（Caddy 原来在 `deploy/compose.yml` 里）

旧版本的 `deploy/compose.yml` 里带着 `caddy` 服务。更新到新版本后照下面做，证书和配置都能沿用：

1. 建 `/opt/matrix-edge/`，放上面的 `compose.yml`、`Caddyfile`；把旧 `deploy/.env` 里的 `MATRIX_SITE`、`MATRIX_TLS_ISSUER`、
   `MATRIX_BASIC_*` 挪进新目录的 `.env`（旧 `.env` 里留着也没关系，Matrix 不读它们）。
2. 沿用旧的证书卷：把示例 `compose.yml` 末尾的 `volumes` 换成

   ```yaml
   volumes:
     caddy-data:
       external: true
       name: matrix_caddy-data
     caddy-config:
       external: true
       name: matrix_caddy-config
   ```

3. 停掉旧的 Caddy，再起新的（中间有几秒不可访问）：

   ```bash
   cd /opt/matrix/deploy && git pull && docker compose up -d --build --remove-orphans
   cd /opt/matrix-edge && docker compose up -d
   ```

`--remove-orphans` 会删掉旧的 `caddy` 容器，卷不受影响。
