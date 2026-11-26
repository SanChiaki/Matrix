# 用公司账号登录：配置与登录驱动

给要把公司的统一登录（单点登录）接进 Matrix 的人：部署方、写驱动的人。做完之后，登录页上多一个“用公司账号登录”的按钮：
点了跳到公司的登录页，登录完回到 Matrix，已经是登录状态；第一次来的人自动有账号和助理。

- 仓库里的是通用的部分：登录驱动接口、内置的通用 OAuth2 驱动、身份怎么对到 Matrix 用户的规则。
  公司自己的适配（地址、密钥、字段，或一个驱动包）留在内网，不进这个仓库。
- 公司的登录是 OAuth2（或 OIDC）：多半**不用写代码**，照第二、三节写 `login.json`。
- 不是 OAuth2（自研的票据、CAS……），或者 OAuth2 但有配置表达不了的差别：照第四节写一个驱动包。
  样板：[examples/login-driver](../examples/login-driver)（虚构的票据式“演示 SSO”的驱动包，带一个能在浏览器里点的假身份系统）。
- 安全边界见 [reference/security.md](reference/security.md)。改了驱动接口（`matrix/login/base.py`）、`login.json` 的字段或一致性检查，要同步改这份文档与样板。

## 一、先和管统一登录的人确认这些

| 要确认的 | 为什么 | 落到哪里 |
| --- | --- | --- |
| 协议：OAuth2 授权码、OIDC，还是别的 | 决定用内置驱动还是写驱动 | `driver` |
| 给 Matrix 登记一个应用，拿到应用 id 与密钥 | 每个应用一套 | `client_id`、`client_secret`（密钥放环境变量） |
| 登记回调地址：`https://<Matrix 的地址>/api/auth/sso/<入口 id>/callback` | 身份系统只把人送回登记过的地址；要一字不差 | `public_url`、入口的 `id` |
| 三个地址：登录页（authorize）、换令牌（token）、取用户信息（userinfo） | 没有自动发现时要手写 | `authorize_url`、`token_url`、`userinfo_url` |
| 用户信息长什么样（要一份返回的 JSON 样例） | 取标识与邮箱 | `fields` |
| 哪个字段是**不会变**的用户标识（工号、用户 id），不是会改的名字 | 绑定就绑它 | `fields.subject` |
| 能不能拿到邮箱；拿不到的话账号名加公司域名是不是邮箱 | Matrix 里邀请、认管理员按邮箱 | `fields.email`、`email_domain` |
| 邮箱是不是公司统一分配、本人改不了 | 决定能不能按邮箱合并已有账号 | `link_by_email` |
| 换令牌时密钥放表单还是 HTTP Basic；取用户信息时令牌放请求头还是查询参数；支不支持 PKCE | 各家不一样 | `token_auth`、`userinfo_auth`、`pkce` |
| Matrix 的服务器能不能连到换令牌、取用户信息的地址（内网、代理、自签证书） | 这两步是服务器直接连 | `proxy`；自签 CA 见 [deploy/README.md](../deploy/README.md) |
| 有没有测试环境、测试账号 | 第六节的验收 | — |

## 二、`login.json`

默认读 `<data>/login.json`，也可以用 `--login` / `MATRIX_LOGIN` 指到别处。改了就生效，不用重启；没有这个文件就只有邮箱密码。

