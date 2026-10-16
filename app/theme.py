"""扁平化主题：浅色 / 深色两套 QSS 与调色板、应用图标。"""
from pathlib import Path
from string import Template

from PyQt5.QtCore import QRect, Qt
from PyQt5.QtGui import QColor, QFont, QIcon, QPainter, QPixmap

ASSETS_DIR = Path(__file__).resolve().parent / "assets"

# 调色板：QSS 模板中的 ${key} 占位符
_LIGHT = {
    "bg": "#f5f7fa", "bg2": "#fafbfc", "card": "#ffffff",
    "border": "#e4e7ed", "input_border": "#dcdfe6",
    "text": "#303133", "text2": "#606266", "text3": "#909399",
    "disabled_bg": "#f5f7fa", "disabled_fg": "#c0c4cc",
    "accent": "#409eff", "accent_hover": "#66b1ff", "accent_press": "#3a8ee6",
    "accent_disabled": "#a0cfff", "accent_bg": "#ecf5ff", "accent_border": "#c6e2ff",
    "hover": "#f5f7fa", "scroll": "#c8cdd6", "scroll_hover": "#a8aeb8",
    "danger": "#f56c6c", "danger_hover": "#f78989", "danger_press": "#e45656",
    "progress_bg": "#f0f2f5", "tooltip_bg": "#303133", "tooltip_fg": "#ffffff",
}

_DARK = {
    "bg": "#1e2126", "bg2": "#23262b", "card": "#26292f",
    "border": "#3a3e45", "input_border": "#454a52",
    "text": "#d8dbe0", "text2": "#a9adb5", "text3": "#7d828b",
    "disabled_bg": "#2b2e34", "disabled_fg": "#5a5f68",
    "accent": "#409eff", "accent_hover": "#66b1ff", "accent_press": "#3a8ee6",
    "accent_disabled": "#3a5f8a", "accent_bg": "#20364d", "accent_border": "#2f567d",
    "hover": "#2e3239", "scroll": "#4a4f57", "scroll_hover": "#5d636d",
    "danger": "#f56c6c", "danger_hover": "#f78989", "danger_press": "#e45656",
    "progress_bg": "#33373e", "tooltip_bg": "#3c4148", "tooltip_fg": "#ffffff",
}

