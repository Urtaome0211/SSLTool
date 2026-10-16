"""SFTP 文件管理标签页：本地目录树 + 远程目录树 + 传输操作。"""
import os
import posixpath

from PyQt5.QtCore import QDir, QMimeData, Qt, pyqtSignal
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
                             QLineEdit, QPushButton, QSplitter, QTreeView,
                             QTreeWidget, QTreeWidgetItem, QHeaderView,
                             QFileSystemModel, QMessageBox, QInputDialog,
                             QFileDialog, QProgressDialog, QStyle,
                             QAbstractItemView)

from app.core.sftp_worker import SFTPWorker


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
        for text, slot in actions:
            btn = QPushButton(text)
            btn.setObjectName("secondary")
            btn.clicked.connect(slot)
            bar.addWidget(btn)
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
        return tree

    # ---------------- worker 生命周期 ----------------

    def _create_worker(self):
        p = self._params
        w = SFTPWorker(p["host"], int(p["port"]), p.get("username", ""),
                       password=p.get("password"), key_path=p.get("key_path") or None,
                       passphrase=p.get("passphrase"))
        w.connected.connect(self._on_connected)
        w.error.connect(self._on_error)
        w.disconnected.connect(self._on_disconnected)
        w.dirListed.connect(self._on_dir_listed)
        w.opDone.connect(self._on_op_done)
        w.opFailed.connect(self._on_op_failed)
        w.transferProgress.connect(self._on_progress)
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
        if not self._check_worker():
            return
        local, _ = QFileDialog.getOpenFileName(self, "选择要上传的文件", "", "所有文件 (*)")
        if not local:
            return
        remote = posixpath.join(self._remote_path, os.path.basename(local))
        self._begin_progress("正在上传…")
        self._pending_ops += 1
        self.worker.upload(local, remote)

    def _upload_dir(self):
        if not self._check_worker():
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
            self._pending_ops += 1
            self.worker.download(it.data(0, Qt.UserRole),
                                 os.path.join(target_dir, it.text(0)))

    def _drop_upload(self, local_paths):
        if not self._check_worker():
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
            self._pending_ops += 1
            self.worker.download(rp, os.path.join(target_dir, base))

    def _mkdir(self):
        if not self._check_worker():
            return
        name, ok = QInputDialog.getText(self, "新建文件夹", "文件夹名称:")
        if not ok or not name.strip():
            return
        self.worker.mkdir(posixpath.join(self._remote_path, name.strip()))

    def _rename(self):
        if not self._check_worker():
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
        if not self._check_worker():
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
