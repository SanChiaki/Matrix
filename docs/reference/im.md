# IM 入口：在企业微信等 IM 里和自己的助理单聊

> 入口见 [AGENTS.md](../../AGENTS.md)。需求 #63，不变量见 [invariants.md](invariants.md) 第 25 条（也涉及 10、13、21）。
> 接企业内部 IM（写驱动、本地测、上线、真机验收）见 [im-driver-authoring.md](../im-driver-authoring.md)，样板驱动包在 `examples/im-driver/`。

人在 IM 里和一个机器人单聊，就是在和自己的助理说话：消息进他的**助理对话**，回复回到 IM。第一版只做单聊助理；
群里 @ 机器人只回一句“请私聊”。参照 OpenAI dots 的“只有所有者能指挥”与“各渠道历史分开”
（research: openai-dots#who-can-direct、research: openai-dots#channels-identity）；对外用标明是 Agent 的机器人身份
（research: always-on-personal-agents#matrix-takeaways）。企业微信的接口事实见 research: wecom-aibot#long-connection。

## 分层

```
IM ⇄ 驱动（matrix/im/wecom.py，或 entry point 装上的外部驱动）⇄ 桥（matrix/im/bridge.py）⇄ Network（助理对话）
```

- **驱动**只管收发：连接、心跳、重连、把各家的消息统一成 `Inbound`、把桥要发的东西发出去。用 `Capabilities` 声明能做什么
  （流式、改消息、卡片、主动推送、单条长度、回复时限、刷新间隔、每分钟条数）。
- **桥**写全部规则，对每个驱动都一样：绑定、只认绑定的人、回复回到哪里、推什么、按能力降级、限频。
- **注册**（`matrix/im/registry.py`）：内置企业微信；外部驱动包用 entry point `matrix.im_drivers` 注册，装进同一个环境就被发现，
  重名、加载失败的跳过（后台“IM 机器人”页列出原因）。外部包不能顶替内置驱动。Docker 部署用 `MATRIX_IM_DRIVERS` 在构建镜像时装（`deploy/README.md`）。
- **样板**（`examples/im-driver/`）：虚构的“演示 IM”（能改消息、不能发卡片）的驱动包与本地服务；`tests/test_im.py` 对它经桥跑一遍，
  它自己的测试也在默认的 `uv run pytest` 里。
- 服务进程里 `server.main()` 建一个 `ImBridge` 并 `start()`（一个机器人只能有一条连接：只支持单进程部署）。
  `create_app(network)` 不传桥时建一个不连 IM 的，供测试与只看 REST 的场合。

## 机器人与绑定

- **`ImChannel`**（`store.im_channels`）是一个机器人：
  - 个人的：`owner_id` 是主人，每人每种 IM 一个，在“助理与定时 → 在 IM 里找助理”里接；只能绑主人。
  - 部署方的：`owner_id` 为空，管理员在 `/admin#im` 配；所有人都看得到、能绑定自己，只有管理员能改、停用、删。
  - 驱动的非密钥字段在 `config`，密钥在保管库 `vault/im-<id>`；改的时候密钥留空 = 沿用。删掉机器人连同密钥与所有绑定一起删。
- **`ImBinding`**（`store.im_bindings`）是某个人在某个机器人上的身份：`sender`（这个人在那个 IM 里的标识）、`chat_id`（推送用的单聊）、
  `push`（没人说话时的消息推不推）、`error`（最近一次推送失败的原因）。部署方机器人上每人一条，一个 `sender` 只对应一个人。
- **绑定码**：`POST /api/me/im/<id>/code`（个人机器人建好时直接返回一个）。8 位、只显示一次、10 分钟有效、用一次就作废，
  只在桥的内存里存哈希（服务重启后作废，重新拿）。没绑定的人在单聊里发一串像码的字就算一次尝试：
  同一个人 10 分钟内错 5 次先停；个人机器人上累计错 5 次，待用的码作废。
