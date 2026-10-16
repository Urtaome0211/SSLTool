"""SFTP 后台线程：连接、目录浏览与文件传输（命令队列串行执行）。"""
import os
import posixpath
import queue
import stat
from datetime import datetime

import paramiko
from PyQt5.QtCore import QThread, pyqtSignal


class _TransferCancelled(Exception):
    """传输被用户取消（内部控制流）。"""


class SFTPWorker(QThread):
    connected = pyqtSignal()
    error = pyqtSignal(str)
    disconnected = pyqtSignal(str)
    dirListed = pyqtSignal(str, list)        # path, [entry dict]
    opDone = pyqtSignal(str)                 # 操作完成描述
    opFailed = pyqtSignal(str)               # 操作失败原因
    transferProgress = pyqtSignal(int, int)  # done, total

    def __init__(self, host, port, username, password=None, key_path=None,
                 passphrase=None, parent=None):
        super().__init__(parent)
        self.host = host
        self.port = int(port)
        self.username = username
        self.password = password
        self.key_path = key_path
        self.passphrase = passphrase
        self._queue = queue.Queue()
        self._running = True
        self._client = None
        self._sftp = None
        self._connected = False
        self._cancel = False

    # ---------------- UI 线程命令接口 ----------------
    def listdir(self, path: str) -> None:
        self._queue.put(("listdir", path))

    def upload(self, local: str, remote: str) -> None:
        self._queue.put(("upload", (local, remote)))

    def download(self, remote: str, local: str) -> None:
        self._queue.put(("download", (remote, local)))

    def remove(self, path: str) -> None:
        self._queue.put(("remove", path))

    def mkdir(self, path: str) -> None:
        self._queue.put(("mkdir", path))

    def rename(self, old: str, new: str) -> None:
        self._queue.put(("rename", (old, new)))

    def stop(self) -> None:
        self._running = False
        self._queue.put(("stop", None))

    def begin_transfer(self) -> None:
        """新一轮传输开始前调用：清除旧的取消标记。"""
        self._cancel = False

    def cancel_transfer(self) -> None:
        """取消当前传输，并清空队列中尚未开始的传输任务。"""
        self._cancel = True
        self._queue.put(("purge", None))

    def _check_cancel(self) -> None:
        if self._cancel:
            raise _TransferCancelled()

    def _purge_transfers(self) -> None:
        """丢弃队列里未开始的 upload/download 任务，其余保留。"""
        keep = []
        while True:
            try:
                k, p = self._queue.get_nowait()
            except queue.Empty:
                break
            if k not in ("upload", "download"):
                keep.append((k, p))
        for item in keep:
            self._queue.put(item)

    # ---------------- 线程主循环 ----------------
    def run(self):
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
            sftp = client.open_sftp()
            self._client = client
            self._sftp = sftp
            self._connected = True
            self.connected.emit()
            while self._running:
                try:
                    kind, payload = self._queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if kind == "stop":
                    break
                if kind == "purge":
                    self._purge_transfers()
                    continue
                try:
                    self._dispatch(kind, payload)
                except _TransferCancelled:
                    self.opDone.emit("已取消")
                except Exception as e:
                    self.opFailed.emit(str(e))
        except Exception as e:
            self.error.emit(f"连接失败: {e}")
        finally:
            self._running = False
            try:
                if self._sftp is not None:
                    self._sftp.close()
            except Exception:
                pass
            try:
                if self._client is not None:
                    self._client.close()
            except Exception:
                pass
            if self._connected:
                self.disconnected.emit("")

    # ---------------- 操作实现 ----------------
    def _dispatch(self, kind, payload):
        if kind == "listdir":
            self._do_listdir(payload)
        elif kind == "upload":
            self._do_upload(*payload)
        elif kind == "download":
            self._do_download(*payload)
        elif kind == "remove":
            self._do_remove(payload)
        elif kind == "mkdir":
            self._sftp.mkdir(payload)
            self.opDone.emit(f"已创建文件夹 {payload}")
        elif kind == "rename":
            old, new = payload
            self._sftp.rename(old, new)
            self.opDone.emit(f"已重命名为 {posixpath.basename(new)}")

    def _do_listdir(self, path):
        path = path or "."
        attrs = self._sftp.listdir_attr(path)
        entries = []
        for a in attrs:
            entries.append({
                "name": a.filename,
                "size": a.st_size or 0,
                "mtime": datetime.fromtimestamp(a.st_mtime) if a.st_mtime else None,
                "is_dir": stat.S_ISDIR(a.st_mode),
                "mode": a.st_mode,
            })
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        self.dirListed.emit(path, entries)

    def _do_upload(self, local, remote):
        if os.path.isdir(local):
            total = self._local_size(local)
            state = {"done": 0}

            def cb(d, t):
                self._check_cancel()
                state["done"] += d
                self.transferProgress.emit(state["done"], total)

            try:
                self._sftp.mkdir(remote)
            except IOError:
                pass
            for root, dirs, files in os.walk(local):
                self._check_cancel()
                rel = os.path.relpath(root, local)
                rdir = remote if rel == "." else posixpath.join(remote, *rel.split(os.sep))
                for d in dirs:
                    try:
                        self._sftp.mkdir(posixpath.join(rdir, d))
                    except IOError:
                        pass
                for f in files:
                    self._check_cancel()
                    self._sftp.put(os.path.join(root, f), posixpath.join(rdir, f), callback=cb)
            self.opDone.emit(f"上传完成: {os.path.basename(local.rstrip(os.sep)) or local}")
        else:
            size = os.path.getsize(local)

            def cb(d, t):
                self._check_cancel()
                self.transferProgress.emit(d, size)

            self._sftp.put(local, remote, callback=cb)
            self.opDone.emit(f"上传完成: {os.path.basename(local)}")

    def _do_download(self, remote, local):
        st = self._sftp.stat(remote)
        if stat.S_ISDIR(st.st_mode):
            total = self._remote_size(remote)
            state = {"done": 0}

            def cb(d, t):
                self._check_cancel()
                state["done"] += d
                self.transferProgress.emit(state["done"], total)

            os.makedirs(local, exist_ok=True)
            for dirpath, dirs, files in self._walk_remote(remote):
                self._check_cancel()
                rel = posixpath.relpath(dirpath, remote)
                ldir = local if rel == "." else os.path.join(local, *rel.split("/"))
                for d in dirs:
                    os.makedirs(os.path.join(ldir, d), exist_ok=True)
                for f in files:
                    self._check_cancel()
                    self._sftp.get(posixpath.join(dirpath, f), os.path.join(ldir, f),
                                   callback=cb)
            self.opDone.emit(f"下载完成: {os.path.basename(local.rstrip(os.sep)) or local}")
        else:
            size = st.st_size or 0

            def cb(d, t):
                self._check_cancel()
                self.transferProgress.emit(d, size)

            os.makedirs(os.path.dirname(local) or ".", exist_ok=True)
            self._sftp.get(remote, local, callback=cb)
            self.opDone.emit(f"下载完成: {posixpath.basename(remote)}")

    def _do_remove(self, path):
        st = self._sftp.stat(path)
        if stat.S_ISDIR(st.st_mode):
            self._rm_rf(path)
        else:
            self._sftp.remove(path)
        self.opDone.emit(f"已删除 {posixpath.basename(path.rstrip('/'))}")

    # ---------------- 工具 ----------------
    def _walk_remote(self, path):
        """前序遍历远程目录树，产出 (dirpath, dirs, files)。"""
        dirs, files = [], []
        for a in self._sftp.listdir_attr(path):
            (dirs if stat.S_ISDIR(a.st_mode) else files).append(a.filename)
        yield path, dirs, files
        for d in dirs:
            yield from self._walk_remote(posixpath.join(path, d))

    def _remote_size(self, path):
        st = self._sftp.stat(path)
        if not stat.S_ISDIR(st.st_mode):
            return st.st_size or 0
        total = 0
        for p, dirs, files in self._walk_remote(path):
            for f in files:
                try:
                    total += self._sftp.stat(posixpath.join(p, f)).st_size or 0
                except IOError:
                    pass
        return total

    def _rm_rf(self, path):
        walk = list(self._walk_remote(path))
        for dirpath, dirs, files in walk:
            for f in files:
                self._sftp.remove(posixpath.join(dirpath, f))
        for dirpath, dirs, files in reversed(walk):
            for d in dirs:
                self._sftp.rmdir(posixpath.join(dirpath, d))
        self._sftp.rmdir(path)

    @staticmethod
    def _local_size(path):
        total = 0
        for root, dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return total
