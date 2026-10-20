"""会话日志：把终端会话输出原样记录到 ~/.ssltool/logs/ 下。

- 每个会话一个 UTF-8 日志文件，文件名 = 时间戳_会话名（会话名做安全化处理）；
- write() 记录服务器原始输出（解码失败用 U+FFFD 替换，绝不因此中断终端）；
- note() 写入带时间戳的事件行（连接/断开/错误等）；
- 所有写盘异常一律吞掉：日志是增值功能，绝不能影响终端主流程。
"""
import re
import time

from app import config

_SAFE_RE = re.compile(r"[^\w.\-\u4e00-\u9fff]+")
_MAX_NAME = 40


def logs_dir():
    """日志目录（跟随 config.DATA_DIR，测试重定向 DATA_DIR 即生效）。"""
    return config.DATA_DIR / "logs"


class SessionLog:
    """一个会话的日志文件句柄。构造即打开文件并写开始事件。"""

    def __init__(self, name: str, host: str, port: int, user: str = ""):
        d = logs_dir()
        d.mkdir(parents=True, exist_ok=True)
        safe = _SAFE_RE.sub("_", (name or "session").strip())[:_MAX_NAME]
        safe = safe.strip("_") or "session"
        ts = time.strftime("%Y%m%d_%H%M%S")
        self.path = d / f"{ts}_{safe}.log"
        try:
            self._f = open(self.path, "w", encoding="utf-8",
                           errors="replace")
        except OSError:
            self._f = None
        who = f"{user}@{host}:{port}" if user else f"{host}:{port}"
        self.note(f"会话开始: {who}")

    @property
    def active(self) -> bool:
        return self._f is not None and not self._f.closed

    def write(self, data: bytes) -> None:
        """记录终端原始输出字节。任何异常吞掉，不影响终端。"""
        if not self.active:
            return
        try:
            self._f.write(data.decode("utf-8", "replace"))
            self._f.flush()
        except Exception:
            pass

    def note(self, text: str) -> None:
        """写一行带时间戳的事件注释。"""
        if not self.active:
            return
        try:
            self._f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                         text))
            self._f.flush()
        except Exception:
            pass

    def close(self) -> None:
        if not self.active:
            return
        try:
            self.note("会话结束")
            self._f.flush()
            self._f.close()
        except Exception:
            pass
        finally:
            self._f = None
