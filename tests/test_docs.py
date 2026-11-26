"""给业务系统的接口文档（docs/api-for-business-systems.md）要和代码对得上：写了的接口都在，请求体的字段都写了。"""
import re
from pathlib import Path

from matrix import server
from matrix.engine import Network
from matrix.runtime import ScriptedRuntime

DOC = Path(__file__).resolve().parent.parent / "docs" / "api-for-business-systems.md"


def test_documented_endpoints_exist(tmp_path: Path):
    """每个 `#### METHOD /路径` 小节对应一条真的路由。"""
    app = server.create_app(Network(ScriptedRuntime(lambda call: "ok"), tmp_path, seed=False))
    routes = {(m, r.path) for r in app.routes for m in getattr(r, "methods", None) or ()}
    heads = re.findall(r"^#### `(GET|POST|PATCH|PUT|DELETE) (/api/[^`]+)`", DOC.read_text(encoding="utf-8"), re.M)
    assert len(heads) >= 25
    missing = [f"{m} {p}" for m, p in heads if (m, p) not in routes]
    assert not missing, f"文档里写了、代码里没有的接口：{missing}"


def test_documented_request_fields_cover_the_models():
    """请求体模型里的字段都在对应小节里写了（新加字段要同步改文档）。只给 Matrix 网页用的字段列在 skip 里。"""
    text = DOC.read_text(encoding="utf-8")
    sections = {m.group(1): m.group(2) for m in re.finditer(r"^#### `([A-Z]+ /api/[^`]+)`(.*?)(?=^#### |^### |^## |\Z)",
                                                             text, re.M | re.S)}
    skip = {"model", "device_runtime", "harness", "skills", "connections", "subscriptions", "join"}
    cases = {
        "POST /api/agents": server.AgentIn, "PATCH /api/agents/{agent_id}": server.AgentPatch,
        "POST /api/projects": server.ProjectIn, "PATCH /api/projects/{project_id}": server.ProjectPatch,
        "POST /api/projects/{project_id}/members": server.MemberIn,
        "POST /api/projects/{project_id}/invitations": server.InviteIn,
        "POST /api/projects/{project_id}/invitation": server.RespondIn,
        "POST /api/projects/{project_id}/sessions": server.SessionIn,
        "PATCH /api/sessions/{conv_id}": server.SessionPatch,
        "POST /api/conversations/{conv_id}/messages": server.MessageIn,
        "POST /api/questions/{question_id}": server.AnswerIn,
    }
    for head, model in cases.items():
        body = sections[head]
        missing = [f for f in model.model_fields if f not in skip and f"`{f}`" not in body]
        assert not missing, f"{head} 的文档没写这些请求字段：{missing}"
