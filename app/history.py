"""历史记录：存在 data\\history.db（SQLite）。

每个直播间每一次“开始发弹幕 → 停止发弹幕”是一条记录：开始和结束时间、发了多少条、成功和失败多少条，
以及每一条失败的弹幕和原因。停止包括下播、手动停止、说书说完、连续失败停发、被禁言、暂停监控、
登录失效、程序退出等。发送不到 5 分钟的记录不保存。

被 B 站过滤的弹幕（通常是含屏蔽词）不管那次发了多久，都另外汇总进“屏蔽词整理表”
data\\被过滤的弹幕.csv：同一个直播间里同样的内容合并成一行，记被过滤的次数和屏蔽类型
（系统屏蔽 / 主播屏蔽），用 Excel 可以直接打开。

引擎线程写、界面线程读，所以每个方法都加了锁。
"""

from __future__ import annotations

import csv
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .bili_api import filter_kind
from .config import data_dir

MIN_RECORD_SECONDS = 5 * 60  # 发送不到这么久的记录不保存
FILTERED_CSV_NAME = "被过滤的弹幕.csv"

# 失败原因（bili_api.SendOutcome 的值）的中文说法
OUTCOME_LABELS = {
    "filtered": "被 B 站过滤",
    "rate_limited": "被限流",
    "muted": "被禁言",
    "auth_invalid": "登录失效",
    "error": "发送出错",
}


@dataclass
class SessionRecord:
    id: int
    room_id: int
    uname: str
    mode: str
    content: str  # 独轮车：弹幕内容；说书：书名
    started_at: float
    last_at: float  # 最后一次发送的时间
    ended_at: float | None  # None 表示还在发送
    end_reason: str
    sent: int
    ok: int

    @property
    def failed(self) -> int:
        return self.sent - self.ok

    @property
    def ongoing(self) -> bool:
        return self.ended_at is None

    def duration(self, now: float | None = None) -> float:
        end = self.ended_at if self.ended_at is not None else (now or time.time())
        return max(0.0, end - self.started_at)


@dataclass
class FailureRecord:
    at: float
    outcome: str
    message: str  # B 站返回的原文
    text: str  # 这条弹幕的内容

    @property
    def category(self) -> str:
        """失败的类别；被过滤的再分成系统屏蔽 / 主播屏蔽。"""
        if self.outcome == "filtered":
            return filter_kind(self.message)
        return OUTCOME_LABELS.get(self.outcome, self.outcome)

    @property
    def reason(self) -> str:
        return f"{self.category}（{self.message}）" if self.message else self.category


def filtered_csv_path() -> Path:
    return data_dir() / FILTERED_CSV_NAME


