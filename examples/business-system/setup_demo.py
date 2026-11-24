"""搭“业务系统对接 Matrix”的演示数据（脚本运行时，离线），然后起 Matrix 和模拟的报销系统看效果。

    uv run python examples/business-system/setup_demo.py

Matrix 这边准备的是“业务系统来对接之前，公司里已经有的东西”：

- 管理员 ops 把「报销系统」登记成外部应用，建了它的业务系统账号（负责人 maya），签了令牌，开了网页登录（只读）；
- 五个 Agent，演示多 Agent 协同：ops 维护的公共 Agent「财务助手」（服务的入口）「票据核验」「法务顾问」；
  ann 的个人 Agent「合同审核」、ben 的个人 Agent「预算管家」，两人都把它开放给了「报销系统」；
- 部署方目录里的模板「报销审核小组」与插件（examples/plugins.json）；
- maya 的项目「差旅政策」发布了服务「差旅咨询」，点名给报销系统用（“调用别人的服务”）；
- ann 也授权报销系统代表她自己（只给“查”），演示第二种身份。

项目、资料、技能、服务都**不在这里建**：那是报销系统接入以后经 /open/v1 自己做的，演示时一步步点出来。
令牌与系统账号的网页登录密码写进 `<数据目录>/biz-system.json`，给模拟的报销系统读（真实系统要放进密钥管理）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parents[1]))

from matrix import auth  # noqa: E402
from matrix import plugins as pluginfs  # noqa: E402
from matrix.engine import Network  # noqa: E402
from matrix.runtime import ScriptedRuntime  # noqa: E402
from matrix.scripted import scenario_handler  # noqa: E402

PASSWORD = "demo-pass-123"
SYSTEM_PASSWORD = "biz-system-123"
CONFIG = "biz-system.json"
PLUGINS = ROOT.parent / "plugins.json"


def _entries(src: Path, prefix: str = "expense-review") -> list[tuple[str, bytes]]:
    return [(f"{prefix}/{p.relative_to(src).as_posix()}", p.read_bytes()) for p in sorted(src.rglob("*")) if p.is_file()]


def build(data: Path, matrix_url: str = "http://127.0.0.1:8765", quiet: bool = False) -> dict[str, Any]:
    """在 data 里搭好演示用的 Matrix 数据，返回（并写进 biz-system.json）报销系统要用的配置。"""
    say = (lambda *_: None) if quiet else (lambda s: print(s, flush=True))
    net = Network(ScriptedRuntime(scenario_handler), data, seed=False, plugins=pluginfs.PluginCatalog(PLUGINS))
    ids = {}
    for email in ("ops@demo.local", "maya@demo.local", "ann@demo.local", "ben@demo.local"):  # 第一个注册的 ops 是管理员
        ids[email.split("@")[0]] = auth.register(net.store, email, PASSWORD)[0].id
        net.ensure_assistant(ids[email.split("@")[0]])
    ops, maya, ann, ben = ids["ops"], ids["maya"], ids["ann"], ids["ben"]

    def public(name: str, desc: str, caps: list[str], authority: list[str]):
        a = net.create_agent(name, desc, caps, authority, actor=ops)
        net.request_public(a.id, desc, [], actor=ops)
        net.review_public(a.id, True, actor=ops)
        return a

    public("财务助手", "公司的财务入口：审核报销单、回答报销与差旅制度；大额的分头交给票据、预算、合同", ["报销审核", "差旅制度"], ["delegate"])
    public("票据核验", "校验发票真伪、金额与开票方", ["发票校验"], [])
    public("法务顾问", "看合同里的违约、保密、争议条款的风险", ["合同风险"], [])
    contract = net.create_agent("合同审核", "看合同条款：服务期、金额、付款节点；有风险条款转给法务", ["合同审核"],
                                ["delegate"], actor=ann)
    budget = net.create_agent("预算管家", "查部门预算余额，判断这笔开销在不在预算内", ["预算"], [], actor=ben)
    say("· 公共 Agent「财务助手」「票据核验」「法务顾问」（ops 维护）；个人 Agent「合同审核」（ann）、「预算管家」（ben）")

    bp = net.admin_blueprint_upload(ops, _entries(ROOT / "blueprint"))
    say(f"· 部署方目录里的模板「{bp['title']}」")

    app = net.create_app(ops, "报销系统", "公司的报销系统：员工提交报销单，经 Matrix 的服务审核", system=True,
                         managers=["maya@demo.local"])
    sysu = net.store.users[app.system_user_id]
    net.set_agent_open(contract.id, [app.id], actor=ann)
    net.set_agent_open(budget.id, [app.id], actor=ben)
    _, token = net.issue_system_token(ops, app.id)
    _, personal = net.grant_app(ann, app.id, ["agents.read"])
    reset, _ = auth.issue_reset(net.store, sysu.id, by=ops)
    auth.reset_password(net.store, reset, SYSTEM_PASSWORD)
    say(f"· 管理员登记了「报销系统」：业务系统账号 {sysu.email}（负责人 maya），签了令牌、开了网页登录；ann、ben 把自己的 Agent 开放给它")

    fin = next(a for a in net.store.agents.values() if a.name == "财务助手")
    travel = net.create_project("差旅政策", "公司的差旅标准与咨询", [fin.id], actor=maya)
    net.publish_file(travel.id, "公开/差旅标准.md", (ROOT / "materials" / "差旅标准.md").read_bytes(), actor=maya)
    x = net.create_service(travel.id, "chailv", "差旅咨询", "差旅标准咨询：住宿、交通、补助", actor=maya, entry=fin.id,
                           scope=["公开"], readers=[sysu.email])
    say(f"· maya 发布了服务「{x.title}」，点名给报销系统用")
    net.store.save()

    config = {"matrix_url": matrix_url.rstrip("/"), "app": app.name, "token": token, "personal_token": personal,
              "personal_owner": "ann@demo.local",
              "services": {"差旅咨询": x.id},  # 别人发布的服务：服务 id 由发布方告诉你
              "system_login": {"email": sysu.email, "password": SYSTEM_PASSWORD}}
    (data / CONFIG).write_text(json.dumps(config, ensure_ascii=False, indent=1), encoding="utf-8")
    return config


def main() -> None:
    ap = argparse.ArgumentParser(description="搭“业务系统对接 Matrix”的演示数据")
    ap.add_argument("--data", default=".matrix-biz-demo")
    ap.add_argument("--matrix-url", default="http://127.0.0.1:8765")
    ap.add_argument("--fresh", action="store_true", help="先删掉已有的数据目录")
    args = ap.parse_args()
    data = Path(args.data).resolve()
    if data.exists():
        if not args.fresh:
            sys.exit(f"{data} 已经存在：加 --fresh 重建")
        shutil.rmtree(data)
    data.mkdir(parents=True)
    cfg = build(data, args.matrix_url)
    print(f"""
演示数据在 {data}。看效果（两个终端）：
  uv run matrix --runtime scripted --data {args.data} --plugins examples/plugins.json
  uv run python examples/business-system/biz_system.py --config {data / CONFIG}
打开 http://127.0.0.1:8800 是模拟的报销系统。
Matrix 网页（{cfg['matrix_url']}）：
  系统账号只读页面：{cfg['system_login']['email']} / {cfg['system_login']['password']}
  负责人 maya@demo.local、ann@demo.local、ben@demo.local、管理员 ops@demo.local，密码都是 {PASSWORD}""")


if __name__ == "__main__":
    main()