```json
{
  "public_url": "https://matrix.corp.example",
  "password_login": true,
  "providers": [
    {
      "id": "corp",
      "name": "公司账号",
      "driver": "oauth2",
      "auto_create": true,
      "link_by_email": false,
      "allow_domains": ["corp.example"],
      "config": { "…": "驱动自己的配置，见第三节或驱动的说明" }
    }
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `public_url` | 用户在浏览器里打开 Matrix 用的地址。回调地址按它拼。不写时按请求里的地址拼，只适合本机试；上线必须写 |
| `password_login` | `false` = 关掉邮箱密码的注册与登录，只留管理员（应急）。只在至少有一个入口可用时生效：配置写坏了不会把所有人关在外面 |
| `providers[].id` | 2–32 位小写字母、数字、`-`、`_`，字母开头。它在回调地址里，用户的绑定也按它记：**定了就不要改** |
| `name` | 登录页按钮上的名字：“用〈name〉登录” |
| `driver` | 内置 `oauth2`，或装上的驱动包的 `kind` |
| `auto_create` | 默认 `true`：第一次用公司账号登录的人自动建 Matrix 账号。`false` = 只让已经绑定的人登录 |
| `link_by_email` | 默认 `false`。`true` = 公司账号的邮箱和一个已有的 Matrix 账号相同时，当成同一个人、绑上去。见下面的“已有账号” |
| `allow_domains` | 只放这些邮箱域名的人进来；不写 = 不限 |
| `config` | 交给驱动的配置。里面的字符串可以写 `${环境变量}`：**密钥放在环境变量（`deploy/.env`）里，不要直接写进文件**。变量名里要带 `KEY`（如 `MATRIX_SSO_KEY`），不带的不收：服务进程的环境会传给云侧的 dsh，dsh 按名字不把带 `KEY` 的变量交给 Agent 的命令 |

某个入口写错了（驱动不存在、环境变量没设、驱动说配置不对）只跳过它，原因打在服务日志里；整份文件读不了时沿用上一份。

### 身份怎么对到 Matrix 用户

- 先按绑定找：这个入口下的这个标识登录过，就是那个用户。绑定以后**只认标识**：身份系统里改了邮箱，Matrix 里的邮箱不跟着变。
- 没绑定过、邮箱也没人用：新建账号（`auto_create`），没有密码，进来就有助理。第一个账号和注册时一样，是管理员并接管已有数据。
- **已有账号**（邮箱相同）：默认拒绝，提示联系管理员。原因：如果谁能在身份系统里填上别人的邮箱，谁就能进别人的账号。
  公司的邮箱由公司统一分配、本人改不了时，打开 `link_by_email`：老用户第一次用公司账号登录就绑上了，原来的数据都在，密码也还能用。
  一个账号在一个入口下只绑一个标识，绑过的不会被另一个标识顶替。
- 邀请别人进项目按邮箱找用户：同事要先登录过一次，才邀请得到。
- 管理员照旧按 `MATRIX_ADMINS` 里的邮箱认。

### 已有部署怎么切过去

1. 写好 `login.json`，`password_login` 先留 `true`，自己用公司账号登录一次，确认拿到的邮箱是对的。
2. 已经有用户的：确认公司邮箱可信后打开 `link_by_email`，让大家用公司账号登录一次。
3. 都绑上以后把 `password_login` 改成 `false`。管理员留一个能用密码登录的账号应急
   （用公司账号建的管理员没有密码，在管理后台给自己发一条重置链接设一个）。

## 三、内置的 `oauth2` 驱动

通用的 OAuth2 授权码登录，可带 PKCE。OIDC 的身份系统也这样接（它就是 OAuth2 加一层）：`scope` 里写上 `openid`，`fields.subject` 用 `sub`。

```json
"config": {
  "authorize_url": "https://sso.corp.example/oauth2/authorize",
  "token_url": "https://sso.corp.example/oauth2/token",
  "userinfo_url": "https://sso.corp.example/oauth2/userinfo",
  "client_id": "matrix",
  "client_secret": "${MATRIX_SSO_KEY}",
  "scope": "profile email",
  "fields": {"subject": "id", "email": "email", "name": "name"}
}
```

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `authorize_url`、`token_url`、`client_id` | 必填 | |
| `client_secret` | 空 | 写成 `${环境变量}` |
| `userinfo_url` | 空 | 不写 = 用户信息直接从换令牌的响应里取 |
| `scope` | 空 | 空格隔开 |
| `fields` | `sub` / `email` / `name` / `access_token` | 各项在返回的 JSON 里的位置，可以用点号进到里层：`data.user.id`。`subject`、`email`、`name` 在用户信息里找；`access_token` 在换令牌的响应里找（有的身份系统包了一层：`data.access_token`） |
| `email_domain` | 空 | `fields.email` 取到的值没有 `@`（只是账号名）时补上的域名 |
| `pkce` | `true` | 带 S256 的 `code_challenge`。身份系统因为不认识这个参数而报错时关掉 |
| `token_auth` | `body` | 换令牌时应用 id 与密钥放表单里；`basic` = 放 HTTP Basic 请求头 |
| `userinfo_auth` | `header` | `Authorization: Bearer <令牌>`；`query` = `?access_token=<令牌>` |
| `authorize_params` | 无 | 跳去登录页时额外带的查询参数 |
| `proxy` | `direct` | 内网直连；`env` = 照环境变量里的代理 |
| `timeout_s` | 15 | 每次请求的超时 |

换令牌的响应是 JSON 或表单编码都认。这里不验 `id_token`：用户信息是服务器拿着刚换到的令牌直接向身份系统取的，不经过浏览器。

接不上时先看登录页上的那句原因，再看这几处：

| 现象 | 多半是 |
| --- | --- |
| 身份系统的页面报回调地址不对 | 登记的回调地址和 `<public_url>/api/auth/sso/<id>/callback` 不是一字不差（http / https、末尾斜杠、端口） |
| “换令牌失败：…” | 密钥不对；`token_auth` 该用 `basic`；`pkce` 该关；Matrix 的服务器连不到 `token_url` |
| “换令牌的响应里没有 access_token” | 响应包了一层，写 `fields.access_token` |
| “取用户信息失败：身份系统返回 401” | `userinfo_auth` 该用 `query`，或 `scope` 不够 |
| “用户信息里没有 …” | `fields` 的路径不对：对着返回的 JSON 样例改 |
| “这次登录已经过期，或不是从这个浏览器发起的” | 登录页开了超过 10 分钟；或发起和回调用的不是同一个域名（cookie 带不回来），检查 `public_url` |

## 四、写一个驱动包

```
examples/login-driver/
  pyproject.toml                  包名、entry point
  matrix_login_demo/driver.py     驱动（换掉标着「协议」的三处）
  matrix_login_demo/devserver.py  本地的演示 SSO：给测试用，也能在浏览器里点
  tests/test_demo_login_driver.py 驱动自己的测试
