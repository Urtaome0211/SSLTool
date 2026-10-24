"""SFTP 文件管理标签页：本地目录树 + 远程目录树 + 传输操作。"""
import os
import posixpath

from PyQt5.QtCore import QDir, QMimeData, Qt, pyqtSignal
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
                             QLineEdit, QPushButton, QSplitter, QTreeView,
                             QTreeWidget, QTreeWidgetItem, QHeaderView,
                             QFileSystemModel, QMessageBox, QInputDialog,
                             QFileDialog, QProgressDialog, QStyle, QMenu,
                             QApplication, QAbstractItemView)

from app.core.sftp_worker import SFTPWorker
from app.core.jump import JumpConnector
from app.ui.hostkey_dialog import (confirm_changed_host_key,
                                   confirm_unknown_host_key)


def _fmt_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return "0 B"


class _RemoteDropTree(QTreeWidget):
    """远程目录树：可拖出（自定义 mime），接受本地文件拖入上传。"""
    uploadRequested = pyqtSignal(list)  # 本地路径列表

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)

    def mimeData(self, items):
        m = QMimeData()
        paths = [it.data(0, Qt.UserRole) for it in items if it.data(0, Qt.UserRole)]
        m.setData("application/x-ssltool-remote", "\n".join(paths).encode("utf-8"))
        return m

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            e.ignore()

    def dropEvent(self, e):
        urls = e.mimeData().urls()
        paths = [u.toLocalFile() for u in urls if u.isLocalFile()]
        if paths:
            self.uploadRequested.emit(paths)
            e.acceptProposedAction()
        else:
            e.ignore()


class _LocalDropTree(QTreeView):
    """本地目录树：接受远程项目拖入 → 下载到对应文件夹。"""
    downloadRequested = pyqtSignal(str, list)  # target_dir, remote paths

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)

    def _accept_remote(self, e):
        if e.mimeData().hasFormat("application/x-ssltool-remote"):
            e.acceptProposedAction()
            return True
        e.ignore()
        return False

    def dragEnterEvent(self, e):
        self._accept_remote(e)

    def dragMoveEvent(self, e):
        self._accept_remote(e)

    def dropEvent(self, e):
        m = e.mimeData()
        if not m.hasFormat("application/x-ssltool-remote"):
            e.ignore()
            return
        raw = bytes(m.data("application/x-ssltool-remote")).decode("utf-8")
        paths = [p for p in raw.split("\n") if p]
        if not paths:
            e.ignore()
            return
        idx = self.indexAt(e.pos())
        model = self.model()
        if idx.isValid() and model is not None:
            fi = model.fileInfo(idx)
            target = fi.absoluteFilePath() if fi.isDir() else fi.absolutePath()
        else:
            target = model.rootPath() if model is not None else QDir.homePath()
        self.downloadRequested.emit(target, paths)
        e.acceptProposedAction()