- 企业微信的 userid 可能是加密的（research: wecom-aibot#identity）：桥只把它当稳定标识，靠绑定码认人，不需要转明文。

## 收消息（不变量 25）

1. 按 `msg_id` 去重（IM 可能重推）。
2. 群里的：回“请私聊”，内容不进网络。
3. 没绑定的：像绑定码就试绑定，否则回“还没有绑定……”。内容不进网络。
4. 绑定的人：`发送 N` / `放弃 N` 是草稿的文字指令；其余在锁外下载附件（企业微信要 AES 解密，下载只连 https 公网，`mcp_client.fetch_public`），
   再 `send_message(助理对话, text, actor=这个人, via="来自<驱动名>", attachments)`。引用的消息写成 `> ` 开头的引文。
   界面上这条消息带“来自企业微信”。

发消息与登记“这条经哪个 IM 来、回复回到哪里”在桥的同一把锁里：事件线程（也拿这把锁）不会先看到它引出的回复。
加锁顺序固定是桥在外、网络在内。

## 回到 IM 与推送

桥在自己的线程里消费事件总线（同 `cli.py`），每条都先 `event_view(…, 那个人)`：

- **回复**：经 IM 来的消息先开一条回复（能流式的先发“收到，正在处理…”），`run.progress` 的文字步骤刷新它（至少隔 5 秒），
  助理的回复（或这一轮的失败说明）来了就收尾；超过单条长度的切段，后面几段主动推送。快到驱动的回复时限（企业微信 10 分钟）
  先收尾成“还在做，做好了发给你”，之后的回复走主动推送。这一轮结束了却没有回复：收尾成“这一轮没有新的回复”。
- **推不推、推到哪**：看这条助理消息前面最近的那条人的消息。经 IM 来的 → 回到那个 IM；网页里说的 → 不推（各渠道历史分开）；
  中间没有人说话（定时、早报、委托回报唤醒的）→ 推到这个人所有开了 `push` 的绑定。
- **草稿**（不变量 21）：起草的消息推成卡片（“发送”“放弃”，按钮的 key 是 `draft:<id>:send|discard`），不能发卡片的 IM 用文字
  “回复「发送 3」”。按钮与指令只认绑定的那个人，调的是网页确认卡上同一个 `send_draft` / `discard_draft`。
- **等你处理的事**：拦下的输出（`held.updated`）、沙箱升权与连接器授权申请（`approval.updated`）只推一条提醒，让人回 Matrix 处理——
  它们要看得到细节、逐次确认（不变量 14、19、22），第一版不在 IM 里批准。推到正在说话的那个 IM 与开了 `push` 的绑定。
- 发送：每个机器人一个队列，按顺序发；按会话限频（`rate_per_minute`），进度刷新超了就丢，正式消息等到能发。
  发不出去（例如对方还没在这个会话里说过话）记在绑定的 `error` 上，内容仍在 Matrix 里。

## 按能力降级

| 驱动没有 | 桥怎么做 |
| --- | --- |
| `stream` 与 `edit` | 不发占位、不刷新，收尾时回一条 |
| `cards` | 卡片改成文字，按钮换成文字指令 |
| `proactive` | 只在回复里说话；推送记成绑定的 `error`；太长的回复截断并提示“其余在 Matrix 里看” |

## 连接状态

`ImChannel.status`：`connecting` / `online` / `auth_failed`（凭证不对或没有长连接权限，不重试）/ `kicked`（同一个机器人在别处连上了，
不自动重连，免得两个实例互相踢）/ `error` / `stopped`。卡片与后台显示状态，“重新连接”重启驱动。变化发 `im.updated`
（`user_id` 是主人，部署方机器人是每个绑定的人），`event_view` 只给本人。

## 企业微信驱动

`matrix/im/wecom.py`：智能机器人 → API 模式 → 长连接（research: wecom-aibot#long-connection）。

