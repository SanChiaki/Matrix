"""连接器凭证的加密保管（docs/connectors-and-skills.md 4.2，候选不变量 19）。

- 每个连接一份 `vault/<connection_id>.bin`，内容是 Fernet（AES-128-CBC + HMAC-SHA256）加密的 JSON。
- 主密钥取自环境变量 `AGENTNET_VAULT_KEY`；没有就用 `<data>/vault.key`（第一次用时生成，权限 0600）。
  密钥和数据放在同一台机器上只防“数据目录被拷走 / 误提交”，防不了能读这台机器的人。
- 凭证只在网络代调用外部服务时解开，不进 state.json、提示词、事件、日志、API 返回与任何工作目录。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class VaultError(RuntimeError):
    pass


class Vault:
    def __init__(self, root: Path, key: str | None = None):
        self.dir = root / "vault"
        self._key_file = root / "vault.key"
        self._fernet: Fernet | None = None
        self._key = key if key is not None else os.environ.get("AGENTNET_VAULT_KEY")

    def _f(self) -> Fernet:
        if self._fernet is None:
            key = self._key
            if not key:
                if not self._key_file.exists():
                    self._key_file.parent.mkdir(parents=True, exist_ok=True)
                    fd = os.open(self._key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w") as f:
                        f.write(Fernet.generate_key().decode())
                key = self._key_file.read_text().strip()
            try:
                self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
            except ValueError as e:
                raise VaultError("保管密钥无效（应为 Fernet 密钥：32 字节的 urlsafe base64）") from e
        return self._fernet

    def _path(self, name: str) -> Path:
        if not name or "/" in name or "\\" in name or name.startswith("."):
            raise VaultError(f"非法的保管项：{name!r}")
        return self.dir / f"{name}.bin"

    def put(self, name: str, secret: dict[str, str]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        data = self._f().encrypt(json.dumps(secret, ensure_ascii=False).encode())
        path = self._path(name)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)

    def get(self, name: str) -> dict[str, str]:
        path = self._path(name)
        if not path.is_file():
            return {}
        try:
            return json.loads(self._f().decrypt(path.read_bytes()))
        except InvalidToken as e:
            raise VaultError("凭证解不开（保管密钥换过了？请重新授权）") from e

    def delete(self, name: str) -> None:
        self._path(name).unlink(missing_ok=True)
