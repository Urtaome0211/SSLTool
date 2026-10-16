"""后台连接线程：SSH 终端会话与 SSL 加密通道。"""
import hashlib
import queue
import socket
import ssl

import paramiko
from paramiko.common import cMSG_CHANNEL_REQUEST
from paramiko.message import Message
from PyQt5.QtCore import QThread, pyqtSignal

from app.core.forwarders import (LocalForward, RemoteForward, Socks5Forward,
                                 parse_forwards)

# RFC 4254 §8 终端模式（pty-req 的 encoded terminal modes）。
# paramiko 的 invoke_shell 发送空 modes，部分服务器（Windows sshd、网络设备等）
# 因此不开启 pty 回显，导致终端里看不到自己敲的字；这里显式请求标准回显模式集。
# 每个条目 = opcode(1 byte) + uint32 值；末尾 TTY_OP_END(0)。
_TERMINAL_MODES = (
    b"\x01\x00\x00\x00\x03"    # 1   VINTR   = 0x03 (Ctrl+C 产生中断)
    b"\x03\x00\x00\x00\x7f"    # 3   VERASE  = 0x7f (Backspace 删除)
    b"\x04\x00\x00\x00\x15"    # 4   VKILL   = 0x15 (Ctrl+U 删整行)
    b"\x05\x00\x00\x00\x04"    # 5   VEOF    = 0x04 (Ctrl+D)
    b"\x2a\x00\x00\x00\x01"    # 42  IUTF8   = 1
    b"\x32\x00\x00\x00\x01"    # 50  ISIG    = 1 (信号字符生效)
    b"\x33\x00\x00\x00\x01"    # 51  ICANON  = 1 (行编辑模式)
    b"\x35\x00\x00\x00\x01"    # 53  ECHO    = 1 ← 关键：请求服务器回显输入
    b"\x36\x00\x00\x00\x01"    # 54  ECHOE   = 1
    b"\x37\x00\x00\x00\x01"    # 55  ECHOK   = 1
    b"\x3b\x00\x00\x00\x01"    # 59  IEXTEN  = 1
    b"\x3c\x00\x00\x00\x01"    # 60  ECHOCTL = 1
    b"\x3d\x00\x00\x00\x01"    # 61  ECHOKE  = 1
    b"\x00"                    # 0   TTY_OP_END
)


class BaseWorker(QThread):
    """带线程安全发送队列的连接线程基类。"""

    error = pyqtSignal(str)
    disconnected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue = queue.Queue()
        self._running = True

    def send(self, data: bytes) -> None:
        """从任意线程投递要发送的数据。"""
        if self._running:
            self._queue.put(("data", data))

    def stop(self) -> None:
        self._running = False
        self._queue.put(("stop", None))

    def _drain_queue(self) -> None:
        while True:
            try:
                kind, payload = self._queue.get_nowait()
            except queue.Empty:
                return
            if kind == "stop":
                self._running = False
                continue
            self._handle_item(kind, payload)

    def _handle_item(self, kind, payload):
        """子类覆写：处理队列中的自定义命令。"""
        pass


