"""GUI 完整启动验证：离屏启动主窗口，2 秒后自动退出。"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QApplication

from app.theme import apply_theme, make_app_icon
from app.ui.main_window import MainWindow

app = QApplication(sys.argv)
app.setWindowIcon(make_app_icon())
apply_theme(app)
win = MainWindow()
win.show()
QTimer.singleShot(2000, app.quit)
sys.exit(app.exec_())