```

1. 复制这个目录，改包名、`kind`（2–32 位小写字母、数字、`-`、`_`，字母开头）、`title`。
2. 换掉标着「协议」的三处：登录页地址怎么拼、回调带回什么、怎么向身份系统核实。
3. 在 `pyproject.toml` 里注册，装进 Matrix 所在的环境，`login.json` 里的 `driver` 写这个 `kind`：

   ```toml
   [project.entry-points."matrix.login_drivers"]
   acme = "acme_login.driver:AcmeDriver"
   ```

   `kind` 和内置驱动重名、加载失败、不是 `Driver` 子类的会被跳过，启动时打一行“有登录驱动没加载上”。外部包不能顶替内置驱动。

公司的登录是 OAuth2、只差一两处时，不用从头写：继承 `matrix.login.oauth2.OAuth2Driver`，换一个 `kind`，覆盖 `authorize_url` 或 `identify`。

### 接口

```python
from matrix.login.base import Driver, Flow, Identity, LoginError

class AcmeDriver(Driver):
    kind = "acme"
    title = "Acme 统一登录"

    def __init__(self, config): ...                      # 只校验配置，不连网；不对就抛 LoginError
    def authorize_url(self, flow: Flow) -> str: ...      # 浏览器要跳去的登录地址
    def identify(self, flow: Flow, params: dict[str, str]) -> Identity: ...   # 回调的查询参数 → 是谁
    def verify(self, credential: str) -> Identity: ...  # 可选：外部系统的后端转来的用户凭据 → 是谁（第八节）
```

- `config`：`login.json` 里这个入口的 `config`，`${环境变量}` 已经换成值。
- `Flow`：一次进行中的登录。`flow.state` 是随机串，`flow.redirect_uri` 是回调地址，`flow.data` 是驱动这次登录要记的东西
  （如 PKCE 的 verifier；只在服务器内存里）。两个方法可能被多个请求同时调用，**这次登录的状态放 `flow.data`，不要放 `self`**。
- **`flow.state` 必须跟着回调回来**，Matrix 据此确认回调属于这次登录。OAuth2 这类会原样带回 `state` 参数的，把它放进登录地址；
  只会跳回你给的地址的（票据式），把 `flow.return_url`（回调地址加上 `?state=…`）当回跳地址。身份系统用别的参数名带回时，改类属性 `state_param`。
- `identify`：**在这里向身份系统核实**（换令牌、验票据），只信从身份系统直接拿到的结果，不信浏览器带来的用户信息。
  返回 `Identity(subject, email, name)`：`subject` 用不会变的标识；`email` 必须有（没有邮箱时用账号名拼一个公司域名下的）。
- 出错抛 `LoginError`，消息会显示在登录页上：写给人看，**不要带密钥、令牌、票据**。别的异常只在日志里记类型，登录页上是一句“登录驱动出错”。
- 回调只支持 GET（浏览器跳回来）。只能 POST 回来的协议（如 SAML 的 POST 绑定）接不了：让身份系统那边用 OAuth2，或者前面放一个转换。

### 这些不用驱动管

- 一次登录只认发起它的那个浏览器（发起时设的一次性 cookie）、10 分钟内有效、用过即作废。
- 身份怎么对到 Matrix 用户、谁能进来（第二节）。
- 登录状态：仍是 Matrix 自己发的 cookie，退出登录只退出 Matrix，不退出公司的登录。

## 五、本地测与上线

```bash
uv run pytest -q examples/login-driver/tests       # 样板自己的测试（在 Matrix 仓库根目录）
```

驱动的测试照样板写：`assert_driver(MyDriver)` 查结构；起一个假的身份系统，把驱动的地址指过去，测登录成功、凭据不对、凭据不能重放。

在浏览器里走一遍样板（两个终端，在 Matrix 仓库根目录）：

```bash
uv run --with-editable examples/login-driver python -m matrix_login_demo.devserver --port 9300
```

```bash
DEMO_SSO_KEY=demo-secret uv run --with-editable examples/login-driver matrix --runtime scripted --login examples/login-driver/login.json
```

打开 `http://127.0.0.1:8765`，点“用演示 SSO 登录”。

