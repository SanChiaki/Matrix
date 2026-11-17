"""出网 CA（docs/cloud-runtime.md 6.4，阶段 2b）：出口网关解开沙箱的 HTTPS，按主机现签证书。

- CA 在部署时（第一次用到时）生成，私钥只在控制面（`<data>/gateway/ca/ca.key`，0600），不进仓库、不进沙箱；沙箱只拿到 CA 证书。
- 轮换：删掉 `<data>/gateway/ca/` 重启服务，新 CA 随下一轮 run.request 下发（旧的叶子证书一并作废）。
- 叶子证书按主机签发（有效期 30 天，提前一天换），缓存成 ssl.SSLContext；签发只用已有依赖 cryptography。
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import os
import ssl
import threading
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

LEAF_DAYS = 30


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class EgressCA:
    def __init__(self, root: Path):
        self.root = root
        self._lock = threading.Lock()
        self._ctx: dict[str, tuple[dt.datetime, ssl.SSLContext]] = {}
        root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass
        kf, cf = root / "ca.key", root / "ca.crt"
        if kf.is_file() and cf.is_file():
            self.key = serialization.load_pem_private_key(kf.read_bytes(), None)
            self.cert = x509.load_pem_x509_certificate(cf.read_bytes())
        else:
            self.key = ec.generate_private_key(ec.SECP256R1())
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Matrix egress CA"),
                              x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Matrix")])
            self.cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                         .public_key(self.key.public_key()).serial_number(x509.random_serial_number())
                         .not_valid_before(_now() - dt.timedelta(days=1)).not_valid_after(_now() + dt.timedelta(days=3650))
                         .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                         .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                                      content_commitment=False, key_encipherment=False,
                                                      data_encipherment=False, key_agreement=False,
                                                      encipher_only=False, decipher_only=False), critical=True)
                         .add_extension(x509.SubjectKeyIdentifier.from_public_key(self.key.public_key()), critical=False)
                         .sign(self.key, hashes.SHA256()))
            tmp = kf.with_suffix(".tmp")
            tmp.write_bytes(self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                   serialization.NoEncryption()))
            os.chmod(tmp, 0o600)
            os.replace(tmp, kf)
            cf.write_bytes(self.cert.public_bytes(serialization.Encoding.PEM))

    @property
    def pem(self) -> str:
        return self.cert.public_bytes(serialization.Encoding.PEM).decode()

    def leaf(self, host: str) -> x509.Certificate:
        key = ec.generate_private_key(ec.SECP256R1())
        return self._sign(host, key)[0]

    def _sign(self, host: str, key: ec.EllipticCurvePrivateKey) -> tuple[x509.Certificate, dt.datetime]:
        try:
            san: x509.GeneralName = x509.IPAddress(ipaddress.ip_address(host))
        except ValueError:
            san = x509.DNSName(host)
        until = _now() + dt.timedelta(days=LEAF_DAYS)
        cert = (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host[:64])]))
                .issuer_name(self.cert.subject).public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(_now() - dt.timedelta(hours=1)).not_valid_after(until)
                .add_extension(x509.SubjectAlternativeName([san]), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.key.public_key()), critical=False)
                .sign(self.key, hashes.SHA256()))
        return cert, until

    def context(self, host: str) -> ssl.SSLContext:
        """这个主机的服务端 TLS 上下文（只说 HTTP/1.1）。"""
        with self._lock:
            got = self._ctx.get(host)
            if got is not None and got[0] - _now() > dt.timedelta(days=1):
                return got[1]
            key = ec.generate_private_key(ec.SECP256R1())
            cert, until = self._sign(host, key)
            leaf_dir = self.root / "leaf"
            leaf_dir.mkdir(exist_ok=True)
            f = leaf_dir / (host.replace(":", "_").replace("/", "_")[:200] + ".pem")
            f.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption())
                          + cert.public_bytes(serialization.Encoding.PEM)
                          + self.cert.public_bytes(serialization.Encoding.PEM))
            os.chmod(f, 0o600)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(f)
            ctx.set_alpn_protocols(["http/1.1"])
            self._ctx[host] = (until, ctx)
            return ctx
