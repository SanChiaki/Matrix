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


def fake_bin(tmp_path: Path) -> str:
    """把假 Agent 包成一个可执行文件（真的 Agent 就是你们装在电脑上的那个程序）。"""
    exe = tmp_path / "demo-agent"
    exe.write_text(f"#!{sys.executable}\nimport sys; sys.path.insert(0, {str(ROOT)!r})\n"
                   "from matrix_device_demo.fake_agent import main\nmain()\n", encoding="utf-8")
    exe.chmod(0o755)
    return str(exe)


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
    d = DemoAgentDriver(bin_path=fake_bin(tmp_path))
    assert d.available()
    ctx = Ctx()
    out = d.run(spec(tmp_path, "报销单 42"), ctx, threading.Event())
    assert out.ok and out.final_text == "已处理：报销单 42" and out.session_id
    assert ctx.events == [{"type": "text", "text": "收到，处理中…"}] and out.tool_calls[0]["name"] == "查单据"
    again = d.run(spec(tmp_path, "继续", out.session_id), Ctx(), threading.Event())
    assert again.session_id == out.session_id


def test_failure_is_reported(tmp_path):
    out = DemoAgentDriver(bin_path=fake_bin(tmp_path)).run(spec(tmp_path, "请失败"), Ctx(), threading.Event())
    assert not out.ok and "exited 3" in out.error and "按要求失败" in out.error