上线（Docker 部署，见 [deploy/README.md](../deploy/README.md)）：

1. 驱动包：在 `deploy/.env` 里写 `MATRIX_LOGIN_DRIVERS`（pip 安装参数，空格隔开），`docker compose up -d --build`。只用内置 `oauth2` 的跳过这一步。
2. 密钥：写进 `deploy/.env`（变量名里要带 `KEY`，和 `login.json` 里的 `${…}` 对上），`docker compose up -d` 重建容器（只 `restart` 不会重读 `.env`）。
3. `login.json` 放进数据卷（`/data/login.json`）。
4. 反向代理不用为它加规则：发起与回调都在 `/api/auth/sso/*` 下，是普通的浏览器请求。
   代理要原样传 `Host`，或者写好 `public_url`。

## 六、验收

- [ ] 登录页上有“用〈name〉登录”，点了到公司的登录页
- [ ] 登录后回到 Matrix，右上角是自己的邮箱，有助理
- [ ] 退出后再登录，还是同一个账号（数据都在）
- [ ] 换一个人登录，是另一个账号
- [ ] 在公司登录页点取消 / 拒绝：回到 Matrix 的登录页，有一句看得懂的原因
- [ ] 已有账号的人：按第二节定的方式（`link_by_email` 或拒绝）
- [ ] `allow_domains` 之外的人进不来
- [ ] 关掉 `password_login` 后：普通用户不能用密码登录，管理员可以
- [ ] 服务日志里没有密钥、令牌

## 七、还没做的

- 已登录的人在设置里自己绑定 / 解绑公司账号。
- 离职回收：身份系统里停用的人，Matrix 里已有的登录与设备令牌不会自动失效。
- 登录令牌过期、CSRF 令牌、登录限流（见 [reference/security.md](reference/security.md)）。
- 只能 POST 回调的协议（SAML）。
- 写操作的档案日志还不区分“网页上做的”与“外部系统带着凭据做的”（第八节）。

## 八、外部系统带着用户凭据调接口

和 Matrix 用同一套统一登录的业务系统，可以在自己的后端带着**用户的凭据**直接调 Matrix 网页在用的那套 `/api/*`：
这次请求就等同那个用户本人，能做的就是他在网页上能做的，看到的也只是他看得到的。不需要另外登记应用、签令牌。

```
GET /api/me
Authorization: Bearer <用户的凭据>
X-Matrix-Login: corp            ← 只在 login.json 里不止一个入口打开了 api_auth 时要
```

凭据是什么由身份系统定：主域名下各系统共享的登录 cookie 的值、OAuth2 的访问令牌、票据……驱动的 `verify` 负责向身份系统核实它。

### 打开

1. 驱动实现 `verify(credential) -> Identity`：向身份系统核实（调它的校验接口，或按它给的公钥验签），核实不了抛 `LoginError`。
   **不要只解析凭据里自称的用户**。内置的 `oauth2` 驱动已经实现了：拿访问令牌调 `userinfo_url`（要配它）。
   样板 `examples/login-driver` 实现的是共享登录 cookie：拿 cookie 的值问演示 SSO 的 `/session`。
2. `login.json` 里那个入口写 `"api_auth": true`。驱动没有 `verify` 时这个入口不启用，原因打在服务日志里。
3. 业务系统的后端从用户的请求里取出凭据（如共享 cookie 的值），放进 `Authorization: Bearer …` 转给 Matrix。

### 规则（驱动不用管）

- **只认请求头，不直接读浏览器带来的共享 cookie。** 共享 cookie 设在主域名上，同一主域名下任何子域名的页面都能让浏览器带着它
  请求 Matrix，`SameSite` 挡不住同站的伪造请求；请求头只有后端加得上。Matrix 自己的登录 cookie 照旧优先。
- 对到 Matrix 用户的规则和公司账号登录一样（第二节）：先按绑定的标识，再按 `auto_create`、`link_by_email`、`allow_domains`。
  第一次来的人自动建账号、有助理。不发 Matrix 自己的登录 cookie。
