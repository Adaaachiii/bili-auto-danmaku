"""说书人模式的发送序列（界面）测试：从收藏夹一次选好几组加入序列，调整顺序、移除、编辑后的进度和保存结果。

运行：python tests/test_story_queue_ui.py（不显示窗口，不联网，用临时数据目录）。
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
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox  # noqa: E402

from app import bili_api, story, ui_room_dialog  # noqa: E402
from app.config import MODE_STORY, RoomConfig  # noqa: E402
from app.favorites import FavoritesStore  # noqa: E402
from app.ui_favorites import FavoritePickerDialog  # noqa: E402
from app.ui_room_dialog import QUEUE_HINT, RoomDialog  # noqa: E402

app = QApplication(sys.argv)


def offline(_room_id):
    raise bili_api.BiliApiError("测试里不联网")


ui_room_dialog._lookup_room = offline
store = FavoritesStore(Path(HOME) / "favorites.db")
ids = {title: store.add(title, [], content) for title, content in (("乙", "五六七"), ("丙", "八九"), ("空的", "  \n "))}

# ---- 收藏夹多选：按列表里的顺序加入，按钮上写着选了几条
picker = FavoritePickerDialog(None, store, multi=True)
rows = {summary.id: row for row, summary in enumerate(picker.browser._rows)}
for title in ("丙", "乙"):
    picker.browser.table.selectionModel().select(
        picker.browser.table.model().index(rows[ids[title]], 0),
        picker.browser.table.selectionModel().SelectionFlag.Select | picker.browser.table.selectionModel().SelectionFlag.Rows,
    )
assert "2 条" in picker.buttons.button(QDialogButtonBox.StandardButton.Save).text()
picker.accept()
expected = [s.title for s in picker.browser._rows if s.title in ("乙", "丙")]
assert [f.title for f in picker.result_favorites] == expected, [f.title for f in picker.result_favorites]
print("收藏夹多选 OK")

# ---- 已有说书内容、说到一半的直播间：加两组进来，说过的部分没变，保存后接着说
story.save_room_story(1, "一二三四")
digest, chars = story.text_fingerprint("一二三四")
room = RoomConfig(room_id=1, uid=1, uname="主播", mode=MODE_STORY, story_title="甲", story_hash=digest, story_chars=chars)
dialog = RoomDialog(None, room, set(), store, progress=(2, 4))
assert dialog.part_list.count() == 1 and "《甲》" in dialog.part_list.item(0).text()
assert "← 目前在这" in dialog.part_list.item(0).text()
assert dialog.new_story_text is None and "50.0%" in dialog.progress_label.text()


class FakePicker:
    """代替收藏夹对话框：直接“选中”乙、丙和一条没有内容的收藏。"""

    def __init__(self, parent, store, multi=False):
        assert multi, "说书人模式要能多选"
        self.result_favorites = [store.get(ids["乙"]), store.get(ids["空的"]), store.get(ids["丙"])]

    def exec(self):
        return QDialog.DialogCode.Accepted


warnings = []
ui_room_dialog.FavoritePickerDialog = FakePicker
ui_room_dialog.QMessageBox.warning = staticmethod(lambda _parent, _title, text: warnings.append(text))
dialog._pick_story()
assert [dialog.part_list.item(i).text().split("　")[0] for i in range(dialog.part_list.count())] == ["1. 《甲》", "2. 《乙》", "3. 《丙》"]
assert len(warnings) == 1 and "空的" in warnings[0], warnings  # 没有内容的那条提示一下，不加进来
assert dialog.new_story_text is not None and dialog._story_title == "甲 等 3 组"
assert "已经说过的部分没变" in dialog.progress_label.text() and dialog._resume_pos() == 2, dialog.progress_label.text()
assert "第 1/3 组《甲》" in dialog.progress_label.text()
assert "3 组" in dialog.story_label.text() and "9 字" in dialog.story_label.text(), dialog.story_label.text()
print("加入发送序列 OK")

# ---- 调整顺序：说过的部分变了就从头说；挪回去又能接着说
dialog.part_list.setCurrentRow(0)
assert not dialog.part_up_btn.isEnabled() and dialog.part_down_btn.isEnabled()
dialog._move_part(1)
assert dialog.part_list.item(0).text().startswith("1. 《乙》") and dialog.part_list.currentRow() == 1
assert dialog._resume_pos() == 0 and "从头开始说" in dialog.progress_label.text()
dialog._move_part(-1)
assert dialog._resume_pos() == 2
print("调整顺序 OK")

# ---- 移除：删到只剩原来那一组，就和没改过一样
dialog.part_list.setCurrentRow(2)
dialog._remove_part()
dialog.part_list.setCurrentRow(1)
dialog._remove_part()
assert dialog.part_list.count() == 1 and dialog.new_story_text is None and dialog._story_title == "甲"
print("移除 OK")

# ---- 保存的结果：发送序列写成一个文件，拆回来还是这几组
dialog._pick_story()
saved = dialog.result_config()
assert saved.story_title == "甲 等 3 组" and saved.story_hash == story.StoryText(dialog.new_story_text).hash
parts = story.split_parts(dialog.new_story_text)
assert [(p.title, p.text) for p in parts] == [("甲", "一二三四"), ("乙", "五六七"), ("丙", "八九")], parts
print("保存结果 OK")

# ---- 新添加的直播间：一开始没有内容，提示语在
new = RoomDialog(None, None, set(), store)
assert not new.queue_box.isVisibleTo(new) and "还没有设置说书内容" in new.story_label.text()
assert any(QUEUE_HINT == label.text() for label in new.findChildren(ui_room_dialog.QLabel))
new._pick_story()
assert new.part_list.count() == 2 and new.new_story_text is not None and new._resume_pos() == 0
print("新直播间 OK")

store.close()
shutil.rmtree(HOME, ignore_errors=True)
print("全部通过")
