"""完整 VT 终端组件：pyte（VT100/VT220）终端模拟 + QPainter 自绘。

支持完整的 ANSI/VT 控制序列（光标定位、清屏、SGR 颜色、alternate 光标
保存/恢复等），因此 vim / top / htop 等全屏程序可以正常使用。
自带滚动回看缓冲（5000 行）与本地回显去重（见 _echo_expect）。

V2.0 终端体验：
- 备用屏幕（alt-screen）支持：CSI ?47h/l 与 CSI ?1049h/l（DECSET/DECRESET）
  在独立的备用缓冲中运行全屏程序，退出后主屏幕（含滚动历史）原样恢复；
- 括号粘贴（bracketed paste）：粘贴内容用 \\x1b[200~ … \\x1b[201~ 包裹，
  避免多行粘贴被 shell 逐行当作命令执行；
- 终端内搜索：Ctrl+F 打开搜索条，高亮命中并支持上下跳转；
- 可点击 URL：Ctrl+点击 http(s):// 链接用系统浏览器打开，右键菜单可
  复制 / 打开链接。
"""
import re
import unicodedata

import pyte
from PyQt5.QtCore import QRectF, Qt, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import (QColor, QFont, QFontMetricsF, QKeySequence,
                         QPainter, QDesktopServices)
from PyQt5.QtWidgets import (QAction, QApplication, QFrame, QHBoxLayout,
                             QLabel, QLineEdit, QMenu, QPushButton,
                             QScrollBar, QWidget)

# 黑客帝国（Matrix）风格 16 色：黑底绿字，全色板映射为绿色亮度梯度
_XTERM16 = [
    "#000000", "#003300", "#006600", "#009900",
    "#00cc00", "#00e600", "#00ff41", "#66ff99",
    "#003d00", "#008800", "#00cc00", "#00ff41",
    "#33ff66", "#66ff99", "#99ffcc", "#e0ffe8",
]

# 粗体 + 标准色（前 8 色）→ 对应亮色（VT 惯例；pyte 不自动做此映射）
_BOLD_BRIGHT = {
    "000000": "003d00", "003300": "008800", "006600": "00cc00",
    "009900": "00ff41", "00cc00": "33ff66", "00e600": "66ff99",
    "00ff41": "99ffcc", "66ff99": "e0ffe8",
}

_BG = QColor("#000000")   # 默认背景：纯黑
_FG = QColor("#00ff41")   # 默认前景：黑客帝国绿

_BLINK_MS = 600        # 光标闪烁周期

_SEARCH_BG = QColor("#003d00")      # 搜索命中背景（暗绿）
_SEARCH_CUR_BG = QColor("#00ff41")  # 当前命中背景（亮绿）

# http(s) 链接（排除空白与常见引号/尖括号；尾部标点单独剔除）
_URL_RE = re.compile(r"https?://[^\s'\"<>]+")
_URL_TRAIL = ".,;:!?"


def _xterm256(n: int) -> str:
    if n < 16:
        return _XTERM16[n]
    if 16 <= n <= 231:
        n -= 16
        r, g, b = n // 36, (n // 6) % 6, n % 6

        def v(c):
            return 0 if c == 0 else 55 + c * 40

        # Matrix 风格：按 RGB 亮度映射为绿色，保持明暗对比
        gv = max(v(r), v(g), v(b))
        return "#00{:02x}00".format(gv)
    if 232 <= n <= 255:
        g = 8 + (n - 232) * 10
        return "#00{:02x}00".format(g)
    return "#00ff41"


def _echo_expect(data: bytes) -> bytes:
    """近似 ICANON+ECHO+ECHOE+ECHOCTL 的服务器回显模型：返回"需要抑制"的字节。

    本地回显开启时，键入字符已在本地显示，服务器若再回显同一内容会重复。
    这里按常见 pty 配置预测服务器将回显什么，供 feed_remote 前缀匹配去重：
    - 可见字符 → 原样（抑制）
    - Backspace(\\x7f) → "\\b \\b"（ECHOE 回显序列，抑制）
    - Ctrl+C(\\x03) → "^C"（ECHOCTL，抑制；其后的 \\r\\n 放行推进换行）
    - 回车(\\r) → 不建模：服务器回显的 \\r\\n 必须放行，用于推进换行
      （本地只置 CR 挂起，不产生换行）
    - ESC 序列（方向键等）→ 不建模：readline 整行重绘无法预测
    """
    out = bytearray()
    data = data.split(b"\x1b", 1)[0]
    for b in data:
        if b == 0x0D:           # \r：放行服务器回显的换行
            continue
        if b == 0x7F:           # DEL → \b \b
            out += b"\b \b"
        elif b == 0x03:         # Ctrl+C → ^C
            out += b"^C"
        elif b >= 0x20:         # 可见字符（含 UTF-8 多字节片段）
            out.append(b)
    return bytes(out)


