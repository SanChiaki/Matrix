"""让 dsh 里的 Agent 用 bash 跑一条指定命令，打印命令输出。模型是 matrix-dsh-check 的假模型服务，不调真模型、不要密钥。
用法（在部署镜像的 /app 下）：python probe.py <dsh 可执行文件> <工作目录> <命令> [权限...]"""
import sys
from pathlib import Path

from matrix.dsh_check import FakeProvider
from matrix.model_catalog import ModelPreset, Provider, llm_config
from matrix.runtime import AUTHORITY_PLUGINS, DshRuntime, RunRequest

dsh, cwd, cmd = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
auth = sys.argv[4:] or list(AUTHORITY_PLUGINS)
fake = FakeProvider(cmd)
prov = Provider(id="probe", name="probe", api="openai-completions", base_url=fake.url,
                compat={"supportsDeveloperRole": False, "maxTokensField": "max_tokens"})
preset = ModelPreset(id="p", label="p", provider="probe", model="fake-1")
try:
    res = DshRuntime(dsh).run(RunRequest(agent_id="probe", context_id="probe", prompt="go", cwd=cwd, authority=auth,
                                         model=preset.run_model(), timeout_s=120,
                                         llm_provider={"id": "probe", "config": llm_config(prov, preset)},
                                         env={prov.key_env: "sk-probe-secret"}))
finally:
    fake.server.shutdown()
out = [x for r in fake.requests for x in r["results"]]
print(f"ok={res.ok} err={(res.error or '')[:300]}")
print("\n---\n".join(out) or "NO_TOOL_RESULT=1（Agent 的 bash 没有跑起来）")
