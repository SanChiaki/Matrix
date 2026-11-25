"""附件进内容存储（M8，docs/reference/storage.md「文件」）：全站去重但只认本对话上传过的、先写字节再记引用、双读、
搬迁命令与回退、没人引用的过宽限期才删、存储配额。"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from matrix import blobs as blobmod
from matrix import storage_cli
from matrix.engine import Network, NetworkError
from matrix.models import ATTACHMENTS, attachment_rel
from matrix.runtime import ScriptCall, ScriptedRuntime
from tests.test_blobs import s3 as s3   # noqa: F401 —— 假的 S3 服务
from tests.test_users import users


def _net(path: Path, seen: list[ScriptCall] | None = None) -> Network:
    def handler(call: ScriptCall) -> str:
        if seen is not None:
            call.files = {p.relative_to(call.cwd).as_posix(): p.read_bytes()
                          for p in (call.cwd / ATTACHMENTS).rglob("*") if p.is_file()}
            seen.append(call)
        return "收到"
    return Network(ScriptedRuntime(handler), path, seed=False)


@pytest.fixture
def local_blobs(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("MATRIX_BLOBS", "local")
    yield


def _chat(net: Network, email: str = "alice@x.io") -> tuple[str, str, str]:
    uid = users(net, email)[0]
    a = net.create_agent(f"A-{email}", "a", [], ["file.write"], actor=uid)
    return uid, a.id, net.direct_chat(a.id, actor=uid).id


def test_uploads_go_to_the_blob_store_and_reach_the_seat(tmp_path: Path, local_blobs: None):
    seen: list[ScriptCall] = []
    net = _net(tmp_path, seen)
    uid, aid, chat = _chat(net)
    meta = net.upload_attachment(chat, "报告.txt", b"hello blob", "text/plain", actor=uid)
    sha = meta["sha"]
    assert (tmp_path / "blobs" / sha[:2] / sha[2:4] / sha).read_bytes() == b"hello blob"
    assert not (tmp_path / "attachments" / chat / sha).exists(), "新文件只写内容存储"
    assert net.store.attachment_refs[chat][sha]["by"] == uid
    net.send_message(chat, "看看", attachments=[meta], background=False, actor=uid)
    assert seen[-1].files == {attachment_rel(meta): b"hello blob"}
    f, m = net.attachment_file(chat, sha, actor=uid)
    assert f.read_bytes() == b"hello blob" and m["name"] == "报告.txt"
    assert not list((tmp_path / "tmp" / "uploads").iterdir()), "暂存区用完就删"


def test_dedup_does_not_leak_across_conversations(tmp_path: Path, local_blobs: None):
    """内容存储全站去重，但消息上的附件只认这个对话里上传过的：知道别人文件的 sha 也挂不到自己的消息上。"""
    net = _net(tmp_path)
    alice, _, chat_a = _chat(net, "alice@x.io")
    bob, _, chat_b = _chat(net, "bob@x.io")
    meta = net.upload_attachment(chat_a, "机密.txt", b"secret", "", actor=alice)
    with pytest.raises(NetworkError, match="先上传到这个对话"):
        net.send_message(chat_b, "偷看", attachments=[meta], actor=bob)
    with pytest.raises(NetworkError):
        net.attachment_file(chat_b, meta["sha"], actor=bob)
    again = net.upload_attachment(chat_b, "同样的内容.txt", b"secret", "", actor=bob)   # 自己有同样的内容：照样能传
    assert again["sha"] == meta["sha"] and len(list(net.blobs.items())) == 1


def test_bytes_before_refs(tmp_path: Path, local_blobs: None, monkeypatch: pytest.MonkeyPatch):
    """先写字节再记引用：放进内容存储失败时状态里不留引用。"""
    net = _net(tmp_path)
    uid, _, chat = _chat(net)
    monkeypatch.setattr(net.blobs, "put_file", lambda *a, **k: (_ for _ in ()).throw(blobmod.BlobError("盘满了")))
    with pytest.raises(NetworkError, match="没存上"):
        net.upload_attachment(chat, "a.txt", b"x", "", actor=uid)
    assert chat not in net.store.attachment_refs


def test_old_attachments_read_from_legacy_dir_then_migrate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """双读：切到内容存储之前上传的附件照样能读；matrix migrate blobs 搬过去（重算 sha），可重复；
    --to-legacy 回退；--purge-legacy 只删已经搬走的。"""
    monkeypatch.delenv("MATRIX_BLOBS", raising=False)
    net = _net(tmp_path)
    uid, _, chat = _chat(net)
    meta = net.upload_attachment(chat, "旧的.txt", b"old bytes", "", actor=uid)
    net.send_message(chat, "旧消息", attachments=[meta], background=False, actor=uid)
    legacy = tmp_path / "attachments" / chat / meta["sha"]
    assert legacy.is_file()
    bad = tmp_path / "attachments" / chat / ("0" * 64)   # 名字和内容对不上的：不搬
    bad.write_bytes(b"tampered")
    net.store.close()

    monkeypatch.setenv("MATRIX_BLOBS", "local")
    net = _net(tmp_path)
    assert net.attachment_file(chat, meta["sha"], actor=uid)[0] == legacy, "还没搬：从旧目录读"
    net.store.close()
    args = ["--data", str(tmp_path)]
    assert storage_cli.migrate_blobs(args) == 1, "有 sha 不对的要报出来"
    assert storage_cli.migrate_blobs(args) == 1, "可重复"
    net = _net(tmp_path)
    f, _ = net.attachment_file(chat, meta["sha"], actor=uid)
    assert f.is_relative_to(tmp_path / "blobs") and f.read_bytes() == b"old bytes"
    net.store.close()
    assert storage_cli.migrate_blobs([*args, "--purge-legacy"]) == 0 and legacy.exists(), "不带 --yes 只列出"
    assert storage_cli.migrate_blobs([*args, "--purge-legacy", "--yes"]) == 0
    assert not legacy.exists() and bad.exists(), "只删搬走了的"

    # 回退：拷回旧目录，旧版本（不认识内容存储）照样读得到
    assert storage_cli.migrate_blobs([*args, "--to-legacy"]) == 0
    assert legacy.read_bytes() == b"old bytes"


def test_switching_back_to_legacy_still_reads_local_blobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MATRIX_BLOBS", "local")
    net = _net(tmp_path)
    uid, _, chat = _chat(net)
    meta = net.upload_attachment(chat, "a.txt", b"in blobs", "", actor=uid)
    net.send_message(chat, "x", attachments=[meta], background=False, actor=uid)
    net.store.close()
    monkeypatch.setenv("MATRIX_BLOBS", "legacy")
    net = _net(tmp_path)
    assert net.attachment_file(chat, meta["sha"], actor=uid)[0].read_bytes() == b"in blobs"


def test_gc_only_unreferenced_after_grace(tmp_path: Path, local_blobs: None):
    net = _net(tmp_path)
    uid, aid, chat = _chat(net)
    keep = net.upload_attachment(chat, "keep.txt", b"keep", "", actor=uid)
    net.send_message(chat, "x", attachments=[keep], background=False, actor=uid)
    gone = net.upload_attachment(chat, "gone.txt", b"gone", "", actor=uid)   # 传了没发
    with net.store.lock:
        net._prune_attachments(chat)
    assert gone["sha"] not in net.store.attachment_refs[chat] and keep["sha"] in net.store.attachment_refs[chat]
    assert net.gc_blobs() == 0, "宽限期内不删"
    old = time.time() - blobmod.GC_GRACE_S - 60
    for sha in (keep["sha"], gone["sha"]):
        p = net.blobs.path(sha)
        os.utime(p, (old, old))
    assert net.gc_blobs() == 1
    assert net.blobs.has(keep["sha"]) and not net.blobs.has(gone["sha"])


def test_quota_per_user_and_site(tmp_path: Path, local_blobs: None, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MATRIX_QUOTA_USER_MB", "0.001")   # 1000 字节
    net = _net(tmp_path)
    uid, _, chat = _chat(net)
    net.upload_attachment(chat, "a", b"x" * 800, "", actor=uid)
    net.upload_attachment(chat, "a-again", b"x" * 800, "", actor=uid)   # 同一份不再算
    with pytest.raises(NetworkError, match="配额"):
        net.upload_attachment(chat, "b", b"y" * 300, "", actor=uid)
    bob, _, chat_b = _chat(net, "bob@x.io")
    net.upload_attachment(chat_b, "b", b"y" * 300, "", actor=bob)   # 每人分开算
    assert net.storage_usage()["by_user"] == {uid: 800, bob: 300}
    monkeypatch.setenv("MATRIX_QUOTA_TOTAL_GB", "0.0000012")   # 1200 字节
    with pytest.raises(NetworkError, match="全站"):
        net.upload_attachment(chat_b, "c", b"z" * 200, "", actor=bob)


def test_s3_backend_end_to_end(tmp_path: Path, s3: blobmod.S3Blobs, monkeypatch: pytest.MonkeyPatch):  # noqa: F811
    seen: list[ScriptCall] = []
    net = _net(tmp_path, seen)
    net.blobs = s3
    uid, _, chat = _chat(net)
    meta = net.upload_attachment(chat, "云上.txt", b"in s3", "", actor=uid)
    assert any(meta["sha"] in k for k in __import__("tests.test_blobs", fromlist=["FakeS3"]).FakeS3.objects)
    net.send_message(chat, "看", attachments=[meta], background=False, actor=uid)
    assert seen[-1].files == {attachment_rel(meta): b"in s3"}
    f, _ = net.attachment_file(chat, meta["sha"], actor=uid)
    assert f.read_bytes() == b"in s3" and f.is_relative_to(s3.cache.root), "下载经 Matrix：从本机读缓存给"
    assert hashlib.sha256(f.read_bytes()).hexdigest() == meta["sha"]