# pyte.Screen / HistoryScreen 中"一个屏幕人格"的全部可变状态字段。
# alt-screen 切换即是在同一个外观对象上换入/换出这些字段。
_SCREEN_STATE_FIELDS = (
    "columns", "lines", "buffer", "dirty", "mode", "margins", "title",
    "icon_name", "charset", "g0_charset", "g1_charset", "tabstops",
    "cursor", "savepoints", "saved_columns", "history",
)


class _SSLToolScreen(pyte.HistoryScreen):
    """HistoryScreen + ED2 清滚动缓冲 + 备用屏幕（alt-screen）支持。

    备用屏幕实现：本对象是「外观（facade）」，pyte 的 Stream 自始至终只
    持有这一个 listener（pyte 的解析器在初始化时缓存了 listener 的绑定
    方法，中途换 listener 不会生效）。进入备用屏幕时，把当前（主屏）状态
    快照收起来、把备用 pyte.Screen 的状态搬进自身；退出时反向换回。
    这样同一批数据里 "\\x1b[?1049h" 之后的字节能立即落进备用缓冲。

    语义对齐 xterm：CSI ?1049 h = 保存光标 + 进入备用屏，CSI ?1049 l =
    退出备用屏 + 恢复光标；CSI ?47 h/l 只切换缓冲，不保存/恢复光标。
    """

    def __init__(self, columns: int, lines: int, history: int = 5000):
        super().__init__(columns, lines, history=history)
        self._alt = None          # 备用缓冲（pyte.Screen）
        self._main_snapshot = None  # 处于备用屏时的主屏状态快照

    # ---- alt-screen ----

    def is_alt_active(self) -> bool:
        return self._main_snapshot is not None

    def _enter_alt(self, preserve_cursor: bool) -> None:
        if self.is_alt_active():
            return
        if self._alt is None:
            self._alt = pyte.Screen(self.columns, self.lines)
        else:
            self._alt.resize(self.lines, self.columns)
        # 47（不带 1048/1049）语义：光标位置在两个缓冲间共享
        main_cursor = (self.cursor.y, self.cursor.x)
        self._main_snapshot = self._snapshot_state()
        self._restore_state(self._snapshot_of(self._alt))
        if preserve_cursor:
            self.cursor.y = min(main_cursor[0], self.lines - 1)
            self.cursor.x = min(main_cursor[1], self.columns - 1)

    def _exit_alt(self, preserve_cursor: bool) -> None:
        if not self.is_alt_active():
            return
        # 47 语义：退出时把备用屏的光标位置带回主屏
        alt_cursor = (self.cursor.y, self.cursor.x)
        alt_snap = self._snapshot_state()
        self._restore_state(self._main_snapshot)
        self._main_snapshot = None
        for f in _SCREEN_STATE_FIELDS:
            setattr(self._alt, f, alt_snap[f])
        # 备用屏期间窗口尺寸可能变过：把主屏恢复到当前尺寸
        if self.columns != alt_snap["columns"] or self.lines != alt_snap["lines"]:
            pyte.HistoryScreen.resize(self, alt_snap["lines"],
                                      alt_snap["columns"])
        if preserve_cursor:
            self.cursor.y = min(alt_cursor[0], self.lines - 1)
            self.cursor.x = min(alt_cursor[1], self.columns - 1)

    @staticmethod
    def _snapshot_of(screen) -> dict:
        # history 只在 HistoryScreen（主屏）上存在；备用 pyte.Screen 没有
        return {f: getattr(screen, f, None) for f in _SCREEN_STATE_FIELDS}

    def _snapshot_state(self) -> dict:
        return {f: getattr(self, f) for f in _SCREEN_STATE_FIELDS}

    def _restore_state(self, snap: dict) -> None:
        for f, v in snap.items():
            setattr(self, f, v)

    def set_mode(self, *modes, **kwargs):
        private = kwargs.get("private")
        with_cursor = False
        if private:
            for m in modes:
                if m in (47, 1049):
                    with_cursor = with_cursor or m == 1049
        if not self.is_alt_active():
            if with_cursor:
                self.save_cursor()
            if 47 in modes or 1049 in modes:
                self._enter_alt(preserve_cursor=not with_cursor)
        super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes, **kwargs):
        private = kwargs.get("private")
        with_cursor = False
        if private:
            for m in modes:
                if m in (47, 1049):
                    with_cursor = with_cursor or m == 1049
        exited = False
        if self.is_alt_active():
            self._exit_alt(preserve_cursor=not with_cursor)
            exited = True
        super().reset_mode(*modes, **kwargs)
        if exited and with_cursor:
            self.restore_cursor()

    def erase_in_display(self, how=0, *args, **kwargs):
        super().erase_in_display(how, *args, **kwargs)
        if how in (2, 3) and getattr(self, "history", None) is not None:
            self.history.top.clear()
            self.history.bottom.clear()
            self.history = self.history._replace(position=self.history.size)

    def before_event(self, event):
        # 备用屏幕（history=None）时没有历史缓冲可释放/收集
        if getattr(self, "history", None) is None:
            return
        super().before_event(event)

    def after_event(self, event):
        # 备用屏幕（history=None）滚动时不能做历史缓冲收集；
        # 只保留 dirty 标记，行为对齐无滚动回看的终端。
        if getattr(self, "history", None) is None:
            self.dirty.add(event)
            return
        super().after_event(event)


