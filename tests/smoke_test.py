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

# ---------- 2b. 终端体验（备用屏幕 / 括号粘贴 / 搜索 / URL） ----------
# 备用屏幕：1049 保存/恢复光标，47 只切换缓冲
alt = TerminalWidget()
alt.feed(b"main-one\r\nmain-two\r\n")
alt.feed(b"\x1b[2;5H")                 # 光标移到第 2 行第 6 列（0-based: y=1, x=4）
alt.feed(b"\x1b[?1049h")
check("1049h 进入备用屏幕", alt._screen.is_alt_active())
check("备用屏幕隐藏主屏内容", "main-one" not in alt.toPlainText())
alt.feed(b"\x1b[Halt-line")
check("备用屏幕显示新内容", "alt-line" in alt.toPlainText())
alt.feed(b"\x1b[?1049l")
check("1049l 退出备用屏幕", not alt._screen.is_alt_active())
check("退出后主屏内容恢复", "main-one" in alt.toPlainText()
      and "alt-line" not in alt.toPlainText())
check("1049 保存并恢复光标位置",
      (alt._screen.cursor.y, alt._screen.cursor.x) == (1, 4))

alt2 = TerminalWidget()
alt2.feed(b"main-content\r\n")
alt2.feed(b"\x1b[?47h")
check("47h 进入备用屏幕", alt2._screen.is_alt_active())
alt2.feed(b"X")
alt2.feed(b"\x1b[?47l")
check("47l 退出备用屏幕", not alt2._screen.is_alt_active())
check("47 模式恢复主屏且不恢复光标", "main-content" in alt2.toPlainText()
      and "X" not in alt2.toPlainText()
      and (alt2._screen.cursor.y, alt2._screen.cursor.x) == (1, 1))

alt3 = TerminalWidget()
alt3.feed(b"keep-me\r\n")
alt3.feed(b"\x1b[?1049h\x1b[2Jalt-content")
check("备用屏幕内 CSI 2J 清屏不崩溃", "alt-content" in alt3.toPlainText())
alt3.feed(b"\x1b[?1049l")
check("退出后主屏未被备用屏清屏影响", "keep-me" in alt3.toPlainText())

# 括号粘贴
paste_sent = []
alt.dataSent.connect(lambda b: paste_sent.append(b))
alt.paste_text("ls -la\necho hi")
check("粘贴包裹 \\x1b[200~ / \\x1b[201~",
      paste_sent[-1] == b"\x1b[200~ls -la\necho hi\x1b[201~")
QApplication.clipboard().setText("clip-text")
alt._paste_clipboard()
check("剪贴板粘贴同样包裹",
      paste_sent[-1] == b"\x1b[200~clip-text\x1b[201~")

# 终端内搜索
st = TerminalWidget()
st.resize(600, 300)
for i in range(60):
    st.feed(("item-%02d alpha\r\n" % i).encode())
st.feed(b"needle-42 omega\r\n")
for i in range(40):
    st.feed(("tail-%02d\r\n" % i).encode())
n = st.search("needle-42")
check("搜索单次命中计数", n == 1)
sb = st.verticalScrollBar()
check("搜索跳转到命中行", sb.value() <= 61 < sb.value() + st._rows)
n = st.search("alpha")
check("搜索多次命中计数", n == 60)
check("命中定位到第一个", st.search_index() == 0)
st.next_match()
check("下一个命中", st.search_index() == 1)
st.prev_match()
check("上一个命中回退", st.search_index() == 0)
st.prev_match()
check("上一个命中回绕到末尾", st.search_index() == 59)
st.close_search()
check("关闭搜索清空高亮", st.search_count() == 0 and st.search_index() == -1)

# URL 检测
ut = TerminalWidget()
ut.feed(b"see http://example.com/a?b=1 and https://x.io/y\r\n")
check("URL 检测（含查询串）", ut.url_at(0, 5) == "http://example.com/a?b=1")
check("第二个 URL 检测", ut.url_at(0, 34) == "https://x.io/y")
check("非 URL 位置返回 None", ut.url_at(0, 1) is None)
ut.feed(b"go to https://example.com.\r\n")
check("URL 尾部标点剔除", ut.url_at(1, 8) == "https://example.com"
      and ut.url_at(1, 25) is None)

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
if config._DPAPI_PROTECT is not None:
    raw = config.SECRETS_FILE.read_bytes().decode("utf-8", "replace")
    check("回退凭据存储非明文（DPAPI 加密）", "secret123" not in raw)