class HistoryStore:
    def __init__(self, path=None):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path or data_dir() / "history.db"), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")  # 边写边读不互相卡住
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id INTEGER NOT NULL,
                uname TEXT NOT NULL DEFAULT '',
                mode TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                started_at REAL NOT NULL,
                last_at REAL NOT NULL,
                ended_at REAL,
                end_reason TEXT NOT NULL DEFAULT '',
                sent INTEGER NOT NULL DEFAULT 0,
                ok INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS sessions_room ON sessions(room_id, started_at);
            CREATE TABLE IF NOT EXISTS failures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                at REAL NOT NULL,
                outcome TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                text TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS failures_session ON failures(session_id);
            CREATE TABLE IF NOT EXISTS filtered (
                text TEXT NOT NULL,
                room_id INTEGER NOT NULL,
                uname TEXT NOT NULL DEFAULT '',
                count INTEGER NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                first_at REAL NOT NULL,
                last_at REAL NOT NULL,
                PRIMARY KEY (text, room_id)
            );
            """
        )
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------ 记录（引擎调用）

    def open_session(self, room_id: int, uname: str, mode: str, content: str, started_at: float) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO sessions (room_id, uname, mode, content, started_at, last_at) VALUES (?, ?, ?, ?, ?, ?)",
                (room_id, uname, mode, content, started_at, started_at),
            )
            self._db.commit()
            return cur.lastrowid

    def add_result(self, session_id: int, room_id: int, uname: str, at: float, outcome: str, message: str, text: str) -> None:
        """记一次发送结果。outcome 是 SendOutcome 的值，"ok" 表示成功。"""
        ok = outcome == "ok"
        with self._lock:
            self._db.execute(
                "UPDATE sessions SET sent = sent + 1, ok = ok + ?, last_at = ?, uname = ? WHERE id = ?",
                (1 if ok else 0, at, uname, session_id),
            )
            if not ok:
                self._db.execute(
                    "INSERT INTO failures (session_id, at, outcome, message, text) VALUES (?, ?, ?, ?, ?)",
                    (session_id, at, outcome, message, text),
                )
            if outcome == "filtered":
                self._db.execute(
                    """INSERT INTO filtered (text, room_id, uname, count, message, first_at, last_at)
                       VALUES (?, ?, ?, 1, ?, ?, ?)
                       ON CONFLICT (text, room_id) DO UPDATE SET
                         count = count + 1, uname = excluded.uname, message = excluded.message, last_at = excluded.last_at""",
                    (text, room_id, uname, message, at, at),
                )
            self._db.commit()

    def close_session(self, session_id: int, ended_at: float, reason: str) -> bool:
        """结束一条记录。发送不到 5 分钟的直接删掉，返回是否保存了。"""
        with self._lock:
            row = self._db.execute("SELECT started_at FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if row is None:
                return False
            kept = ended_at - row[0] >= MIN_RECORD_SECONDS
            if kept:
                self._db.execute("UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ?", (ended_at, reason, session_id))
            else:
                self._delete([session_id])
            self._db.commit()
            return kept

    def close_unfinished(self, reason: str) -> int:
        """上次程序没正常退出时留下的“进行中”记录：按最后一次发送的时间结束掉。返回保存下来的条数。"""
        with self._lock:
            rows = self._db.execute("SELECT id FROM sessions WHERE ended_at IS NULL").fetchall()
        return sum(self._close_at_last(row[0], reason) for row in rows)

    def _close_at_last(self, session_id: int, reason: str) -> bool:
        with self._lock:
            row = self._db.execute("SELECT last_at FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return bool(row) and self.close_session(session_id, row[0], reason)

    # ------------------------------------------------------------ 查看（界面调用）

    def sessions(self, room_id: int | None = None) -> list[SessionRecord]:
        sql = "SELECT id, room_id, uname, mode, content, started_at, last_at, ended_at, end_reason, sent, ok FROM sessions"
        args: tuple = ()
        if room_id is not None:
            sql += " WHERE room_id = ?"
            args = (room_id,)
        sql += " ORDER BY started_at DESC"
        with self._lock:
            return [SessionRecord(*row) for row in self._db.execute(sql, args)]

    def rooms(self) -> list[tuple[int, str]]:
        """有记录的直播间：（房间号, 最近一次记录里的主播名）。"""
        with self._lock:
            rows = self._db.execute(
                """SELECT room_id, uname FROM sessions s
                   WHERE started_at = (SELECT MAX(started_at) FROM sessions WHERE room_id = s.room_id)
                   ORDER BY uname, room_id"""
            ).fetchall()
        return [(room_id, uname) for room_id, uname in rows]

    def failures(self, session_id: int) -> list[FailureRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT at, outcome, message, text FROM failures WHERE session_id = ? ORDER BY at", (session_id,)
            ).fetchall()
        return [FailureRecord(*row) for row in rows]

    def delete_sessions(self, ids: list[int]) -> None:
        """删掉已经结束的记录（还在进行的不删）。"""
        with self._lock:
            ended = [row[0] for row in self._db.execute(
                f"SELECT id FROM sessions WHERE ended_at IS NOT NULL AND id IN ({','.join('?' * len(ids))})", ids
            )] if ids else []
            self._delete(ended)
            self._db.commit()

    def _delete(self, ids: list[int]) -> None:
        if not ids:
            return
        marks = ",".join("?" * len(ids))
        self._db.execute(f"DELETE FROM failures WHERE session_id IN ({marks})", ids)
        self._db.execute(f"DELETE FROM sessions WHERE id IN ({marks})", ids)

    # ------------------------------------------------------------ 屏蔽词整理表

    def filtered_count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM filtered").fetchone()[0]

    def write_filtered_csv(self, path: Path | None = None) -> Path:
        """把被过滤的弹幕写成 CSV（带 BOM，Excel 打开不乱码）。文件正被 Excel 打开时会抛 PermissionError。"""
        path = path or filtered_csv_path()
        with self._lock:
            rows = self._db.execute(
                "SELECT text, uname, room_id, count, message, first_at, last_at FROM filtered ORDER BY count DESC, last_at DESC"
            ).fetchall()
        temp = path.with_name(path.name + ".tmp")
        with open(temp, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["弹幕内容", "主播", "房间号", "被过滤次数", "屏蔽类型", "第一次被过滤", "最近一次被过滤"])
            for text, uname, room_id, count, message, first_at, last_at in rows:
                kind = f"{filter_kind(message)}（{message}）" if message else filter_kind(message)
                writer.writerow([_csv_text(text), uname, room_id, count, kind, _stamp(first_at), _stamp(last_at)])
        os.replace(temp, path)
        return path


def _stamp(timestamp: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))


def _csv_text(text: str) -> str:
    # 以 = + - @ 开头的内容 Excel 会当成公式，前面加个 ' 让它按文字显示
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text
