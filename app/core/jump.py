"""跳板机（jump host / bastion）支持：经 SSH 代理连接目标服务器。

原理与 OpenSSH 的 ProxyJump 相同：先与跳板机建立 SSH 连接并完成认证，
再用 `direct-tcpip` 通道把目标主机的 TCP 流量转发过去，最后在通道上
再跑一层 SSH 协议（paramiko 的 `sock=` 参数）。

安全：跳板机自身的主机密钥同样走 TOFU 确认流程（在 workers 中与目标
主机密钥一起经 HostKeyGate 确认），确认之前不会向跳板机发送任何凭据。
"""
import paramiko


class JumpError(Exception):
    """跳板机连接或转发通道建立失败。"""


class JumpConnector:
    """管理一条跳板机 SSH 连接，并按需为 (host, port) 开 direct-tcpip 通道。

    通道对象（paramiko.Channel）实现了 recv/send/settimeout/close，可直接
    作为 paramiko.Transport / SSHClient.connect 的 sock= 参数使用。

    惰性连接：connect() 在首次使用时才真正建立跳板机连接（由 worker 线程
    调用，不阻塞 UI）；密码 / 私钥口令由 UI 在连接前收集好后传入。
    """

    def __init__(self, host, port=22, username="", password=None, key_path=None):
        self.host = host
        self.port = int(port)
        self.username = username or ""
        self.password = password
        self.key_path = key_path or ""
        self._client = None
        self._transport = None

    @property
    def label(self) -> str:
        return f"{self.username}@{self.host}:{self.port}"

    def connect(self):
        """连接跳板机并完成认证（幂等；失败抛 JumpError）。"""
        if self._client is not None:
            return self._client
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs = dict(
            hostname=self.host, port=self.port, username=self.username,
            timeout=12, look_for_keys=False, allow_agent=False,
            banner_timeout=30, auth_timeout=30,
        )
        try:
            if self.key_path:
                kwargs["key_filename"] = self.key_path
                if self.password:
                    kwargs["passphrase"] = self.password
            else:
                kwargs["password"] = self.password or ""
            client.connect(**kwargs)
        except Exception as e:
            client.close()
            raise JumpError(f"跳板机 {self.label} 连接失败: {e}")
        self._client = client
        self._transport = client.get_transport()
        return client

    def open_channel(self, host, port):
        """为 (host, port) 打开一条 direct-tcpip 转发通道（失败抛 JumpError）。

        调用者需已通过 connect()（本方法也会幂等地确保连接）。
        """
        self.connect()
        try:
            chan = self._transport.open_channel(
                "direct-tcpip", (host, int(port)), ("127.0.0.1", 0))
        except Exception as e:
            raise JumpError(f"跳板机 {self.label} 转发通道建立失败: {e}")
        chan.settimeout(12)
        return chan

    def close(self):
        """关闭跳板机连接（关闭其上的所有转发通道）。"""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
            self._transport = None
