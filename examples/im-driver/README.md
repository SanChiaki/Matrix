# 演示 IM 的驱动包：企业内部 IM 接入的样板

“演示 IM”是虚构的 IM（一条出站 WebSocket，JSON 帧，协议写在 `matrix_im_demo/devserver.py` 开头）。
它能改已发的消息、不能发卡片，所以接进来后：回复先“收到，正在处理…”再改成正式回复；草稿用文字指令“发送 1 / 放弃 1”。

接你们的 IM 时复制这个目录，照 [docs/im-driver-authoring.md](../../docs/im-driver-authoring.md) 改：
换掉 `driver.py` 里标着「协议」的几个方法，定 `caps`，改包名与 entry point。

```bash
uv run pytest -q examples/im-driver/tests                                                   # 驱动自己的测试（在 Matrix 仓库根目录）
uv run --with-editable examples/im-driver python -m matrix_im_demo.devserver --port 8790     # 终端 1：本地的演示 IM，在终端里扮演 IM 里的人
DEMO_IM_URL=ws://127.0.0.1:8790 uv run --with-editable examples/im-driver matrix --runtime scripted   # 终端 2：装着这个驱动的 Matrix
```

然后在 Matrix 的“助理与定时 → 在 IM 里找助理 → 接上我自己的机器人”选“演示 IM”，App Secret 填 `demo-secret`，把拿到的绑定码在终端 1 里发出去：
`alice: <绑定码>`，再 `alice: 有什么要我处理的？`。

| 文件 | 是什么 |
| --- | --- |
| `pyproject.toml` | 包与 entry point `matrix.im_drivers`（Matrix 按它发现驱动） |
| `matrix_im_demo/driver.py` | 驱动：连接线程 + 心跳线程 + 派发线程，登录、收、发、改消息 |
| `matrix_im_demo/devserver.py` | 本地的演示 IM：测试用，也能交互 |
| `tests/test_demo_im_driver.py` | 结构检查与收发测试（Matrix 的 `uv run pytest` 也会跑它） |
