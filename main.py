"""SSLTool 入口。"""
import datetime
import sys
import traceback

from PyQt5.QtWidgets import QApplication

from app import config
from app.theme import apply_theme, make_app_icon
from app.ui.main_window import MainWindow


def _enable_crash_log():
    """把未捕获异常与 C 层崩溃（段错误 / abort）写入 ~/.ssltool/error.log。

    闪退通常没有控制台输出，此日志是排查闪退的关键依据。"""
    try:
        config.ensure_data_dir()
        log = open(str(config.DATA_DIR / "error.log"), "a", encoding="utf-8")
        log.write(f"\n==== SSLTool 启动 {datetime.datetime.now()} ====\n")
        log.flush()
        import faulthandler
        faulthandler.enable(log)

        def hook(exc_type, exc, tb):
            log.write(f"[{datetime.datetime.now()}] 未捕获异常:\n")
            traceback.print_exception(exc_type, exc, tb, file=log)
            log.flush()
            sys.__excepthook__(exc_type, exc, tb)

        sys.excepthook = hook
        return log
    except Exception:
        return None


def main():
    _crash_log = _enable_crash_log()
    app = QApplication(sys.argv)
    app.setApplicationName("SSLTool")
    app.setOrganizationName("SSLTool")
    app.setWindowIcon(make_app_icon())
    apply_theme(app)
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
