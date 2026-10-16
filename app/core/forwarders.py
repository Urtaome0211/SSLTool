"""SSH 端口转发线程：本地转发(-L)、远程转发(-R)、动态 SOCKS5(-D)。

规则格式（OpenSSH 风格，会话配置里每行一条，不区分 L/R/D 大小写）：
    L:bind_host:bind_port:target_host:target_port   本机监听 → 转发到服务器可达目标
    R:bind_host:bind_port:target_host:target_port   服务器监听 → 转发到本机可达目标
    D:bind_host:bind_port                           本机 SOCKS5 代理（仅 CONNECT、无认证）
端口写 0 表示由系统/服务器分配实际端口（启动提示里会显示实际端口）。
不支持 IPv6 字面量地址。

线程模型说明：转发器用纯 Python threading.Thread 而非 QThread——PyQt5 的
QThread.start() 在非主线程（如 SSHWorker 的 run()）里调用是静默 no-op，
而 SSHWorker 的转发器恰好是在其 worker 线程内创建的。回调 on_started/on_failed
由 SSHWorker 接成 pyqtSignal 发射（从任意线程发射信号均安全，自动排队投递）。
"""
import socket
import struct
import threading


def parse_forwards(rules):
    """解析转发规则列表，返回 spec 字典列表；无效规则抛 ValueError。"""
    specs = []
    for raw in rules:
        rule = (raw or "").strip()
        if not rule or rule.startswith("#"):
            continue
        kind, sep, rest = rule.partition(":")
        kind = kind.upper()
        if not sep:
            raise ValueError(f"无效的端口转发规则: {rule}")
        try:
            if kind in ("L", "R"):
                bhost, bport, thost, tport = rest.split(":", 3)
                specs.append({
                    "kind": kind,
                    "bind": (bhost, int(bport)),
                    "target": (thost, int(tport)),
                    "raw": rule,
                })
            elif kind == "D":
                bhost, bport = rest.rsplit(":", 1)
                specs.append({
                    "kind": "D",
                    "bind": (bhost, int(bport)),
                    "raw": rule,
                })
            else:
                raise ValueError(f"未知的转发类型 '{kind}'（仅支持 L/R/D）: {rule}")
        except ValueError as e:
            raise ValueError(f"无效的端口转发规则 '{rule}': {e}")
    return specs


def _pump(a, b):
    """双向泵：把 a 收到的数据发送给 b，直到任一端关闭；结束时关闭两端。"""
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except Exception:
        pass
    finally:
        for s in (a, b):
            try:
                s.close()
            except Exception:
                pass


class _ForwardBase(threading.Thread):
    """转发线程基类：持有 SSH transport 引用、停止标志与启动回调。"""

    def __init__(self, transport, on_started=None, on_failed=None):
        super().__init__(daemon=True)
        self._transport = transport
        self._on_started = on_started
        self._on_failed = on_failed
        self._stop_flag = False

    def _emit_started(self, message):
        if self._on_started:
            self._on_started(message)

    def _emit_failed(self, message):
        if self._on_failed:
            self._on_failed(message)

    def stop(self):
        self._stop_flag = True


class LocalForward(_ForwardBase):
    """-L：监听本机端口，把连接经 SSH 隧道转发到目标。"""

    def __init__(self, transport, bind, target, on_started=None, on_failed=None):
        super().__init__(transport, on_started, on_failed)
        self._bind = bind
        self._target = target
        self._listener = None

    def stop(self):
        super().stop()
        if self._listener is not None:
            try:
                self._listener.close()
            except Exception:
                pass

    def run(self):
        try:
            lst = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            lst.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            lst.bind(self._bind)
            lst.listen(16)
            self._listener = lst
            actual = lst.getsockname()
            self._emit_started(
                f"本地转发 {actual[0]}:{actual[1]} → {self._target[0]}:{self._target[1]}")
            lst.settimeout(0.5)
            while not self._stop_flag:
                try:
                    conn, _ = lst.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                if self._stop_flag:
                    conn.close()
                    break
                threading.Thread(target=self._serve_conn, args=(conn,),
                                 daemon=True).start()
        except Exception as e:
            self._emit_failed(str(e))
        finally:
            if self._listener is not None:
                try:
                    self._listener.close()
                except Exception:
                    pass

    def _serve_conn(self, conn):
        try:
            chan = self._transport.open_channel(
                "direct-tcpip", self._target, conn.getpeername())
        except Exception:
            conn.close()
            return
        t1 = threading.Thread(target=_pump, args=(conn, chan), daemon=True)
        t2 = threading.Thread(target=_pump, args=(chan, conn), daemon=True)
        t1.start()
        t2.start()


