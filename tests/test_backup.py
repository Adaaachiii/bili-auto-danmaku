"""数据备份和恢复的测试：导出哪些文件、数据库正在用时也能导出完整的一份、
不是本程序的备份不让恢复、恢复后和备份时一模一样、恢复前的数据另存一份、换到一半出错时数据保持原样。

运行：python tests/test_backup.py（用临时数据文件夹，不碰真实数据）。
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
(ROOT / ".test").mkdir(exist_ok=True)
WORK = Path(tempfile.mkdtemp(dir=ROOT / ".test"))
HOME = WORK / "data"
os.environ["BILI_AUTO_DANMAKU_HOME"] = str(HOME)
sys.path.insert(0, str(ROOT))

from app import backup, story  # noqa: E402
from app.favorites import FavoritesStore  # noqa: E402
from app.history import HistoryStore  # noqa: E402


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---- 准备一份数据：数据库开着、刚写进去的内容还在 WAL 里
write(HOME / "config.json", '{"rooms": [{"room_id": 1}]}')
story.save_room_story(1, "第一本书")
write(HOME / "progress.json", '{"1": {"pos": 2, "hash": "x"}}')
write(HOME / "被过滤的弹幕.csv", "弹幕内容\n测试\n")
write(HOME / "logs" / "app.log", "运行日志")
write(HOME / "config.tmp", "写到一半的")
favorites = FavoritesStore()
favorites.add("收藏一", ["标签"], "收藏的内容")
history = HistoryStore()
record = history.open_session(1, "主播", "repeat", "弹幕", 1_000_000.0)
history.add_result(record, 1, "主播", 1_000_100.0, "filtered", "f", "被屏蔽的弹幕")

# ---- 导出：不含运行日志、临时文件、WAL；数据库是完整的一份
dest = WORK / "备份.zip"
info = backup.export_backup(dest)
with zipfile.ZipFile(dest) as archive:
    names = set(archive.namelist())
    manifest = json.loads(archive.read(backup.MANIFEST))
    archive.extract("history.db", WORK / "check")
expected = {"config.json", "stories/1.txt", "progress.json", "被过滤的弹幕.csv", "favorites.db", "history.db", backup.MANIFEST}
assert names == expected, names
assert manifest["app"] == "BiliAutoDanmaku" and set(manifest["files"]) == expected - {backup.MANIFEST}
check = sqlite3.connect(str(WORK / "check" / "history.db"))
assert check.execute("SELECT COUNT(*) FROM failures").fetchone()[0] == 1, "WAL 里的内容也要在备份里"
check.close()
assert backup.read_backup(dest).files == info.files
print("导出 OK")

# ---- 不是本程序的备份、路径不正常、不完整：都不让恢复
def make_zip(name: str, entries: dict[str, str]) -> Path:
    path = WORK / name
    with zipfile.ZipFile(path, "w") as archive:
        for entry, text in entries.items():
            archive.writestr(entry, text)
    return path


ok_manifest = json.dumps({"app": "BiliAutoDanmaku"})
for name, entries, expect in [
    ("random.zip", {"a.txt": "x"}, "不是本程序"),
    ("other.zip", {backup.MANIFEST: json.dumps({"app": "别的程序"}), "config.json": "{}"}, "不是本程序"),
    ("evil.zip", {backup.MANIFEST: ok_manifest, "config.json": "{}", "../evil.txt": "x"}, "不正常"),
    ("partial.zip", {backup.MANIFEST: ok_manifest, "favorites.db": "x"}, "不完整"),
]:
    try:
        backup.read_backup(make_zip(name, entries))
    except backup.BackupError as e:
        assert expect in str(e), (name, e)
    else:
        raise AssertionError(f"{name} 应该被拒绝")
(WORK / "notzip.zip").write_text("不是压缩包", encoding="utf-8")
try:
    backup.read_backup(WORK / "notzip.zip")
except backup.BackupError as e:
    assert "打不开" in str(e)
print("检查备份 OK")

# ---- 改动数据后恢复：和备份时一模一样（多出来的说书文字也清掉），恢复前的数据另存一份，运行日志不动
favorites.close()
history.close()
write(HOME / "config.json", '{"rooms": []}')
story.save_room_story(2, "第二本书")
(HOME / "被过滤的弹幕.csv").unlink()
assert backup.apply_pending_restore() is None, "没有等着恢复的备份时什么都不做"
backup.stage_restore(dest)
assert backup.has_pending_restore()
result = backup.apply_pending_restore()
assert result and result.ok, result
assert read(HOME / "config.json") == '{"rooms": [{"room_id": 1}]}'
assert read(HOME / "stories" / "1.txt") == "第一本书" and not (HOME / "stories" / "2.txt").exists()
assert (HOME / "被过滤的弹幕.csv").exists() and read(HOME / "logs" / "app.log") == "运行日志"
favorites = FavoritesStore()
assert [f.title for f in favorites.search()] == ["收藏一"]
favorites.close()
restored = HistoryStore()
assert restored.filtered_count() == 1 and restored.is_filtered("被屏蔽的弹幕", 1)
restored.close()
assert result.previous and result.previous.exists()
with zipfile.ZipFile(result.previous) as archive:
    assert archive.read("config.json").decode("utf-8") == '{"rooms": []}', "恢复前的数据要另存下来"
    assert "stories/2.txt" in archive.namelist()
assert not backup.has_pending_restore()
assert not any(p.name.startswith("_restore") for p in HOME.iterdir()), list(HOME.iterdir())
print("恢复 OK")

# ---- 换到一半出错：数据保持原样
write(HOME / "config.json", '{"rooms": ["改过的"]}')
before = {p.relative_to(HOME).as_posix(): p.read_bytes() for p in HOME.rglob("*") if p.is_file() and p.suffix != ".zip"}
backup.stage_restore(dest)
real_replace = os.replace
calls = []


def flaky_replace(src, dst):
    calls.append(dst)
    if "_restore_new" in str(src) and len([c for c in calls if "_restore_new" not in str(c)]) > 3:
        raise OSError("文件被占用")
    return real_replace(src, dst)


backup.os.replace = flaky_replace
result = backup.apply_pending_restore()
backup.os.replace = real_replace
assert result and not result.ok and "数据保持原样" in result.message, result
after = {p.relative_to(HOME).as_posix(): p.read_bytes() for p in HOME.rglob("*") if p.is_file() and p.suffix != ".zip"}
assert after == before, set(after) ^ set(before)
assert not backup.has_pending_restore() and not any(p.name.startswith("_restore") for p in HOME.iterdir())
print("出错时保持原样 OK")

shutil.rmtree(WORK, ignore_errors=True)
print("全部通过")
