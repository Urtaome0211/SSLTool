"""SSLTool 主窗口：工程 / 侧栏会话管理 + 标签页工作区。"""
import os
from datetime import datetime

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QGridLayout, QLabel, QFrame,
                             QLineEdit, QComboBox, QSpinBox, QPushButton,
                             QTreeWidget, QTreeWidgetItem, QTabWidget,
                             QMessageBox, QInputDialog, QAction, QDialog,
                             QFileDialog, QMenu, QShortcut)

import paramiko

from app import config
from app import theme
from app.ui.connect_dialog import ConnectDialog
from app.ui.terminal_tab import ConsoleTab
from app.ui.sftp_tab import SFTPTab

APP_VERSION = "3.1.0"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"SSLTool v{APP_VERSION}")
        self.resize(1100, 700)
        self.sessions = config.load_sessions()
        self.settings = config.load_settings()
        self.projects = config.load_projects()
        self._current_project = self._resolve_current_project()
        self._tab_names = {}  # 标签页部件 -> 基础标题（状态点之前的名字）
        if self.settings.get("dark"):
            theme.apply_theme(QApplication.instance(), dark=True)

        self._build_menu()
        self._build_central()
        self._refresh_list()
        if self.settings.get("restore_workspace"):
            self._restore_workspace(on_startup=True)
        self.statusBar().showMessage("就绪", 5000)

    def _resolve_current_project(self):
        pid = self.settings.get("current_project")
        ids = [p["id"] for p in self.projects]
        if pid in ids:
            return pid
        return ids[0] if ids else config.DEFAULT_PROJECT_ID

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

        project_menu = self.menuBar().addMenu("工程")
        new_proj_act = QAction("新建工程…", self)
        new_proj_act.triggered.connect(self._new_project)
        project_menu.addAction(new_proj_act)
        rename_proj_act = QAction("重命名当前工程…", self)
        rename_proj_act.triggered.connect(self._rename_project)
        project_menu.addAction(rename_proj_act)
        del_proj_act = QAction("删除当前工程…", self)
        del_proj_act.triggered.connect(self._delete_project)
        project_menu.addAction(del_proj_act)
        project_menu.addSeparator()
        restore_act = QAction("恢复当前工程工作区", self)
        restore_act.setShortcut("Ctrl+R")
        restore_act.triggered.connect(self._restore_workspace)
        project_menu.addAction(restore_act)
        self.restore_ws_act = QAction("启动时自动恢复上次工作区", self)
        self.restore_ws_act.setCheckable(True)
        self.restore_ws_act.setChecked(bool(self.settings.get("restore_workspace")))
        self.restore_ws_act.toggled.connect(self._toggle_restore_ws)
        project_menu.addAction(self.restore_ws_act)

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

        # 工程切换器
        proj_row = QHBoxLayout()
        proj_row.setSpacing(6)
        self.project_combo = QComboBox()
        self.project_combo.setToolTip("当前工程：会话与标签页工作区按工程隔离")
        self._sync_project_combo()
        add_proj_btn = QPushButton("+")
        add_proj_btn.setObjectName("secondary")
        add_proj_btn.setFixedWidth(34)
        add_proj_btn.setToolTip("新建工程")
        add_proj_btn.clicked.connect(self._new_project)
        proj_row.addWidget(self.project_combo, 1)
        proj_row.addWidget(add_proj_btn)
        v.addLayout(proj_row)

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
        welcome = self._build_welcome()
        welcome.setProperty("isWelcome", True)
        self.tabs.addTab(welcome, "欢迎")

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
        self.project_combo.currentIndexChanged.connect(self._on_project_changed)
        self._install_tab_shortcuts()
        tab_bar = self.tabs.tabBar()
        tab_bar.setContextMenuPolicy(Qt.CustomContextMenu)
        tab_bar.customContextMenuRequested.connect(self._on_tab_context_menu)

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
            "• 连接能力 —— SSH 跳板机 / 会话分组与搜索 / 导入导出\n"
            "• 会话与工程 —— 多工程隔离 / 工作区快照与恢复 / 最近连接 / 标签页快捷键")
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
        current = [s for s in self.sessions
                   if (s.get("project") or config.DEFAULT_PROJECT_ID) == self._current_project]
        groups = {}
        for s in current:
            name = s.get("name") or s.get("host") or "未命名"
            if query and query not in name.lower() \
                    and query not in str(s.get("host", "")).lower() \
                    and query not in str(s.get("group", "")).lower() \
                    and query not in str(s.get("username", "")).lower():
                continue
            gname = s.get("group") or "未分组"
            groups.setdefault(gname, []).append(s)
        if not query:
            recent = sorted([s for s in current if s.get("last_used")],
                            key=lambda x: str(x.get("last_used", "")), reverse=True)[:5]
            if recent:
                ritem = self._add_group_item(tree, "最近连接")
                for s in recent:
                    self._add_session_item(ritem, s)
        for gname in sorted(groups):
            gitem = self._add_group_item(tree, f"{gname} ({len(groups[gname])})")
            for s in sorted(groups[gname], key=lambda x: (x.get("name") or x.get("host") or "").lower()):
                self._add_session_item(gitem, s)
        tree.expandAll()
        self._update_buttons()

    @staticmethod
    def _add_group_item(tree, title):
        gitem = QTreeWidgetItem()
        gitem.setText(0, title)
        gitem.setFlags(gitem.flags() & ~Qt.ItemIsSelectable)
        tree.addTopLevelItem(gitem)
        return gitem

    @staticmethod
    def _add_session_item(parent_item, s):
        item = QTreeWidgetItem()
        item.setText(0, s.get("name") or s.get("host") or "未命名")
        proto = "SSL" if s.get("protocol") == "ssl" else "SSH"
        item.setToolTip(0, f"{proto} {s.get('username','')}@{s.get('host','')}:{s.get('port')}")
        item.setData(0, Qt.UserRole, s)
        parent_item.addChild(item)

    # ---------------- 工程 ----------------

    def _sync_project_combo(self):
        self.project_combo.blockSignals(True)
        self.project_combo.clear()
        for p in self.projects:
            self.project_combo.addItem(p["name"], p["id"])
        idx = self.project_combo.findData(self._current_project)
        self.project_combo.setCurrentIndex(max(idx, 0))
        self.project_combo.blockSignals(False)

    def _project_name(self, pid=None):
        pid = pid or self._current_project
        for p in self.projects:
            if p["id"] == pid:
                return p["name"]
        return "默认工程"

    def _on_project_changed(self, idx):
        pid = self.project_combo.itemData(idx)
        if not pid or pid == self._current_project:
            return
        self._save_workspace()
        self._current_project = pid
        self.settings["current_project"] = pid
        config.save_settings(self.settings)
        self._refresh_list(self.search_edit.text().strip().lower())
        count = len(self.settings.get("workspaces", {}).get(pid) or [])
        if count:
            self.statusBar().showMessage(
                f"已切换到工程「{self._project_name()}」；有 {count} 个已保存标签页，"
                "Ctrl+R 可恢复工作区", 8000)
        else:
            self.statusBar().showMessage(f"已切换到工程「{self._project_name()}」", 5000)

    def _new_project(self):
        name, ok = QInputDialog.getText(self, "新建工程", "工程名称:")
        if not ok or not name.strip():
            return
        name = name.strip()
        if any(p["name"] == name for p in self.projects):
            QMessageBox.warning(self, "SSLTool", "已存在同名工程")
            return
        self._save_workspace()
        pid = config.new_project_id()
        self.projects.append({"id": pid, "name": name})
        config.save_projects(self.projects)
        self._current_project = pid
        self.settings["current_project"] = pid
        config.save_settings(self.settings)
        self._sync_project_combo()
        self._refresh_list(self.search_edit.text().strip().lower())
        self.statusBar().showMessage(f"已创建工程「{name}」", 5000)

    def _rename_project(self):
        pid = self._current_project
        name, ok = QInputDialog.getText(self, "重命名工程", "工程名称:",
                                        text=self._project_name())
        if not ok or not name.strip():
            return
        name = name.strip()
        for p in self.projects:
            if p["id"] == pid:
                p["name"] = name
        config.save_projects(self.projects)
        self._sync_project_combo()
        self.statusBar().showMessage(f"工程已重命名为「{name}」", 5000)

    def _delete_project(self):
        pid = self._current_project
        if len(self.projects) <= 1:
            QMessageBox.information(self, "SSLTool", "至少需要保留一个工程")
            return
        name = self._project_name()
        ret = QMessageBox.question(
            self, "删除工程",
            f"删除工程「{name}」？\n其下的会话不会被删除，将移回默认工程。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        self.projects = [p for p in self.projects if p["id"] != pid]
        config.save_projects(self.projects)
        fallback = config.DEFAULT_PROJECT_ID
        if not any(p["id"] == fallback for p in self.projects):
            fallback = self.projects[0]["id"]
        for s in self.sessions:
            if (s.get("project") or config.DEFAULT_PROJECT_ID) == pid:
                s["project"] = fallback
        config.save_sessions(self.sessions)
        self.settings.get("workspaces", {}).pop(pid, None)
        self._current_project = fallback
        self.settings["current_project"] = fallback
        config.save_settings(self.settings)
        self._sync_project_combo()
        self._refresh_list(self.search_edit.text().strip().lower())
        self.statusBar().showMessage(
            f"已删除工程「{name}」，其会话已移回「{self._project_name()}」", 8000)

    def _toggle_restore_ws(self, checked):
        self.settings["restore_workspace"] = bool(checked)
        config.save_settings(self.settings)

    def _new_session(self):
        dlg = ConnectDialog(self)
        idx = dlg.project_combo.findData(self._current_project)
        if idx >= 0:
            dlg.project_combo.setCurrentIndex(idx)
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

    def _touch_last_used(self, session):
        if session.get("id"):
            session["last_used"] = datetime.now().isoformat(timespec="seconds")
            config.save_sessions(self.sessions)

    def _open_console(self, session):
        params = self._gather_auth(session)
        if params is None:
            return False
        name = session.get("name") or f"{params.get('username','')}@{params['host']}"
        tab = ConsoleTab(name, params)
        tab._ws_kind = "console"
        tab._ws_sid = session.get("id")
        self._tab_names[tab] = name
        tab.tabClosed.connect(self._close_tab)
        tab.stateChanged.connect(lambda st, t=tab: self._update_tab_state(t, st))
        idx = self.tabs.addTab(tab, name)
        self.tabs.setCurrentIndex(idx)
        tab.start()
        self._touch_last_used(session)
        self._refresh_list(self.search_edit.text().strip().lower())
        return True

    def _open_sftp(self, session):
        params = self._gather_auth(session)
        if params is None:
            return False
        name = session.get("name") or f"{params.get('username','')}@{params['host']}"
        tab = SFTPTab(name, params)
        tab._ws_kind = "sftp"
        tab._ws_sid = session.get("id")
        self._tab_names[tab] = name
        tab.tabClosed.connect(self._close_tab)
        idx = self.tabs.addTab(tab, name)
        self.tabs.setCurrentIndex(idx)
        tab.start()
        self._touch_last_used(session)
        self._refresh_list(self.search_edit.text().strip().lower())
        return True

    def _update_tab_state(self, tab, state):
        idx = self.tabs.indexOf(tab)
        if idx < 0:
            return
        marker = {"connecting": "◌", "connected": "●",
                  "error": "○", "disconnected": "○"}.get(state, "")
        base = self._tab_names.get(tab, self.tabs.tabText(idx))
        self.tabs.setTabText(idx, f"{marker} {base}".strip())

    def _close_tab(self, tab):
        self._tab_names.pop(tab, None)
        tab.shutdown()
        idx = self.tabs.indexOf(tab)
        if idx >= 0:
            self.tabs.removeTab(idx)
        tab.deleteLater()

    # ---------------- 工作区（标签页快照） ----------------

    def _snapshot_workspace(self):
        entries = []
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            kind = getattr(w, "_ws_kind", None)
            sid = getattr(w, "_ws_sid", None)
            if kind and sid:
                entries.append([kind, sid])
        return entries

    def _save_workspace(self):
        ws = self.settings.setdefault("workspaces", {})
        entries = self._snapshot_workspace()
        if entries:
            ws[self._current_project] = entries
        else:
            ws.pop(self._current_project, None)
        config.save_settings(self.settings)

    def _restore_workspace(self, on_startup=False):
        entries = self.settings.get("workspaces", {}).get(self._current_project) or []
        by_id = {s.get("id"): s
                 for s in self.sessions
                 if (s.get("project") or config.DEFAULT_PROJECT_ID) == self._current_project}
        plans = [(k, by_id.get(sid)) for k, sid in entries if by_id.get(sid)]
        if not plans:
            if not on_startup:
                self.statusBar().showMessage("当前工程没有已保存的工作区标签页", 5000)
            return
        if not on_startup:
            ret = QMessageBox.question(
                self, "恢复工作区",
                f"恢复工程「{self._project_name()}」的 {len(plans)} 个标签页？\n"
                "需要认证的标签页会弹出密码框。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if ret != QMessageBox.Yes:
                return
        opened = 0
        for kind, s in plans:
            if self._is_open(kind, s.get("id")):
                continue
            ok_open = self._open_sftp(s) if kind == "sftp" else self._open_console(s)
            if ok_open:
                opened += 1
        if not on_startup:
            self.statusBar().showMessage(f"已恢复 {opened} 个标签页", 5000)

    def _is_open(self, kind, sid):
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if getattr(w, "_ws_kind", None) == kind \
                    and getattr(w, "_ws_sid", None) == sid:
                return True
        return False

    # ---------------- 标签页操作 ----------------

    def _install_tab_shortcuts(self):
        QShortcut(QKeySequence("Ctrl+Tab"), self, activated=self._next_tab)
        QShortcut(QKeySequence("Ctrl+Shift+Tab"), self, activated=self._prev_tab)
        QShortcut(QKeySequence("Ctrl+W"), self, activated=self._close_current_tab)
        for i in range(1, 10):
            QShortcut(QKeySequence(f"Ctrl+{i}"), self,
                      activated=lambda i=i: self._focus_tab(i - 1))

    def _next_tab(self):
        self._cycle_tab(1)

    def _prev_tab(self):
        self._cycle_tab(-1)

    def _cycle_tab(self, delta):
        n = self.tabs.count()
        if n <= 1:
            return
        self.tabs.setCurrentIndex((self.tabs.currentIndex() + delta) % n)

    def _focus_tab(self, idx):
        if 0 <= idx < self.tabs.count():
            self.tabs.setCurrentIndex(idx)

    def _close_current_tab(self):
        self._close_tab_index(self.tabs.currentIndex())

    def _on_tab_context_menu(self, pos):
        bar = self.tabs.tabBar()
        idx = bar.tabAt(pos)
        if idx < 0:
            return
        w = self.tabs.widget(idx)
        if getattr(w, "isWelcome", False):
            return
        menu = QMenu(self)
        act_close = menu.addAction("关闭标签页")
        act_others = menu.addAction("关闭其它标签页")
        act_all = menu.addAction("关闭全部标签页")
        menu.addSeparator()
        act_reconnect = menu.addAction("重新连接")
        act_reconnect.setEnabled(
            isinstance(w, ConsoleTab) and getattr(w, "state", "") in ("disconnected", "error"))
        chosen = menu.exec_(bar.mapToGlobal(pos))
        if chosen is act_close:
            self._close_tab_index(idx)
        elif chosen is act_others:
            self._close_other_tabs(idx)
        elif chosen is act_all:
            self._close_all_tabs()
        elif chosen is act_reconnect:
            w._reconnect()

    def _close_tab_index(self, idx):
        if not (0 <= idx < self.tabs.count()):
            return
        w = self.tabs.widget(idx)
        if getattr(w, "isWelcome", False):
            return
        if hasattr(w, "tabClosed"):
            self._close_tab(w)

    def _close_other_tabs(self, keep_idx):
        keep = self.tabs.widget(keep_idx)
        for i in range(self.tabs.count() - 1, -1, -1):
            w = self.tabs.widget(i)
            if w is keep or getattr(w, "isWelcome", False):
                continue
            if hasattr(w, "tabClosed"):
                self._close_tab(w)

    def _close_all_tabs(self):
        for i in range(self.tabs.count() - 1, -1, -1):
            w = self.tabs.widget(i)
            if getattr(w, "isWelcome", False):
                continue
            if hasattr(w, "tabClosed"):
                self._close_tab(w)

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
        self._save_workspace()
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if hasattr(w, "shutdown"):
                w.shutdown()
        super().closeEvent(e)
