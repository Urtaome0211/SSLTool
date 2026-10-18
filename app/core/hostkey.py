"""SSH 主机密钥 TOFU（首次使用信任）存储与校验。

威胁模型：
- 中间人攻击者替换服务器主机密钥时，连接会静默成功（原 AutoAddPolicy 行为）。
- 本模块在发送任何凭据（密码/私钥签名）之前完成主机密钥探测与校验：
  探测连接只做密钥交换、不做用户认证，未获用户确认的服务器永远收不到凭据。
- 真正建立认证连接后还会复核一次密钥指纹（探测与认证连接是两条独立 TCP
  连接，防中间人两条连接给不同密钥）。

存储：~/.ssltool/known_hosts.json
格式：{"[host]:port": {"key_type": "ssh-rsa", "fingerprint": "SHA256:...", "added": ISO8601}}
"""
import base64
import hashlib
import json
import socket
import threading
from datetime import datetime
from pathlib import Path

import paramiko

from app import config

# 探测连接的超时（秒）：只做 TCP + 密钥交换，一般远快于此
PROBE_TIMEOUT = 8.0
# 等待用户在主机密钥确认对话框中做决定的超时（秒）
DECISION_TIMEOUT = 300.0


class HostKeyProbeError(Exception):
    """探测服务器主机密钥失败（网络错误 / 密钥交换失败等）。"""


def known_hosts_file() -> Path:
    """known_hosts 存储路径（动态取自 config.DATA_DIR，测试可重定向）。"""
    return config.DATA_DIR / "known_hosts.json"


def hostkey_id(host: str, port: int) -> str:
    """known_hosts.json 中条目的键，IPv6 也安全（始终加方括号）。"""
    return f"[{host}]:{int(port)}"


def fingerprint_sha256(key: paramiko.PKey) -> str:
    """OpenSSH 风格指纹：SHA256 摘要的 base64（无填充），前缀 SHA256:。"""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def load_known_hosts() -> dict:
    try:
        with open(known_hosts_file(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_known_hosts(data: dict) -> None:
    config.ensure_data_dir()
    with open(known_hosts_file(), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def save_known_host(host: str, port: int, key_type: str, fingerprint: str) -> None:
    """记录（或覆盖）一台主机的主机密钥。"""
    data = load_known_hosts()
    data[hostkey_id(host, port)] = {
        "key_type": key_type,
        "fingerprint": fingerprint,
        "added": datetime.now().isoformat(timespec="seconds"),
    }
    save_known_hosts(data)


def delete_known_host(host: str, port: int) -> None:
    data = load_known_hosts()
    data.pop(hostkey_id(host, port), None)
    save_known_hosts(data)


def check_known_host(host: str, port: int, key: paramiko.PKey):
    """校验主机密钥。

    返回三元组：
        ("ok", fingerprint, None)                   密钥与记录一致
        ("unknown", fingerprint, None)              无记录（首次连接）
        ("changed", fingerprint, expected_fp)       与记录不符（疑似中间人）
    """
    fp = fingerprint_sha256(key)
    entry = load_known_hosts().get(hostkey_id(host, port))
    if entry is None:
        return "unknown", fp, None
    expected = entry.get("fingerprint")
    if expected == fp:
        return "ok", fp, None
    return "changed", fp, expected


def probe_host_key(host: str, port: int, timeout: float = PROBE_TIMEOUT) -> paramiko.PKey:
    """只做密钥交换取回服务器主机密钥，绝不发送任何用户凭据。

    使用裸 paramiko.Transport：start_client 完成 KEX 后立即关闭，
    密码 / 私钥认证（userauth 阶段）根本不会发生。
    """
    try:
        sock = socket.create_connection((host, int(port)), timeout=timeout)
    except OSError as e:
        raise HostKeyProbeError(f"无法连接 {host}:{port}（{e}）")
    t = paramiko.Transport(sock)
    try:
        try:
            t.start_client(timeout=timeout)
        except Exception as e:
            raise HostKeyProbeError(f"SSH 握手失败: {e}")
        key = t.get_remote_server_key()
        if key is None:
            raise HostKeyProbeError("服务器未提供主机密钥")
        return key
    finally:
        t.close()


class HostKeyGate:
    """探测 + 校验 + 等待用户决定的阻塞门（在 worker 线程 run() 中使用）。

    用法：
        gate = HostKeyGate(host, port, on_unknown=..., on_changed=...)
        verdict, key, fp = gate.wait()          # 阻塞至用户决定或超时
        # verdict: "ok" | "accepted" | "rejected" | "cancelled" | "timeout"
    GUI 线程通过 accept()/reject()/cancel() 唤醒等待中的 worker。
    """

    def __init__(self, host, port, on_unknown=None, on_changed=None):
        self.host = host
        self.port = int(port)
        self._on_unknown = on_unknown
        self._on_changed = on_changed
        self._event = threading.Event()
        self._accepted = False
        self._cancelled = False

    def wait(self, timeout: float = DECISION_TIMEOUT):
        """返回 (verdict, key, fp, expected)。可能抛出 HostKeyProbeError。"""
        key = probe_host_key(self.host, self.port)
        status, fp, expected = check_known_host(self.host, self.port, key)
        if status == "ok":
            return "ok", key, fp, expected
        if status == "unknown":
            if self._on_unknown:
                self._on_unknown(fp)
        else:
            if self._on_changed:
                self._on_changed(fp, expected)
        if not self._event.wait(timeout):
            return "timeout", key, fp, expected
        if self._cancelled:
            return "cancelled", key, fp, expected
        if self._accepted:
            save_known_host(self.host, self.port, key.get_name(), fp)
            return "accepted", key, fp, expected
        return "rejected", key, fp, expected

    def accept(self) -> None:
        """用户确认接受该主机密钥。"""
        self._accepted = True
        self._event.set()

    def reject(self) -> None:
        """用户拒绝该主机密钥。"""
        self._accepted = False
        self._event.set()

    def cancel(self) -> None:
        """标签页关闭 / 应用退出：静默唤醒 worker。"""
        self._cancelled = True
        self._event.set()
