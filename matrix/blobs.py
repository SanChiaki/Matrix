"""内容存储（M8，docs/reference/storage.md「文件」）：写一次不再改的文件按 sha256 寻址，全站去重。

- `LocalBlobs`：本机目录 `<root>/ab/cd/<sha>`。
- `S3Blobs`：S3 兼容的对象存储（自己签 SigV4，仅标准库）。下载经 Matrix：先做可见性检查，再从本机读缓存
  （`<data>/cache/blobs/`，按 sha 校验）把文件交给浏览器；对象存储本身不对外暴露。
- `Staging`：暂存区 `<data>/tmp/`：上传先流式写进这里、边写边算 sha、fsync，再放进内容存储；
  超过 24 小时的残留定期删掉。

规则（方案文档「文件存储方案」）：先写字节，再写库。中途失败最多留下没人引用的块，不会出现“库里有记录、文件不在”。
写入时不维护引用计数：引用以状态里的记录为准（`Store.attachment_refs`），没有引用且过了宽限期才由 `gc` 删。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import http.client
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from . import compat

SHA_RE = re.compile(r"[0-9a-f]{64}")
GC_GRACE_S = 7 * 24 * 3600      # 没人引用的内容过这么久才删（误删比晚删代价大得多）
STAGING_MAX_AGE_S = 24 * 3600   # 暂存区的残留过这么久一律删


class BlobError(RuntimeError):
    pass


def check_sha(sha: str) -> str:
    if not SHA_RE.fullmatch(sha or ""):
        raise BlobError(f"非法的 sha：{sha!r}")
    return sha


def _fsync_file(p: Path) -> None:
    # 要用可写的句柄：Windows 上对只读打开的文件 fsync（FlushFileBuffers）会报 EBADF
    with p.open("r+b") as f:
        os.fsync(f.fileno())


def sha_file(p: Path) -> tuple[str, int]:
    h, n = hashlib.sha256(), 0
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


class Staging:
    """暂存区：上传中途的文件、分块传输。一个文件一个随机名字，用完就删；`sweep` 清掉超过 24 小时的。"""

    def __init__(self, root: Path):
        self.root = root

    def write(self, data: bytes) -> tuple[Path, str]:
        """写进暂存区并 fsync，返回（路径, sha）。"""
        self.root.mkdir(parents=True, exist_ok=True)
        p = self.root / f"up-{uuid.uuid4().hex}"
        with p.open("wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        return p, hashlib.sha256(data).hexdigest()

    def sweep(self, max_age_s: float = STAGING_MAX_AGE_S) -> int:
        n, cutoff = 0, time.time() - max_age_s
        for p in self.root.rglob("*") if self.root.is_dir() else []:
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
                    n += 1
            except OSError:
                continue
        return n


class LocalBlobs:
    """本机目录 `<root>/ab/cd/<sha>`，文件只读。"""

    kind = "local"

    def __init__(self, root: Path):
        self.root = root

    def describe(self) -> str:
        return str(self.root)

    def _p(self, sha: str) -> Path:
        check_sha(sha)
        return self.root / sha[:2] / sha[2:4] / sha

    def has(self, sha: str) -> bool:
        try:
            return self._p(sha).is_file()
        except BlobError:
            return False

    def path(self, sha: str) -> Path | None:
        """本机能读的那个文件；没有就是 None。"""
        p = self._p(sha)
        return p if p.is_file() else None

    def put_file(self, src: Path, sha: str | None = None) -> tuple[str, int]:
        """放进来（拷一份，不动原文件）。sha 给了就核对。已经有了就不再写。"""
        got, size = sha_file(src)
        if sha is not None and got != sha:
            raise BlobError(f"内容和 sha 对不上：{src}")
        dest = self._p(got)
        if dest.is_file():
            return got, size
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{got}.{uuid.uuid4().hex[:8]}")
        shutil.copyfile(src, tmp)
        _fsync_file(tmp)
        tmp.chmod(0o444)
        os.replace(tmp, dest)
        return got, size

    def delete(self, sha: str) -> None:
        p = self._p(sha)
        if p.exists():
            p.chmod(0o644)
            p.unlink(missing_ok=True)

    def items(self) -> Iterator[tuple[str, int, float]]:
        """（sha, 大小, 放进来的时间）。"""
        for p in self.root.glob("*/*/*") if self.root.is_dir() else []:
            if SHA_RE.fullmatch(p.name):
                st = p.stat()
                yield p.name, st.st_size, st.st_mtime


# ---------------- S3 兼容存储 ----------------
def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sigv4_headers(method: str, url: str, region: str, access_key: str, secret_key: str, payload_sha: str,
                  now: dt.datetime | None = None, extra: dict[str, str] | None = None) -> dict[str, str]:
    """AWS Signature Version 4 的请求头（service = s3）。payload_sha 是请求体的 sha256（空请求体也要算）。"""
    u = urllib.parse.urlsplit(url)
    now = now or dt.datetime.now(dt.UTC)
    amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    headers = {"host": u.netloc, "x-amz-content-sha256": payload_sha, "x-amz-date": amz_date,
               **{k.lower(): v for k, v in (extra or {}).items()}}
    signed = ";".join(sorted(headers))
    query = "&".join(f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}"
                     for k, v in sorted(urllib.parse.parse_qsl(u.query, keep_blank_values=True)))
    canonical = "\n".join([method, urllib.parse.quote(urllib.parse.unquote(u.path or "/"), safe="/-_.~"), query,
                           "".join(f"{k}:{headers[k].strip()}\n" for k in sorted(headers)), signed, payload_sha])
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    k = _sign(_sign(_sign(_sign(f"AWS4{secret_key}".encode(), day), region), "s3"), "aws4_request")
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    out = {k2: v for k2, v in headers.items() if k2 != "host"}
    out["Authorization"] = f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={sig}"
    return out


EMPTY_SHA = hashlib.sha256(b"").hexdigest()


class S3Blobs:
    """S3 兼容存储（路径风格：`<endpoint>/<bucket>/<prefix>ab/cd/<sha>`，和本机目录同一个布局）。读的时候拉到本机读缓存（按 sha 校验），
    缓存可以随时删。凭证只从部署方的环境变量来（名字带 KEY）。"""

    kind = "s3"

    def __init__(self, endpoint: str, bucket: str, prefix: str, access_key: str, secret_key: str, region: str,
                 cache: Path, timeout: float = 60):
        self.endpoint = endpoint.rstrip("/")
        self.bucket, self.prefix = bucket, prefix.strip("/") + "/" if prefix.strip("/") else ""
        self.access_key, self.secret_key, self.region = access_key, secret_key, region
        self.cache = LocalBlobs(cache)
        self.timeout = timeout

    def describe(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}（{self.endpoint}）"

    def _key(self, sha: str) -> str:
        check_sha(sha)
        return f"{self.prefix}{sha[:2]}/{sha[2:4]}/{sha}"   # 和本机目录同一个布局：blobs/ 可以原样同步过去

    def _url(self, key: str = "", query: str = "") -> str:
        path = f"/{self.bucket}/{urllib.parse.quote(key, safe='/-_.~')}" if key else f"/{self.bucket}"
        return f"{self.endpoint}{path}{'?' + query if query else ''}"

    def _req(self, method: str, url: str, body: Any = None, payload_sha: str = EMPTY_SHA,
             size: int | None = None) -> Any:
        extra = {"content-length": str(size)} if size is not None else {}
        headers = sigv4_headers(method, url, self.region, self.access_key, self.secret_key, payload_sha, extra=extra)
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)  # 地址只来自部署方的配置
        except urllib.error.HTTPError:
            raise   # 状态码由调用方分辨（404 = 没有）
        except (OSError, http.client.HTTPException) as e:   # 连不上、超时、连接中断：统一成 BlobError，调用方只认它
            raise BlobError(f"连不上对象存储（{method}）：{e}") from e

    def has(self, sha: str) -> bool:
        try:
            with self._req("HEAD", self._url(self._key(sha))):
                return True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            raise BlobError(f"对象存储 HEAD 失败：{e.code}") from e

    def path(self, sha: str) -> Path | None:
        hit = self.cache.path(sha)
        if hit is not None:
            return hit
        tmp = self.cache.root / ".incoming" / uuid.uuid4().hex
        tmp.parent.mkdir(parents=True, exist_ok=True)
        try:
            try:
                with self._req("GET", self._url(self._key(sha))) as r, tmp.open("wb") as f:
                    shutil.copyfileobj(r, f, 1 << 20)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                raise BlobError(f"对象存储 GET 失败：{e.code}") from e
            except (OSError, http.client.HTTPException) as e:   # 下到一半断了
                raise BlobError(f"对象存储 GET 中断：{e}") from e
            self.cache.put_file(tmp, sha)   # 校验 sha：对象存储里的内容被改过就不认
        finally:
            tmp.unlink(missing_ok=True)
        return self.cache.path(sha)

    def put_file(self, src: Path, sha: str | None = None) -> tuple[str, int]:
        got, size = sha_file(src)
        if sha is not None and got != sha:
            raise BlobError(f"内容和 sha 对不上：{src}")
        if not self.has(got):
            try:
                with src.open("rb") as f, self._req("PUT", self._url(self._key(got)), body=f, payload_sha=got, size=size):
                    pass
            except urllib.error.HTTPError as e:
                raise BlobError(f"对象存储 PUT 失败：{e.code}") from e
        return got, size

    def delete(self, sha: str) -> None:
        try:
            with self._req("DELETE", self._url(self._key(sha))):
                pass
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise BlobError(f"对象存储 DELETE 失败：{e.code}") from e
        self.cache.delete(sha)

    def items(self) -> Iterator[tuple[str, int, float]]:
        token = ""
        ns = "{http://s3.amazonaws.com/doc/2006-03-01/}"
        while True:
            q = urllib.parse.urlencode({"list-type": "2", "prefix": self.prefix,
                                        **({"continuation-token": token} if token else {})})
            try:
                with self._req("GET", self._url(query=q)) as r:
                    root = ET.fromstring(r.read())
            except urllib.error.HTTPError as e:
                raise BlobError(f"对象存储列目录失败：{e.code}") from e
            except (OSError, http.client.HTTPException, ET.ParseError) as e:
                raise BlobError(f"对象存储列目录失败：{e}") from e
            for c in root.iter(f"{ns}Contents"):
                key = c.findtext(f"{ns}Key") or ""
                name = key.rsplit("/", 1)[-1]
                if SHA_RE.fullmatch(name):
                    when = c.findtext(f"{ns}LastModified") or ""
                    try:
                        ts = dt.datetime.fromisoformat(when.replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        ts = time.time()
                    yield name, int(c.findtext(f"{ns}Size") or 0), ts
            if (root.findtext(f"{ns}IsTruncated") or "").lower() != "true":
                return
            token = root.findtext(f"{ns}NextContinuationToken") or ""
            if not token:
                return


Blobs = LocalBlobs | S3Blobs


def from_config(spec: str | None, root: Path) -> Blobs | None:
    """`MATRIX_BLOBS` → 内容存储；None 表示照旧（附件放在每个对话自己的目录里）。

    - 空、`legacy`：照旧 `<data>/attachments/<对话>/<sha>`（默认）
    - `local`：`<data>/blobs/`；`local:<绝对路径>`
    - `s3://<桶>/<前缀>`：S3 兼容存储；地址、凭证、区域从 MATRIX_S3_ENDPOINT、MATRIX_S3_ACCESS_KEY、
      MATRIX_S3_SECRET_KEY、MATRIX_S3_REGION（默认 us-east-1）来
    """
    spec = (spec or "").strip()
    if spec in ("", "legacy"):
        return None
    if spec == "local":
        return LocalBlobs(root / "blobs")
    if spec.startswith("local:"):
        return LocalBlobs(Path(spec.removeprefix("local:")))
    if spec.startswith("s3://"):
        bucket, _, prefix = spec.removeprefix("s3://").partition("/")
        endpoint = compat.env("S3_ENDPOINT") or ""
        ak, sk = compat.env("S3_ACCESS_KEY") or "", compat.env("S3_SECRET_KEY") or ""
        if not bucket or not endpoint or not ak or not sk:
            raise BlobError("MATRIX_BLOBS=s3://… 要同时设 MATRIX_S3_ENDPOINT、MATRIX_S3_ACCESS_KEY、MATRIX_S3_SECRET_KEY")
        return S3Blobs(endpoint, bucket, prefix, ak, sk, compat.env("S3_REGION") or "us-east-1", root / "cache" / "blobs")
    raise BlobError(f"MATRIX_BLOBS 写法不认识：{spec!r}（legacy / local / local:<路径> / s3://<桶>/<前缀>）")
