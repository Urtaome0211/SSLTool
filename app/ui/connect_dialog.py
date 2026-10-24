"""新建 / 编辑连接对话框。"""
import os

from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
                             QGridLayout, QLineEdit, QComboBox, QSpinBox,
                             QCheckBox, QPushButton, QWidget, QFileDialog,
                             QMessageBox, QLabel, QPlainTextEdit)

from app import config
from app.core.forwarders import parse_forwards


class ConnectDialog(QDialog):
    def __init__(self, parent=None, session=None):
        super().__init__(parent)
        self._session_id = session.get("id") if session else None
        self.setWindowTitle("编辑连接" if session else "新建连接")
        self.setMinimumWidth(480)
        s = session or config.make_session()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(12)

        self.form = QFormLayout()
        self.form.setSpacing(10)

        self.name_edit = QLineEdit(s.get("name", ""))
        self.name_edit.setPlaceholderText("例如: 我的服务器")
        self.form.addRow("名称", self.name_edit)

        self.group_edit = QLineEdit(s.get("group", ""))
        self.group_edit.setPlaceholderText("例如: 生产 / 测试（留空为未分组）")
        self.form.addRow("分组", self.group_edit)

        self.project_combo = QComboBox()
        for p in config.load_projects():
            self.project_combo.addItem(p["name"], p["id"])
        idx = self.project_combo.findData(s.get("project") or config.DEFAULT_PROJECT_ID)
        self.project_combo.setCurrentIndex(max(idx, 0))
        self.form.addRow("工程", self.project_combo)

        self.protocol_combo = QComboBox()
        self.protocol_combo.addItems(["SSH 终端", "SSL 加密通道"])
        self.protocol_combo.setCurrentIndex(0 if s.get("protocol", "ssh") == "ssh" else 1)
        self.form.addRow("协议", self.protocol_combo)

        self.host_edit = QLineEdit(s.get("host", ""))
        self.host_edit.setPlaceholderText("主机名或 IP 地址")
        self.form.addRow("主机", self.host_edit)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(int(s.get("port") or 22))
        self.form.addRow("端口", self.port_spin)

        self.user_edit = QLineEdit(s.get("username", ""))
        self.user_edit.setPlaceholderText("登录用户名")
        self.form.addRow("用户名", self.user_edit)

        self.auth_combo = QComboBox()
        self.auth_combo.addItems(["密码认证", "私钥认证"])
        self.auth_combo.setCurrentIndex(1 if s.get("auth") == "key" else 0)
        self.form.addRow("认证方式", self.auth_combo)

        self.pass_edit = QLineEdit()
        self.pass_edit.setEchoMode(QLineEdit.Password)
        self.pass_edit.setPlaceholderText("留空则在连接时询问")
        self.form.addRow("密码", self.pass_edit)

        self.save_pass = QCheckBox("记住密码（存入系统钥匙串）")
        self.save_pass.setChecked(bool(s.get("save_password")))
        self.form.addRow("", self.save_pass)

        key_row = QWidget()
        kh = QHBoxLayout(key_row)
        kh.setContentsMargins(0, 0, 0, 0)
        self.key_edit = QLineEdit(s.get("key_path", ""))
        self.key_edit.setPlaceholderText("OpenSSH / PEM 私钥路径")
        key_browse = QPushButton("浏览…")
        key_browse.setObjectName("secondary")
        key_browse.clicked.connect(lambda: self._browse(self.key_edit, "选择私钥文件"))
        kh.addWidget(self.key_edit, 1)
        kh.addWidget(key_browse)
        self.form.addRow("私钥", key_row)

        self.save_pp = QCheckBox("记住私钥口令（存入系统钥匙串）")
        self.save_pp.setChecked(bool(s.get("save_passphrase")))
        self.form.addRow("", self.save_pp)

        self.verify_check = QCheckBox("验证服务器证书")
        self.verify_check.setChecked(bool(s.get("ssl_verify", True)))
        self.form.addRow("", self.verify_check)

        cert_row = QWidget()
        ch_ = QHBoxLayout(cert_row)
        ch_.setContentsMargins(0, 0, 0, 0)
        self.cert_edit = QLineEdit(s.get("client_cert", ""))
        self.cert_edit.setPlaceholderText("客户端证书（可选）")
        cert_browse = QPushButton("浏览…")
        cert_browse.setObjectName("secondary")
        cert_browse.clicked.connect(lambda: self._browse(self.cert_edit, "选择客户端证书"))
        ch_.addWidget(self.cert_edit, 1)
        ch_.addWidget(cert_browse)
        self.form.addRow("客户端证书", cert_row)

        ckey_row = QWidget()
        ckh = QHBoxLayout(ckey_row)
        ckh.setContentsMargins(0, 0, 0, 0)
        self.ckey_edit = QLineEdit(s.get("client_key", ""))
        self.ckey_edit.setPlaceholderText("客户端私钥（可选）")
        ckey_browse = QPushButton("浏览…")
        ckey_browse.setObjectName("secondary")
        ckey_browse.clicked.connect(lambda: self._browse(self.ckey_edit, "选择客户端私钥"))
        ckh.addWidget(self.ckey_edit, 1)
        ckh.addWidget(ckey_browse)
        self.form.addRow("客户端私钥", ckey_row)

        fwd_row = QWidget()
        fh = QVBoxLayout(fwd_row)
        fh.setContentsMargins(0, 0, 0, 0)
        fh.setSpacing(4)
        fwd_hint = QLabel(
            "每行一条（OpenSSH 风格），例如：\n"
            "L:127.0.0.1:8080:internal:80    本地转发（本机 8080 → 内网 80）\n"
            "R:0.0.0.0:9000:127.0.0.1:22     远程转发（服务器 9000 → 本机 22）\n"
            "D:127.0.0.1:1080                 SOCKS5 动态代理")
        fwd_hint.setStyleSheet("color: #909399; font-size: 11px;")
        self.forwards_edit = QPlainTextEdit()
        self.forwards_edit.setPlaceholderText("L:127.0.0.1:8080:internal:80")
        self.forwards_edit.setMaximumHeight(96)
        self.forwards_edit.setPlainText("\n".join(s.get("forwards") or []))
        fh.addWidget(fwd_hint)
        fh.addWidget(self.forwards_edit)
        self.form.addRow("端口转发", fwd_row)

        jump_row = QWidget()
        jg = QGridLayout(jump_row)
        jg.setContentsMargins(0, 0, 0, 0)
        jg.setSpacing(6)
        self.jump_host_edit = QLineEdit(s.get("jump_host", ""))
        self.jump_host_edit.setPlaceholderText("跳板机地址（可选）")
        self.jump_port_spin = QSpinBox()
        self.jump_port_spin.setRange(1, 65535)
        self.jump_port_spin.setValue(int(s.get("jump_port") or 22))
        self.jump_user_edit = QLineEdit(s.get("jump_username", ""))
        self.jump_user_edit.setPlaceholderText("跳板机用户")
        self.jump_auth_combo = QComboBox()
        self.jump_auth_combo.addItems(["密码认证", "私钥认证"])
        self.jump_auth_combo.setCurrentIndex(
            1 if s.get("jump_auth") == "key" else 0)
        self.jump_key_edit = QLineEdit(s.get("jump_key_path", ""))
        self.jump_key_edit.setPlaceholderText("跳板机私钥路径")
        jump_key_browse = QPushButton("浏览…")
        jump_key_browse.setObjectName("secondary")
        jump_key_browse.clicked.connect(
            lambda: self._browse(self.jump_key_edit, "选择跳板机私钥"))
        jg.addWidget(self.jump_host_edit, 0, 0, 1, 2)
        jg.addWidget(self.jump_port_spin, 0, 2)
        jg.addWidget(self.jump_user_edit, 0, 3)
        jg.addWidget(self.jump_auth_combo, 1, 0)
        jg.addWidget(self.jump_key_edit, 1, 1, 1, 2)
        jg.addWidget(jump_key_browse, 1, 3)
        jump_hint = QLabel("经 SSH 跳板机连接目标（跳板机密码在连接时询问，不保存）")
        jump_hint.setStyleSheet("color: #909399; font-size: 11px;")
        self.form.addRow("跳板机", jump_row)
        self.form.addRow("", jump_hint)

        self.readonly_check = QCheckBox("SFTP 只读模式（禁止上传 / 删除 / 修改）")
        self.readonly_check.setChecked(bool(s.get("sftp_readonly")))
        self.form.addRow("", self.readonly_check)

        # 供 _apply_visibility 使用的行引用（PyQt5 的 QFormLayout 无
        # Qt 6.4 的 setRowVisible(row, bool)，只能显式隐藏控件+标签）
        self._key_row = key_row
        self._cert_row = cert_row
        self._ckey_row = ckey_row
        self._fwd_row = fwd_row
        self._jump_row = jump_row
        self._jump_hint = jump_hint

        layout.addLayout(self.form)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel_btn = QPushButton("取消")
        cancel_btn.setObjectName("secondary")
        save_btn = QPushButton("保存")
        cancel_btn.clicked.connect(self.reject)
        save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

        # 协议 / 认证方式切换时刷新可见行
        self.protocol_combo.currentIndexChanged.connect(self._apply_visibility)
        self.auth_combo.currentIndexChanged.connect(self._apply_visibility)
        self._apply_visibility()

    # ---------------- 可见性 ----------------

    def _is_ssh(self):
        return self.protocol_combo.currentIndex() == 0

    def _is_key_auth(self):
        return self.auth_combo.currentIndex() == 1

    def _apply_visibility(self):
        ssh = self._is_ssh()
        key = self._is_key_auth()
        # (字段控件, 是否可见)；标签通过 labelForField 一并隐藏。
        rows = (
            (self.user_edit, ssh),            # 用户名
            (self.auth_combo, ssh),           # 认证方式
            (self.pass_edit, ssh and not key),  # 密码
            (self.save_pass, ssh and not key),  # 记住密码
            (self._key_row, ssh and key),     # 私钥
            (self.save_pp, ssh and key),      # 记住口令
            (self.verify_check, not ssh),     # 验证证书
            (self._cert_row, not ssh),        # 客户端证书
            (self._ckey_row, not ssh),        # 客户端私钥
            (self._fwd_row, ssh),             # 端口转发
            (self._jump_row, ssh),            # 跳板机
            (self._jump_hint, ssh),           # 跳板机说明
            (self.readonly_check, ssh),       # SFTP 只读
        )
        for widget, visible in rows:
            widget.setVisible(visible)
            label = self.form.labelForField(widget)
            if label is not None:
                label.setVisible(visible)
        # 端口默认值跟随协议切换
        if ssh and self.port_spin.value() == 443:
            self.port_spin.setValue(22)
        elif not ssh and self.port_spin.value() == 22:
            self.port_spin.setValue(443)

    @staticmethod
    def _browse(edit, title):
        path, _ = QFileDialog.getOpenFileName(None, title, "", "所有文件 (*)")
        if path:
            edit.setText(path)

    # ---------------- 保存 ----------------

    def _on_save(self):
        ssh = self._is_ssh()
        host = self.host_edit.text().strip()
        if not host:
            QMessageBox.warning(self, "SSLTool", "请输入主机地址")
            return
        if ssh:
            if not self.user_edit.text().strip():
                QMessageBox.warning(self, "SSLTool", "请输入用户名")
                return
            if self._is_key_auth():
                key = self.key_edit.text().strip()
                if not key or not os.path.isfile(key):
                    QMessageBox.warning(self, "SSLTool", "请选择有效的私钥文件")
                    return
            elif not self.pass_edit.text().strip():
                QMessageBox.warning(self, "SSLTool", "请输入密码（或改用私钥认证）")
                return
            jump_host = self.jump_host_edit.text().strip()
            if jump_host:
                if not self.jump_user_edit.text().strip():
                    QMessageBox.warning(self, "SSLTool", "请输入跳板机用户名")
                    return
                if self.jump_auth_combo.currentIndex() == 1:
                    jkey = self.jump_key_edit.text().strip()
                    if not jkey or not os.path.isfile(jkey):
                        QMessageBox.warning(self, "SSLTool", "请选择有效的跳板机私钥文件")
                        return
            try:
                parse_forwards(self._forward_lines())
            except ValueError as e:
                QMessageBox.warning(self, "SSLTool", str(e))
                return
        self.accept()

    def _forward_lines(self):
        return [ln.strip() for ln in self.forwards_edit.toPlainText().splitlines()
                if ln.strip() and not ln.strip().startswith("#")]

    def get_session(self) -> dict:
        s = config.make_session()
        if self._session_id:
            s["id"] = self._session_id
        s["name"] = self.name_edit.text().strip() or self.host_edit.text().strip()
        s["protocol"] = "ssh" if self._is_ssh() else "ssl"
        s["host"] = self.host_edit.text().strip()
        s["port"] = self.port_spin.value()
        s["username"] = self.user_edit.text().strip()
        s["auth"] = "key" if self._is_key_auth() else "password"
        s["save_password"] = self.save_pass.isChecked()
        s["key_path"] = self.key_edit.text().strip()
        s["save_passphrase"] = self.save_pp.isChecked()
        s["ssl_verify"] = self.verify_check.isChecked()
        s["client_cert"] = self.cert_edit.text().strip()
        s["client_key"] = self.ckey_edit.text().strip()
        s["forwards"] = self._forward_lines()
        s["group"] = self.group_edit.text().strip()
        s["project"] = self.project_combo.currentData() or config.DEFAULT_PROJECT_ID
        s["sftp_readonly"] = self.readonly_check.isChecked()
        s["jump_host"] = self.jump_host_edit.text().strip()
        s["jump_port"] = self.jump_port_spin.value()
        s["jump_username"] = self.jump_user_edit.text().strip()
        s["jump_auth"] = "key" if self.jump_auth_combo.currentIndex() == 1 else "password"
        s["jump_key_path"] = self.jump_key_edit.text().strip()
        return s
