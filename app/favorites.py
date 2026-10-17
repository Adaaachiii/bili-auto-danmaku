"""弹幕收藏夹：存在 data\\favorites.db（SQLite，Python 自带）。

每条收藏有标题（小类）、若干标签（大类）和内容。内容不限字数，可以是一句话，也可以是整本小说。
列表只取标题、标签和开头一小段预览，不会把整本小说读进来，收藏再多也不卡。
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass

from .config import data_dir

PREVIEW_CHARS = 80


@dataclass
class FavoriteSummary:
    """列表里显示的一行。"""

    id: int
    title: str
    tags: list[str]
    chars: int
    preview: str
    updated_at: float


@dataclass
class Favorite:
    id: int
    title: str
    tags: list[str]
    content: str


def count_chars(text: str) -> int:
    """字数：不算换行符。"""
    return len(text) - text.count("\n") - text.count("\r")


def parse_tags(text: str) -> list[str]:
    """把“应援, 小说 歌词”这样的输入拆成标签列表，去重并保持顺序。"""
    tags: list[str] = []
    for tag in re.split(r"[,，;；、\s]+", text):
        tag = tag.strip()
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def _join_tags(tags: list[str]) -> str:
    # 前后都加逗号，按标签筛选时用 LIKE '%,标签,%' 就能精确匹配
    return "," + ",".join(tags) + "," if tags else ""


def _split_tags(stored: str) -> list[str]:
    return [tag for tag in stored.split(",") if tag]


def _like_pattern(keyword: str) -> str:
    escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class FavoritesStore:
    def __init__(self, path=None):
        self._db = sqlite3.connect(str(path or data_dir() / "favorites.db"))
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS favorites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                chars INTEGER NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )"""
        )
        # 常用标签：用过的标签都存在这里，新建收藏时可以直接点选
        self._db.execute("CREATE TABLE IF NOT EXISTS tag_library (name TEXT PRIMARY KEY, created_at REAL NOT NULL)")
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            # 把已有收藏用到的标签收进常用标签
            self._remember_tags(self.used_tags())
        if version < 2:
            # 字数改成不算换行符，旧数据重新算一遍
            self._db.execute(
                "UPDATE favorites SET chars = length(replace(replace(content, char(10), ''), char(13), ''))"
            )
        self._db.execute("PRAGMA user_version = 2")
        self._db.commit()

    def search(self, keyword: str = "", tag: str = "") -> list[FavoriteSummary]:
        """按关键词（标题、内容、标签都搜）和标签筛选，最近改过的排在前面。"""
        where, params = [], []
        keyword = keyword.strip()
        if keyword:
            pattern = _like_pattern(keyword)
            where.append("(title LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\')")
            params += [pattern, pattern, pattern]
        if tag:
            where.append("tags LIKE ? ESCAPE '\\'")
            params.append(_like_pattern("," + tag + ","))
        # 预览：搜到了内容就从命中的地方附近开始显示，方便看出为什么搜到它
        sql = (
            "SELECT id, title, tags, chars, "
            "CASE WHEN ? != '' AND instr(content, ?) > 0 "
            f"THEN substr(content, max(instr(content, ?) - 15, 1), {PREVIEW_CHARS}) "
            f"ELSE substr(content, 1, {PREVIEW_CHARS}) END, updated_at FROM favorites"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC"
        rows = self._db.execute(sql, [keyword, keyword, keyword, *params]).fetchall()
        return [
            FavoriteSummary(row[0], row[1], _split_tags(row[2]), row[3], " ".join(row[4].split()), row[5])
            for row in rows
        ]

    def get(self, favorite_id: int) -> Favorite | None:
        row = self._db.execute("SELECT id, title, tags, content FROM favorites WHERE id = ?", (favorite_id,)).fetchone()
        return Favorite(row[0], row[1], _split_tags(row[2]), row[3]) if row else None

    def add(self, title: str, tags: list[str], content: str) -> int:
        now = time.time()
        cursor = self._db.execute(
            "INSERT INTO favorites (title, tags, content, chars, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (title, _join_tags(tags), content, count_chars(content), now, now),
        )
        self._remember_tags(tags)
        self._db.commit()
        return int(cursor.lastrowid)

    def update(self, favorite_id: int, title: str, tags: list[str], content: str) -> None:
        self._db.execute(
            "UPDATE favorites SET title = ?, tags = ?, content = ?, chars = ?, updated_at = ? WHERE id = ?",
            (title, _join_tags(tags), content, count_chars(content), time.time(), favorite_id),
        )
        self._remember_tags(tags)
        self._db.commit()

    def delete(self, favorite_id: int) -> None:
        self._db.execute("DELETE FROM favorites WHERE id = ?", (favorite_id,))
        self._db.commit()

    def used_tags(self) -> list[str]:
        """收藏里实际用到的标签（按标签筛选时用）。"""
        tags: set[str] = set()
        for (stored,) in self._db.execute("SELECT tags FROM favorites"):
            tags.update(_split_tags(stored))
        return sorted(tags)

    def library_tags(self) -> list[str]:
        """常用标签（新建、编辑收藏时可以直接点选），按加入的先后排列。"""
        return [name for (name,) in self._db.execute("SELECT name FROM tag_library ORDER BY created_at, name")]

    def forget_tag(self, name: str) -> None:
        """从常用标签里去掉。已经贴在收藏上的标签不受影响。"""
        self._db.execute("DELETE FROM tag_library WHERE name = ?", (name,))
        self._db.commit()

    def _remember_tags(self, tags: list[str]) -> None:
        now = time.time()
        self._db.executemany("INSERT OR IGNORE INTO tag_library (name, created_at) VALUES (?, ?)", [(t, now) for t in tags])

    def close(self) -> None:
        self._db.close()