class SSHWorker(BaseWorker):
    """SSH 交互式终端线程（pty shell）。"""

    connected = pyqtSignal()
    dataReceived = pyqtSignal(bytes)
    forwardStarted = pyqtSignal(str)
    forwardError = pyqtSignal(str)

    def __init__(self, host, port, username, password=None, key_path=None,
                 passphrase=None, forwards=None, parent=None):
        super().__init__(parent)
        self.host = host
        self.port = int(port)
        self.username = username
        self.password = password
        self.key_path = key_path
        self.passphrase = passphrase
        self.forwards = list(forwards or [])
        self._client = None
        self._chan = None
        self._connected = False
        self._forwarders = []

    def resize_pty(self, cols: int, rows: int) -> None:
        self._queue.put(("resize", (cols, rows)))

    def _open_shell(self, client):
        """打开 pty shell 并显式请求 ECHO 等终端模式（paramiko 的 invoke_shell
        只发送空 terminal-modes，部分服务器因此不回显输入）。"""
        transport = client.get_transport()
        chan = transport.open_session()
        m = Message()
        m.add_byte(cMSG_CHANNEL_REQUEST)
        m.add_int(chan.remote_chanid)
        m.add_string("pty-req")
        m.add_boolean(True)
        m.add_string("xterm")
        m.add_int(80)
        m.add_int(24)
        m.add_int(0)
        m.add_int(0)
        m.add_string(_TERMINAL_MODES)
        transport._send_user_message(m)
        chan.invoke_shell()
        return chan

    def _handle_item(self, kind, payload):
        if kind == "data" and self._chan is not None:
            try:
                self._chan.sendall(payload)
            except Exception:
                pass
        elif kind == "resize" and self._chan is not None:
            try:
                self._chan.resize_pty(width=payload[0], height=payload[1])
            except Exception:
                pass

    def _start_forwarders(self, client):
        """按会话配置启动端口转发线程；解析/启动失败经信号上报，不影响终端。
        转发器为纯 Python 线程（PyQt5 的 QThread 无法在 worker 线程内 start），
        启动/失败回调接成本 worker 的信号（从任意线程发射均安全）。"""
        if not self.forwards:
            return
        try:
            specs = parse_forwards(self.forwards)
        except ValueError as e:
            self.forwardError.emit(str(e))
            return
        transport = client.get_transport()
        for spec in specs:
            if spec["kind"] == "L":
                fw = LocalForward(transport, spec["bind"], spec["target"],
                                  on_started=self.forwardStarted.emit,
                                  on_failed=self.forwardError.emit)
            elif spec["kind"] == "R":
                fw = RemoteForward(transport, spec["bind"], spec["target"],
                                   on_started=self.forwardStarted.emit,
                                   on_failed=self.forwardError.emit)
            else:
                fw = Socks5Forward(transport, spec["bind"],
                                   on_started=self.forwardStarted.emit,
                                   on_failed=self.forwardError.emit)
            fw.start()
            self._forwarders.append(fw)

    def _stop_forwarders(self):
        for fw in self._forwarders:
            fw.stop()
        for fw in self._forwarders:
            fw.join(1)
        self._forwarders = []

    def run(self):
        reason = ""
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            kwargs = dict(
                hostname=self.host, port=self.port, username=self.username,
                timeout=12, look_for_keys=False, allow_agent=False,
                banner_timeout=30, auth_timeout=30,
            )
            if self.key_path:
                kwargs["key_filename"] = self.key_path
                if self.passphrase:
                    kwargs["passphrase"] = self.passphrase
            else:
                kwargs["password"] = self.password or ""
            client.connect(**kwargs)
            self._start_forwarders(client)
            try:
                chan = self._open_shell(client)
            except Exception:
                # 万一自定义 pty 请求不被接受，退回 paramiko 默认行为
                chan = client.invoke_shell(term="xterm", width=80, height=24)
            chan.settimeout(0.1)
            client.get_transport().set_keepalive(30)
            self._client = client
            self._chan = chan
            self._connected = True
            self.connected.emit()
            while self._running:
                self._drain_queue()
                try:
                    if chan.recv_ready():
                        data = chan.recv(65536)
                        if data:
                            self.dataReceived.emit(data)
                    if chan.exit_status_ready():
                        reason = "远程会话已结束"
                        break
                except socket.timeout:
                    continue
                except Exception as e:
                    reason = str(e)
                    break
        except Exception as e:
            if not self._connected:
                self.error.emit(f"连接失败: {e}")
        finally:
            self._running = False
            self._stop_forwarders()
            try:
                if self._chan is not None:
                    self._chan.close()
            except Exception:
                pass
            try:
                if self._client is not None:
                    self._client.close()
            except Exception:
                pass
            self._chan = None
            self._client = None
            if self._connected:
                self.disconnected.emit(reason)


class SSLWorker(BaseWorker):
    """原始 TLS 文本通道线程（类似 telnet-over-TLS 控制台）。"""

    connected = pyqtSignal()
    certInfo = pyqtSignal(str)
    dataReceived = pyqtSignal(bytes)

    def __init__(self, host, port, verify=True, client_cert=None, client_key=None, parent=None):
        super().__init__(parent)
        self.host = host
        self.port = int(port)
        self.verify = bool(verify)
        self.client_cert = client_cert
        self.client_key = client_key
        self._sock = None
        self._connected = False

    def _handle_item(self, kind, payload):
        if kind == "data" and self._sock is not None:
            try:
                self._sock.sendall(payload)
            except Exception:
                pass

    def run(self):
        reason = ""
        try:
            raw = socket.create_connection((self.host, self.port), timeout=12)
            if self.verify:
                ctx = ssl.create_default_context()
            else:
                ctx = ssl._create_unverified_context()
            if self.client_cert:
                ctx.load_cert_chain(certfile=self.client_cert,
                                    keyfile=self.client_key or self.client_cert)
            tls = ctx.wrap_socket(raw, server_hostname=self.host)
            self._sock = tls
            self._connected = True
            self.connected.emit()
            info = self._cert_info(tls)
            if info:
                self.certInfo.emit(info)
            tls.settimeout(0.1)
            while self._running:
                self._drain_queue()
                try:
                    data = tls.recv(65536)
                    if data:
                        self.dataReceived.emit(data)
                    else:
                        reason = "对端已关闭连接"
                        break
                except socket.timeout:
                    continue
                except Exception as e:
                    reason = str(e)
                    break
        except Exception as e:
            if not self._connected:
                self.error.emit(f"连接失败: {e}")
        finally:
            self._running = False
            try:
                if self._sock is not None:
                    self._sock.close()
            except Exception:
                pass
            self._sock = None
            if self._connected:
                self.disconnected.emit(reason)

    @staticmethod
    def _cert_info(tls) -> str:
        try:
            der = tls.getpeercert(binary_form=True)
            cipher = tls.cipher()
            version = tls.version()
            lines = [f"TLS {version}  ·  加密套件: {cipher[0] if cipher else '未知'}"]
            if der:
                fp = hashlib.sha256(der).hexdigest().upper()
                pretty = ":".join(fp[i:i + 2] for i in range(0, len(fp), 2))
                lines.append(f"服务器证书 SHA-256 指纹: {pretty}")
            else:
                lines.append("（未获取到服务器证书）")
            cert = tls.getpeercert()
            if isinstance(cert, dict) and cert.get("subject"):
                subj = ", ".join(f"{k}={v}" for k, v in cert["subject"])
                lines.append(f"证书主题: {subj}")
            return "\n".join(lines)
        except Exception as e:
            return f"获取证书信息失败: {e}"