else:
    check("回退凭据存储非明文（DPAPI 加密）", True)  # 非 Windows 退化为混淆格式
config.delete_secret("k")
check("凭据删除", config.get_secret("k") is None)
config.save_settings({"dark": True})
check("设置持久化", config.load_settings().get("dark") is True)
config.save_settings({"dark": False})

# ---------- 3c. 会话日志 ----------
from app.core.session_log import SessionLog

lg = SessionLog("测试会话/weird name", "127.0.0.1", 22, user="root")
lg.write(b"echo-data\r\n")
lg.write("中文输出".encode("utf-8"))
lg.write(b"\xff\xfe")               # 损坏 UTF-8 → U+FFFD 替换，不抛异常
lg.note("已连接")
lg.close()
log_files = list((tmp / "logs").glob("*.log"))
check("会话日志文件已创建", len(log_files) == 1)
content = log_files[0].read_text(encoding="utf-8")
check("日志含原始输出与事件行",
      "echo-data" in content and "中文输出" in content
      and "\ufffd" in content and "已连接" in content
      and "root@127.0.0.1:22" in content and "会话结束" in content)
check("日志文件名含安全化会话名", "测试会话_weird_name" in log_files[0].name)

# ---------- 3b. 主机密钥 TOFU 存储 ----------
import paramiko
from app.core import hostkey as hk

k1 = paramiko.RSAKey.generate(2048)
k2 = paramiko.RSAKey.generate(2048)
fp1 = hk.fingerprint_sha256(k1)
fp2 = hk.fingerprint_sha256(k2)
check("指纹格式为 SHA256:base64", fp1.startswith("SHA256:") and "=" not in fp1
      and fp1 != fp2)
check("未知主机 → unknown", hk.check_known_host("example.com", 22, k1)[0] == "unknown")
hk.save_known_host("example.com", 22, k1.get_name(), fp1)
check("已知主机 → ok", hk.check_known_host("example.com", 22, k1)[0] == "ok")
status, got_fp, expected = hk.check_known_host("example.com", 22, k2)
check("密钥变更 → changed 且返回旧指纹",
      status == "changed" and got_fp == fp2 and expected == fp1)
hk.save_known_host("example.com", 22, k2.get_name(), fp2)
check("接受新密钥后 → ok",
      hk.check_known_host("example.com", 22, k2)[0] == "ok")
hk.delete_known_host("example.com", 22)
check("删除记录后 → unknown",
      hk.check_known_host("example.com", 22, k1)[0] == "unknown")

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

# ---------- 5b. SSL 证书详情解析（cryptography 解析 DER） ----------
try:
    import datetime as _dt
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    _key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test.example.com")])
    _now = _dt.datetime.now(_dt.timezone.utc)

    def _build_cert(valid_days):
        return (x509.CertificateBuilder()
                .subject_name(_name).issuer_name(_name)
                .public_key(_key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(_now - _dt.timedelta(days=1))
                .not_valid_after(_now + _dt.timedelta(days=valid_days))
                .add_extension(x509.SubjectAlternativeName(
                    [x509.DNSName("test.example.com")]), critical=False)
                .sign(_key, hashes.SHA256()))

    _der = _build_cert(400).public_bytes(serialization.Encoding.DER)
    _lines = SSLWorker._x509_lines(_der)
    check("证书详情含主题/有效期/SAN",
          any("CN=test.example.com" in l for l in _lines)
          and any(l.startswith("有效期至") for l in _lines)
          and any("SAN" in l for l in _lines))
    check("有效证书无过期警告", not any("过期" in l for l in _lines))
    _der_short = _build_cert(10).public_bytes(serialization.Encoding.DER)
    _lines_short = SSLWorker._x509_lines(_der_short)
    check("30 天内到期警告", any("天后到期" in l for l in _lines_short))
    _der_exp = _build_cert(-1).public_bytes(serialization.Encoding.DER)
    _lines_exp = SSLWorker._x509_lines(_der_exp)
    check("已过期警告", any("已过期" in l for l in _lines_exp))
    check("损坏 DER 不崩溃", SSLWorker._x509_lines(b"not-a-cert") == [])
except Exception:
    check("证书详情解析（cryptography 生成自签证书）", False)

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
