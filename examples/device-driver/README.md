# 演示用的设备端驱动包：把业务系统自己的设备端 Agent 接进 matrix-connect 的样板

`demo-agent` 是一个虚构的设备端 Agent（命令行程序，协议写在 `matrix_device_demo/driver.py` 开头）。
你们的 Agent 换成它自己的调用方式：复制这个目录，照 [docs/device-driver-authoring.md](../../docs/device-driver-authoring.md) 改
`driver.py` 里标着「协议」的三处，改驱动的 `name`、包名与 entry point。

```bash
uv run pytest -q examples/device-driver/tests       # 驱动自己的测试（在 Matrix 仓库根目录）
```

完整走一遍（服务器认这个 harness、签一次性令牌、连接器凭令牌配对）见指南第六节。

| 文件 | 是什么 |
| --- | --- |
| `pyproject.toml` | 包与 entry point `matrix.connect_drivers`（matrix-connect 按它发现驱动） |
| `matrix_device_demo/driver.py` | 驱动：怎么拉起一轮、交给它什么、怎么读它的输出，以及能力报告 |
| `matrix_device_demo/fake_agent.py` | 假的 `demo-agent`：本地测试用，真的 Agent 不需要 |
| `tests/test_demo_device_driver.py` | 结构检查与跑一轮的测试（Matrix 的 `uv run pytest` 也会跑它） |