class TerminalWidget(QWidget):
    dataSent = pyqtSignal(bytes)            # 键盘输入
    ptySizeChanged = pyqtSignal(int, int)   # cols, rows

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("terminal")
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.IBeamCursor)
        self.setMinimumSize(80, 40)
        f = QFont("Consolas")
        f.setStyleHint(QFont.Monospace)
        f.setFixedPitch(True)
        f.setPointSize(12)
        self.setFont(f)

        self._screen = _SSLToolScreen(80, 24, history=5000)
        self._stream = pyte.ByteStream(self._screen)
        self._echo_model = b""          # 期望服务器回显、需抑制的字节（回显去重）
        self._local_echo_on = False
        self._cols = 80
        self._rows = 24
        self._cw = 8.0
        self._ch = 16.0
        self._font_cache = {}
        self._color_cache = {}
        self._selection = None          # (锚点行, 锚点列, 当前行, 当前列) 绝对坐标
        self._cursor_visible = True
        self._cursor_blink = QTimer(self)
        self._cursor_blink.setInterval(_BLINK_MS)
        self._cursor_blink.timeout.connect(self._blink)

        # 搜索状态
        self._search_text = ""
        self._search_ranges = {}        # 绝对行 -> {列集合}（命中高亮）
        self._search_order = []         # [(绝对行, 起始列)] 按视觉顺序
        self._search_index = -1
        self._search_cur = None         # (绝对行, 起始列) 当前命中
        self._build_search_bar()

        # 右侧滚动条：value 0 = 回看顶部，maximum = 底部（跟随最新输出）
        self._scrollbar = QScrollBar(Qt.Vertical, self)
        self._scrollbar.setSingleStep(1)
        self._scrollbar.valueChanged.connect(lambda _v: self.update())

        # 默认单元格（空行/越界时使用，pyte 的静态默认 Char）
        self._default_cell = self._screen.default_char

    # ---------------- 数据输入 ----------------

    def feed(self, data: bytes) -> None:
        """把原始字节送入终端模拟器并重绘（含本地回显即时落屏）。"""
        sb = self._scrollbar
        at_bottom = sb.value() >= sb.maximum() - 4
        # \x7f（DEL，Backspace 键发送的字节）在本地回显语境下是"删除光标左
        # 一个字符"，等价于 ECHOE 的 "\b \b"；\x08 本身仅移动光标。
        if b"\x7f" in data:
            out = bytearray()
            for b in data:
                if b == 0x7F and self._screen.cursor.x > 0:
                    out += b"\x08 \x08"
                else:
                    out.append(b)
            data = bytes(out)
        self._stream.feed(data)
        self._sync_scrollbar()
        if at_bottom:
            sb.setValue(sb.maximum())
        if self._search_text:
            self._recompute_search()
        self.update()

    def set_local_echo(self, on: bool) -> None:
        """本地回显开关：关闭时清空去重模型，服务器数据原样显示。"""
        self._local_echo_on = on
        if not on:
            self._echo_model = b""

    def feed_local(self, data: bytes) -> None:
        """本地回显：显示键入内容，并按服务器回显模型记录待抑制字节。"""
        self._local_echo_on = True
        self._echo_model += _echo_expect(data)
        if len(self._echo_model) > 8192:
            self._echo_model = b""
        self.feed(data)

    def feed_remote(self, data: bytes) -> None:
        """服务器数据：先抑制与本地回显重复的字节，再显示剩余部分。

        本地回显开启时，服务器（如 sshd/readline）会把键入字符再回显一遍，
        若不处理会显示两个字符。此处按 _echo_expect 建立的模型做字节前缀
        匹配：匹配上的部分是重复回显，丢弃；不匹配说明服务器输出的是真实
        内容（或不回显），放弃抑制、原样显示。
        """
        if self._local_echo_on and self._echo_model and data:
            i = 0
            n = min(len(data), len(self._echo_model))
            while i < n and data[i] == self._echo_model[i]:
                i += 1
            if i:
                self._echo_model = self._echo_model[i:]
                data = data[i:]
            else:
                self._echo_model = b""
        self.feed(data)

    # ---------------- 内容导出（测试与复制用） ----------------

    def _total_lines(self):
        """滚动缓冲 + 当前屏幕的全部行（历史行是快照引用，不会被后续修改）。

        备用屏幕激活时无滚动历史（history 字段被换走），只返回备用缓冲。
        """
        h = getattr(self._screen, "history", None)
        top = list(h.top) if h is not None else []
        return top + [self._screen.buffer[y] for y in range(self._screen.lines)]

    def _line_str(self, line) -> str:
        parts = []
        for col in range(self._cols):
            ch = line[col].data
            if ch:
                parts.append(ch)
        return "".join(parts).rstrip()

    @staticmethod
    def _line_char_cols(line, cols):
        """行内字符索引 → 单元格列号映射（宽字符占两列，索引只算一次）。"""
        idx_to_col = []
        for col in range(cols):
            ch = line[col].data
            if ch:
                idx_to_col.append(col)
        return idx_to_col

    def toPlainText(self) -> str:
        return "\n".join(self._line_str(line) for line in self._total_lines())

    def selectedText(self) -> str:
        if self._selection is None:
            return ""
        (al, ac), (bl, bc) = self._selection[:2], self._selection[2:]
        if (al, ac) > (bl, bc):
            (al, ac), (bl, bc) = (bl, bc), (al, ac)
        total = self._total_lines()
        out = []
        for l in range(al, min(bl + 1, len(total))):
            line = total[l]
            lo = ac if l == al else 0
            hi = bc + 1 if l == bl else self._cols
            parts = []
            for col in range(lo, min(hi, self._cols)):
                if col in line and line[col].data:
                    parts.append(line[col].data)
            out.append("".join(parts))
        return "\n".join(out)

    def copy(self) -> None:
        text = self.selectedText()
        if text:
            QApplication.clipboard().setText(text)

    def selectAll(self) -> None:
        total = len(self._total_lines())
        if total:
            self._selection = (0, 0, total - 1, self._cols - 1)
        else:
            self._selection = (0, 0, 0, 0)
        self.update()

    def _has_selection(self) -> bool:
        return self._selection is not None

    # ---------------- 括号粘贴 ----------------

    def paste_text(self, text: str) -> None:
        """以括号粘贴模式发送文本：\\x1b[200~ … \\x1b[201~ 包裹。

        支持 bracketed paste 的 shell（bash/zsh 等）会把整段内容当作一次
        粘贴处理，而不是逐行回车执行 —— 避免多行粘贴被当作命令执行。
        """
        if not text:
            return
        self.dataSent.emit(b"\x1b[200~" + text.encode("utf-8") + b"\x1b[201~")

    def _paste_clipboard(self) -> None:
        self.paste_text(QApplication.clipboard().text())

    # ---------------- 终端内搜索 ----------------

    def _build_search_bar(self) -> None:
        bar = QFrame(self)
        bar.setObjectName("searchBar")
        bar.setStyleSheet(
            "#searchBar { background: #001a00; border: 1px solid #003d00;"
            " border-radius: 4px; }"
            " QLineEdit { background: #002b00; color: #00ff41;"
            " border: 1px solid #005c00; border-radius: 3px; padding: 2px 6px; }"
            " QPushButton { background: transparent; color: #33ff66;"
            " border: none; padding: 2px 6px; }"
            " QPushButton:hover { color: #ccffcc; }"
            " QLabel { color: #66ff99; }")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(6, 3, 6, 3)
        lay.setSpacing(4)
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("搜索终端输出…")
        self._search_count = QLabel("0/0")
        prev_btn = QPushButton("↑")
        prev_btn.setToolTip("上一个 (Shift+Enter)")
        next_btn = QPushButton("↓")
        next_btn.setToolTip("下一个 (Enter)")
        close_btn = QPushButton("✕")
        close_btn.setToolTip("关闭 (Esc)")
        lay.addWidget(self._search_edit, 1)
        lay.addWidget(self._search_count)
        lay.addWidget(prev_btn)
        lay.addWidget(next_btn)
        lay.addWidget(close_btn)
        self._search_bar = bar
        self._search_bar.hide()
        self._search_edit.textChanged.connect(self._on_search_text_changed)
        self._search_edit.returnPressed.connect(lambda: self._goto_match(1))
        prev_btn.clicked.connect(lambda: self._goto_match(-1))
        next_btn.clicked.connect(lambda: self._goto_match(1))
        close_btn.clicked.connect(self.close_search)

    def _show_search(self) -> None:
        self._search_bar.show()
        self._search_bar.raise_()
        self._place_search_bar()
        self._search_edit.setFocus()
        self._search_edit.selectAll()

    def _place_search_bar(self) -> None:
        w = min(360, max(220, self.width() - 16))
        self._search_bar.setGeometry(self.width() - w - 8, 6, w,
                                     self._search_bar.sizeHint().height())

    def _on_search_text_changed(self, text: str) -> None:
        self.search(text)

    def search(self, text: str) -> int:
        """按文本搜索（不区分大小写），跳转到第一个命中，返回命中数。"""
        self._search_text = text
        self._recompute_search()
        if self._search_order:
            self._search_index = 0
            self._search_cur = self._search_order[0]
            self._scroll_to_line(self._search_cur[0])
        else:
            self._search_index = -1
            self._search_cur = None
        self._update_search_count()
        self.update()
        return len(self._search_order)

    def search_count(self) -> int:
        return len(self._search_order)

    def search_index(self) -> int:
        """当前命中序号（0 起；无命中时为 -1）。"""
        return self._search_index

    def _recompute_search(self) -> None:
        needle = self._search_text.lower()
        if not needle:
            self._search_ranges = {}
            self._search_order = []
            return
        ranges, order = {}, []
        for abs_line, line in enumerate(self._total_lines()):
            s = self._line_str(line)
            if not s:
                continue
            ls = s.lower()
            idx_to_col = self._line_char_cols(line, self._cols)
            cols = set()
            start = 0
            while True:
                i = ls.find(needle, start)
                if i < 0:
                    break
                for ci in range(i, i + len(needle)):
                    if ci < len(idx_to_col):
                        cols.add(idx_to_col[ci])
                order.append((abs_line, idx_to_col[i]))
                start = i + 1
            if cols:
                ranges[abs_line] = cols
        # 宽字符占两列：命中列若落在宽字符的占位列，占位列无需高亮，
        # 但上一列（宽字符首列）已包含；直接保留即可。
        self._search_ranges = ranges
        self._search_order = order
        self._update_search_count()

    def _update_search_count(self) -> None:
        total = len(self._search_order)
        cur = self._search_index + 1 if self._search_index >= 0 else 0
        self._search_count.setText(f"{cur}/{total}")

    def next_match(self) -> None:
        self._goto_match(1)

    def prev_match(self) -> None:
        self._goto_match(-1)

    def _goto_match(self, delta: int) -> None:
        if not self._search_order:
            return
        n = len(self._search_order)
        self._search_index = (self._search_index + delta) % n
        self._search_cur = self._search_order[self._search_index]
        self._scroll_to_line(self._search_cur[0])
        self._update_search_count()
        self.update()

    def _scroll_to_line(self, abs_line: int) -> None:
        sb = self._scrollbar
        vis = max(1, self._rows - 1)
        sb.setValue(max(0, min(sb.maximum(), abs_line - vis // 2)))

    def close_search(self) -> None:
        self._search_text = ""
        self._search_ranges = {}
        self._search_order = []
        self._search_index = -1
        self._search_cur = None
        self._search_bar.hide()
        self.setFocus()
        self.update()

    def _search_active(self) -> bool:
        return bool(self._search_text) and self._search_bar.isVisible()

    # ---------------- URL 检测 ----------------

    def _urls_on_line(self, line, cols):
        """返回 [(url, 起始列, 结束列)]。字符索引经 _line_char_cols 映射到列。"""
        s = self._line_str(line)
        if not s:
            return []
        idx_to_col = self._line_char_cols(line, cols)
        out = []
        for m in _URL_RE.finditer(s):
            url = m.group(0)
            if url[-1] in _URL_TRAIL:
                url = url.rstrip(_URL_TRAIL)
                if not url:
                    continue
            end = m.start() + len(url)
            c0 = idx_to_col[m.start()]
            c1 = idx_to_col[end - 1]
            ch = line[c1].data
            w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
            out.append((url, c0, c1 + w))
        return out

    def url_at(self, row: int, col: int):
        """返回 (row, col) 处的 URL 字符串；无则返回 None。"""
        total = self._total_lines()
        if not (0 <= row < len(total)):
            return None
        for url, c0, c1 in self._urls_on_line(total[row], self._cols):
            if c0 <= col < c1:
                return url
        return None

    @staticmethod
    def _open_url(url: str) -> None:
        try:
            QDesktopServices.openUrl(QUrl(url))
        except Exception:
            pass

    # ---------------- 绘制 ----------------

    def _font(self, bold: bool) -> QFont:
        key = 1 if bold else 0
        f = self._font_cache.get(key)
        if f is None:
            f = QFont(self.font())
            f.setBold(bold)
            self._font_cache[key] = f
        return f

    def _color(self, name: str, kind: str) -> QColor:
        key = (kind, name)
        c = self._color_cache.get(key)
        if c is None:
            if name == "default":
                c = _FG if kind == "fg" else _BG
            elif len(name) == 6 and all(ch in "0123456789abcdef" for ch in name.lower()):
                # pyte 把 16/256/24bit 色都解析成 'rrggbb' 十六进制
                c = QColor("#" + name)
            else:
                try:
                    c = QColor(_xterm256(int(name)))
                except ValueError:
                    c = _FG if kind == "fg" else _BG
            self._color_cache[key] = c
        return c

    def _in_selection(self, line: int, col: int) -> bool:
        if self._selection is None:
            return False
        cols = self._cols
        a = self._selection[0] * cols + self._selection[1]
        b = self._selection[2] * cols + self._selection[3]
        if a > b:
            a, b = b, a
        x = line * cols + col
        return a <= x <= b

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), _BG)
        total = self._total_lines()
        v = self._scrollbar.value()
        rows = self._rows
        lines = total[v:v + rows]
        y = 0.0
        for i in range(rows):
            line = lines[i] if i < len(lines) else None
            if line is not None:
                self._paint_line(p, i, line, v + i)
            y += self._ch
        self._paint_cursor(p, v)

    def _paint_line(self, p, row: int, line, abs_line: int) -> None:
        x = 0.0
        col = 0
        cols = self._cols
        srch_cols = self._search_ranges.get(abs_line)
        cur_line, cur_col = self._search_cur or (None, None)
        while col < cols:
            cell = line[col]
            ch = cell.data
            if not ch:
                col += 1
                x += self._cw
                continue
            fg = self._color(cell.fg, "fg")
            bg = self._color(cell.bg, "bg")
            if cell.reverse:
                fg, bg = bg, fg
            if cell.bold and cell.fg.lower() in _BOLD_BRIGHT:
                fg = self._color(_BOLD_BRIGHT[cell.fg.lower()], "fg")
            wide = unicodedata.east_asian_width(ch) in ("W", "F")
            w = 2 if wide else 1
            width = w * self._cw
            is_cur = (abs_line, col) == (cur_line, cur_col)
            is_match = srch_cols is not None and col in srch_cols
            if self._in_selection(abs_line, col):
                fg, bg = bg, fg
            if is_cur:
                fg, bg = QColor("#000000"), _SEARCH_CUR_BG
            elif is_match:
                fg, bg = QColor("#ccffcc"), _SEARCH_BG
            if bg != _BG:
                p.fillRect(QRectF(x, row * self._ch, width, self._ch), bg)
            p.setPen(fg)
            p.setFont(self._font(cell.bold))
            p.drawText(QRectF(x, row * self._ch, width, self._ch),
                       Qt.AlignLeft | Qt.AlignVCenter, ch)
            if cell.underscore:
                p.fillRect(QRectF(x, (row + 1) * self._ch - 1, width, 1), fg)
            col += w if wide else 1
            x += width

    def _paint_cursor(self, p, scroll_value: int) -> None:
        sb = self._scrollbar
        if sb.value() < sb.maximum() - 1:
            return  # 正在回看历史，光标不在视口内
        scr = self._screen
        if scr.cursor.hidden:
            return
        cy, cx = scr.cursor.y, scr.cursor.x
        if not (0 <= cy < self._rows and 0 <= cx < self._cols):
            return
        line = scr.buffer[cy]
        cell = line[cx]
        ch = cell.data
        fg = self._color(cell.fg, "fg")
        bg = self._color(cell.bg, "bg")
        if cell.reverse:
            fg, bg = bg, fg
        rect = QRectF(cx * self._cw, cy * self._ch, self._cw, self._ch)
        if self._cursor_visible:
            p.fillRect(rect, fg)
            p.setPen(bg)
            p.drawText(rect, Qt.AlignCenter, ch if ch else " ")
        else:
            p.setPen(fg)
            p.drawRect(rect)

    def _blink(self) -> None:
        self._cursor_visible = not self._cursor_visible
        self.update()

    # ---------------- 滚动 ----------------

    def verticalScrollBar(self) -> QScrollBar:
        """兼容旧 API：返回滚动回看缓冲的滚动条（e2e 滚动测试依赖）。"""
        return self._scrollbar

    def _sync_scrollbar(self) -> None:
        h = getattr(self._screen, "history", None)
        total = (len(h.top) if h is not None else 0) + self._rows
        vis = self._rows
        sb = self._scrollbar
        sb.blockSignals(True)
        sb.setRange(0, max(0, total - vis))
        sb.setPageStep(max(1, vis - 1))
        sb.setSingleStep(1)
        sb.blockSignals(False)

    def _scroll_page(self, up: bool) -> None:
        sb = self._scrollbar
        step = max(1, self._rows - 1)
        sb.setValue(sb.value() + (-step if up else step))

    # ---------------- 鼠标 ----------------

    def _cell_at(self, pos) -> tuple:
        line = int(pos.y() / self._ch)
        col = int(pos.x() / self._cw)
        return (self._scrollbar.value() + max(0, line),
                max(0, min(col, self._cols - 1)))

    def _url_under(self, pos):
        l, c = self._cell_at(pos)
        return self.url_at(l, c)

    def mousePressEvent(self, e) -> None:
        self.setFocus()
        if e.button() == Qt.LeftButton and e.modifiers() & Qt.ControlModifier:
            url = self._url_under(e.pos())
            if url:
                self._open_url(url)
                return
        if e.button() == Qt.LeftButton:
            l, c = self._cell_at(e.pos())
            self._selection = (l, c, l, c)
            self.update()
        elif e.button() == Qt.MiddleButton:
            self._paste_clipboard()
        elif e.button() == Qt.RightButton:
            self._context_menu(e.globalPos())

    def _context_menu(self, global_pos) -> None:
        menu = QMenu(self)
        url = self._url_under(self.mapFromGlobal(global_pos))
        if url:
            open_act = QAction("打开链接", self)
            open_act.triggered.connect(lambda: self._open_url(url))
            copy_act = QAction("复制链接", self)
            copy_act.triggered.connect(
                lambda: QApplication.clipboard().setText(url))
            menu.addAction(open_act)
            menu.addAction(copy_act)
            menu.addSeparator()
        if self._has_selection():
            copy_act = QAction("复制", self)
            copy_act.triggered.connect(self.copy)
            menu.addAction(copy_act)
        paste_act = QAction("粘贴（括号模式）", self)
        paste_act.triggered.connect(self._paste_clipboard)
        menu.addAction(paste_act)
        menu.exec_(global_pos)

    def mouseMoveEvent(self, e) -> None:
        if self._selection is not None and e.buttons() & Qt.LeftButton:
            l, c = self._cell_at(e.pos())
            al, ac = self._selection[0], self._selection[1]
            self._selection = (al, ac, l, c)
            self.update()

    def wheelEvent(self, e) -> None:
        steps = -e.angleDelta().y() / 120.0
        sb = self._scrollbar
        sb.setValue(sb.value() + int(steps * 3))

    # ---------------- 键盘 ----------------

    def keyPressEvent(self, e) -> None:
        mods = e.modifiers()
        # Ctrl+F：终端内搜索
        if mods & Qt.ControlModifier and e.key() == Qt.Key_F:
            self._show_search()
            return
        # Esc：搜索条打开时优先关闭搜索，否则发送 ESC 序列
        if e.key() == Qt.Key_Escape and self._search_bar.isVisible():
            self.close_search()
            return
        # Ctrl+C：有选中文本则复制，否则发送中断信号 \x03
        if mods & Qt.ControlModifier and e.key() == Qt.Key_C:
            if self._has_selection():
                self.copy()
            else:
                self.dataSent.emit(b"\x03")
            return
        if e.matches(QKeySequence.Copy):  # Ctrl+Shift+C
            self.copy()
            return
        if mods & Qt.ControlModifier and e.key() == Qt.Key_V:
            self._paste_clipboard()
            return
        if mods & Qt.ControlModifier and e.key() == Qt.Key_A:
            self.selectAll()
            return
        if mods & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
            return
        key = e.key()
        # PgUp/PgDn 滚动本地回看缓冲；Shift+PgUp/PgDn 发送序列给远端
        if key == Qt.Key_PageUp and not (mods & Qt.ShiftModifier):
            self._scroll_page(up=True)
            return
        if key == Qt.Key_PageDown and not (mods & Qt.ShiftModifier):
            self._scroll_page(up=False)
            return
        seq = None
        if key in (Qt.Key_Return, Qt.Key_Enter):
            seq = b"\r"
        elif key == Qt.Key_Backspace:
            seq = b"\x7f"
        elif key == Qt.Key_Tab:
            seq = b"\t"
        elif key == Qt.Key_Escape:
            seq = b"\x1b"
        elif key == Qt.Key_Up:
            seq = b"\x1b[A"
        elif key == Qt.Key_Down:
            seq = b"\x1b[B"
        elif key == Qt.Key_Right:
            seq = b"\x1b[C"
        elif key == Qt.Key_Left:
            seq = b"\x1b[D"
        elif key == Qt.Key_Home:
            seq = b"\x1b[H"
        elif key == Qt.Key_End:
            seq = b"\x1b[F"
        elif key == Qt.Key_PageUp:
            seq = b"\x1b[5~"
        elif key == Qt.Key_PageDown:
            seq = b"\x1b[6~"
        elif key == Qt.Key_Insert:
            seq = b"\x1b[2~"
        elif key == Qt.Key_Delete:
            seq = b"\x1b[3~"
        if seq is not None:
            self.dataSent.emit(seq)
            return
        text = e.text()
        if text and all(ord(c) >= 0x20 for c in text):
            self.dataSent.emit(text.encode("utf-8"))

    # ---------------- 尺寸 ----------------

    def update_pty_size(self, force: bool = False) -> None:
        fm = QFontMetricsF(self.font())
        cw = fm.horizontalAdvance("M")
        ch = fm.lineSpacing()
        avail_w = max(40, self.width() - self._scrollbar.width())
        avail_h = max(24, self.height())
        cols = max(2, int(avail_w / cw))
        rows = max(1, int(avail_h / ch))
        self._cw, self._ch = float(cw), float(ch)
        if force or (cols, rows) != (self._cols, self._rows):
            self._cols, self._rows = cols, rows
            self._screen.resize(rows, cols)
            self.ptySizeChanged.emit(cols, rows)
            self._sync_scrollbar()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._scrollbar.setGeometry(
            self.width() - self._scrollbar.sizeHint().width(), 0,
            self._scrollbar.sizeHint().width(), self.height())
        self.update_pty_size()
        self._place_search_bar()

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self._cursor_blink.start()

    def hideEvent(self, e) -> None:
        self._cursor_blink.stop()
        super().hideEvent(e)