_QSS = """
* { font-family: "Segoe UI", "Microsoft YaHei", "PingFang SC", "Helvetica Neue", sans-serif; font-size: 13px; }
QMainWindow, QDialog { background-color: ${bg}; }
QLabel { color: ${text}; background: transparent; }
QLabel#sidebarTitle { font-size: 18px; font-weight: 700; color: ${text}; }
QLabel#sidebarSubtitle { color: ${text3}; font-size: 12px; }
QLabel#sidebarSectionLabel { color: ${text2}; font-weight: 600; }
QLabel#hint { color: ${text3}; font-size: 13px; }
QLabel#welcomeTitle { font-size: 34px; font-weight: 700; color: ${text}; }
QLabel#welcomeSub { font-size: 15px; color: ${text3}; }
QLabel#welcomeFeats { color: ${text2}; }

QFrame#sidebar { background-color: ${card}; border-right: 1px solid ${border}; }
QFrame#card { background-color: ${card}; border: 1px solid ${border}; border-radius: 8px; }
QFrame#echoBar { background-color: ${card}; border-bottom: 1px solid ${border}; }

QLineEdit, QComboBox, QSpinBox {
    background-color: ${card}; border: 1px solid ${input_border}; border-radius: 4px;
    padding: 6px 10px; color: ${text};
    selection-background-color: ${accent}; selection-color: #ffffff;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border-color: ${accent}; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled { background-color: ${disabled_bg}; color: ${disabled_fg}; }
QComboBox::drop-down { border: none; width: 26px; }
QComboBox QAbstractItemView {
    background-color: ${card}; border: 1px solid ${input_border}; border-radius: 4px;
    selection-background-color: ${accent_bg}; selection-color: ${accent}; outline: none;
}
QSpinBox::up-button, QSpinBox::down-button { border: none; background: transparent; width: 18px; }

QPushButton {
    background-color: ${accent}; color: #ffffff; border: none; border-radius: 4px;
    padding: 7px 16px; font-weight: 500;
}
QPushButton:hover { background-color: ${accent_hover}; }
QPushButton:pressed { background-color: ${accent_press}; }
QPushButton:disabled { background-color: ${accent_disabled}; color: #ffffff; }
QPushButton#secondary { background-color: ${card}; color: ${text2}; border: 1px solid ${input_border}; font-weight: 400; }
QPushButton#secondary:hover { color: ${accent}; border-color: ${accent_border}; background-color: ${accent_bg}; }
QPushButton#secondary:pressed { color: ${accent_press}; }
QPushButton#secondary:disabled { color: ${disabled_fg}; background-color: ${disabled_bg}; border-color: ${border}; }
QPushButton#danger { background-color: ${danger}; }
QPushButton#danger:hover { background-color: ${danger_hover}; }
QPushButton#danger:pressed { background-color: ${danger_press}; }
QPushButton#ghost { background-color: transparent; color: ${accent}; border: 1px solid ${accent}; }
QPushButton#ghost:hover { background-color: ${accent_bg}; }

QListWidget { background-color: ${card}; border: none; outline: none; }
QListWidget::item { padding: 8px 10px; margin: 2px 6px; border-radius: 6px; color: ${text}; }
QListWidget::item:hover { background-color: ${hover}; }
QListWidget::item:selected { background-color: ${accent_bg}; color: ${accent}; }

QTabWidget::pane { border: none; background-color: ${bg}; }
QTabBar { background-color: ${card}; border-bottom: 1px solid ${border}; }
QTabBar::tab { background: transparent; color: ${text2}; padding: 9px 18px; margin: 0; border: none; }
QTabBar::tab:hover { color: ${accent}; }
QTabBar::tab:selected { color: ${accent}; border-bottom: 2px solid ${accent}; font-weight: 600; }

QTreeView { background-color: ${card}; border: none; outline: none; alternate-background-color: ${bg2}; }
QTreeView::item { padding: 3px 2px; }
QTreeView::item:hover { background-color: ${hover}; }
QTreeView::item:selected { background-color: ${accent_bg}; color: ${accent}; }
QHeaderView::section {
    background-color: ${bg}; color: ${text2}; border: none; border-bottom: 1px solid ${border};
    padding: 6px 8px; font-weight: 600;
}

QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: ${scroll}; border-radius: 5px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: ${scroll_hover}; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 0; }
QScrollBar::handle:horizontal { background: ${scroll}; border-radius: 5px; min-width: 30px; }
QScrollBar::handle:horizontal:hover { background: ${scroll_hover}; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }

QSplitter::handle { background-color: ${border}; }
QSplitter::handle:horizontal { width: 1px; }
QSplitter::handle:vertical { height: 1px; }

QStatusBar { background-color: ${card}; border-top: 1px solid ${border}; color: ${text3}; }
QStatusBar::item { border: none; }

QMenu { background-color: ${card}; border: 1px solid ${border}; border-radius: 4px; padding: 4px; }
QMenu::item { padding: 6px 28px 6px 16px; border-radius: 4px; color: ${text}; }
QMenu::item:selected { background-color: ${accent_bg}; color: ${accent}; }
QMenu::separator { height: 1px; background: ${border}; margin: 4px 8px; }

QCheckBox { color: ${text}; spacing: 6px; background: transparent; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid ${input_border}; border-radius: 3px; background-color: ${card}; }
QCheckBox::indicator:hover { border-color: ${accent}; }
QCheckBox::indicator:checked { background-color: ${accent}; border-color: ${accent}; image: url("{CHECK}"); }
QCheckBox::indicator:disabled { background-color: ${disabled_bg}; border-color: ${border}; }

QToolTip { background-color: ${tooltip_bg}; color: ${tooltip_fg}; border: none; padding: 6px 8px; }
QProgressDialog { background-color: ${card}; }
QProgressBar { background-color: ${progress_bg}; border: none; border-radius: 6px; text-align: center; color: ${text2}; }
QProgressBar::chunk { background-color: ${accent}; border-radius: 6px; }
"""


def get_qss(dark: bool = False) -> str:
    pal = _DARK if dark else _LIGHT
    qss = Template(_QSS).safe_substitute(pal)
    check = (ASSETS_DIR / "check.svg").as_posix()
    return qss.replace("{CHECK}", check)


def apply_theme(app, dark: bool = False) -> None:
    app.setStyle("Fusion")
    app.setStyleSheet(get_qss(dark))


def make_app_icon():
    """程序内绘制扁平化应用图标（蓝底白色终端），返回 QIcon。"""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#409eff"))
    painter.drawRoundedRect(0, 0, 64, 64, 14, 14)
    painter.setBrush(QColor("#ffffff"))
    painter.drawRoundedRect(14, 20, 36, 26, 5, 5)
    painter.setPen(QColor("#409eff"))
    font = QFont("Consolas")
    font.setPixelSize(18)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(QRect(14, 20, 36, 26), Qt.AlignCenter, ">_")
    painter.end()
    return QIcon(pm)
