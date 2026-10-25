"""一键更新的窗口：显示新版本的更新说明，点“下载并更新”后下载、核对、解压，准备好了就关闭窗口。

之后由主窗口启动新版本去替换文件（和“从压缩包更新”同一套流程，出错会恢复旧版本）。
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import update_check, updater
from .config import APP_VERSION, _writable
from .ui_common import StatusColors, hint_label, set_colored


class _Signals(QObject):
    """后台线程把进度和结果送回界面线程。在界面线程创建，不挂在窗口上，窗口先关了也不会出问题。"""

    progress = Signal(int, int)  # （已下载字节, 总字节）
    stage = Signal(str)
    done = Signal(object)  # updater.StagedUpdate
    failed = Signal(str)
    cancelled = Signal()


def _plain_notes(markdown: str) -> str:
    """发布说明是 Markdown，按纯文字显示，把加粗和代码的记号去掉好读一些。"""
    return re.sub(r"\*\*|`", "", markdown).strip() or "（这个版本没有写更新说明）"


class UpdateDialog(QDialog):
    """确定（准备好新版本）后用 staged 取结果；取消、出错时 staged 是 None。"""

    def __init__(self, parent: QWidget | None, release: update_check.Release, target: Path, sending: bool):
        super().__init__(parent)
        self.setWindowTitle(f"更新到 {release.version}")
        self.resize(560, 460)
        self._release = release
        self._target = target
        self._colors = StatusColors()
        self._cancel = threading.Event()
        self._busy = ""  # "" 没在干活；"download" 下载中（可以取消）；"prepare" 解压检查中（不能取消）
        self.staged: updater.StagedUpdate | None = None

        title = QLabel(f"新版本 {release.version}（现在是 {APP_VERSION}）")
        title.setStyleSheet("font-size: 11pt; font-weight: bold;")
        notes = QPlainTextEdit(_plain_notes(release.notes))
        notes.setReadOnly(True)
        detail = (
            f"会从 GitHub 下载 {release.size / 2**20:.1f} MB 的压缩包，核对无误后程序关闭并自动重新打开，"
            "直播间、收藏夹、历史记录等数据不受影响。替换中出错会自动恢复成旧版本。"
        )
        if sending:
            detail += "\n正在发送的直播间会中断十几秒，重新打开后照常处理。"
        self.progress = QProgressBar()
        self.progress.setRange(0, max(1, release.size // 1024))
        self.progress.setTextVisible(False)
        self.progress.hide()
        self.status = hint_label()

        self.buttons = QDialogButtonBox()
        self.start_btn = self.buttons.addButton("下载并更新", QDialogButtonBox.ButtonRole.AcceptRole)
        self.start_btn.setDefault(True)
        page_btn = QPushButton("打开下载页")
        page_btn.setAutoDefault(False)
        self.buttons.addButton(page_btn, QDialogButtonBox.ButtonRole.ActionRole)
        self.cancel_btn = self.buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        self.start_btn.clicked.connect(self._start)
        page_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(release.page_url)))
        self.cancel_btn.clicked.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(QLabel("更新说明："))
        layout.addWidget(notes, 1)
        layout.addWidget(hint_label(detail))
        layout.addWidget(self.progress)
        layout.addWidget(self.status)
        layout.addWidget(self.buttons)

        if not _writable(target):
            self.start_btn.setEnabled(False)
            set_colored(
                self.status,
                f"程序所在的文件夹不能写入（{target}），没法自动更新。可以退出程序，把整个程序文件夹挪到 D 盘等位置，"
                "或者点“打开下载页”下载压缩包，退出程序后手动覆盖（保留 data 文件夹）。",
                self._colors.error,
            )

    # ------------------------------------------------------------ 下载 → 核对 → 解压

    def _start(self) -> None:
        self._busy = "download"
        self.start_btn.setEnabled(False)
        self.progress.show()
        set_colored(self.status, "正在下载…", self._colors.muted)
        signals = _Signals()
        signals.progress.connect(self._on_progress)
        signals.stage.connect(self._on_stage)
        signals.done.connect(self._on_done)
        signals.failed.connect(self._on_failed)
        signals.cancelled.connect(self._on_cancelled)
        release, target, cancel = self._release, self._target, self._cancel

        def emit(signal, *args) -> None:
            try:
                signal.emit(*args)
            except RuntimeError:
                pass  # 程序正在退出

        def work() -> None:
            zip_path = target / updater.DOWNLOAD_NAME
            try:
                update_check.download(release, zip_path, lambda got, total: emit(signals.progress, got, total), cancel)
                if cancel.is_set():
                    raise update_check.DownloadCancelled("已取消下载")
                emit(signals.stage, "下载完成，校验通过，正在解压和检查新版本…")
                try:
                    staged = updater.prepare_from_zip(zip_path, target)
                finally:
                    zip_path.unlink(missing_ok=True)
                if staged.version != release.version:
                    updater.discard(target)
                    raise update_check.UpdateCheckError(f"下载的压缩包里是 {staged.version}，不是 {release.version}，没有更新")
                if cancel.is_set():  # 解压期间窗口被关掉了
                    updater.discard(target)
                    raise update_check.DownloadCancelled("已取消更新")
                emit(signals.done, staged)
            except update_check.DownloadCancelled:
                emit(signals.cancelled)
            except Exception as e:  # 界面只需要一句能看懂的话
                emit(signals.failed, str(e) or e.__class__.__name__)

        threading.Thread(target=work, name="update-download", daemon=True).start()

    def _on_progress(self, got: int, total: int) -> None:
        self.progress.setValue(got // 1024)
        self.status.setText(f"正在下载… {got / 2**20:.1f} / {total / 2**20:.1f} MB")

    def _on_stage(self, text: str) -> None:
        self._busy = "prepare"
        self.cancel_btn.setEnabled(False)  # 解压到一半不能停
        self.progress.setRange(0, 0)  # 转圈
        self.status.setText(text)

    def _on_done(self, staged: updater.StagedUpdate) -> None:
        self._busy = ""
        self.staged = staged
        self.accept()

    def _on_failed(self, message: str) -> None:
        self._busy = ""
        self.progress.hide()
        self.progress.setRange(0, max(1, self._release.size // 1024))
        self.cancel_btn.setEnabled(True)
        self.start_btn.setEnabled(True)
        self.start_btn.setText("重试")
        set_colored(self.status, f"没有更新：{message}", self._colors.error)

    def _on_cancelled(self) -> None:
        self._busy = ""
        if self.isVisible():
            super().reject()

    def reject(self) -> None:
        if self._busy == "prepare":
            return  # 解压到一半，等它完成
        if self._busy == "download":
            self._cancel.set()  # 后台线程停下后会删掉下载了一半的文件
            set_colored(self.status, "正在取消…", self._colors.muted)
            return  # 等 cancelled 信号再关窗口
        super().reject()

    def closeEvent(self, event) -> None:
        if self._busy:
            event.ignore()
            self.reject()
            return
        super().closeEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape and self._busy:
            self.reject()
            return
        super().keyPressEvent(event)
