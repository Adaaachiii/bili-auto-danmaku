"""设置页面（左侧功能栏里的“设置”）。"""

from __future__ import annotations

import os
import sys

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
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
    THEME_CHOICES,
    AppConfig,
    data_dir,
)
from .ui_common import StatusColors, help_icon, hint_label, make_primary, set_colored, titled_section


def _combo(choices: dict[str, str]) -> QComboBox:
    combo = QComboBox()
    for key, label in choices.items():
        combo.addItem(label, key)
    return combo


class SettingsPage(QWidget):
    """点“保存设置”时发出 saved 信号，主窗口负责把设置应用到程序。"""

    saved = Signal()
    update_requested = Signal()  # 点了“从压缩包更新”
    check_update_requested = Signal()  # 点了“检查更新”
    install_update_requested = Signal()  # 点了“更新到 x.y.z”
    notice_requested = Signal()  # 点了“使用须知”
    backup_requested = Signal()  # 点了“导出备份”
    restore_requested = Signal()  # 点了“从备份恢复”

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        title = QLabel("设置")
        title.setStyleSheet("font-size: 11pt; font-weight: bold;")

        self.poll_spin = QSpinBox()
        self.poll_spin.setRange(MIN_POLL_INTERVAL, MAX_POLL_INTERVAL)
        self.poll_spin.setFixedWidth(100)
        self.auto_monitor_check = QCheckBox("程序启动后自动检测直播间状态")
        self.send_on_startup_check = QCheckBox("开启软件后即开始发送弹幕")
        self.browser_combo = _combo(BROWSER_CHOICES)
        self.close_combo = _combo(CLOSE_CHOICES)
        self.theme_combo = _combo(THEME_CHOICES)
        # 下拉框只要放得下最长的选项就行，不铺满整行；几个一样宽，看着整齐
        combos = (self.browser_combo, self.close_combo, self.theme_combo)
        width = max(combo.sizeHint().width() for combo in combos)
        for combo in combos:
            combo.setFixedWidth(width)
        self.notify_check = QCheckBox("开播、下播或发送出错时弹出系统通知")
        self.autostart_check = QCheckBox("开机自动启动（启动后直接缩小到托盘）")
        self.auto_update_check = QCheckBox("自动检查更新")

        label_width = max(QLabel(text).sizeHint().width() for text in ("检测间隔", "打开直播页用", "关闭窗口时", "外观")) + 16

        def field(text: str) -> QLabel:
            label = hint_label(text)  # 次要文字色
            label.setWordWrap(False)
            label.setFixedWidth(label_width)
            return label

        def row(*items) -> QHBoxLayout:
            line = QHBoxLayout()
            line.setSpacing(8)
            for item in items:
                if isinstance(item, int):
                    line.addSpacing(item)
                else:
                    line.addWidget(item)
            line.addStretch(1)
            return line

        def short_hint(text: str) -> QLabel:
            label = hint_label(text)
            label.setWordWrap(False)
            return label

        # ---- 监控与发送
        monitor = QVBoxLayout()
        monitor.setSpacing(8)
        monitor.addLayout(row(field("检测间隔"), self.poll_spin, QLabel("秒"), 8, short_hint("检测间隔建议大于30秒")))
        monitor.addLayout(row(self.auto_monitor_check, help_icon("关闭后，程序启动时先暂停监控，点“继续监控”才开始检测和发送")))
        monitor.addLayout(row(self.send_on_startup_check, help_icon("程序打开时主播已经在播的直播间直接开始发送，不用点“本场开始发送”")))

        # ---- 窗口与通知
        window = QVBoxLayout()
        window.setSpacing(8)
        window.addLayout(row(field("打开直播页用"), self.browser_combo))
        window.addLayout(row(field("关闭窗口时"), self.close_combo))
        window.addLayout(row(field("外观"), self.theme_combo))
        window.addLayout(row(self.notify_check, help_icon("“只提醒”的直播间不受这个开关影响，开播、下播总会提醒")))
        window.addLayout(row(self.autostart_check))
        window.addLayout(row(self.auto_update_check))

        # ---- 保存
        self.save_btn = make_primary(QPushButton("保存设置"))
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self.saved.emit)
        self.saved_label = QLabel()
        self.saved_label.setStyleSheet(f"color: {StatusColors().ok};")
        self._saved_timer = QTimer(self)
        self._saved_timer.setSingleShot(True)
        self._saved_timer.setInterval(2500)
        self._saved_timer.timeout.connect(self.saved_label.clear)
        save_row = row(self.save_btn, self.saved_label)

        # ---- 数据
        folder = str(data_dir())
        folder_label = hint_label(f"配置、收藏夹和日志保存在：{folder}")
        folder_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        open_btn = QPushButton("打开文件夹")
        open_btn.clicked.connect(lambda: os.startfile(folder))
        folder_row = QHBoxLayout()
        folder_row.addWidget(folder_label, 1)
        folder_row.addWidget(open_btn)
        backup_btn = QPushButton("导出备份…")
        backup_btn.clicked.connect(self.backup_requested.emit)
        restore_btn = QPushButton("从备份恢复…")
        restore_btn.clicked.connect(self.restore_requested.emit)
        backup_row = QHBoxLayout()
        backup_row.addWidget(hint_label("备份包含直播间和设置、收藏夹、历史记录、说书文字和进度、屏蔽词整理表（不含登录、运行日志和表情图片）"), 1)
        backup_row.addWidget(backup_btn)
        backup_row.addWidget(restore_btn)
        data = QVBoxLayout()
        data.setSpacing(8)
        data.addLayout(folder_row)
        data.addLayout(backup_row)

        # ---- 关于
        version_row = QHBoxLayout()
        version_label = QLabel(f"{APP_TITLE} {APP_VERSION}")
        version_row.addWidget(version_label)
        version_row.addSpacing(8)
        self.check_btn = QPushButton("检查更新")
        self.check_btn.clicked.connect(self.check_update_requested.emit)
        version_row.addWidget(self.check_btn)
        if getattr(sys, "frozen", False):
            update_btn = QPushButton("从压缩包更新…")
            update_btn.setToolTip("选择从 GitHub Releases 下载的新版本压缩包，程序会自动替换并重新打开，数据不受影响")
            update_btn.clicked.connect(self.update_requested.emit)
            version_row.addWidget(update_btn)
        else:
            version_row.addWidget(short_hint("（源码版，用 git pull 更新）"))
        notice_btn = QPushButton("使用须知")
        notice_btn.clicked.connect(self.notice_requested.emit)
        version_row.addWidget(notice_btn)
        version_row.addStretch(1)
        # 检查更新的结果：已是最新 / 有新版本（旁边是“更新到 x.y.z…”按钮）/ 出错
        self.update_label = hint_label()
        self.update_label.setWordWrap(False)
        self.install_btn = QPushButton()
        self.install_btn.clicked.connect(self.install_update_requested.emit)
        self._update_row = QWidget()
        update_row = QHBoxLayout(self._update_row)
        update_row.setContentsMargins(0, 0, 0, 0)
        update_row.addWidget(self.update_label)
        update_row.addWidget(self.install_btn)
        update_row.addStretch(1)
        about = QVBoxLayout()
        about.setSpacing(8)
        about.addLayout(version_row)
        about.addWidget(self._update_row)
        self.show_update_status("")

        content = QWidget()
        content.setMaximumWidth(760)
        box = QVBoxLayout(content)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(12)
        box.addWidget(titled_section("监控与发送", monitor))
        box.addWidget(titled_section("窗口与通知", window))
        box.addLayout(save_row)
        box.addSpacing(4)
        box.addWidget(titled_section("数据", data))
        box.addWidget(titled_section("关于", about))
        box.addStretch(1)

        # 窗口矮的时候可以滚动
        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.viewport().setAutoFillBackground(False)
        content.setAutoFillBackground(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)
        layout.addWidget(title)
        layout.addWidget(scroll, 1)

    def load(self, cfg: AppConfig, autostart: bool) -> None:
        self.poll_spin.setValue(cfg.poll_interval_sec)
        self.auto_monitor_check.setChecked(cfg.auto_monitor)
        self.send_on_startup_check.setChecked(cfg.send_on_startup)
        self.browser_combo.setCurrentIndex(self.browser_combo.findData(cfg.browser))
        self.close_combo.setCurrentIndex(self.close_combo.findData(cfg.close_action))
        self.theme_combo.setCurrentIndex(self.theme_combo.findData(cfg.theme))
        self.notify_check.setChecked(cfg.notify)
        self.autostart_check.setChecked(autostart)
        self.auto_update_check.setChecked(cfg.auto_check_update)
        self.saved_label.clear()

    def apply_to(self, cfg: AppConfig) -> None:
        cfg.poll_interval_sec = self.poll_spin.value()
        cfg.auto_monitor = self.auto_monitor_check.isChecked()
        cfg.send_on_startup = self.send_on_startup_check.isChecked()
        cfg.browser = self.browser_combo.currentData()
        cfg.close_action = self.close_combo.currentData()
        cfg.theme = self.theme_combo.currentData()
        cfg.notify = self.notify_check.isChecked()
        cfg.auto_check_update = self.auto_update_check.isChecked()

    def autostart_checked(self) -> bool:
        return self.autostart_check.isChecked()

    def show_update_status(self, text: str, color: str = "", new_version: str | None = None) -> None:
        """检查更新的结果。new_version 不为空时显示“更新到 x.y.z…”按钮（源码版没有这个按钮，用 git pull 更新）。"""
        set_colored(self.update_label, text, color or StatusColors().muted)
        self.update_label.setVisible(bool(text))
        self._update_row.setVisible(bool(text) or new_version is not None)
        self.install_btn.setVisible(new_version is not None and getattr(sys, "frozen", False))
        if new_version:
            self.install_btn.setText(f"更新到 {new_version}…")

    def set_checking(self, checking: bool) -> None:
        self.check_btn.setEnabled(not checking)
        self.check_btn.setText("正在检查…" if checking else "检查更新")

    def show_saved(self) -> None:
        self.saved_label.setText("✓ 已保存")
        self._saved_timer.start()