class SFTPTab(QWidget):
    tabClosed = pyqtSignal(object)  # 关闭请求，携带自身

    def __init__(self, name: str, params: dict, parent=None):
        super().__init__(parent)
        self._params = params
        self.worker = None
        self._remote_path = "."
        self._connected = False
        self._progress = None
        self._pending_ops = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # ---- 头部 ----
        header = QFrame()
        header.setObjectName("card")
        h = QHBoxLayout(header)
        h.setContentsMargins(10, 6, 10, 6)
        name_label = QLabel(name)
        name_label.setStyleSheet("font-weight: 600; color: #303133;")
        self._path_edit = QLineEdit(".")
        self._path_edit.setPlaceholderText("远程路径")
        jump_btn = QPushButton("跳转")
        jump_btn.setObjectName("secondary")
        up_btn = QPushButton("上级")
        up_btn.setObjectName("secondary")
        refresh_btn = QPushButton("刷新")
        refresh_btn.setObjectName("secondary")
        self._status = QLabel("连接中…")
        self._status.setStyleSheet("color: #909399;")
        close_btn = QPushButton("关闭")
        close_btn.setObjectName("secondary")
        h.addWidget(name_label)
        h.addWidget(self._path_edit, 1)
        h.addWidget(jump_btn)
        h.addWidget(up_btn)
        h.addWidget(refresh_btn)
        h.addWidget(self._status)
        h.addWidget(close_btn)

        # ---- 工具栏 ----
        bar = QHBoxLayout()
        actions = [
            ("上传文件", self._upload_file),
            ("上传文件夹", self._upload_dir),
            ("下载", self._download),
            ("新建文件夹", self._mkdir),
            ("重命名", self._rename),
            ("删除", self._remove),
        ]
        self._action_btns = {}
        for text, slot in actions:
            btn = QPushButton(text)
            btn.setObjectName("secondary")
            btn.clicked.connect(slot)
            bar.addWidget(btn)
            self._action_btns[text] = btn
        bar.addStretch(1)

        # ---- 文件区 ----
        splitter = QSplitter(Qt.Horizontal)
        self._local_tree = self._build_local_tree()
        self._remote_tree = self._build_remote_tree()
        self._local_tree.downloadRequested.connect(self._drop_download)
        self._remote_tree.uploadRequested.connect(self._drop_upload)
        splitter.addWidget(self._local_tree)
        splitter.addWidget(self._remote_tree)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([420, 560])

        layout.addWidget(header)
        layout.addLayout(bar)
        layout.addWidget(splitter, 1)

        jump_btn.clicked.connect(self._jump)
        up_btn.clicked.connect(self._go_up)
        refresh_btn.clicked.connect(self._refresh)
        close_btn.clicked.connect(lambda: self.tabClosed.emit(self))

        # ---- 只读模式 ----
        self._readonly = bool(params.get("sftp_readonly"))
        if self._readonly:
            for text in ("上传文件", "上传文件夹", "新建文件夹", "重命名", "删除"):
                self._action_btns[text].setEnabled(False)
            self._remote_tree.setAcceptDrops(False)
            self._remote_tree.viewport().setAcceptDrops(False)
            self._remote_tree.setDragEnabled(False)
            self._remote_tree.setDragDropMode(QAbstractItemView.NoDragDrop)

        self._create_worker()

    # ---------------- 界面构件 ----------------

    def _build_local_tree(self):
        model = QFileSystemModel(self)
        model.setReadOnly(True)
        home = QDir.homePath()
        model.setRootPath(home)
        tree = _LocalDropTree()
        tree.setModel(model)
        tree.setRootIndex(model.index(home))
        tree.setColumnHidden(2, True)   # 类型
        tree.setColumnWidth(0, 200)
        tree.setColumnWidth(1, 80)
        tree.setSortingEnabled(True)
        return tree

    def _build_remote_tree(self):
        tree = _RemoteDropTree()
        tree.setColumnCount(3)
        tree.setHeaderLabels(["名称", "大小", "修改时间"])
        tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        tree.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        tree.setRootIsDecorated(False)
        tree.setAlternatingRowColors(True)
        tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        tree.setContextMenuPolicy(Qt.CustomContextMenu)
        tree.customContextMenuRequested.connect(self._on_remote_context_menu)
        return tree

    # ---------------- worker 生命周期 ----------------

    def _create_worker(self):
        p = self._params
        jump = None
        if p.get("jump_host"):
            jump = JumpConnector(
                p["jump_host"], p.get("jump_port") or 22,
                p.get("jump_username", ""), password=p.get("jump_password"),
                key_path=p.get("jump_key_path") or None)
        w = SFTPWorker(p["host"], int(p["port"]), p.get("username", ""),
                       password=p.get("password"), key_path=p.get("key_path") or None,
                       passphrase=p.get("passphrase"), jump=jump)
        w.connected.connect(self._on_connected)
        w.error.connect(self._on_error)
        w.disconnected.connect(self._on_disconnected)
        w.dirListed.connect(self._on_dir_listed)
        w.opDone.connect(self._on_op_done)
        w.opFailed.connect(self._on_op_failed)
        w.transferProgress.connect(self._on_progress)
        w.hostKeyUnknown.connect(self._on_host_key_unknown)
        w.hostKeyChanged.connect(self._on_host_key_changed)
        w.jumpKeyUnknown.connect(self._on_jump_key_unknown)
        w.jumpKeyChanged.connect(self._on_jump_key_changed)
        self.worker = w

    def start(self):
        self._set_status("连接中…", "#909399")
        self.worker.start()

    def shutdown(self):
        self._end_progress()
        if self.worker:
            w = self.worker
            self.worker = None
            w.stop()
            if not w.wait(1000):
                w.finished.connect(w.deleteLater)

    # ---------------- 信号处理 ----------------

    def _set_status(self, text, color):
        self._status.setText(text)
        self._status.setStyleSheet(f"color: {color};")

    def _on_connected(self):
        self._connected = True
        self._set_status("已连接", "#67c23a")
        self.worker.listdir(".")

    def _on_error(self, msg):
        self._end_progress()
        QMessageBox.critical(self, "SFTP 连接失败", msg)
        self.tabClosed.emit(self)

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

    def _on_jump_key_unknown(self, host, port, fp):
        self._set_status("确认跳板机密钥…", "#e6a23c")
        ok = confirm_unknown_host_key(self, host, int(port), fp, jump=True)
        if self.worker:
            self.worker.resolve_host_key(ok)
        if not ok:
            self._set_status("已取消：未确认跳板机密钥", "#f56c6c")

    def _on_jump_key_changed(self, host, port, fp, expected):
        self._set_status("跳板机密钥已变更！", "#f56c6c")
        ok = confirm_changed_host_key(self, host, int(port), fp, expected,
                                      jump=True)
        if self.worker:
            self.worker.resolve_host_key(ok)
        if not ok:
            self._set_status("已取消：跳板机密钥与记录不一致", "#f56c6c")

    def _on_disconnected(self, reason):
        self._connected = False
        self._set_status("连接已断开", "#909399")

    def _on_dir_listed(self, path, entries):
        self._remote_path = path
        self._path_edit.setText(path)
        tree = self._remote_tree
        tree.clear()
        style = self.style()
        for e in entries:
            item = QTreeWidgetItem()
            item.setText(0, e["name"])
            if e["is_dir"]:
                item.setIcon(0, style.standardIcon(QStyle.SP_DirIcon))
                item.setText(1, "-")
            else:
                item.setIcon(0, style.standardIcon(QStyle.SP_FileIcon))
                item.setText(1, _fmt_size(e["size"]))
            if e["mtime"]:
                item.setText(2, e["mtime"].strftime("%Y-%m-%d %H:%M"))
            item.setData(0, Qt.UserRole, posixpath.join(path, e["name"]))
            item.setData(0, Qt.UserRole + 1, e["is_dir"])
            tree.addTopLevelItem(item)
        self._set_status(f"{len(entries)} 个项目", "#909399")

    def _on_op_done(self, msg):
        self._pending_ops = max(0, self._pending_ops - 1)
        if self._pending_ops == 0:
            self._end_progress()
        self._set_status(msg, "#67c23a")
        if self.worker and self._connected:
            self.worker.listdir(self._remote_path)

    def _on_op_failed(self, msg):
        self._pending_ops = max(0, self._pending_ops - 1)
        if self._pending_ops == 0:
            self._end_progress()
        self._set_status("操作失败", "#f56c6c")
        QMessageBox.warning(self, "SFTP 操作失败", msg)

    def _on_progress(self, done, total):
        if self._progress:
            self._progress.setMaximum(total)
            self._progress.setValue(done)

    # ---------------- 操作 ----------------

    def _check_worker(self):
        if not self.worker or not self._connected:
            QMessageBox.warning(self, "SSLTool", "尚未连接到服务器")
            return False
        return True

    def _check_writable(self):
        if not self._check_worker():
            return False
        if self._readonly:
            QMessageBox.information(self, "只读模式", "该会话已开启 SFTP 只读模式，禁止上传 / 删除 / 修改。")
            return False
        return True

    def _confirm_resume(self, local_path):
        """本地文件已存在时询问处理方式。返回 ("resume", True) / ("fresh", True) /
        ("skip", False)。目录项无需询问（按合并遍历处理）。"""
        if not os.path.isfile(local_path):
            return ("fresh", True)
        box = QMessageBox(self)
        box.setWindowTitle("文件已存在")
        box.setText(f"本地已存在：{os.path.basename(local_path)}\n\n"
                    "「续传」将仅下载缺失的部分（断点续传）；\n"
                    "「重新下载」将覆盖现有文件。")
        resume_btn = box.addButton("续传", QMessageBox.AcceptRole)
        fresh_btn = box.addButton("重新下载", QMessageBox.DestructiveRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.exec_()
        clicked = box.clickedButton()
        if clicked is resume_btn:
            return ("resume", True)
        if clicked is fresh_btn:
            return ("fresh", True)
        return ("skip", False)

    def _begin_progress(self, text):
        if self.worker:
            self.worker.begin_transfer()
        self._progress = QProgressDialog(text, "取消", 0, 0, self)
        self._progress.setWindowTitle("传输中")
        self._progress.setWindowModality(Qt.WindowModal)
        self._progress.setAutoClose(False)
        self._progress.setAutoReset(False)
        self._progress.canceled.connect(self._on_cancel_transfer)
        self._progress.show()

    def _on_cancel_transfer(self):
        if self.worker:
            self.worker.cancel_transfer()
        if self._progress:
            self._progress.setLabelText("正在取消…")
            self._progress.setCancelButton(None)
            self._progress.show()

    def _end_progress(self):
        if self._progress:
            self._progress.close()
            self._progress = None

    def _upload_file(self):
        if not self._check_writable():
            return
        locals_, _ = QFileDialog.getOpenFileNames(self, "选择要上传的文件", "", "所有文件 (*)")
        if not locals_:
            return
        self._begin_progress("正在上传…")
        for local in locals_:
            remote = posixpath.join(self._remote_path, os.path.basename(local))
            self._pending_ops += 1
            self.worker.upload(local, remote)

    def _upload_dir(self):
        if not self._check_writable():
            return
        local = QFileDialog.getExistingDirectory(self, "选择要上传的文件夹")
        if not local:
            return
        remote = posixpath.join(self._remote_path, os.path.basename(local.rstrip(os.sep)))
        self._begin_progress("正在上传…")
        self._pending_ops += 1
        self.worker.upload(local, remote)

    def _download(self):
        if not self._check_worker():
            return
        items = self._remote_tree.selectedItems()
        if not items:
            QMessageBox.warning(self, "SSLTool", "请先在右侧选择要下载的文件或文件夹")
            return
        target_dir = QFileDialog.getExistingDirectory(self, "选择保存目录")
        if not target_dir:
            return
        self._begin_progress("正在下载…")
        for it in items:
            remote = it.data(0, Qt.UserRole)
            local = os.path.join(target_dir, it.text(0))
            mode, ok = ("fresh", True) if it.data(0, Qt.UserRole + 1) \
                else self._confirm_resume(local)
            if not ok:
                continue
            self._pending_ops += 1
            self.worker.download(remote, local, resume=(mode == "resume"))

    def _drop_upload(self, local_paths):
        if not self._check_writable():
            return
        if not local_paths:
            return
        self._begin_progress("正在上传…")
        for lp in local_paths:
            base = os.path.basename(lp.rstrip(os.sep)) or lp
            self._pending_ops += 1
            self.worker.upload(lp, posixpath.join(self._remote_path, base))

    def _drop_download(self, target_dir, remote_paths):
        if not self._check_worker():
            return
        if not remote_paths:
            return
        self._begin_progress("正在下载…")
        for rp in remote_paths:
            base = posixpath.basename(rp.rstrip("/")) or "download"
            local = os.path.join(target_dir, base)
            mode = "fresh"
            if os.path.isfile(local):
                mode, ok = self._confirm_resume(local)
                if not ok:
                    continue
            self._pending_ops += 1
            self.worker.download(rp, local, resume=(mode == "resume"))

    def _mkdir(self):
        if not self._check_writable():
            return
        name, ok = QInputDialog.getText(self, "新建文件夹", "文件夹名称:")
        if not ok or not name.strip():
            return
        self.worker.mkdir(posixpath.join(self._remote_path, name.strip()))

    def _rename(self):
        if not self._check_writable():
            return
        items = self._remote_tree.selectedItems()
        if len(items) != 1:
            QMessageBox.warning(self, "SSLTool", "请选择一个要重命名的项目")
            return
        it = items[0]
        old = it.data(0, Qt.UserRole)
        new, ok = QInputDialog.getText(self, "重命名", "新名称:", text=it.text(0))
        if not ok or not new.strip():
            return
        self.worker.rename(old, posixpath.join(posixpath.dirname(old), new.strip()))

    def _remove(self):
        if not self._check_writable():
            return
        items = self._remote_tree.selectedItems()
        if not items:
            QMessageBox.warning(self, "SSLTool", "请先选择要删除的项目")
            return
        names = "\n".join(f"  • {it.text(0)}" for it in items)
        ret = QMessageBox.question(
            self, "确认删除", f"确定删除以下项目吗？\n{names}\n\n此操作不可恢复！",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        for it in items:
            self.worker.remove(it.data(0, Qt.UserRole))

    # ---------------- 导航 ----------------

    def _jump(self):
        if not self._check_worker():
            return
        text = self._path_edit.text().strip()
        if text:
            self.worker.listdir(text)

    def _go_up(self):
        if not self._check_worker():
            return
        self.worker.listdir(posixpath.dirname(self._remote_path) or "/")

    def _refresh(self):
        if not self._check_worker():
            return
        self.worker.listdir(self._remote_path)

    def _on_item_double_clicked(self, item, col):
        if not self._check_worker():
            return
        if item.data(0, Qt.UserRole + 1):
            self.worker.listdir(item.data(0, Qt.UserRole))

    # ---------------- 远程树右键菜单 ----------------

    def _on_remote_context_menu(self, pos):
        tree = self._remote_tree
        item = tree.itemAt(pos)
        if item is not None:
            item.setSelected(True)
        menu = QMenu(self)
        act_dl = menu.addAction("下载到…")
        act_copy = menu.addAction("复制路径")
        menu.addSeparator()
        act_rn = menu.addAction("重命名")
        act_rm = menu.addAction("删除")
        if self._readonly or item is None:
            act_rn.setEnabled(False)
            act_rm.setEnabled(False)
        if item is None:
            act_dl.setEnabled(False)
            act_copy.setEnabled(False)
        act = menu.exec_(tree.viewport().mapToGlobal(pos))
        if act is act_dl:
            self._download()
        elif act is act_copy:
            paths = [it.data(0, Qt.UserRole)
                     for it in tree.selectedItems()
                     if it.data(0, Qt.UserRole)]
            if paths:
                QApplication.clipboard().setText("\n".join(paths))
                self._set_status(f"已复制 {len(paths)} 个路径", "#67c23a")
        elif act is act_rn:
            self._rename()
        elif act is act_rm:
            self._remove()
