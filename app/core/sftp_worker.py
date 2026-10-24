"""SFTP 后台线程：连接、目录浏览与文件传输（命令队列串行执行）。"""
import os
import posixpath
import queue
import stat
from datetime import datetime

import paramiko
from PyQt5.QtCore import QThread, pyqtSignal

from app.core.hostkey import HostKeyGate, HostKeyProbeError, fingerprint_sha256


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
    hostKeyUnknown = pyqtSignal(str)         # 服务器指纹（首次连接）
    hostKeyChanged = pyqtSignal(str, str)    # 新指纹, 已记录指纹（疑似中间人）
    jumpKeyUnknown = pyqtSignal(str, int, str)      # 跳板机 host, port, 指纹
    jumpKeyChanged = pyqtSignal(str, int, str, str)  # 跳板机 host, port, 新指纹, 旧指纹

    def __init__(self, host, port, username, password=None, key_path=None,
                 passphrase=None, jump=None, parent=None):
        super().__init__(parent)
        self.host = host
        self.port = int(port)
        self.username = username
        self.password = password
        self.key_path = key_path
        self.passphrase = passphrase
        self._jump = jump          # JumpConnector 或 None
        self._queue = queue.Queue()
        self._running = True
        self._client = None
        self._sftp = None
        self._connected = False
        self._cancel = False
        self._gate = None

    def stop(self) -> None:
        """停止线程；若正阻塞等待主机密钥确认，同时唤醒。"""
        self._running = False
        self._queue.put(("stop", None))
        if self._gate is not None:
            self._gate.cancel()

    def resolve_host_key(self, accept: bool) -> None:
        """GUI 对主机密钥确认对话框做出决定后调用（可从任意线程调用）。"""
        if self._gate is not None:
            if accept:
                self._gate.accept()
            else:
                self._gate.reject()

    # ---------------- UI 线程命令接口 ----------------
    def listdir(self, path: str) -> None:
        self._queue.put(("listdir", path))

    def upload(self, local: str, remote: str) -> None:
        self._queue.put(("upload", (local, remote)))

    def download(self, remote: str, local: str, resume: bool = False) -> None:
        self._queue.put(("download", (remote, local, resume)))

    def remove(self, path: str) -> None:
        self._queue.put(("remove", path))

    def mkdir(self, path: str) -> None:
        self._queue.put(("mkdir", path))

    def rename(self, old: str, new: str) -> None:
        self._queue.put(("rename", (old, new)))

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
        sock_factory = None
        try:
            if self._jump is not None:
                jh, jp = self._jump.host, self._jump.port
                # 跳板机自身的主机密钥同样 TOFU 确认（确认前不发凭据）
                self._gate = HostKeyGate(
                    jh, jp,
                    on_unknown=lambda fp: self.jumpKeyUnknown.emit(jh, jp, fp),
                    on_changed=lambda fp, e: self.jumpKeyChanged.emit(jh, jp, fp, e))
                try:
                    verdict, _jk, _jfp, jexp = self._gate.wait()
                except HostKeyProbeError as e:
                    self.error.emit(f"跳板机：{e}")
                    return
                if verdict == "timeout":
                    self.error.emit("跳板机主机密钥确认超时，连接已取消")
                    return
                if verdict == "cancelled":
                    return
                if verdict == "rejected":
                    self.error.emit("已取消：跳板机主机密钥" + (
                        "与既有记录不一致，未获确认" if jexp else "未获确认"))
                    return
                self._jump.connect()
                sock_factory = lambda: self._jump.open_channel(self.host, self.port)

            self._gate = HostKeyGate(self.host, self.port,
                                     on_unknown=self.hostKeyUnknown.emit,
                                     on_changed=self.hostKeyChanged.emit,
                                     sock_factory=sock_factory)
            verdict, _key, fp, expected = self._gate.wait()
            if verdict == "timeout":
                self.error.emit("主机密钥确认超时，连接已取消")
                return
            if verdict == "cancelled":
                return
            if verdict == "rejected":
                if expected:
                    self.error.emit("已取消：主机密钥与既有记录不一致，未获确认")
                else:
                    self.error.emit("已取消：未确认服务器主机密钥")
                return
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
            if sock_factory is not None:
                kwargs["sock"] = sock_factory()
            client.connect(**kwargs)
            # 复核：认证连接的实际主机密钥必须与用户确认的值一致。
            actual = fingerprint_sha256(
                client.get_transport().get_remote_server_key())
            if actual != fp:
                client.close()
                raise RuntimeError(
                    "主机密钥在连接过程中被替换（疑似中间人攻击），已中止")
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
        except HostKeyProbeError as e:
            prefix = "经跳板机 " if self._jump is not None else ""
            self.error.emit(prefix + str(e))
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
            if self._jump is not None:
                self._jump.close()
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

    def _do_download(self, remote, local, resume=False):
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
            # 断点续传：resume=True 且本地已有部分文件时，从已写字节数
            # 处继续（进度条按「本地已有 + 本次已传」显示）。
            size = st.st_size or 0
            off = 0
            mode = "wb"
            if resume and os.path.exists(local):
                off = os.path.getsize(local)
                if off >= size:
                    self.opDone.emit(f"已存在且大小一致，跳过: {posixpath.basename(remote)}")
                    return
                mode = "ab"

            def cb(d, t):
                self._check_cancel()
                self.transferProgress.emit(off + d, size)

            os.makedirs(os.path.dirname(local) or ".", exist_ok=True)
            with self._sftp.open(remote, "rb") as rfh:
                if off:
                    rfh.seek(off)
                with open(local, mode) as lfh:
                    while True:
                        self._check_cancel()
                        chunk = rfh.read(65536)
                        if not chunk:
                            break
                        lfh.write(chunk)
                        cb(len(chunk), size)
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
