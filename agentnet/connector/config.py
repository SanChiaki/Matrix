"""连接器的本机配置：服务器地址、设备令牌、工作目录。

`matrix-connect setup` 配对成功后写入，`matrix-connect run`（以及后台服务）读它。
令牌只写在这个文件里（0600），不进命令行参数、不进服务定义、不打印。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from urllib.parse import urlparse

CONNECT_PATH = "/api/devices/connect"


def default_path() -> Path:
    env = os.environ.get("MATRIX_CONNECT_CONFIG")
    if env:
        return Path(env).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "matrix-connect" / "config.json"


def normalize_server(url: str) -> str:
    """服务器地址统一成 `http(s)://host[:port]`；没写协议按 http（本地 Demo 只监听 127.0.0.1）。"""
    raw = (url or "").strip().rstrip("/")
    if "://" not in raw:
        raw = f"http://{raw}"
    u = urlparse(raw)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ValueError(f"服务器地址要是 http(s)://主机[:端口]：{url!r}")
    return f"{u.scheme}://{u.netloc}"


@dataclass
class Config:
    server: str                      # http(s)://host[:port]
    token: str
    device_id: str = ""
    device_name: str = ""
    email: str = ""                  # 批准配对的账号，只用于 status 展示
    workroot: str = str(Path.home() / ".matrix")
    drivers: list[str] = field(default_factory=list)  # 只用这些 harness；空 = 本机装了的全用

    @property
    def ws_url(self) -> str:
        base = normalize_server(self.server)
        return ("wss" + base[5:] if base.startswith("https") else "ws" + base[4:]) + CONNECT_PATH


def save(conf: Config, path: Path | None = None) -> Path:
    path = Path(path or default_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # 先以 0600 创建，令牌不会短暂可读
    with os.fdopen(fd, "w") as f:
        json.dump(asdict(conf), f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def load(path: Path | None = None) -> Config | None:
    path = Path(path or default_path())
    if not path.exists():
        return None
    raw = json.loads(path.read_text())
    known = {f.name for f in fields(Config)}
    return Config(**{k: v for k, v in raw.items() if k in known})
