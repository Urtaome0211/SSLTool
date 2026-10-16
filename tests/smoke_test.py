"""SSLTool 离屏冒烟测试。

用法: python tests/smoke_test.py
通过全部检查时退出码 0，否则退出码 1。
"""
import os
import queue
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt5.QtCore import QEvent, Qt
from PyQt5.QtGui import QKeyEvent
from PyQt5.QtWidgets import QApplication

from app import config
from app.theme import get_qss, make_app_icon
from app.ui.terminal_widget import TerminalWidget
from app.ui.main_window import MainWindow
from app.core.workers import SSHWorker, SSLWorker
from app.core.sftp_worker import SFTPWorker, _TransferCancelled

app = QApplication(sys.argv)
results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)


def wait_signal(worker, attr, timeout=30.0):
    """启动 worker 并等待指定信号发射（用于连接失败路径）。"""
    got = {"v": False}
    getattr(worker, attr).connect(lambda *a: got.__setitem__("v", True))
    worker.start()
    t0 = time.time()
    while time.time() - t0 < timeout:
        app.processEvents()
        if got["v"]:
            break
        time.sleep(0.02)
    worker.stop()
    worker.wait(2000)
    return got["v"]


# ---------- 1. 主题 ----------
qss = get_qss()
check("QSS 包含主色 #409eff", "#409eff" in qss)
check("应用图标可绘制", not make_app_icon().isNull())
qss_dark = get_qss(dark=True)
check("深色 QSS 使用深色调色板", "#1e2126" in qss_dark and qss_dark != qss)
check("深色 QSS 无未替换占位符", "${" not in qss_dark and "${" not in qss)

# ---------- 2. 终端 VT 模拟 ----------
term = TerminalWidget()
term.feed(b"hello \x1b[31mred\x1b[0m world\r\n")
term.feed(b"line2\rreplaced\n")
term.feed(b"a\bb\n")
term.feed(b"\xe4\xb8")          # UTF-8 跨包
term.feed(b"\xad\n")
text = term.toPlainText()
check("ANSI 彩色文本可见", "red" in text and "hello" in text)
check("CR 回车覆盖生效", "replaced" in text and "line2" not in text)
check("UTF-8 跨包解码", "中" in text)
term.feed(b"\x1b[2J")
check("CSI 2J 清屏", term.toPlainText().strip() == "")
term.feed(b"\x1b[38;5;196mX\x1b[0m\n")
check("256 色 SGR 不崩溃", "X" in term.toPlainText())

sent = []
term.dataSent.connect(lambda b: sent.append(b))
term.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_A, Qt.NoModifier, "a"))
check("按键发送字符 a", sent and sent[-1] == b"a")
term.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_C, Qt.ControlModifier, ""))
check("Ctrl+C 发送中断 \\x03", sent[-1] == b"\x03")
term.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Up, Qt.NoModifier, ""))
check("方向键发送 ESC 序列", sent[-1] == b"\x1b[A")

# ---------- 3. 会话与凭据存取（文件回退路径） ----------
tmp = Path(tempfile.mkdtemp())
config._KEYRING_OK = False
config.DATA_DIR = tmp
config.SESSIONS_FILE = tmp / "sessions.json"
config.SECRETS_FILE = tmp / "secrets.json"
config.SETTINGS_FILE = tmp / "settings.json"
s = config.make_session(name="测试机", host="127.0.0.1", port=22, username="root")
config.save_sessions([s])
loaded = config.load_sessions()
check("会话保存/读取", len(loaded) == 1 and loaded[0]["host"] == "127.0.0.1")
check("会话 ID 自动生成", bool(loaded[0].get("id")))
config.set_secret("k", "secret123")
check("凭据存取回环", config.get_secret("k") == "secret123")
config.delete_secret("k")
check("凭据删除", config.get_secret("k") is None)
config.save_settings({"dark": True})
check("设置持久化", config.load_settings().get("dark") is True)
config.save_settings({"dark": False})

# ---------- 4. 主窗口 ----------
win = MainWindow()
check("主窗口会话列表加载", win.session_list.count() == 1)
check("欢迎页存在", win.tabs.count() == 1 and win.tabs.tabText(0) == "欢迎")
check("菜单已创建", len(win.menuBar().actions()) == 3)
win.dark_act.setChecked(True)
check("深色主题实时切换", "#1e2126" in QApplication.instance().styleSheet())
win.dark_act.setChecked(False)
check("切回浅色主题", "#f5f7fa" in QApplication.instance().styleSheet())
check("主题偏好持久化", config.load_settings().get("dark") is False)
win.close()

# ---------- 5. 连接失败路径（127.0.0.1:1 快速拒绝） ----------
check("SSH 连接失败触发 error",
      wait_signal(SSHWorker("127.0.0.1", 1, "u", password="x"), "error"))
check("SSL 连接失败触发 error",
      wait_signal(SSLWorker("127.0.0.1", 1, verify=False), "error"))
check("SFTP 连接失败触发 error",
      wait_signal(SFTPWorker("127.0.0.1", 1, "u", password="x"), "error"))

# ---------- 6. SFTP 取消语义（purge + 取消标志，队列模拟，不起线程） ----------
w = SFTPWorker("127.0.0.1", 1, "u", password="x")
w.upload("a", "/r/a")
w.listdir("/tmp")
w.download("/r/b", "b")
w.cancel_transfer()          # 置 _cancel + 入队 purge
w.upload("c", "/r/c")        # 未开始的传输应被 purge 丢弃
w.listdir("/home")           # 非传输操作应保留
seq = []
while True:
    try:
        kind, payload = w._queue.get_nowait()
    except queue.Empty:
        break
    if kind == "purge":
        w._purge_transfers()
        continue
    if kind in ("upload", "download"):
        try:
            w._check_cancel()
        except _TransferCancelled:
            seq.append("cancelled")
            continue
        seq.append(kind)
    else:
        seq.append(kind)
check("SFTP 取消后 purge 语义",
      seq == ["cancelled", "listdir", "cancelled", "listdir"])

# ---------- 7. 端口转发规则解析 ----------
from app.core.forwarders import parse_forwards

try:
    specs = parse_forwards([
        "L:127.0.0.1:8080:10.0.0.5:80",
        "  R:0.0.0.0:0:127.0.0.1:3306  ",
        "D:127.0.0.1:1080",
        "",
        "# 注释行",
    ])
    check("转发规则解析（L/R/D + 空行/注释）",
          [s["kind"] for s in specs] == ["L", "R", "D"]
          and specs[0]["bind"] == ("127.0.0.1", 8080)
          and specs[0]["target"] == ("10.0.0.5", 80)
          and specs[1]["bind"] == ("0.0.0.0", 0)
          and specs[2]["bind"] == ("127.0.0.1", 1080))
except ValueError:
    check("转发规则解析（L/R/D + 空行/注释）", False)

for bad in ["L:127.0.0.1:8080", "X:1:2:3:4", "L:a:b:80", "D:127.0.0.1"]:
    try:
        parse_forwards([bad])
        check(f"非法转发规则抛错: {bad}", False)
    except ValueError:
        check(f"非法转发规则抛错: {bad}", True)

# ---------- 汇总 ----------
fails = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(fails)}/{len(results)} 项检查通过")
if fails:
    print("失败项:", ", ".join(fails))
sys.exit(1 if fails else 0)