- 连接线程：`aibot_subscribe{bot_id, secret}` → 读帧；断线指数退避（1s 起、30s 封顶）重连；订阅被拒报 `auth_failed` 停下；
  收到 `disconnected_event` 报 `kicked` 停下。心跳线程每 30 秒 `ping`，连续 2 次没有回执就关掉连接交给重连。
- 派发线程把消息交给桥：桥处理时可能要等回执，回执由读帧线程收，所以回调不在读帧线程里跑。
- 回复用流式（`aibot_respond_msg`，透传 `req_id`）；主动推送 `aibot_send_msg` 用 markdown；草稿卡片先推一条 markdown 正文，
  再推一张 `button_interaction` 卡片；点按钮后 5 秒内用 `aibot_respond_update_msg` 把按钮换成结果。
- 地址默认 `wss://openws.work.weixin.qq.com`，私有化部署的企业由部署方设 `MATRIX_WECOM_URL`（不做成用户可填，见 [security.md](security.md)）。
- 限制（research: wecom-aibot#limits）：流式 10 分钟、每会话 30 条/分钟、markdown 20480 字节；千人以上企业的长连接权限默认关，要管理员开。

## REST

| 方法与路径 | 作用 |
| --- | --- |
| `GET /api/me/im` | 可用的驱动（字段、能力）、我的个人机器人、部署方的机器人；每个带我的绑定 |
| `POST /api/me/im` | 接一个个人机器人 `{kind, title?, values}`，返回机器人与绑定码 |
| `PATCH /api/me/im/<id>` | 改名称、字段（密钥留空沿用）、启用；部署方的只给管理员 |
| `POST /api/me/im/<id>/reconnect`、`DELETE /api/me/im/<id>` | 重新连接、删除（连密钥与绑定） |
| `POST /api/me/im/<id>/code` | 拿一个绑定码 |
| `PATCH /api/me/im-bindings/<id>`、`DELETE …` | 开关推送、解除绑定 |
| `GET /api/admin/im`、`POST /api/admin/im` | 管理员：部署方机器人列表（带绑定人数、驱动加载错误）、新建 |

别人的机器人与绑定一律 404；返回里没有密钥，只有 `secrets_set`（设了哪些）。

## 限制

- 只做单聊助理：群绑项目、在 IM 里 @ 别的 Agent 都没做（群成员不等于项目里在场的人，见不变量 22）。
- 拦下的输出、沙箱升权、连接器授权不能在 IM 里批准，只推提醒。
- 主动推送要对方先在会话里说过话，企业微信可能还有 24 小时窗口；推不出去时记在绑定上。
- 服务重启后，进行中的回复改由主动推送补发；没用的绑定码作废。
- 只支持单进程部署：一个机器人只能有一条连接，两个进程会互相顶掉。
- 飞书还没有驱动：接口为它留了位置（事件长连接 + 改消息 / 更新卡片对应 `edit` 与 `cards`）。
- 只有回调地址（webhook）的 IM：桥没有给驱动开 HTTP 入口，要经内网的转发服务或拉取接口（[im-driver-authoring.md](../im-driver-authoring.md) 第二节）。

## 真机验证

2026-11-01 用一个真的企业微信智能机器人（个人机器人、长连接）按 [im-driver-authoring.md](../im-driver-authoring.md) 第六节走过：
绑定、单聊收发（先用脚本助理，再用 dsh + 平台配的模型）、定时到点主动推送、草稿的正文 + 按钮卡片与点“发送”（草稿发进了项目群聊）、
服务重启后自动重连且绑定还在，都和预期一致；网页里说的那一轮，企业微信里没有收到。脚本助理回得太快时只看得到最后的文字，看不到占位。
没在真机上专门测：没绑定的人、群里 @、图片与文件、被另一条连接顶掉（这些由 `tests/test_im.py`、`tests/test_wecom.py` 对假服务覆盖）。
