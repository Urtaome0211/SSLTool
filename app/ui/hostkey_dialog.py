"""主机密钥确认对话框：首次连接信任（TOFU）与密钥变更警告。

威胁模型：SSH 主机密钥是服务器身份的唯一凭据。首次连接时用户应核对指纹
（可与服务器管理员公布的指纹比对）；已记录的主机密钥一旦变化，
大概率意味着中间人攻击，必须醒目警告。
"""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (QDialog, QDialogButtonBox, QFrame, QLabel,
                             QVBoxLayout)


def _fp_box(title: str, fingerprint: str) -> QFrame:
    """指纹展示卡片：标题 + 等宽可复制的指纹文本。"""
    box = QFrame()
    box.setObjectName("card")
    v = QVBoxLayout(box)
    v.setContentsMargins(12, 10, 12, 10)
    v.setSpacing(4)
    head = QLabel(title)
    head.setStyleSheet("color: #909399; font-size: 12px;")
    fp = QLabel(fingerprint)
    fp.setTextInteractionFlags(Qt.TextSelectableByMouse)
    fp.setStyleSheet(
        "font-family: Consolas, 'Courier New', monospace; font-size: 13px;")
    v.addWidget(head)
    v.addWidget(fp)
    return box


class HostKeyDialog(QDialog):
    """expected=None 表示首次连接（未知密钥）；否则为密钥变更警告。"""

    def __init__(self, parent, host, port, fingerprint, expected=None):
        super().__init__(parent)
        self.setModal(True)
        self.setMinimumWidth(540)
        if expected is None:
            self.setWindowTitle("首次连接：确认服务器主机密钥")
        else:
            self.setWindowTitle("警告：服务器主机密钥已变更！")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        target = QLabel(f"服务器：{host}:{port}")
        target.setStyleSheet("font-weight: 600; font-size: 14px;")
        layout.addWidget(target)

        if expected is None:
            desc = QLabel(
                "这是首次连接到该服务器，SSLTool 还没有它的主机密钥记录，\n"
                "无法验证其身份。请核对下方指纹（可向服务器管理员索取），\n"
                "确认无误后再连接。指纹一旦确认将被保存，此后密钥变化会收到警告。")
            desc.setWordWrap(True)
            layout.addWidget(desc)
            layout.addWidget(_fp_box("服务器主机密钥 SHA-256 指纹", fingerprint))
        else:
            warn = QLabel(
                "⚠ 该服务器的主机密钥与既有记录不一致！\n"
                "这可能意味着你正在遭受中间人攻击（有人试图截获你的会话和凭据），\n"
                "也可能是服务器重装/更换了密钥。请务必先向服务器管理员核实！")
            warn.setWordWrap(True)
            warn.setStyleSheet("color: #f56c6c; font-weight: 600;")
            layout.addWidget(warn)
            layout.addWidget(_fp_box("此前记录的指纹", expected))
            layout.addWidget(_fp_box("本次连接收到的指纹", fingerprint))

        buttons = QDialogButtonBox()
        if expected is None:
            accept_btn = buttons.addButton("接受并连接",
                                           QDialogButtonBox.AcceptRole)
        else:
            accept_btn = buttons.addButton("仍然连接（接受新密钥）",
                                           QDialogButtonBox.AcceptRole)
            accept_btn.setStyleSheet("color: #f56c6c; font-weight: 600;")
        buttons.addButton("取消", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


def confirm_unknown_host_key(parent, host, port, fingerprint) -> bool:
    """首次连接：请用户确认主机密钥。返回 True 表示接受。"""
    return HostKeyDialog(parent, host, port, fingerprint, None).exec_() \
        == QDialog.Accepted


def confirm_changed_host_key(parent, host, port, fingerprint, expected) -> bool:
    """密钥变更：醒目警告后请用户决定。返回 True 表示仍要连接。"""
    return HostKeyDialog(parent, host, port, fingerprint, expected).exec_() \
        == QDialog.Accepted
