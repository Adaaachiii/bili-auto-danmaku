"""直播间列表的画法测试：主播名字很短时，“主播”一列也放得下名字下面那行房间号。

运行：python tests/test_rooms_table.py（不显示窗口，不联网，用临时数据目录）。
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # 不弹窗口
ROOT = Path(__file__).resolve().parent.parent
(ROOT / ".test").mkdir(exist_ok=True)
HOME = tempfile.mkdtemp(dir=ROOT / ".test")  # 测试用的数据目录，跑完删掉，不碰真实数据
os.environ["BILI_AUTO_DANMAKU_HOME"] = HOME
sys.path.insert(0, str(ROOT))
from PySide6.QtGui import QFontMetrics  # noqa: E402
from PySide6.QtWidgets import QApplication, QStyleOptionViewItem, QTableWidget, QTableWidgetItem  # noqa: E402

from app.ui_main import COL_NAME, COL_ROOM, COLUMNS, RoomDelegate  # noqa: E402

app = QApplication(sys.argv)
table = QTableWidget(0, len(COLUMNS))
delegate = RoomDelegate(table)
table.setItemDelegate(delegate)


def name_width(name: str, room_id: int) -> tuple[int, int]:
    """返回（“主播”一格想要的宽度, 画房间号那行字需要的宽度）。"""
    table.setRowCount(1)
    table.setItem(0, COL_NAME, QTableWidgetItem(name))
    table.setItem(0, COL_ROOM, QTableWidgetItem(str(room_id)))
    option = QStyleOptionViewItem()
    option.font = table.font()
    index = table.model().index(0, COL_NAME)
    need = QFontMetrics(delegate._small(option.font)).horizontalAdvance(f"房间 {room_id}") + 12  # 画的时候左右各留 6
    return delegate.sizeHint(option, index).width(), need


def test_short_name_keeps_room_id() -> None:
    for name, room_id in (("甲", 1024), ("主播乙", 12345678), ("A", 1234567890)):
        width, need = name_width(name, room_id)
        assert width >= need, (name, room_id, width, need)


def test_long_name_still_decides_width() -> None:
    short, _ = name_width("甲", 6)
    long, need = name_width("名字特别特别长的一位主播", 6)
    assert long > short and long > need, (short, long, need)


try:
    test_short_name_keeps_room_id()
    test_long_name_still_decides_width()
    print("全部通过")
finally:
    shutil.rmtree(HOME, ignore_errors=True)
