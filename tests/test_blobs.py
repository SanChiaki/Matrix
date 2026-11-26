"""内容存储（M8，docs/reference/storage.md「文件」）：本机目录与 S3 兼容存储、SigV4、读缓存按 sha 校验、暂存区清理。"""

from __future__ import annotations

import datetime as dt
import hashlib
import http.server
import os
import threading
import time
import urllib.parse
from collections.abc import Iterator
from pathlib import Path

import pytest

from matrix import blobs
from matrix.blobs import BlobError, LocalBlobs, S3Blobs, Staging, sigv4_headers


def test_sigv4_matches_reference_vector():
    """和 botocore 的 S3SigV4Auth 对同一个请求算出的签名一致（向量是用 botocore 算的）。"""
    now = dt.datetime(2026, 11, 25, 12, 0, 0, tzinfo=dt.UTC)
    h = sigv4_headers("PUT", "http://s3.local:9000/bkt/pre/ab/" + "a" * 64, "us-east-1", "AK", "SK",
                      hashlib.sha256(b"hello").hexdigest(), now=now, extra={"content-length": "5"})
    assert h["Authorization"].endswith("Signature=fdcab1781de469105b17b9c8474b32a477ba5848928b12185550ac8ce89036ba")
    assert h["x-amz-date"] == "20261125T120000Z"


def test_local_blobs_dedupe_and_verify(tmp_path: Path):
    b = LocalBlobs(tmp_path / "blobs")
    src = tmp_path / "a.txt"
    src.write_bytes(b"hello")
    sha, size = b.put_file(src)
    assert size == 5 and b.has(sha) and b.path(sha).read_bytes() == b"hello"
    assert b.path(sha).relative_to(tmp_path / "blobs").parts[:2] == (sha[:2], sha[2:4])
    assert b.put_file(src) == (sha, 5), "同一份内容只存一次"
    with pytest.raises(BlobError):
        b.put_file(src, sha="0" * 64)
    assert [x[0] for x in b.items()] == [sha]
    b.delete(sha)
    assert not b.has(sha) and b.path(sha) is None
    with pytest.raises(BlobError):
        b.path("../../etc/passwd")


def test_staging_sweep(tmp_path: Path):
    st = Staging(tmp_path / "tmp")
    p, sha = st.write(b"x")
    assert sha == hashlib.sha256(b"x").hexdigest() and p.read_bytes() == b"x"
    old = time.time() - blobs.STAGING_MAX_AGE_S - 10
    os.utime(p, (old, old))
    fresh, _ = st.write(b"y")
    assert st.sweep() == 1 and not p.exists() and fresh.exists()


class FakeS3(http.server.BaseHTTPRequestHandler):
    """够用的 S3：路径风格的 PUT / GET / HEAD / DELETE 与 ListObjectsV2，核对 SigV4 签名。"""

    objects: dict[str, tuple[bytes, float]] = {}
    bad_sig = 0

    def log_message(self, *a: object) -> None:
        pass

    def _auth_ok(self, body: bytes) -> bool:
        got = self.headers.get("Authorization", "")
        sha = self.headers.get("x-amz-content-sha256", "")
        if sha != hashlib.sha256(body).hexdigest():
            return False
        now = dt.datetime.strptime(self.headers["x-amz-date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC)
        extra = {"content-length": self.headers["content-length"]} if "content-length" in got else {}
        want = sigv4_headers(self.command, f"http://{self.headers['host']}{self.path}", "us-east-1", "AK", "SK", sha,
                             now=now, extra=extra)["Authorization"]
        if got != want:
            FakeS3.bad_sig += 1
            return False
        return True

    def _reply(self, code: int, body: bytes = b"") -> None:
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _handle(self) -> None:
        n = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(n) if n else b""
        if not self._auth_ok(body):
            return self._reply(403)
        u = urllib.parse.urlsplit(self.path)
        key = urllib.parse.unquote(u.path).removeprefix("/bkt/")
        if self.command == "PUT":
            FakeS3.objects[key] = (body, time.time())
            return self._reply(200)
        if self.command == "DELETE":
            FakeS3.objects.pop(key, None)
            return self._reply(204)
        if u.path == "/bkt":
            prefix = dict(urllib.parse.parse_qsl(u.query)).get("prefix", "")
            items = "".join(f"<Contents><Key>{k}</Key><Size>{len(v[0])}</Size>"
                            f"<LastModified>2026-11-25T12:00:00.000Z</LastModified></Contents>"
                            for k, v in FakeS3.objects.items() if k.startswith(prefix))
            xml = (f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">{items}'
                   f"<IsTruncated>false</IsTruncated></ListBucketResult>")
            return self._reply(200, xml.encode())
        if key not in FakeS3.objects:
            return self._reply(404)
        return self._reply(200, FakeS3.objects[key][0])

    do_PUT = do_GET = do_HEAD = do_DELETE = _handle


@pytest.fixture
def s3(tmp_path: Path) -> Iterator[S3Blobs]:
    FakeS3.objects, FakeS3.bad_sig = {}, 0
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeS3)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield S3Blobs(f"http://127.0.0.1:{srv.server_port}", "bkt", "matrix", "AK", "SK", "us-east-1", tmp_path / "cache")
    finally:
        srv.shutdown()


def test_s3_round_trip_and_cache_checks_sha(tmp_path: Path, s3: S3Blobs):
    src = tmp_path / "a.bin"
    src.write_bytes(b"\x00" * 1000 + "中文".encode())
    sha, size = s3.put_file(src)
    assert f"matrix/{sha[:2]}/{sha[2:4]}/{sha}" in FakeS3.objects and FakeS3.bad_sig == 0
    assert s3.has(sha) and not s3.has("0" * 64)
    p = s3.path(sha)
    assert p is not None and p.read_bytes() == src.read_bytes() and p.is_relative_to(tmp_path / "cache")
    assert [(x[0], x[1]) for x in s3.items()] == [(sha, size)]
    # 对象存储里的内容被改了：读缓存按 sha 校验，不认
    s3.cache.delete(sha)
    FakeS3.objects[f"matrix/{sha[:2]}/{sha[2:4]}/{sha}"] = (b"tampered", time.time())
    with pytest.raises(BlobError):
        s3.path(sha)
    s3.delete(sha)
    assert not s3.has(sha) and s3.path(sha) is None


def test_s3_wrong_secret_is_refused(tmp_path: Path, s3: S3Blobs):
    s3.secret_key = "wrong"
    src = tmp_path / "a"
    src.write_bytes(b"a")
    with pytest.raises(Exception):
        s3.put_file(src)
    assert FakeS3.bad_sig >= 1


def test_from_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    assert blobs.from_config(None, tmp_path) is None and blobs.from_config("legacy", tmp_path) is None
    local = blobs.from_config("local", tmp_path)
    assert isinstance(local, LocalBlobs) and local.root == tmp_path / "blobs"
    with pytest.raises(BlobError, match="MATRIX_S3_ENDPOINT"):
        blobs.from_config("s3://bkt/p", tmp_path)
    monkeypatch.setenv("MATRIX_S3_ENDPOINT", "http://s3.local:9000")
    monkeypatch.setenv("MATRIX_S3_ACCESS_KEY", "AK")
    monkeypatch.setenv("MATRIX_S3_SECRET_KEY", "SK")
    s = blobs.from_config("s3://bkt/p", tmp_path)
    assert isinstance(s, S3Blobs) and s.prefix == "p/" and "SK" not in s.describe()
    with pytest.raises(BlobError):
        blobs.from_config("ftp://x", tmp_path)
