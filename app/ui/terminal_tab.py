"""终端会话标签页：SSH 交互式终端 / SSL 文本控制台。"""
from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
                             QPushButton, QCheckBox)

from app.core.workers import SSHWorker, SSLWorker
from app.core.session_log import SessionLog
from app.ui.hostkey_dialog import (confirm_changed_host_key,
                                   confirm_unknown_host_key)
from app.ui.terminal_widget import TerminalWidget


class ConsoleTab(QWidget):
    tabClosed = pyqtSignal(object)  # 关闭请求，携带自身

    def __init__(self, name: str, params: dict, parent=None):
        super().__init__(parent)
        self._params = params
        self._name = name
        self.worker = None
        self._log = None  # SessionLog；日志是增值功能，任何异常都不影响终端

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        header = QFrame()
        header.setObjectName("card")
        h = QHBoxLayout(header)
        h.setContentsMargins(10, 6, 10, 6)
        name_label = QLabel(name)
        name_label.setStyleSheet("font-weight: 600; color: #303133;")
        self._status = QLabel("连接中…")
        self._status.setStyleSheet("color: #909399;")
        self._echo = QCheckBox("本地回显")
        # 默认开启本地回显：即使服务器不回显（Windows sshd、网络设备等），
        # 输入的文字也立即可见；若服务器也会回显，回显会被自动去重，
        # 仍然只显示一个字符。
        self._echo.setChecked(True)
        self._echo.setToolTip(
            "在本地直接显示键入内容，并自动去除服务器的重复回显。\n"
            "若显示仍有异常，可取消勾选，完全依赖服务器回显。")
        self._log_chk = QCheckBox("会话日志")
        self._log_chk.setChecked(True)
        self._log_chk.setToolTip(
            "把会话输出记录到 ~/.ssltool/logs/ 下的日志文件。\n"
            "取消勾选后新会话不再记录。")
        reconnect_btn = QPushButton("重新连接")
        reconnect_btn.setObjectName("secondary")
        close_btn = QPushButton("关闭")
        close_btn.setObjectName("secondary")
        h.addWidget(name_label)
        h.addWidget(self._status)
        h.addWidget(self._echo)
        h.addWidget(self._log_chk)
        h.addStretch(1)
        h.addWidget(reconnect_btn)
        h.addWidget(close_btn)

        self.terminal = TerminalWidget()
        layout.addWidget(header)
        layout.addWidget(self.terminal, 1)

        reconnect_btn.clicked.connect(self._reconnect)
        close_btn.clicked.connect(lambda: self.tabClosed.emit(self))
        self._echo.toggled.connect(self.terminal.set_local_echo)
        self._log_chk.toggled.connect(self._on_log_toggled)
        self.terminal.dataSent.connect(self._on_data_sent)
        self.terminal.ptySizeChanged.connect(self._on_pty_size)

        self._create_worker()

    # ---------------- 会话日志 ----------------

    @property
    def log_path(self):
        """当前日志文件路径（未启用或无日志时 None）。测试用。"""
        return self._log.path if self._log else None

    def _open_log(self):
        p = self._params
        self._log = SessionLog(self._name, p["host"], int(p["port"]),
                               user=p.get("username", ""))

    def _close_log(self):
        if self._log:
            self._log.close()
            self._log = None

    def _on_log_toggled(self, on: bool):
        if on:
            if not self._log:
                self._open_log()
        else:
            self._close_log()

    # ---------------- worker 生命周期 ----------------

    def _create_worker(self):
        p = self._params
        if p.get("protocol") == "ssl":
            w = SSLWorker(p["host"], int(p["port"]), verify=p.get("verify", True),
                          client_cert=p.get("client_cert") or None,
                          client_key=p.get("client_key") or None)
        else:
            w = SSHWorker(p["host"], int(p["port"]), p.get("username", ""),
                          password=p.get("password"),
                          key_path=p.get("key_path") or None,
                          passphrase=p.get("passphrase"),
                          forwards=p.get("forwards"))
        w.connected.connect(self._on_connected)
        w.dataReceived.connect(self._on_data)
        w.error.connect(self._on_error)
        w.disconnected.connect(self._on_disconnected)
        if isinstance(w, SSLWorker):
            w.certInfo.connect(self._on_cert_info)
        else:
            w.forwardStarted.connect(self._on_forward_started)
            w.forwardError.connect(self._on_forward_error)
            w.hostKeyUnknown.connect(self._on_host_key_unknown)
            w.hostKeyChanged.connect(self._on_host_key_changed)
        self.worker = w

    def start(self):
        self._set_status("连接中…", "#909399")
        if self._log_chk.isChecked() and not self._log:
            self._open_log()
        self.worker.start()

    def shutdown(self):
        self._close_log()
        if self.worker:
            w = self.worker
            self.worker = None
            w.stop()
            if not w.wait(1000):
                w.finished.connect(w.deleteLater)

    def _reconnect(self):
        self._close_log()
        if self.worker:
            old = self.worker
            self.worker = None
            old.stop()
            if not old.wait(1000):
                old.finished.connect(old.deleteLater)
        self.terminal.feed(b"\r\n--- \xe9\x87\x8d\xe6\x96\xb0\xe8\xbf\x9e\xe6\x8e\xa5 ---\r\n")
        self._create_worker()
        self.start()

    # ---------------- 信号处理 ----------------

    def _set_status(self, text, color):
        self._status.setText(text)
        self._status.setStyleSheet(f"color: {color};")

    def _on_data(self, data: bytes):
        self.terminal.feed_remote(data)
        if self._log:
            self._log.write(data)

    def _on_connected(self):
        self._set_status("已连接", "#67c23a")
        if self._log:
            self._log.note("已连接")
        self.terminal.update_pty_size(force=True)

    def _on_error(self, msg):
        self._set_status(msg, "#f56c6c")
        if self._log:
            self._log.note(f"错误: {msg}")
        self.terminal.feed(("\r\n[" + msg + "]\r\n").encode("utf-8"))

    def _on_disconnected(self, reason):
        text = "已断开" + (f": {reason}" if reason else "")
        self._set_status(text, "#909399")
        if self._log:
            self._log.note(text)

    def _on_cert_info(self, text):
        self.terminal.feed(("\r\n" + text + "\r\n").encode("utf-8"))

    def _on_forward_started(self, msg):
        self.terminal.feed(("\r\n[" + msg + "]\r\n").encode("utf-8"))

    def _on_forward_error(self, msg):
        self.terminal.feed(("\r\n[端口转发失败: " + msg + "]\r\n").encode("utf-8"))

    # ---------------- 主机密钥 TOFU ----------------

    def _on_host_key_unknown(self, fp):
        self._set_status("确认主机密钥…", "#e6a23c")
        p = self._params
        ok = confirm_unknown_host_key(self, p["host"], int(p["port"]), fp)
        if self.worker:
            self.worker.resolve_host_key(ok)
        if not ok:
            self._set_status("已取消：未确认主机密钥", "#f56c6c")

    def _on_host_key_changed(self, fp, expected):
        self._set_status("主机密钥已变更！", "#f56c6c")
        p = self._params
        ok = confirm_changed_host_key(self, p["host"], int(p["port"]),
                                      fp, expected)
        if self.worker:
            self.worker.resolve_host_key(ok)
        if not ok:
            self._set_status("已取消：主机密钥与记录不一致", "#f56c6c")

    def _on_data_sent(self, data: bytes):
        if self.worker:
            self.worker.send(data)
        if self._echo.isChecked():
            self.terminal.feed_local(data)

    def _on_pty_size(self, cols, rows):
        if isinstance(self.worker, SSHWorker):
            self.worker.resize_pty(cols, rows)
