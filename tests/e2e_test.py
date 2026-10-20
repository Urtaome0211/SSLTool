"""端到端测试：内嵌 paramiko SSH 服务器（127.0.0.1 随机端口）。

测试 1（服务器回显）：服务器只在客户端 pty-req 携带 ECHO=1 时才回显输入（模拟 sshd
    行为），验证 SSHWorker 打开 shell 时显式请求了 ECHO 终端模式。
测试 2（闪退）：已连接且有数据流量的状态下，打开「新建连接」对话框（真实 exec_
    嵌套事件循环）并保存，验证应用不崩溃。
测试 3（对话框可见性）：协议/认证方式切换时 ConnectDialog 各行显隐正确。
测试 4（本地回显兜底）：服务器完全不回显时，终端勾选「本地回显」后键入的文字可见。
测试 5（滚动位置）：用户向上翻看历史时，新输出不把页面拉回底部。
测试 6（回显去重）：服务器回显 + 本地回显同时开启时，键入字符只显示一次。
测试 7（主机密钥 TOFU）：未知密钥需确认并保存；密钥变更警告旧指纹；拒绝变更时
     服务器不应收到任何认证凭据；接受变更后记录更新并重新连接。
"""
import os
import re
import socket
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import paramiko
from PyQt5.QtCore import QEvent, Qt, QTimer
from PyQt5.QtGui import QKeyEvent
from PyQt5.QtWidgets import QApplication, QDialog

from app import config
from app.core import hostkey as hk
from app.core.workers import SSHWorker
from app.ui.connect_dialog import ConnectDialog
from app.ui.hostkey_dialog import HostKeyDialog
from app.ui.main_window import MainWindow
from app.ui.terminal_tab import ConsoleTab
from app.ui.terminal_widget import TerminalWidget

# 隔离测试数据，避免污染用户真实的 ~/.ssltool/sessions.json
_TMP = Path(tempfile.mkdtemp(prefix="ssltool_e2e_"))
config.DATA_DIR = _TMP / ".ssltool"
config.SESSIONS_FILE = config.DATA_DIR / "sessions.json"
config.SECRETS_FILE = config.DATA_DIR / "secrets.json"

_HOST_KEY = paramiko.RSAKey.generate(2048)
_TRANSPORTS = []  # 保持 transport 引用，防止被 GC
_FAILURES = []


def _parse_modes(modes: bytes) -> dict:
    """解析 pty-req 的 encoded terminal modes（opcode 1 字节 + uint32 值，0 结尾）。"""
    out = {}
    i = 0
    while i + 1 < len(modes):
        op = modes[i]
        if op == 0:
            break
        if i + 5 > len(modes):
            break
        out[op] = int.from_bytes(modes[i + 1:i + 5], "big")
        i += 5
    return out


def _pump(src, dst):
    """单向泵：把 src 收到的数据全部发给 dst；任一端关闭即结束并关闭两端。"""
    try:
        while True:
            try:
                data = src.recv(65536)
            except socket.timeout:
                continue
            if not data:
                break
            dst.sendall(data)
    except Exception:
        pass
    finally:
        for s in (src, dst):
            try:
                s.close()
            except Exception:
                pass