class RemoteForward(_ForwardBase):
    """-R：请求服务器监听端口，把进入的连接转发到本机目标。"""

    def __init__(self, transport, bind, target, on_started=None, on_failed=None):
        super().__init__(transport, on_started, on_failed)
        self._bind = bind
        self._target = target

    def stop(self):
        super().stop()
        try:
            self._transport.cancel_port_forward(self._bind[0], self._bind[1])
        except Exception:
            pass

    def run(self):
        try:
            port = self._transport.request_port_forward(self._bind[0], self._bind[1])
            self._emit_started(
                f"远程转发 {self._bind[0]}:{port} → {self._target[0]}:{self._target[1]}")
            while not self._stop_flag:
                try:
                    chan = self._transport.accept(0.5)
                except socket.timeout:
                    continue
                if chan is None:
                    continue
                threading.Thread(target=self._serve_chan, args=(chan,),
                                 daemon=True).start()
        except Exception as e:
            self._emit_failed(str(e))

    def _serve_chan(self, chan):
        try:
            conn = socket.create_connection(self._target, timeout=12)
        except Exception:
            try:
                chan.close()
            except Exception:
                pass
            return
        t1 = threading.Thread(target=_pump, args=(conn, chan), daemon=True)
        t2 = threading.Thread(target=_pump, args=(chan, conn), daemon=True)
        t1.start()
        t2.start()


class Socks5Forward(_ForwardBase):
    """-D：本机 SOCKS5 代理（仅支持 CONNECT 命令与无认证）。"""

    def __init__(self, transport, bind, on_started=None, on_failed=None):
        super().__init__(transport, on_started, on_failed)
        self._bind = bind
        self._listener = None

    def stop(self):
        super().stop()
        if self._listener is not None:
            try:
                self._listener.close()
            except Exception:
                pass

    def run(self):
        try:
            lst = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            lst.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            lst.bind(self._bind)
            lst.listen(16)
            self._listener = lst
            actual = lst.getsockname()
            self._emit_started(f"SOCKS5 代理 {actual[0]}:{actual[1]}（经 SSH 动态转发）")
            lst.settimeout(0.5)
            while not self._stop_flag:
                try:
                    conn, _ = lst.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                if self._stop_flag:
                    conn.close()
                    break
                threading.Thread(target=self._serve_conn, args=(conn,),
                                 daemon=True).start()
        except Exception as e:
            self._emit_failed(str(e))
        finally:
            if self._listener is not None:
                try:
                    self._listener.close()
                except Exception:
                    pass

    def _serve_conn(self, conn):
        try:
            conn.settimeout(15)
            head = self._recv_exact(conn, 2)
            ver, nmethods = head[0], head[1]
            if ver != 0x05:
                raise ValueError("仅支持 SOCKS5")
            methods = self._recv_exact(conn, nmethods)
            if 0x00 not in methods:  # 只支持无认证
                conn.sendall(b"\x05\xff")
                return
            conn.sendall(b"\x05\x00")
            head = self._recv_exact(conn, 4)
            ver, cmd, rsv, atyp = head
            if ver != 0x05 or cmd != 0x01:
                raise ValueError("仅支持 CONNECT 命令")
            if atyp == 0x01:      # IPv4
                addr = socket.inet_ntoa(self._recv_exact(conn, 4))
            elif atyp == 0x03:    # 域名
                n = self._recv_exact(conn, 1)[0]
                addr = self._recv_exact(conn, n).decode("utf-8", "replace")
            elif atyp == 0x04:    # IPv6
                addr = socket.inet_ntop(socket.AF_INET6, self._recv_exact(conn, 16))
            else:
                raise ValueError(f"不支持的地址类型: {atyp}")
            port = struct.unpack(">H", self._recv_exact(conn, 2))[0]
            conn.settimeout(None)
            chan = self._transport.open_channel(
                "direct-tcpip", (addr, port), conn.getsockname())
            conn.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")  # 成功
            t1 = threading.Thread(target=_pump, args=(conn, chan), daemon=True)
            t2 = threading.Thread(target=_pump, args=(chan, conn), daemon=True)
            t1.start()
            t2.start()
        except Exception:
            try:
                conn.sendall(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")  # 一般失败
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

    @staticmethod
    def _recv_exact(conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise EOFError("对端提前关闭连接")
            buf += chunk
        return buf
