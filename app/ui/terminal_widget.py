"""完整 VT 终端组件：pyte（VT100/VT220）终端模拟 + QPainter 自绘。

支持完整的 ANSI/VT 控制序列（光标定位、清屏、SGR 颜色、alternate 光标
保存/恢复等），因此 vim / top / htop 等全屏程序可以正常使用。
自带滚动回看缓冲（5000 行）与本地回显去重（见 _echo_expect）。
"""
import unicodedata

import pyte
from PyQt5.QtCore import QRectF, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import (QColor, QFont, QFontMetricsF, QKeySequence,
                         QPainter)
from PyQt5.QtWidgets import QApplication, QScrollBar, QWidget

# xterm 标准 16 色
_XTERM16 = [
    "#000000", "#cd0000", "#00cd00", "#cdcd00",
    "#0000ee", "#cd00cd", "#00cdcd", "#e5e5e5",
    "#7f7f7f", "#ff0000", "#00ff00", "#ffff00",
    "#5c5cff", "#ff00ff", "#00ffff", "#ffffff",
]

# 粗体 + 标准色（前 8 色）→ 对应亮色（VT 惯例；pyte 不自动做此映射）
_BOLD_BRIGHT = {
    "000000": "7f7f7f", "cd0000": "ff0000", "00cd00": "00ff00",
    "cdcd00": "ffff00", "0000ee": "5c5cff", "cd00cd": "ff00ff",
    "00cdcd": "00ffff", "e5e5e5": "ffffff",
}

_BG = QColor("#1e1e1e")   # 默认背景
_FG = QColor("#d4d4d4")   # 默认前景

_BLINK_MS = 600        # 光标闪烁周期


def _xterm256(n: int) -> str:
    if n < 16:
        return _XTERM16[n]
    if 16 <= n <= 231:
        n -= 16
        r, g, b = n // 36, (n // 6) % 6, n % 6

        def v(c):
            return 0 if c == 0 else 55 + c * 40

        return "#{:02x}{:02x}{:02x}".format(v(r), v(g), v(b))
    if 232 <= n <= 255:
        g = 8 + (n - 232) * 10
        return "#{:02x}{:02x}{:02x}".format(g, g, g)
    return "#d4d4d4"


def _echo_expect(data: bytes) -> bytes:
    """近似 ICANON+ECHO+ECHOE+ECHOCTL 的服务器回显模型：返回"需要抑制"的字节。

    本地回显开启时，键入字符已在本地显示，服务器若再回显同一内容会重复。
    这里按常见 pty 配置预测服务器将回显什么，供 feed_remote 前缀匹配去重：
    - 可见字符 → 原样（抑制）
    - Backspace(\x7f) → "\\b \\b"（ECHOE 回显序列，抑制）
    - Ctrl+C(\x03) → "^C"（ECHOCTL，抑制；其后的 \\r\\n 放行推进换行）
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


class _SSLToolScreen(pyte.HistoryScreen):
    """在 HistoryScreen 基础上：ED 2（清屏）同时清空滚动缓冲。

    与旧实现（QPlainTextEdit.clear()）的行为保持一致，冒烟测试
    「CSI 2J 清屏」断言 toPlainText() 为空依赖这一点。
    """

    def erase_in_display(self, how=0, *args, **kwargs):
        super().erase_in_display(how, *args, **kwargs)
        if how in (2, 3):
            self.history.top.clear()
            self.history.bottom.clear()
            self.history = self.history._replace(position=self.history.size)


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
        """滚动缓冲 + 当前屏幕的全部行（历史行是快照引用，不会被后续修改）。"""
        return list(self._screen.history.top) + [
            self._screen.buffer[y] for y in range(self._screen.lines)]

    def _line_str(self, line) -> str:
        parts = []
        for col in range(self._cols):
            ch = line[col].data
            if ch:
                parts.append(ch)
        return "".join(parts).rstrip()

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

    def _paste_clipboard(self) -> None:
        text = QApplication.clipboard().text()
        if text:
            self.dataSent.emit(text.encode("utf-8"))

    def _has_selection(self) -> bool:
        return self._selection is not None

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
            if self._in_selection(abs_line, col):
                fg, bg = bg, fg
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
        total = len(self._screen.history.top) + self._rows
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

    def mousePressEvent(self, e) -> None:
        self.setFocus()
        if e.button() == Qt.LeftButton:
            l, c = self._cell_at(e.pos())
            self._selection = (l, c, l, c)
            self.update()
        elif e.button() == Qt.MiddleButton:
            self._paste_clipboard()

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

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self._cursor_blink.start()

    def hideEvent(self, e) -> None:
        self._cursor_blink.stop()
        super().hideEvent(e)