def _start_echo_server() -> int:
    """本地 TCP echo 服务器：作为端口转发的测试目标。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    port = sock.getsockname()[1]

    def loop():
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            threading.Thread(target=_echo_serve, args=(conn,),
                             daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return port


def _echo_serve(conn):
    try:
        while True:
            data = conn.recv(65536)
            if not data:
                break
            conn.sendall(data)
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


class EchoServer(paramiko.ServerInterface):
    """pty-req 携带 ECHO=1 才回显输入的 SSH 服务器（模拟 sshd 行为）。"""

    spam = False       # 类属性开关：持续向会话灌数据（模拟繁忙服务器）
    never_echo = False  # 类属性开关：无论请求与否都不回显（模拟不回显的设备）
    auth_attempts = 0   # 类属性：累计认证尝试次数（TOFU 拒绝路径验证用）
    record_data = False  # 类属性开关：记录客户端发来的原始字节（粘贴验证用）
    received = []       # record_data 开启时收集的原始字节

    def __init__(self):
        self.echo = False
        self._transport = None
        self._direct_targets = {}
        self._direct_lock = threading.Lock()
        self._forward_listeners = []

    def check_auth_password(self, username, password):
        EchoServer.auth_attempts += 1
        return paramiko.AUTH_SUCCESSFUL

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, channel, term, width, height,
                                  pixelwidth, pixelheight, modes):
        # modes 为原始字节串；53 = ECHO（RFC 4254 §8）
        if self.never_echo:
            self.echo = False
        else:
            self.echo = _parse_modes(modes).get(53, 0) == 1
        return True

    def check_channel_shell_request(self, channel):
        threading.Thread(target=self._shell_loop, args=(channel,),
                         daemon=True).start()
        return True

    def _shell_loop(self, chan):
        try:
            chan.send(b"Welcome to SSLTool test sshd\r\n$ ")
            while not chan.closed:
                if self.spam:
                    chan.send(b"spam-data\r\n")
                    time.sleep(0.02)
                if chan.recv_ready():
                    data = chan.recv(1024)
                    if not data:
                        break
                    if EchoServer.record_data:
                        EchoServer.received.append(data)
                    if self.echo:
                        chan.send(data)
        except Exception:
            pass
        finally:
            try:
                chan.close()
            except Exception:
                pass

    # ---- 端口转发支持 ----

    def check_port_forward_request(self, address, port):
        """服务器侧 -R：自行监听；请求端口为 0 时返回实际分配端口。"""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((address, port))
            sock.listen(16)
        except Exception:
            return False
        actual = sock.getsockname()[1]
        self._forward_listeners.append(sock)
        threading.Thread(target=self._forward_accept_loop, args=(sock,),
                         daemon=True).start()
        return actual

    def cancel_port_forward_request(self, address, port):
        for sock in self._forward_listeners:
            try:
                if sock.getsockname()[1] == port:
                    sock.close()
            except Exception:
                pass

    def _forward_accept_loop(self, sock):
        sock.settimeout(0.5)
        while self._transport is not None and self._transport.is_active():
            try:
                conn, addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                chan = self._transport.open_channel(
                    "forwarded-tcpip", sock.getsockname(), addr)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                continue
            threading.Thread(target=_pump, args=(conn, chan),
                             daemon=True).start()
            threading.Thread(target=_pump, args=(chan, conn),
                             daemon=True).start()

    def check_channel_direct_tcpip_request(self, chanid, origin, destination):
        """客户端 -L：服务器侧接受通道并转发到 destination。"""
        self._direct_targets[chanid] = destination
        threading.Thread(target=self._serve_direct, args=(chanid,),
                         daemon=True).start()
        return paramiko.OPEN_SUCCEEDED

    def _serve_direct(self, chanid):
        chan = None
        try:
            target = self._direct_targets.pop(chanid, None)
            if not target:
                raise RuntimeError("direct-tcpip 目标丢失")
            # 服务器 accept 队列里混着 session 等其它通道（session 已被
            # _shell_loop 接管），必须按 chanid 过滤出 direct-tcpip 通道。
            with self._direct_lock:
                deadline = time.time() + 10
                while time.time() < deadline:
                    cand = self._transport.accept(1)
                    if cand is None:
                        continue
                    if cand.get_id() == chanid:
                        chan = cand
                        break
            if chan is None:
                raise RuntimeError("未等到 direct-tcpip 通道")
            sock = socket.create_connection(target, timeout=10)
            threading.Thread(target=_pump, args=(chan, sock),
                             daemon=True).start()
            threading.Thread(target=_pump, args=(sock, chan),
                             daemon=True).start()
        except Exception:
            if chan is not None:
                try:
                    chan.close()
                except Exception:
                    pass


def _start_server(seed_host_key: bool = True) -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    port = sock.getsockname()[1]
    if seed_host_key:
        # 预置已知主机记录，让 TOFU 门禁直接放行（TOFU 专项测试传 False）
        hk.save_known_host("127.0.0.1", port, _HOST_KEY.get_name(),
                           hk.fingerprint_sha256(_HOST_KEY))

    def accept_loop():
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            srv = EchoServer()
            t = paramiko.Transport(conn)
            t.add_server_key(_HOST_KEY)
            _TRANSPORTS.append(t)
            srv._transport = t
            try:
                t.start_server(server=srv)
            except Exception:
                pass

    threading.Thread(target=accept_loop, daemon=True).start()
    return port


_RE_HOSTPORT = re.compile(r"([0-9a-zA-Z.\-]+):(\d+)")


def _wait_for(pred, timeout=15.0) -> bool:
    app = QApplication.instance()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        app.processEvents()
        time.sleep(0.02)
    return False


def test_ssh_echo():
    """服务器只在 ECHO=1 被请求时回显 —— 修复前此测试失败。"""
    port = _start_server()

    # 负对照：paramiko 默认 invoke_shell（空 terminal-modes）不应获得回显，
    # 证明测试服务器正确模拟了"不回显的 sshd"。
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect("127.0.0.1", port, username="tester", password="pw",
                   look_for_keys=False, allow_agent=False, timeout=10)
    chan = client.invoke_shell(term="xterm", width=80, height=24)
    chan.settimeout(0.2)
    time.sleep(0.3)
    chan.sendall(b"neg-control\r")
    got = b""
    deadline = time.time() + 2.0
    while time.time() < deadline:
        try:
            data = chan.recv(1024)
        except socket.timeout:
            continue
        if not data:
            break
        got += data
        if b"neg-control" in got:
            break
    client.close()
    if b"neg-control" in got:
        raise AssertionError("负对照失败：服务器在未请求 ECHO 时也回显，测试环境失真")

    received = []
    w = SSHWorker("127.0.0.1", port, "tester", password="pw")
    w.dataReceived.connect(received.append)
    w.error.connect(lambda m: received.append(("ERR", m)))
    w.connected.connect(lambda: w.send(b"hello-echo\r"))
    w.start()
    ok = _wait_for(lambda: any(isinstance(x, bytes) and b"hello-echo" in x
                               for x in received))
    w.stop()
    w.wait(3000)
    if not ok:
        errs = [x[1] for x in received if isinstance(x, tuple)]
        raise AssertionError(
            "SSH 服务器未回显输入（pty-req 未携带 ECHO=1？） errors=%r" % errs)
    print("[PASS] test_ssh_echo: pty 请求携带 ECHO=1，服务器回显输入")


def test_new_session_while_connected():
    """已连接 + 数据流量中打开「新建连接」对话框（真实嵌套事件循环）。"""
    EchoServer.spam = True  # 模拟繁忙服务器持续输出
    port = _start_server()
    win = MainWindow()
    win.show()

    # 免密码弹框：临时替换 QInputDialog.getText
    import app.ui.main_window as mw
    orig_gettext = mw.QInputDialog.getText
    mw.QInputDialog.getText = staticmethod(lambda *a, **k: ("pw", True))
    try:
        s = config.make_session(protocol="ssh", host="127.0.0.1", port=port,
                                username="tester", name="e2e", auth="password")
        win._open_console(s)
    finally:
        mw.QInputDialog.getText = orig_gettext

    connected = _wait_for(
        lambda: any(isinstance(win.tabs.widget(i), ConsoleTab)
                    and win.tabs.widget(i)._status.text() == "已连接"
                    for i in range(win.tabs.count())), timeout=20.0)
    if not connected:
        raise AssertionError("SSH 未能连接到内嵌服务器")

    # 让数据流一会儿（复现"连接中"状态），再打开新建连接对话框
    _wait_for(lambda: False, timeout=0.3)

    # 真实 exec_()：嵌套事件循环运行期间，worker 信号持续投递
    opened = {}
    def _auto_accept():
        for d in QApplication.topLevelWidgets():
            if isinstance(d, ConnectDialog):
                opened["found"] = True
                opened["visible"] = d.isVisible()
                d.host_edit.setText("127.0.0.1")
                d.user_edit.setText("tester")
                d.accept()
                return
        opened["none"] = True

    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(_auto_accept)
    timer.start(800)
    try:
        win._new_session()  # 阻塞在真实嵌套事件循环中，800ms 后被接受
    finally:
        timer.stop()

    win.tabs.setCurrentIndex(0)
    for _ in range(50):
        QApplication.instance().processEvents()
        time.sleep(0.02)
    if len(win.sessions) != 1:
        raise AssertionError("新会话未保存: %d 个会话" % len(win.sessions))
    # 会话已落盘（隔离的临时 config 目录）
    saved = config.load_sessions()
    if len(saved) != 1 or saved[0].get("host") != "127.0.0.1":
        raise AssertionError("新会话未写入磁盘: %r" % (saved,))
    # 期间终端仍在接收服务器数据（证明嵌套循环里数据流正常）
    tab = win.tabs.widget(1)
    text = tab.terminal.toPlainText() if isinstance(tab, ConsoleTab) else ""
    print("[PASS] test_new_session_while_connected: 已连接+数据流中新建连接无崩溃"
          "（对话框已保存，终端收到 %d 字符）" % len(text))
    win.close()
    QApplication.instance().processEvents()
    EchoServer.spam = False


def test_dialog_visibility():
    """新建连接对话框可按协议/认证方式切换可见行（Qt5 兼容的 setRowVisible 替代）。"""
    dlg = ConnectDialog(None)
    # SSH + 密码认证：默认可见用户名/认证/密码，SSL 行隐藏
    assert not dlg.user_edit.isHidden()
    assert not dlg.pass_edit.isHidden()
    assert dlg.verify_check.isHidden()
    assert dlg._cert_row.isHidden()
    assert dlg._ckey_row.isHidden()
    # SSL：隐藏 SSH 专属行
    dlg.protocol_combo.setCurrentIndex(1)
    assert dlg.user_edit.isHidden()
    assert dlg.auth_combo.isHidden()
    assert not dlg.verify_check.isHidden()
    assert not dlg._cert_row.isHidden()
    assert dlg.port_spin.value() == 443
    # SSH + 私钥认证：隐藏密码行，显示私钥行
    dlg.protocol_combo.setCurrentIndex(0)
    dlg.auth_combo.setCurrentIndex(1)
    assert dlg.pass_edit.isHidden()
    assert dlg.save_pass.isHidden()
    assert not dlg._key_row.isHidden()
    assert not dlg.save_pp.isHidden()
    assert dlg.port_spin.value() == 22
    print("[PASS] test_dialog_visibility: 对话框行可见性切换正常")


def test_local_echo():
    """服务器完全不回显时，本地回显（默认勾选）保证键入文字立即可见。"""
    EchoServer.never_echo = True
    port = _start_server()
    params = config.make_session(protocol="ssh", host="127.0.0.1", port=port,
                                 username="tester", name="e2e-local-echo",
                                 auth="password", password="pw")
    tab = ConsoleTab(params["name"], params)
    tab.show()
    tab.start()
    connected = _wait_for(lambda: tab._status.text() == "已连接", timeout=20.0)
    if not connected:
        tab.shutdown()
        EchoServer.never_echo = False
        raise AssertionError("SSH 未能连接到内嵌服务器")
    # 默认勾选本地回显
    assert tab._echo.isChecked(), "SSH 会话默认应勾选本地回显"

    # 等欢迎横幅落屏，此后服务器不再发送任何数据
    _wait_for(lambda: "$ " in tab.terminal.toPlainText())
    time.sleep(0.3)

    # 模拟真实按键（走 keyPressEvent → dataSent → 本地回显 完整链路）。
    # 不按任何控制键、服务器零流量 —— 字符必须立即显示
    # （修复前字符滞留缓冲，要按退格/回车才出现）。
    for ch in "hi":
        ev = QKeyEvent(QEvent.KeyPress, 0, Qt.NoModifier, ch)
        QApplication.sendEvent(tab.terminal, ev)
    ok = _wait_for(lambda: tab.terminal.toPlainText().endswith("hi"), timeout=3.0)
    if not ok:
        text = tab.terminal.toPlainText()[-60:]
        tab.shutdown()
        EchoServer.never_echo = False
        raise AssertionError("键入字符未立即显示（仍滞留缓冲？）: %r" % text)

    # 退格应删除本地回显的最后一个字符
    QApplication.sendEvent(tab.terminal,
                           QKeyEvent(QEvent.KeyPress, Qt.Key_Backspace,
                                     Qt.NoModifier))
    ok = _wait_for(lambda: tab.terminal.toPlainText().endswith("h"), timeout=3.0)
    if not ok:
        text = tab.terminal.toPlainText()[-60:]
        tab.shutdown()
        EchoServer.never_echo = False
        raise AssertionError("退格未删除本地回显字符: %r" % text)

    tab.shutdown()
    EchoServer.never_echo = False
    print("[PASS] test_local_echo: 键入字符立即显示，退格正常删除")


def test_echo_suppression():
    """服务器回显 + 本地回显同时开启时，键入字符只显示一次（自动去重）。"""
    EchoServer.never_echo = False
    port = _start_server()
    params = config.make_session(protocol="ssh", host="127.0.0.1", port=port,
                                 username="tester", name="e2e-dedup",
                                 auth="password", password="pw")
    tab = ConsoleTab(params["name"], params)
    tab.show()
    tab.start()
    connected = _wait_for(lambda: tab._status.text() == "已连接", timeout=20.0)
    if not connected:
        tab.shutdown()
        raise AssertionError("SSH 未能连接到内嵌服务器")
    assert tab._echo.isChecked(), "本地回显默认应勾选"
    _wait_for(lambda: "$ " in tab.terminal.toPlainText())
    time.sleep(0.3)

    # 键入 abc：本地回显立即显示；服务器回显随后到达，必须被抑制
    for ch in "abc":
        QApplication.sendEvent(tab.terminal,
                               QKeyEvent(QEvent.KeyPress, 0, Qt.NoModifier, ch))
    ok = _wait_for(lambda: tab.terminal.toPlainText().endswith("abc"),
                   timeout=3.0)
    # 等服务器回显到达（若抑制失效，会追加成 aabbcc）
    time.sleep(0.5)
    QApplication.instance().processEvents()
    text = tab.terminal.toPlainText()
    tab.shutdown()
    if not ok:
        raise AssertionError("键入字符未立即显示: %r" % text[-60:])
    if text.count("abc") != 1 or not text.endswith("abc"):
        raise AssertionError("本地回显+服务器回显出现重复字符: %r" % text[-80:])
    print("[PASS] test_echo_suppression: 双回显去重，键入字符只显示一次")


def test_scroll_follow():
    """用户向上翻历史时新输出不拉回底部；位于底部时正常跟随。"""
    term = TerminalWidget()
    term.resize(420, 180)
    term.show()
    QApplication.instance().processEvents()
    for i in range(150):
        term.feed(("line-%03d\r\n" % i).encode("ascii"))
    QApplication.instance().processEvents()
    sb = term.verticalScrollBar()
    assert sb.maximum() > 0, "终端应有可滚动内容"

    # 向上翻：新数据到达后位置保持
    sb.setValue(0)
    QApplication.instance().processEvents()
    term.feed(b"new-data-while-scrolled-up")
    QApplication.instance().processEvents()
    if sb.value() > 4:
        raise AssertionError("用户向上翻历史时被新输出拉回底部: value=%d" % sb.value())

    # 回到底部：新数据到达后跟随滚动
    sb.setValue(sb.maximum())
    QApplication.instance().processEvents()
    term.feed(b"tail-data")
    QApplication.instance().processEvents()
    if sb.value() < sb.maximum() - 4:
        raise AssertionError("位于底部时未跟随滚动: value=%d max=%d"
                             % (sb.value(), sb.maximum()))
    print("[PASS] test_scroll_follow: 滚动位置保持/跟随行为正常")


def test_port_forwarding():
    """端口转发：-L 本地转发、-R 远程转发、-D SOCKS5（echo 服务器往返验证）。"""
    echo_port = _start_echo_server()
    ssh_port = _start_server()
    rules = [
        "L:127.0.0.1:0:127.0.0.1:%d" % echo_port,
        "R:127.0.0.1:0:127.0.0.1:%d" % echo_port,
        "D:127.0.0.1:0",
    ]
    w = SSHWorker("127.0.0.1", ssh_port, "tester", password="pw",
                  forwards=rules)
    started, errors = [], []
    w.forwardStarted.connect(started.append)
    w.forwardError.connect(errors.append)
    w.error.connect(lambda m: errors.append("连接错误: " + m))
    w.start()
    ok = _wait_for(lambda: len(started) >= 3 or errors, timeout=25.0)
    if not ok:
        w.stop()
        w.wait(3000)
        raise AssertionError("转发启动超时: started=%r errors=%r"
                             % (started, errors))

    def get_port(prefix):
        for msg in started:
            if msg.startswith(prefix):
                # 消息形如 "本地转发 127.0.0.1:12345 → ..." 或
                # "SOCKS5 代理 127.0.0.1:12345（...）"；取第一个 host:port
                m = _RE_HOSTPORT.search(msg)
                if m:
                    return int(m.group(2))
        raise AssertionError("启动提示缺少端口: %r" % started)

    l_port = get_port("本地转发")
    r_port = get_port("远程转发")
    d_port = get_port("SOCKS5")

    def roundtrip(host, port, payload, timeout=10.0):
        s = socket.create_connection((host, port), timeout=timeout)
        s.settimeout(timeout)
        try:
            s.sendall(payload)
            buf = b""
            deadline = time.time() + timeout
            while time.time() < deadline and len(buf) < len(payload):
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
        finally:
            s.close()
        return buf

    # -L：连本机 l_port → SSH 隧道 → 服务器 → echo 服务器
    got = roundtrip("127.0.0.1", l_port, b"local-forward-ok")
    if got != b"local-forward-ok":
        w.stop()
        w.wait(3000)
        raise AssertionError("-L 本地转发回显失败: %r" % got)

    # -R：连服务器 r_port → SSH 隧道 → 本机 echo 服务器
    got = roundtrip("127.0.0.1", r_port, b"remote-forward-ok")
    if got != b"remote-forward-ok":
        w.stop()
        w.wait(3000)
        raise AssertionError("-R 远程转发回显失败: %r" % got)

    # -D：SOCKS5 握手（无认证）+ CONNECT 到 echo 服务器
    s = socket.create_connection(("127.0.0.1", d_port), timeout=10.0)
    s.settimeout(10.0)
    try:
        s.sendall(b"\x05\x01\x00")
        if s.recv(2) != b"\x05\x00":
            raise AssertionError("-D SOCKS5 无认证握手失败")
        req = (b"\x05\x01\x00\x01" + socket.inet_aton("127.0.0.1")
               + struct.pack(">H", echo_port))
        s.sendall(req)
        reply = b""
        while len(reply) < 10:
            chunk = s.recv(10 - len(reply))
            if not chunk:
                break
            reply += chunk
        if len(reply) < 10 or reply[1] != 0x00:
            raise AssertionError("-D SOCKS5 CONNECT 失败: %r" % reply)
        s.sendall(b"socks5-ok")
        buf = b""
        deadline = time.time() + 10.0
        while time.time() < deadline and len(buf) < len(b"socks5-ok"):
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        if buf != b"socks5-ok":
            raise AssertionError("-D SOCKS5 数据转发失败: %r" % buf)
    finally:
        s.close()

    w.stop()
    w.wait(3000)
    print("[PASS] test_port_forwarding: -L/-R/-D 端口转发回显全部成功")


def test_host_key_tofu():
    """主机密钥 TOFU：未知需确认；变更需警告；拒绝变更后服务器不应收到凭据。"""
    port = _start_server(seed_host_key=False)

    # (1) 未知密钥 → hostKeyUnknown → 接受 → 连接成功并保存记录
    w = SSHWorker("127.0.0.1", port, "tester", password="pw")
    unknowns, errors = [], []
    w.hostKeyUnknown.connect(lambda fp: (unknowns.append(fp),
                                         w.resolve_host_key(True)))
    w.error.connect(errors.append)
    w.start()
    ok = _wait_for(lambda: w._connected, timeout=20.0)
    w.stop()
    w.wait(3000)
    if not ok:
        raise AssertionError("接受主机密钥后应连接成功: errors=%r" % errors)
    assert len(unknowns) == 1, "首次连接应恰好触发一次 hostKeyUnknown"
    entry = hk.load_known_hosts().get(hk.hostkey_id("127.0.0.1", port))
    assert entry and entry["fingerprint"] == unknowns[0], \
        "接受后应将指纹写入 known_hosts"
    assert unknowns[0] == hk.fingerprint_sha256(_HOST_KEY), "指纹内容不符"

    # (2) 密钥变更 → hostKeyChanged → 拒绝 → error 且服务器未收到任何凭据
    hk.save_known_host("127.0.0.1", port, "ssh-rsa",
                       "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    EchoServer.auth_attempts = 0
    w2 = SSHWorker("127.0.0.1", port, "tester", password="pw")
    changed, errors2 = [], []
    w2.hostKeyChanged.connect(lambda fp, old: (changed.append((fp, old)),
                                               w2.resolve_host_key(False)))
    w2.error.connect(errors2.append)
    w2.start()
    ok = _wait_for(lambda: changed or errors2, timeout=20.0)
    # error 通过队列信号投递，需继续跑事件循环直到槽函数执行
    _wait_for(lambda: errors2, timeout=5.0)
    w2.wait(3000)
    assert changed, "密钥变更应触发 hostKeyChanged"
    assert changed[0][1] == "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", \
        "hostKeyChanged 应携带旧指纹"
    assert errors2 and any("不一致" in m for m in errors2), \
        "拒绝变更后应触发 error（密钥不一致）"
    assert not w2._connected
    assert EchoServer.auth_attempts == 0, \
        "拒绝后不得向服务器发送任何凭据（实际认证次数 %d）" % EchoServer.auth_attempts

    # (3) 密钥变更 → 接受 → 重新连接成功且记录更新
    hk.save_known_host("127.0.0.1", port, "ssh-rsa",
                       "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    w3 = SSHWorker("127.0.0.1", port, "tester", password="pw")
    w3.hostKeyChanged.connect(lambda fp, old: w3.resolve_host_key(True))
    w3.start()
    ok = _wait_for(lambda: w3._connected, timeout=20.0)
    w3.stop()
    w3.wait(3000)
    if not ok:
        raise AssertionError("接受变更密钥后应重新连接成功")
    entry = hk.load_known_hosts().get(hk.hostkey_id("127.0.0.1", port))
    assert entry["fingerprint"] == hk.fingerprint_sha256(_HOST_KEY), \
        "接受新密钥后应更新记录"

    # (4) 主机密钥确认对话框可独立构造
    d1 = HostKeyDialog(None, "h", 22, "SHA256:x", None)
    d2 = HostKeyDialog(None, "h", 22, "SHA256:x", "SHA256:y")
    assert "首次" in d1.windowTitle() and "变更" in d2.windowTitle()

    print("[PASS] test_host_key_tofu: 未知确认/变更警告/拒绝不泄凭据 全部正常")


def test_terminal_experience():
    """V2.0 终端体验端到端：会话日志、备用屏幕（ESC 序列真实往返）、括号粘贴。"""
    port = _start_server()
    params = config.make_session(protocol="ssh", host="127.0.0.1", port=port,
                                 username="tester", name="e2e-term",
                                 auth="password", password="pw")
    tab = ConsoleTab(params["name"], params)
    tab.show()
    tab.start()
    connected = _wait_for(lambda: tab._status.text() == "已连接", timeout=20.0)
    if not connected:
        tab.shutdown()
        raise AssertionError("SSH 未能连接到内嵌服务器")
    _wait_for(lambda: "$ " in tab.terminal.toPlainText(), timeout=5.0)
    time.sleep(0.3)

    # (1) 会话日志：文件已创建且包含服务器原始输出
    lp = tab.log_path
    assert lp and os.path.isfile(lp), "会话日志文件应存在: %r" % lp
    with open(lp, encoding="utf-8") as f:
        logtext = f.read()
    assert "Welcome to SSLTool test sshd" in logtext, "日志应含服务器欢迎语"

    EchoServer.record_data = True
    EchoServer.received = []
    try:
        # (2) 备用屏幕：ESC [?1049h / [?1049l 经真实 SSH 通道往返
        QApplication.sendEvent(tab.terminal,
                               QKeyEvent(QEvent.KeyPress, Qt.Key_Escape,
                                         Qt.NoModifier))
        for ch in "[?1049h":
            QApplication.sendEvent(tab.terminal,
                                   QKeyEvent(QEvent.KeyPress, 0,
                                             Qt.NoModifier, ch))
        ok = _wait_for(lambda: tab.terminal._screen.is_alt_active(),
                       timeout=5.0)
        assert ok, "1049h 应进入备用屏幕"
        assert "Welcome to SSLTool test sshd" not in tab.terminal.toPlainText(), \
            "备用屏幕不应显示主屏内容"
        for ch in "vim-mode":
            QApplication.sendEvent(tab.terminal,
                                   QKeyEvent(QEvent.KeyPress, 0,
                                             Qt.NoModifier, ch))
        assert _wait_for(lambda: "vim-mode" in tab.terminal.toPlainText(),
                         timeout=3.0), "备用屏幕应显示新键入内容"
        QApplication.sendEvent(tab.terminal,
                               QKeyEvent(QEvent.KeyPress, Qt.Key_Escape,
                                         Qt.NoModifier))
        for ch in "[?1049l":
            QApplication.sendEvent(tab.terminal,
                                   QKeyEvent(QEvent.KeyPress, 0,
                                             Qt.NoModifier, ch))
        ok = _wait_for(lambda: not tab.terminal._screen.is_alt_active(),
                       timeout=5.0)
        assert ok, "1049l 应退出备用屏幕"
        text = tab.terminal.toPlainText()
        assert "Welcome to SSLTool test sshd" in text, "退出后主屏应恢复"
        assert "vim-mode" not in text, "备用屏内容不应泄漏到主屏"

        # (3) 括号粘贴：服务端收到的原始字节被 200~/201~ 包裹
        QApplication.clipboard().setText("paste-content")
        EchoServer.received = []
        tab.terminal._paste_clipboard()
        ok = _wait_for(
            lambda: any(b"\x1b[200~" in d for d in EchoServer.received),
            timeout=5.0)
        assert ok, "服务端应收到括号粘贴包裹的数据: %r" % (EchoServer.received,)
        wrapped = [d for d in EchoServer.received if b"\x1b[200~" in d]
        assert any(d.rstrip().endswith(b"\x1b[201~") for d in wrapped), \
            "粘贴数据应以 \\x1b[201~ 结尾"
        assert any(b"paste-content" in d for d in wrapped), \
            "服务端应收到粘贴内容"
    finally:
        EchoServer.record_data = False
        EchoServer.received = []
    tab.shutdown()
    print("[PASS] test_terminal_experience: 会话日志/备用屏幕/括号粘贴 全部正常")


def main():
    app = QApplication.instance() or QApplication([])
    tests = [test_ssh_echo, test_new_session_while_connected,
             test_dialog_visibility, test_local_echo, test_echo_suppression,
             test_scroll_follow, test_port_forwarding, test_host_key_tofu,
             test_terminal_experience]
    for fn in tests:
        # 每个测试前复位服务器开关，防止前一测试的类属性状态污染
        EchoServer.spam = False
        EchoServer.never_echo = False
        EchoServer.record_data = False
        EchoServer.received = []
        try:
            fn()
        except Exception as e:
            _FAILURES.append((fn.__name__, e))
            import traceback
            traceback.print_exc()
            print("[FAIL] %s: %s" % (fn.__name__, e))
    if _FAILURES:
        print("\n%d/%d 测试失败" % (len(_FAILURES), len(tests)))
        sys.exit(1)
    print("\n全部 %d 项 e2e 测试通过" % len(tests))
    sys.exit(0)


if __name__ == "__main__":
    main()