- 核实结果按 (入口, 凭据的 sha256) 缓存：通过的 60 秒、不通过的 10 秒。用户在身份系统那边退出后，最多一分钟内 Matrix 也不认了。
  内存里不留凭据明文。
- 管理后台（`/api/admin/*`）不认这种凭据，只能在网页上登录后用。
- 核实不过返回 401，消息是驱动的 `LoginError`；驱动里没预料到的异常只记类型。
- 事件流 `/api/events` 同样认这个请求头：后端发了消息以后，订阅它拿 Agent 的回复。
- 反向代理要把 `Authorization` 请求头原样传给 Matrix。

### 要接受的

- 拿得到用户凭据的系统能以他的身份做他能做的一切（包括删他主持的项目、用他的连接器授权），没有按系统收窄的范围。
  所以只给可信的、和 Matrix 用同一套登录的内部系统用；要收窄就在网络层限定能连到 Matrix 的来源。
- 内置的 `oauth2` 驱动核实访问令牌时只问 `userinfo_url`，不看令牌是签给哪个应用的：同一个身份系统签给**任何**应用的令牌都认。
  身份系统里有不可信的第三方应用时不要对它打开 `api_auth`，改用自己写的驱动在 `verify` 里核对令牌的受众。
- `/api/*` 原本只给 Matrix 自己的网页用，接口变化时对接方要跟着改。

## 九、临时方案：业务系统凭共享密钥替用户调接口

共享 cookie 还接不上（第八节的驱动没写好）时，先用这个：业务系统的后端和 Matrix 共用一把密钥，请求里直接说是哪个用户。
Matrix 不向身份系统核实这个人：**密钥对上，就信后端说的是谁**。以后接好第八节，把 `api_keys` 那一项删掉即可。

`login.json`：

```json
{"api_keys": [
  {"id": "expense", "name": "报销系统",
   "keys": ["${EXPENSE_MATRIX_KEY}"],
   "bind_as": "corp",
   "auto_create": true, "link_by_email": false, "allow_domains": ["corp.example"]}
]}
```

- 默认没有这一段，就没有这条路。每个业务系统一项，`id` 的规则和登录入口一样。
- `keys`：每个都要写成 `${环境变量}`（变量名带 `KEY`，见第二节），至少 32 个字符（`openssl rand -hex 32`）；Matrix 只留 sha256。
  轮换：先加新的（新旧并存），业务系统换上新的以后删掉旧的，改了文件就生效。
- `bind_as`：用户标识绑定在哪个名字下（默认是这一项的 `id`）。写成公司账号登录入口的 id（如 `corp`），
  经密钥来的人和用公司账号登录的人就是同一个 Matrix 用户；以后换成第八节也不会多出重复账号。
- `auto_create` / `link_by_email` / `allow_domains`：和登录入口的同名字段一样（第二节）。

请求头：

| 请求头 | 写什么 |
|---|---|
| `X-Matrix-Key` | 密钥 |
| `X-Matrix-User` | 这个人在公司里唯一、不会变的 user_id：绑定只认它。不要用工号、邮箱这类会换的 |
| `X-Matrix-User-Email` | 邮箱。**建议每次都带**，业务方不用判断是不是第一次：还没绑定的人靠它建账号（Matrix 账号要有邮箱，邀请按它）；已绑定的人不看它，带了别的邮箱也不会改 Matrix 里的邮箱、不会换成别人 |

```bash
curl -H "X-Matrix-Key: $EXPENSE_MATRIX_KEY" -H "X-Matrix-User: 8f3a2c01" -H "X-Matrix-User-Email: wang@corp.example" \
     https://matrix.corp.example/api/me
```

规则：对到用户的规则和公司账号登录一样（`auth.sso_user`，已有同邮箱的账号默认不绑）；第一次来的人自动建账号、有助理；
不发 Matrix 自己的登录 cookie；管理后台（`/api/admin/*`）不认；密钥不对、没写用户、认不出人一律 401。
反向代理要把这三个请求头原样传给 Matrix。

### 要接受的

- 拿到密钥的系统能以**任何**用户的身份做他能做的一切：Matrix 分不出后端说的是不是真的。密钥只放在业务系统的后端，
  不进浏览器、不进前端代码、不进日志；限定只有业务系统的服务器能连到 Matrix（网络层）。
- 用户在身份系统里停用了，经密钥照样能以他的身份调用，直到管理员处理。
- 这是临时方案：第八节接好以后删掉 `api_keys`。
