"""检查点恢复：一个执行单元的 dsh 会话与产物打成包，在另一台“机器”上恢复后能否续接（docs/cloud-runtime.md 5.2）。
要真模型（DEEPSEEK_API_KEY）。用法：
  python ckpt.py first <包路径>    跑第一轮（记住一个编号、写一个产物），把会话的两处文件与产物打成包
  python ckpt.py again <包路径>    在全新的 HOME 与工作目录上解包，续接会话：问编号、接着改产物
环境变量：CKPT_WORKDIR（工作目录的绝对路径，两边必须相同）、CKPT_HOME（dsh 的 HOME）、CKPT_DSH（dsh 可执行文件）。"""
import json
import os
import secrets
import sys
import tarfile
from pathlib import Path

from matrix.runtime import DshRuntime, RunRequest

step, pack = sys.argv[1], Path(sys.argv[2])
work = Path(os.environ.get("CKPT_WORKDIR", "/data/conversations/c1/a1"))
home = Path(os.environ.get("CKPT_HOME", "/tmp/unit-state"))
rt = DshRuntime(os.environ.get("CKPT_DSH", "dsh"), isolation="off")  # 不套 Matrix 的外层：HOME 由这里给
AUTH = ["file.write", "shell.exec"]


def run(prompt: str, session_id: str | None = None):
    return rt.run(RunRequest(agent_id="ckpt", context_id="ckpt", prompt=prompt, cwd=work, authority=AUTH, timeout_s=240,
                             session_id=session_id, env={"HOME": str(home)}))


if step == "first":
    code = "K-" + secrets.token_hex(3).upper()
    work.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    res = run(f"记住这个编号：{code}。然后用 bash 执行 `echo first > note.txt`。完成后只回复 OK。")
    print(f"FIRST_ok={res.ok} session={res.session_id} err={(res.error or '')[:200]}")
    dsh = home / ".dsh"
    sess = [d for d in (dsh / "sessions").glob(f"*/{res.session_id}") if d.is_dir()]
    idx = dsh / "storages" / "session_projcache" / "sessions" / f"{res.session_id}.json"
    with tarfile.open(pack, "w:gz") as t:
        for d in sess:  # 会话的第一处：sessions/--<工作目录路径>--/<会话 id>/，不带锁文件
            for f in d.iterdir():
                if f.name != "session.lock":
                    t.add(f, arcname=f"state/{f.relative_to(home)}")
        if idx.exists():  # 第二处：索引
            t.add(idx, arcname=f"state/{idx.relative_to(home)}")
        t.add(work / "note.txt", arcname="work/note.txt")  # 产物
        meta = json.dumps({"session": res.session_id, "code": code, "workdir": str(work)}).encode()
        info = tarfile.TarInfo("meta.json")
        info.size = len(meta)
        import io
        t.addfile(info, io.BytesIO(meta))
    print(f"CKPT_FILES={len(tarfile.open(pack).getnames())} CKPT_BYTES={pack.stat().st_size}")
else:
    assert not (home / ".dsh").exists() and not (work / "note.txt").exists(), "要在全新的 HOME 与工作目录上恢复"
    with tarfile.open(pack) as t:
        meta = json.load(t.extractfile("meta.json"))
        assert meta["workdir"] == str(work), "工作目录的绝对路径必须和打包时相同"
        for m in t.getmembers():
            if m.name.startswith("state/"):
                dest = home / m.name.removeprefix("state/")
            elif m.name.startswith("work/"):
                dest = work / m.name.removeprefix("work/")
            else:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(t.extractfile(m).read())
    res = run("上一轮让你记住的编号是多少？先用 bash 执行 `echo second >> note.txt`，然后只回复编号；不知道就回复 UNKNOWN。",
              session_id=meta["session"])
    note = (work / "note.txt").read_text(encoding="utf-8").split()
    print(f"AGAIN_ok={res.ok} same_session={res.session_id == meta['session']} err={(res.error or '')[:200]}")
    print(f"RESTORE_RECALL={'ok' if meta['code'] in res.final_text else 'FAIL（回复：' + res.final_text.strip()[:80] + '）'}")
    print(f"RESTORE_PRODUCT={'ok' if note == ['first', 'second'] else 'FAIL（note.txt：' + ' '.join(note) + '）'}")
