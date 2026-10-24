"""SSLTool 主窗口：侧栏会话管理 + 标签页工作区。"""
import os

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QGridLayout, QLabel, QFrame,
                             QLineEdit, QComboBox, QSpinBox, QPushButton,
                             QTreeWidget, QTreeWidgetItem, QTabWidget,
                             QMessageBox, QInputDialog, QAction, QDialog,
                             QFileDialog)

import paramiko

from app import config
from app import theme
from app.ui.connect_dialog import ConnectDialog
from app.ui.terminal_tab import ConsoleTab
from app.ui.sftp_tab import SFTPTab

APP_VERSION = "3.0.0"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"SSLTool v{APP_VERSION}")
        self.resize(1100, 700)
        self.sessions = config.load_sessions()
        self.settings = config.load_settings()
        if self.settings.get("dark"):
            theme.apply_theme(QApplication.instance(), dark=True)

        self._build_menu()
        self._build_central()
        self._refresh_list()
        self.statusBar().showMessage("就绪", 5000)

    # ---------------- 构建界面 ----------------

    def _build_menu(self):
        file_menu = self.menuBar().addMenu("文件")
        new_act = QAction("新建连接…", self)
        new_act.setShortcut("Ctrl+N")
        new_act.triggered.connect(self._new_session)
        file_menu.addAction(new_act)
        import_act = QAction("导入会话…", self)
        import_act.triggered.connect(self._import_sessions)
        file_menu.addAction(import_act)
        export_act = QAction("导出会话…", self)
        export_act.triggered.connect(self._export_sessions)
        file_menu.addAction(export_act)
        quit_act = QAction("退出", self)
        quit_act.setShortcut("Ctrl+Q")
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

        view_menu = self.menuBar().addMenu("视图")
        self.dark_act = QAction("深色主题", self)
        self.dark_act.setCheckable(True)
        self.dark_act.setShortcut("Ctrl+D")
        self.dark_act.setChecked(bool(self.settings.get("dark")))
        self.dark_act.toggled.connect(self._toggle_dark)
        view_menu.addAction(self.dark_act)

        help_menu = self.menuBar().addMenu("帮助")
        about_act = QAction("关于", self)
        about_act.triggered.connect(self._about)
        help_menu.addAction(about_act)

    def _toggle_dark(self, checked):
        theme.apply_theme(QApplication.instance(), dark=checked)
        self.settings["dark"] = checked
        config.save_settings(self.settings)

    def _build_central(self):
        central = QWidget()
        h = QHBoxLayout(central)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        # ---- 侧栏 ----
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(300)
        v = QVBoxLayout(sidebar)
        v.setContentsMargins(16, 18, 16, 16)
        v.setSpacing(10)

        title = QLabel("SSLTool")
        title.setObjectName("sidebarTitle")
        subtitle = QLabel("SSL · SSH · SFTP 客户端")
        subtitle.setObjectName("sidebarSubtitle")
        v.addWidget(title)
        v.addWidget(subtitle)

        # 快速连接卡片
        card = QFrame()
        card.setObjectName("card")
        grid = QGridLayout(card)
        grid.setContentsMargins(12, 12, 12, 12)
        grid.setSpacing(8)
        self.quick_protocol = QComboBox()
        self.quick_protocol.addItems(["SSH 终端", "SSL 加密通道"])
        self.quick_host = QLineEdit()
        self.quick_host.setPlaceholderText("主机地址")
        self.quick_port = QSpinBox()
        self.quick_port.setRange(1, 65535)
        self.quick_port.setValue(22)
        self.quick_user = QLineEdit()
        self.quick_user.setPlaceholderText("用户名 (SSH)")
        quick_btn = QPushButton("连接")
        grid.addWidget(self.quick_protocol, 0, 0, 1, 2)
        grid.addWidget(self.quick_host, 1, 0, 1, 2)
        grid.addWidget(self.quick_port, 2, 0)
        grid.addWidget(self.quick_user, 2, 1)
        grid.addWidget(quick_btn, 3, 0, 1, 2)
        v.addWidget(card)

        sessions_label = QLabel("已保存的会话")
        sessions_label.setObjectName("sidebarSectionLabel")
        v.addWidget(sessions_label)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索会话（名称 / 主机 / 分组）")
        v.addWidget(self.search_edit)
        self.session_tree = QTreeWidget()
        self.session_tree.setHeaderHidden(True)
        self.session_tree.setRootIsDecorated(True)
        self.session_tree.setMinimumHeight(120)
        v.addWidget(self.session_tree, 1)

        new_btn = QPushButton("新建连接")
        new_btn.setObjectName("secondary")
        v.addWidget(new_btn)

        actions_row = QHBoxLayout()
        self.connect_btn = QPushButton("连接")
        self.sftp_btn = QPushButton("SFTP")
        self.edit_btn = QPushButton("编辑")
        self.del_btn = QPushButton("删除")
        for b in (self.connect_btn, self.sftp_btn, self.edit_btn, self.del_btn):
            b.setObjectName("secondary")
            actions_row.addWidget(b)
        v.addLayout(actions_row)

        # ---- 工作区 ----
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._build_welcome(), "欢迎")

        h.addWidget(sidebar)
        h.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

        # 信号
        quick_btn.clicked.connect(self._quick_connect)
        self.quick_protocol.currentIndexChanged.connect(self._on_quick_protocol)
        new_btn.clicked.connect(self._new_session)
        self.connect_btn.clicked.connect(self._connect_selected)
        self.sftp_btn.clicked.connect(self._open_sftp_selected)
        self.edit_btn.clicked.connect(self._edit_selected)
        self.del_btn.clicked.connect(self._delete_selected)
        self.session_tree.itemDoubleClicked.connect(self._on_tree_double_clicked)
        self.session_tree.itemSelectionChanged.connect(self._update_buttons)
        self.search_edit.textChanged.connect(self._on_search_changed)

    def _build_welcome(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.addStretch(2)
        big = QLabel("SSLTool")
        big.setObjectName("welcomeTitle")
        big.setAlignment(Qt.AlignCenter)
        sub = QLabel("一个扁平的 SSL · SSH · SFTP 连接客户端")
        sub.setObjectName("welcomeSub")
        sub.setAlignment(Qt.AlignCenter)
        feats = QLabel(
            "• SSH 终端 —— 密码或私钥登录，ANSI 彩色输出\n"
            "• SSL 加密通道 —— 原始 TLS 控制台，显示证书详情\n"
            "• SFTP 文件管理 —— 拖拽传输 / 批量下载 / 断点续传 / 只读模式\n"
            "• 终端体验 —— 会话日志 / Ctrl+F 搜索 / URL 点击 / 括号粘贴 / 备用屏幕\n"
            "• 连接能力 —— SSH 跳板机 / 会话分组与搜索 / 导入导出")
        feats.setObjectName("welcomeFeats")
        feats.setAlignment(Qt.AlignCenter)
        hint = QLabel("在左侧输入主机地址开始连接，或选择一个已保存的会话。")
        hint.setObjectName("hint")
        hint.setAlignment(Qt.AlignCenter)
        v.addWidget(big)
        v.addSpacing(8)
        v.addWidget(sub)
        v.addSpacing(24)
        v.addWidget(feats)
        v.addSpacing(24)
        v.addWidget(hint)
        v.addStretch(3)
        return w

    # ---------------- 侧栏逻辑 ----------------

    def _on_quick_protocol(self, idx):
        is_ssh = idx == 0
        self.quick_user.setEnabled(is_ssh)
        if is_ssh and self.quick_port.value() == 443:
            self.quick_port.setValue(22)
        elif not is_ssh and self.quick_port.value() == 22:
            self.quick_port.setValue(443)

    def _quick_connect(self):
        host = self.quick_host.text().strip()
        if not host:
            QMessageBox.warning(self, "SSLTool", "请输入主机地址")
            return
        proto = "ssh" if self.quick_protocol.currentIndex() == 0 else "ssl"
        s = config.make_session(
            protocol=proto, host=host, port=self.quick_port.value(),
            username=self.quick_user.text().strip(), name=host)
        self._open_console(s)

    def _selected_session(self):
        item = self.session_tree.currentItem()
        if item is None:
            return None
        return item.data(0, Qt.UserRole)

    def _on_tree_double_clicked(self, item, col):
        if item.data(0, Qt.UserRole):
            self._open_console(item.data(0, Qt.UserRole))

    def _on_search_changed(self, text):
        self._refresh_list(text.strip().lower())

    def _update_buttons(self):
        has = self._selected_session() is not None
        for b in (self.connect_btn, self.sftp_btn, self.edit_btn, self.del_btn):
            b.setEnabled(has)

    def _refresh_list(self, query=""):
        tree = self.session_tree
        tree.clear()
        groups = {}
        for s in self.sessions:
            name = s.get("name") or s.get("host") or "未命名"
            if query and query not in name.lower() \
                    and query not in str(s.get("host", "")).lower() \
                    and query not in str(s.get("group", "")).lower() \
                    and query not in str(s.get("username", "")).lower():
                continue
            gname = s.get("group") or "未分组"
            groups.setdefault(gname, []).append(s)
        for gname in sorted(groups):
            gitem = QTreeWidgetItem()
            gitem.setText(0, f"{gname} ({len(groups[gname])})")
            gitem.setFlags(gitem.flags() & ~Qt.ItemIsSelectable)
            tree.addTopLevelItem(gitem)
            for s in groups[gname]:
                item = QTreeWidgetItem()
                item.setText(0, s.get("name") or s.get("host") or "未命名")
                proto = "SSL" if s.get("protocol") == "ssl" else "SSH"
                item.setToolTip(0, f"{proto} {s.get('username','')}@{s.get('host','')}:{s.get('port')}")
                item.setData(0, Qt.UserRole, s)
                gitem.addChild(item)
        tree.expandAll()
        self._update_buttons()

    def _new_session(self):
        dlg = ConnectDialog(self)
        if dlg.exec_() == QDialog.Accepted:
            s = dlg.get_session()
            self.sessions.append(s)
            config.save_sessions(self.sessions)
            self._refresh_list(self.search_edit.text().strip().lower())

    # ---------------- 导入 / 导出 ----------------

    def _import_sessions(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入会话", "",
                                              "SSLTool 会话文件 (*.json);;所有文件 (*)")
        if not path:
            return
        import json
        try:
            with open(path, "r", encoding="utf-8") as f:
                incoming = json.load(f)
            merged, added, updated = config.merge_sessions(self.sessions, incoming)
        except ValueError as e:
            QMessageBox.warning(self, "导入失败", str(e))
            return
        except Exception as e:
            QMessageBox.warning(self, "导入失败", f"读取文件失败: {e}")
            return
        ret = QMessageBox.question(
            self, "导入会话",
            f"将新增 {added} 个会话，覆盖 {updated} 个同名会话。继续吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        self.sessions = merged
        config.save_sessions(self.sessions)
        self._refresh_list(self.search_edit.text().strip().lower())
        self.statusBar().showMessage(f"已导入 {added} 个会话（覆盖 {updated} 个）", 8000)

    def _export_sessions(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出会话", "ssltool-sessions.json",
                                              "SSLTool 会话文件 (*.json)")
        if not path:
            return
        try:
            n = config.export_sessions(self.sessions, path)
            self.statusBar().showMessage(f"已导出 {n} 个会话到 {path}", 8000)
        except Exception as e:
            QMessageBox.warning(self, "导出失败", str(e))

    def _connect_selected(self):
        s = self._selected_session()
        if s:
            self._open_console(s)

    def _open_sftp_selected(self):
        s = self._selected_session()
        if s:
            self._open_sftp(s)

    def _edit_selected(self):
        s = self._selected_session()
        if not s:
            return
        dlg = ConnectDialog(self, session=s)
        if dlg.exec_() == QDialog.Accepted:
            ns = dlg.get_session()
            self.sessions = [ns if x.get("id") == ns["id"] else x for x in self.sessions]
            config.save_sessions(self.sessions)
            if not ns.get("save_password"):
                config.delete_secret(f"session:{ns['id']}")
            if not ns.get("save_passphrase"):
                config.delete_secret(f"passphrase:{ns['id']}")
            self._refresh_list(self.search_edit.text().strip().lower())

    def _delete_selected(self):
        s = self._selected_session()
        if not s:
            return
        name = s.get("name") or s.get("host")
        ret = QMessageBox.question(self, "删除会话", f"确定删除会话 “{name}” 吗？",
                                   QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        self.sessions = [x for x in self.sessions if x.get("id") != s.get("id")]
        config.save_sessions(self.sessions)
        config.delete_secret(f"session:{s.get('id')}")
        config.delete_secret(f"passphrase:{s.get('id')}")
        self._refresh_list(self.search_edit.text().strip().lower())

    # ---------------- 打开标签页 ----------------

    def _open_console(self, session):
        params = self._gather_auth(session)
        if params is None:
            return
        name = session.get("name") or f"{params.get('username','')}@{params['host']}"
        tab = ConsoleTab(name, params)
        tab.tabClosed.connect(self._close_tab)
        idx = self.tabs.addTab(tab, name)
        self.tabs.setCurrentIndex(idx)
        tab.start()

    def _open_sftp(self, session):
        params = self._gather_auth(session)
        if params is None:
            return
        name = session.get("name") or f"{params.get('username','')}@{params['host']}"
        tab = SFTPTab(name, params)
        tab.tabClosed.connect(self._close_tab)
        idx = self.tabs.addTab(tab, name)
        self.tabs.setCurrentIndex(idx)
        tab.start()

    def _close_tab(self, tab):
        tab.shutdown()
        idx = self.tabs.indexOf(tab)
        if idx >= 0:
            self.tabs.removeTab(idx)
        tab.deleteLater()

    # ---------------- 凭据解析 ----------------

    def _gather_auth(self, s):
        """把会话字典解析为连接参数；需要时弹框询问密码 / 私钥口令。"""
        params = dict(
            protocol=s.get("protocol", "ssh"),
            host=s.get("host", ""),
            port=int(s.get("port") or 22),
            username=s.get("username", ""),
            password=None,
            key_path=s.get("key_path", ""),
            passphrase=None,
            verify=bool(s.get("ssl_verify", True)),
            client_cert=s.get("client_cert", ""),
            client_key=s.get("client_key", ""),
            forwards=list(s.get("forwards") or []),
            jump_host=s.get("jump_host", ""),
            jump_port=int(s.get("jump_port") or 22),
            jump_username=s.get("jump_username", ""),
            jump_auth=s.get("jump_auth", "password"),
            jump_key_path=s.get("jump_key_path", ""),
            jump_password=None,
        )
        if params["protocol"] != "ssh":
            return params
        sid = s.get("id")
        if s.get("auth") == "key":
            path = s.get("key_path", "")
            if not path or not os.path.isfile(path):
                QMessageBox.warning(self, "SSLTool", f"私钥文件不存在: {path}")
                return None
            if self._key_needs_passphrase(path):
                pp = config.get_secret(f"passphrase:{sid}") if sid else None
                if pp is None:
                    pp, ok = QInputDialog.getText(
                        self, "私钥口令", f"密钥 {path} 需要口令:",
                        QLineEdit.Password)
                    if not ok:
                        return None
                    if s.get("save_passphrase") and sid:
                        config.set_secret(f"passphrase:{sid}", pp)
                params["passphrase"] = pp or None
        else:
            pw = config.get_secret(f"session:{sid}") if (sid and s.get("save_password")) else None
            if pw is None:
                pw, ok = QInputDialog.getText(
                    self, "密码",
                    f"请输入 {params['username']}@{params['host']} 的密码:",
                    QLineEdit.Password)
                if not ok:
                    return None
                if s.get("save_password") and sid:
                    config.set_secret(f"session:{sid}", pw)
            params["password"] = pw
        if not self._gather_jump_auth(s, params):
            return None
        return params

    def _gather_jump_auth(self, s, params):
        """跳板机凭据在连接时询问，不保存。返回 False 表示用户取消。"""
        if not params.get("jump_host"):
            return True
        label = f"{params['jump_username']}@{params['jump_host']}:{params['jump_port']}"
        if s.get("jump_auth") == "key":
            path = s.get("jump_key_path", "")
            if not path or not os.path.isfile(path):
                QMessageBox.warning(self, "SSLTool", f"跳板机私钥文件不存在: {path}")
                return False
            pp, ok = QInputDialog.getText(
                self, "跳板机私钥口令", f"跳板机 {label} 的密钥需要口令:",
                QLineEdit.Password)
            if not ok:
                return False
            params["jump_password"] = pp or None
        else:
            pw, ok = QInputDialog.getText(
                self, "跳板机密码", f"请输入跳板机 {label} 的密码:",
                QLineEdit.Password)
            if not ok:
                return False
            params["jump_password"] = pw
        return True

    @staticmethod
    def _key_needs_passphrase(path):
        """用空口令尝试解析私钥，判断是否需要口令。"""
        classes = []
        for name in ("RSAKey", "Ed25519Key", "ECDSAKey", "DSSKey"):
            try:
                classes.append(getattr(paramiko, name))
            except AttributeError:
                pass
        for cls in classes:
            try:
                cls.from_private_key_file(path, None)
                return False
            except paramiko.PasswordRequiredException:
                return True
            except paramiko.SSHException:
                continue
            except Exception:
                continue
        return False

    # ---------------- 其它 ----------------

    def _about(self):
        QMessageBox.about(
            self, "关于 SSLTool",
            f"SSLTool v{APP_VERSION}\n\n"
            "扁平化的 SSL / SSH / SFTP 桌面客户端。\n\n"
            "技术栈: Python + PyQt5 + Paramiko\n"
            "凭据存储: 系统钥匙串 (keyring)")

    def closeEvent(self, e):
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if hasattr(w, "shutdown"):
                w.shutdown()
        super().closeEvent(e)
