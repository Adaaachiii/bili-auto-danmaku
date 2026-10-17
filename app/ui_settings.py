"""设置页面（左侧功能栏里的“设置”）。"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .config import (
    APP_TITLE,
    APP_VERSION,
    BROWSER_CHOICES,
    CLOSE_CHOICES,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    ON_START_CHOICES,
    AppConfig,
    data_dir,
)
from .ui_common import StatusColors, hint_label, unit_row


def _combo(choices: dict[str, str]) -> QComboBox:
    combo = QComboBox()
    for key, label in choices.items():
        combo.addItem(label, key)
    return combo


class SettingsPage(QWidget):
    """点“保存设置”时发出 saved 信号，主窗口负责把设置应用到程序。"""

    saved = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        title = QLabel("设置")
        title.setStyleSheet("font-size: 11pt; font-weight: bold;")

        self.poll_spin = QSpinBox()
        self.poll_spin.setRange(MIN_POLL_INTERVAL, MAX_POLL_INTERVAL)
        self.poll_spin.setFixedWidth(100)
        self.on_start_combo = _combo(ON_START_CHOICES)
        self.browser_combo = _combo(BROWSER_CHOICES)
        self.close_combo = _combo(CLOSE_CHOICES)
        self.notify_check = QCheckBox("开播、下播或发送出错时弹出系统通知")
        self.autostart_check = QCheckBox("开机自动启动（启动后直接缩小到托盘）")

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setVerticalSpacing(10)
        form.addRow("检测间隔：", unit_row(self.poll_spin))
        form.addRow("", hint_label("检测间隔建议大于30秒"))
        form.addRow("若启动时监控直播间已开播：", self.on_start_combo)
        form.addRow("打开直播页用：", self.browser_combo)
        form.addRow("关闭窗口时：", self.close_combo)
        form.addRow("", self.notify_check)
        form.addRow("", self.autostart_check)

        folder = str(data_dir())
        folder_label = hint_label(f"配置、收藏夹和日志保存在：{folder}")
        folder_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        open_btn = QPushButton("打开文件夹")
        open_btn.clicked.connect(lambda: os.startfile(folder))
        folder_row = QHBoxLayout()
        folder_row.addWidget(folder_label, 1)
        folder_row.addWidget(open_btn)

        self.save_btn = QPushButton("保存设置")
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self.saved.emit)
        self.saved_label = QLabel()
        self.saved_label.setStyleSheet(f"color: {StatusColors().ok};")
        self._saved_timer = QTimer(self)
        self._saved_timer.setSingleShot(True)
        self._saved_timer.setInterval(2500)
        self._saved_timer.timeout.connect(self.saved_label.clear)
        save_row = QHBoxLayout()
        save_row.addWidget(self.save_btn)
        save_row.addWidget(self.saved_label)
        save_row.addStretch(1)

        content = QWidget()
        content.setMaximumWidth(720)
        box = QVBoxLayout(content)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(12)
        box.addLayout(form)
        box.addLayout(save_row)
        box.addSpacing(12)
        box.addLayout(folder_row)
        box.addWidget(hint_label(f"{APP_TITLE} {APP_VERSION}"))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)
        layout.addWidget(title)
        layout.addWidget(content)
        layout.addStretch(1)

    def load(self, cfg: AppConfig, autostart: bool) -> None:
        self.poll_spin.setValue(cfg.poll_interval_sec)
        self.on_start_combo.setCurrentIndex(self.on_start_combo.findData(cfg.on_start_live))
        self.browser_combo.setCurrentIndex(self.browser_combo.findData(cfg.browser))
        self.close_combo.setCurrentIndex(self.close_combo.findData(cfg.close_action))
        self.notify_check.setChecked(cfg.notify)
        self.autostart_check.setChecked(autostart)
        self.saved_label.clear()

    def apply_to(self, cfg: AppConfig) -> None:
        cfg.poll_interval_sec = self.poll_spin.value()
        cfg.on_start_live = self.on_start_combo.currentData()
        cfg.browser = self.browser_combo.currentData()
        cfg.close_action = self.close_combo.currentData()
        cfg.notify = self.notify_check.isChecked()

    def autostart_checked(self) -> bool:
        return self.autostart_check.isChecked()

    def show_saved(self) -> None:
        self.saved_label.setText("✓ 已保存")
        self._saved_timer.start()
