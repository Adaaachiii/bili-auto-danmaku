"""卡片式标签编辑：已选的标签显示成卡片，下面列出常用标签，点一下就加上。"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QMenu,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .favorites import FavoritesStore, parse_tags
from .ui_common import StatusColors, hint_label


class FlowLayout(QLayout):
    """从左到右排，一行放不下就换行（Qt 没有自带这种布局）。"""

    def __init__(self, parent: QWidget | None = None, spacing: int = 6):
        super().__init__(parent)
        self._items = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _arrange(self, rect: QRect, apply: bool) -> int:
        x, y, line_height = rect.x(), rect.y(), 0
        for item in self._items:
            if item.isEmpty():
                continue
            hint = item.sizeHint()
            if x > rect.x() and x + hint.width() > rect.right() + 1:
                x = rect.x()
                y += line_height + self._spacing
                line_height = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._spacing
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y()


def _clear(layout: QLayout) -> None:
    while layout.count():
        widget = layout.takeAt(0).widget()
        if widget is not None:
            widget.hide()  # deleteLater 要等一会儿才删，先藏起来免得闪一下
            widget.deleteLater()


class TagChip(QFrame):
    """一张标签卡片。removable 时右边有个 ×；否则整张卡片可以点。"""

    removed = Signal(str)
    clicked = Signal(str)
    forget_requested = Signal(str)

    def __init__(self, text: str, removable: bool, parent: QWidget | None = None):
        super().__init__(parent)
        self.text = text
        colors = StatusColors()
        self.setObjectName("chip")
        # 固定高度：圆角半径不能超过高度的一半，否则 Qt 会直接画成方角
        self.setFixedHeight(26)
        if removable:
            style = f"QFrame#chip {{ background: {colors.selection}; border: 1px solid {colors.selection}; border-radius: 12px; }}"
        else:
            style = (
                f"QFrame#chip {{ background: transparent; border: 1px solid {colors.muted}; border-radius: 12px; }}"
                f"QFrame#chip:hover {{ background: {colors.hover}; }}"
            )
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setToolTip("点一下加上这个标签；右键可以从常用标签里去掉")
        self.setStyleSheet(style + "QFrame#chip QLabel { background: transparent; border: none; }")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 2, 4 if removable else 10, 2)
        layout.setSpacing(2)
        layout.addWidget(QLabel(text if removable else f"＋ {text}"))
        if removable:
            close = QToolButton()
            close.setText("×")
            close.setAutoRaise(True)
            close.setCursor(Qt.CursorShape.PointingHandCursor)
            close.setToolTip("去掉这个标签")
            close.setStyleSheet("QToolButton { border: none; padding: 0 4px; }")
            close.clicked.connect(lambda: self.removed.emit(self.text))
            layout.addWidget(close)
        self._removable = removable

    def mousePressEvent(self, event) -> None:
        if not self._removable and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.text)
        super().mousePressEvent(event)

    def contextMenuEvent(self, event) -> None:
        if self._removable:
            return
        menu = QMenu(self)
        menu.addAction("从常用标签中删除").triggered.connect(lambda: self.forget_requested.emit(self.text))
        menu.exec(event.globalPos())


class _TagInput(QLineEdit):
    """输入新标签的框：回车只添加标签，不会触发对话框的“保存”。"""

    submitted = Signal()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.submitted.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class TagEditor(QWidget):
    """已选标签（卡片，可点 × 去掉）+ 输入框（回车添加）+ 常用标签（点一下加上）。"""

    changed = Signal()

    def __init__(self, store: FavoritesStore, tags: list[str] | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.store = store
        self._tags: list[str] = list(tags or [])

        self.input = _TagInput()
        self.input.setPlaceholderText("可以给内容做个标签")
        self.input.setMinimumWidth(180)
        self.input.submitted.connect(self._add_from_input)
        self.input.textEdited.connect(self._on_text_edited)

        self.selected_box = QWidget()
        self.selected_flow = FlowLayout(self.selected_box)

        self.library_title = hint_label("常用标签（点一下加上）：")
        self.library_box = QWidget()
        self.library_flow = FlowLayout(self.library_box)
        self.library_empty = hint_label("保存后，新加的标签会出现在这里，下次可以直接点选。")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.selected_box)
        layout.addWidget(self.library_title)
        layout.addWidget(self.library_box)
        layout.addWidget(self.library_empty)
        self._refresh()

    def tags(self) -> list[str]:
        """已选的标签；输入框里还没按回车的文字也算上。"""
        tags = list(self._tags)
        for tag in parse_tags(self.input.text()):
            if tag not in tags:
                tags.append(tag)
        return tags

    def _add(self, tag: str) -> None:
        if tag and tag not in self._tags:
            self._tags.append(tag)
            self._refresh()
            self.changed.emit()

    def _remove(self, tag: str) -> None:
        if tag in self._tags:
            self._tags.remove(tag)
            self._refresh()
            self.changed.emit()

    def _forget(self, tag: str) -> None:
        self.store.forget_tag(tag)
        self._refresh()

    def _add_from_input(self) -> None:
        for tag in parse_tags(self.input.text()):
            self._add(tag)
        self.input.clear()

    def _on_text_edited(self, text: str) -> None:
        # 打了逗号、顿号或空格，就把前面的部分变成卡片
        if text and text[-1] in ",，;；、 ":
            self._add_from_input()

    def _refresh(self) -> None:
        # 输入框也放在流式布局里，跟在卡片后面；先拿出来，免得被清掉
        self.selected_flow.removeWidget(self.input)
        _clear(self.selected_flow)
        for tag in self._tags:
            chip = TagChip(tag, removable=True)
            chip.removed.connect(self._remove)
            self.selected_flow.addWidget(chip)
        self.selected_flow.addWidget(self.input)

        _clear(self.library_flow)
        available = [tag for tag in self.store.library_tags() if tag not in self._tags]
        for tag in available:
            chip = TagChip(tag, removable=False)
            chip.clicked.connect(self._add)
            chip.forget_requested.connect(self._forget)
            self.library_flow.addWidget(chip)
        has_library = bool(self.store.library_tags())
        self.library_title.setVisible(bool(available))
        self.library_box.setVisible(bool(available))
        self.library_empty.setVisible(not has_library)
        self.updateGeometry()
