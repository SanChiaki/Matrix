# 写一个 IM 驱动

给要把企业内部 IM（或飞书等）接进 Matrix 的人。驱动只管和 IM 收发；谁的话能进网络、回复推到哪、卡片按钮认谁，
都由桥（`matrix/im/bridge.py`）决定，驱动不用、也不该自己判断（不变量 25，见 [reference/im.md](reference/im.md)）。

改了驱动接口（`matrix/im/base.py`）、注册方式或一致性检查，要同步改这份文档。

## 最小的驱动

```python
# acme_im/driver.py
from matrix.im.base import Capabilities, Chat, Driver, Field, Inbound

class AcmeDriver(Driver):
    kind = "acme"                       # 2–32 位小写字母、数字、-、_
    title = "Acme IM"                   # 界面上的名字，也是消息上的“来自 Acme IM”
    fields = (Field("app_id", "App ID"), Field("app_secret", "App Secret", secret=True))
    caps = Capabilities(stream=False, edit=False, cards=False, proactive=True,
                        max_text_bytes=4000, rate_per_minute=20)

    def start(self):                    # 立即返回；连接、心跳、重连放在自己的线程里
        ...                             # 连上后 self.sink.on_status("online")

    def stop(self): ...

    def respond(self, handle, chat: Chat, text: str): ...   # 回复收到的那条消息
    def push(self, chat: Chat, text: str): ...              # 主动发一条（proactive=True 才会被调用）

    # 收到消息时：
    #   self.sink.on_message(Inbound(msg_id=..., chat=Chat("single", uid), sender=uid, text=..., handle=...))
```

在包的 `pyproject.toml` 里注册：

```toml
[project.entry-points."matrix.im_drivers"]
acme = "acme_im.driver:AcmeDriver"
```

装进 Matrix 所在的环境（`uv pip install ./acme-im` 或作为依赖加进部署），重启服务就能在后台“IM 机器人”页和个人的
“在 IM 里找助理”里选到。`kind` 和内置驱动重名、加载失败、不是 `Driver` 子类的会被跳过，原因列在后台页上。

## 接口

- `fields`：接入时要填的项，界面据此生成表单。`secret=True` 的只进加密保管库、不回显；`required=False` 可以不填。
  构造时拿到 `config`（非密钥）与 `secrets`（密钥）两个 dict。**构造函数里不要连网**。
- `start()` / `stop()`：状态经 `sink.on_status(status, error)` 报：`connecting`、`online`、`auth_failed`（凭证不对，别重试）、
  `kicked`（同一个机器人在别处连上了，别重连）、`error`。报了 `auth_failed` / `kicked` 桥会停掉这个驱动，等人点“重新连接”。
- `sink.on_message(Inbound)`：
  - `msg_id`：IM 里这条消息的标识，桥用它去重；
  - `chat`：`Chat("single", <对方标识>)` 或 `Chat("group", <群标识>)`；
  - `sender`：发消息的人在这个 IM（这个机器人）下的**稳定**标识，绑定就绑它；
  - `text`：去掉开头的 @机器人；`quote`：引用的那条的文字；
  - `files`：`InboundFile(name, mime, fetch)`，`fetch()` 只在桥决定收下这条消息后才调用，可以在里面下载、解密，并改掉 `name` / `mime`；
  - `handle`：你自己回复时要用的东西（如企业微信的 `req_id`），桥原样交回 `respond` / `open_reply`。
- `sink.on_action(Action)`：卡片按钮被点了（`key` 是 `Button.key`）。返回值是要显示在卡片上的结果，能改卡片的驱动用它更新。
- **回调在哪个线程**：如果你的发送要等 IM 的回执、回执又由读消息的线程收，就不要在读消息的线程里调 `sink`（桥处理时会让你发消息）。
  像 `wecom.py` 那样另开一个派发线程。
- 出错时抛 `DriverError`：桥把它记在渠道或绑定上，不往上抛。

## 能力与降级

| `Capabilities` | 含义 | 没有时桥怎么做 |
| --- | --- | --- |
| `stream` / `edit` | 一条回复能边做边刷新 / 发出的消息能改 | 不发占位、不刷新，收尾时 `respond` 一条 |
| `cards` | 能发带按钮的卡片，点击经 `on_action` 回来 | `Card.as_text()`：文字 + “回复「发送 3」” |
| `proactive` | 能主动给一个单聊发消息 | 只在回复里说话 |
| `max_text_bytes` | 一条正文的上限（UTF-8 字节） | 桥按它切段 |
| `reply_ttl` | 一条回复从开始到收尾的时限（秒） | 桥在到点前 30 秒收尾，之后主动推送 |
| `min_update_interval`、`rate_per_minute` | 刷新间隔、每会话每分钟条数 | 桥按它节流 |

能流式或能改消息的，覆盖 `open_reply(handle, chat)` 返回自己的 `Reply`（`update(text)` 刷新、`finish(text)` 收尾）；
能发卡片的覆盖 `push_card(chat, card)`。

## 检查与测试

```python
from matrix.im.testing import assert_driver
from acme_im.driver import AcmeDriver

def test_driver_shape():
    assert_driver(AcmeDriver)    # 声明齐不齐、该实现的方法实现了没有、空配置能不能构造
```

行为测试照 `matrix/im/fake.py`（内存里的假驱动）与 `tests/test_wecom.py`（本地起一个假的长连接服务）的写法。
桥的规则已经由 `tests/test_im.py` 对任何驱动都保证了，驱动只需要测自己的收发。

## 安全

- 驱动要连的地址如果做成可填，只能由部署方填（环境变量或部署方配置），不要做成用户在界面上可填的字段：网络会被当跳板。
- 下载 IM 消息里的文件用 `matrix.mcp_client.fetch_public`（只连 https 公网、不跟重定向、有大小上限）；企业内网的 IM 文件服务
  需要另行评估，别直接放宽。
- 不要在日志、异常信息里带出 `secrets`。
