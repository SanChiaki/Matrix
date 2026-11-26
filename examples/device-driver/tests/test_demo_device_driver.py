"""样板驱动的测试：结构是否符合 matrix-connect 的外部驱动约定，以及用假 Agent 跑一轮。"""

import sys
import threading
import tomllib
from pathlib import Path

from matrix_device_demo.driver import DemoAgentDriver

from matrix.connector.drivers.base import RunSpec

ROOT = Path(__file__).resolve().parents[1]


class Ctx:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


class FakeAgentDriver(DemoAgentDriver):
    """拉起假 Agent 而不是 PATH 上的 demo-agent：直接用当前的 Python 跑它，Windows 上也一样
    （真的 Agent 就是你们装在电脑上的那个程序，不用这样）。"""

    def command(self, spec):
        return [sys.executable, str(ROOT / "matrix_device_demo" / "fake_agent.py"), *super().command(spec)[1:]]


def spec(tmp_path, prompt, session_id=None):
    return RunSpec(run_id="r1", agent_id="a1", context_id="c1", prompt=prompt, workdir=tmp_path / "w",
                   authority=[], tool_names=[], session_id=session_id)


def test_entry_point_points_at_the_driver():
    eps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["entry-points"]
    module, _, attr = eps["matrix.connect_drivers"]["demo-agent"].partition(":")
    assert (module, attr) == ("matrix_device_demo.driver", "DemoAgentDriver")
    d = DemoAgentDriver(bin_path="/nope", bridge_url="")
    assert d.name == "demo-agent" and d.capabilities().placement == "device" and not d.available()


def test_runs_a_turn_and_resumes_the_session(tmp_path):
    assert DemoAgentDriver(bin_path=sys.executable).available()  # 按路径找得到程序就算装了
    d = FakeAgentDriver()
    ctx = Ctx()
    out = d.run(spec(tmp_path, "报销单 42"), ctx, threading.Event())
    assert out.ok and out.final_text == "已处理：报销单 42" and out.session_id
    assert ctx.events == [{"type": "text", "text": "收到，处理中…"}] and out.tool_calls[0]["name"] == "查单据"
    again = d.run(spec(tmp_path, "继续", out.session_id), Ctx(), threading.Event())
    assert again.session_id == out.session_id


def test_failure_is_reported(tmp_path):
    out = FakeAgentDriver().run(spec(tmp_path, "请失败"), Ctx(), threading.Event())
    assert not out.ok and "exited 3" in out.error and "按要求失败" in out.error
