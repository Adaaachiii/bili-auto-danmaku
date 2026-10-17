"""登录对话框：扫码登录或粘贴 Cookie。"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import bili_api
from .bili_api import AccountInfo, Credentials, QrState
from .ui_common import StatusColors, qr_pixmap, run_async, set_colored


def fetch_account(creds: Credentials) -> AccountInfo | None:
    return bili_api.get_account_info(bili_api.new_session(), creds)


# ---------------------------------------------------------------- 登录

COOKIE_STEPS = (
    "1. 用 Edge 打开 www.bilibili.com 并登录<br>"
    "2. 按 F12 打开开发者工具，顶部选“应用程序”（可能收在 » 里）<br>"
    "3. 左侧展开“Cookie”，点 https://www.bilibili.com<br>"
    "4. 找到 <b>SESSDATA</b> 和 <b>bili_jct</b> 两行，把值按下面的格式填进来：<br>"
    "&nbsp;&nbsp;&nbsp;&nbsp;<code>SESSDATA=复制的值; bili_jct=复制的值</code><br>"
    "（直接粘贴整段 Cookie，或者从表格里整行复制也可以）"
)


class LoginDialog(QDialog):
    """扫码或粘贴 Cookie 登录。成功后 credentials 和 account 有值。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("登录 B 站账号")
        self.setMinimumWidth(500)
        self.credentials: Credentials | None = None
        self.account: AccountInfo | None = None
        self._colors = StatusColors()
        self._session = bili_api.new_session()
        self._qr_key: str | None = None
        self._polling = False

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_qr_page(), "扫码登录")
        self.tabs.addTab(self._build_cookie_page(), "粘贴 Cookie")

        cancel_btn = QPushButton("取消")
        cancel_btn.setAutoDefault(False)
        cancel_btn.clicked.connect(self.reject)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(cancel_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)
        layout.addLayout(bottom)

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(2000)
        self._poll_timer.timeout.connect(self._poll_qr)
        self._request_qr()

    def _build_qr_page(self) -> QWidget:
        page = QWidget()
        tip = QLabel("打开哔哩哔哩手机 App，用“扫一扫”扫描下面的二维码")
        tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_label = QLabel()
        self.qr_label.setFixedSize(240, 240)
        self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_label.setStyleSheet("background: white; color: #6E7781; border: 1px solid #D0D7DE; border-radius: 6px;")
        self.qr_status = QLabel()
        self.qr_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_refresh = QPushButton("刷新二维码")
        self.qr_refresh.setAutoDefault(False)
        self.qr_refresh.clicked.connect(self._request_qr)

        layout = QVBoxLayout(page)
        layout.setSpacing(10)
        layout.addWidget(tip)
        layout.addWidget(self.qr_label, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.qr_status)
        layout.addWidget(self.qr_refresh, 0, Qt.AlignmentFlag.AlignHCenter)
        return page

    def _build_cookie_page(self) -> QWidget:
        page = QWidget()
        steps = QLabel(COOKIE_STEPS)
        steps.setTextFormat(Qt.TextFormat.RichText)
        steps.setWordWrap(True)
        self.cookie_edit = QPlainTextEdit()
        self.cookie_edit.setPlaceholderText("SESSDATA=...; bili_jct=...")
        self.cookie_edit.setFixedHeight(90)
        self.cookie_status = QLabel()
        self.cookie_status.setWordWrap(True)
        self.cookie_btn = QPushButton("验证并登录")
        self.cookie_btn.setAutoDefault(False)
        self.cookie_btn.clicked.connect(self._submit_cookie)

        row = QHBoxLayout()
        row.addWidget(self.cookie_status, 1)
        row.addWidget(self.cookie_btn)
        layout = QVBoxLayout(page)
        layout.setSpacing(10)
        layout.addWidget(steps)
        layout.addWidget(self.cookie_edit)
        layout.addLayout(row)
        layout.addStretch(1)
        return page

    # ---- 扫码

    def _request_qr(self) -> None:
        self._poll_timer.stop()
        self._qr_key = None
        self.qr_label.setPixmap(QPixmap())
        self.qr_label.setText("正在获取二维码…")
        set_colored(self.qr_status, "")
        self.qr_refresh.setEnabled(False)
        run_async(self, bili_api.qr_generate, self._session, on_done=self._on_qr_ready, on_error=self._on_qr_failed)

    def _on_qr_ready(self, result) -> None:
        url, key = result
        self._qr_key = key
        self.qr_label.setPixmap(qr_pixmap(url, 220))
        set_colored(self.qr_status, "等待扫码…", self._colors.muted)
        self.qr_refresh.setEnabled(True)
        self._poll_timer.start()

    def _on_qr_failed(self, message: str) -> None:
        self.qr_label.setText("获取二维码失败")
        set_colored(self.qr_status, message, self._colors.error)
        self.qr_refresh.setEnabled(True)

    def _poll_qr(self) -> None:
        if self._polling or not self._qr_key:
            return
        self._polling = True
        key = self._qr_key
        run_async(
            self,
            bili_api.qr_poll,
            self._session,
            key,
            on_done=lambda result: self._on_qr_polled(key, result),
            on_error=self._on_qr_poll_failed,
        )

    def _on_qr_polled(self, key: str, result) -> None:
        self._polling = False
        if key != self._qr_key:  # 已经换了新二维码
            return
        state, creds = result
        if state is QrState.WAITING:
            set_colored(self.qr_status, "等待扫码…", self._colors.muted)
        elif state is QrState.SCANNED:
            set_colored(self.qr_status, "已扫码，请在手机上点“确认登录”", self._colors.ok)
        elif state is QrState.EXPIRED:
            self._poll_timer.stop()
            self.qr_label.setPixmap(QPixmap())
            self.qr_label.setText("二维码已过期")
            set_colored(self.qr_status, "二维码已过期，请点“刷新二维码”", self._colors.warn)
        else:
            self._poll_timer.stop()
            set_colored(self.qr_status, "扫码成功，正在验证账号…", self._colors.ok)
            self._verify(creds, self.qr_status)

    def _on_qr_poll_failed(self, message: str) -> None:
        self._polling = False
        set_colored(self.qr_status, f"{message}（会自动重试）", self._colors.warn)

    # ---- 粘贴 Cookie

    def _submit_cookie(self) -> None:
        try:
            creds = Credentials.from_cookie_text(self.cookie_edit.toPlainText())
        except ValueError as e:
            set_colored(self.cookie_status, str(e), self._colors.error)
            return
        self.cookie_btn.setEnabled(False)
        set_colored(self.cookie_status, "正在验证…", self._colors.muted)
        self._verify(creds, self.cookie_status)

    # ---- 验证

    def _verify(self, creds: Credentials, label: QLabel) -> None:
        run_async(
            self,
            fetch_account,
            creds,
            on_done=lambda account: self._on_verified(creds, account, label),
            on_error=lambda message: self._on_verify_failed(message, label),
        )

    def _on_verified(self, creds: Credentials, account: AccountInfo | None, label: QLabel) -> None:
        if account is None:
            self.cookie_btn.setEnabled(True)
            text = "Cookie 无效或已过期，请重新复制" if label is self.cookie_status else "登录没有成功，请刷新二维码重试"
            set_colored(label, text, self._colors.error)
            return
        self.credentials, self.account = creds, account
        self.accept()

    def _on_verify_failed(self, message: str, label: QLabel) -> None:
        self.cookie_btn.setEnabled(True)
        set_colored(label, f"验证失败：{message}", self._colors.error)

    def done(self, result: int) -> None:
        self._poll_timer.stop()
        super().done(result)
