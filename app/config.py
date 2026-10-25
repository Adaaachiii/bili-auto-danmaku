"""配置文件读写。

直播间列表和各项设置保存在数据文件夹（一般是程序文件夹下的 data，见 data_dir）的 config.json，日志在 logs。
说书用的长文字不放这里（这个文件每次改设置都要整体重写），见 story.py。
登录 Cookie 也不在这里，由 secure_store.py 存进 Windows 凭据管理器。
"""

from __future__ import annotations

import json
import logging
import os
import random
import shutil
import sys
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path

APP_ID = "BiliAutoDanmaku"  # 凭据管理器、开机启动项用这个名字，改了会丢登录，所以不跟着程序名改
APP_TITLE = "B站弹幕自动化"
APP_VERSION = "1.3.1"

# 发送间隔（秒）。设下限是因为发得太快会被 B 站限流，也容易被房管禁言。
MIN_SEND_INTERVAL = 5
MAX_SEND_INTERVAL = 3600
DEFAULT_SEND_INTERVAL = 15
# 检测开播的间隔（秒）。查得太勤可能被风控。
MIN_POLL_INTERVAL = 15
MAX_POLL_INTERVAL = 600
DEFAULT_POLL_INTERVAL = 30
# B 站直播一条弹幕最多 40 个字
DANMAKU_MAX_LENGTH = 40
# 每场发送上限（0 表示不限）
MAX_LIMIT_COUNT = 100_000
MAX_LIMIT_MINUTES = 24 * 60
# 独轮车最多选多少个表情
MAX_EMOTICONS = 100

# 弹幕发送模式
MODE_REPEAT = "repeat"  # 独轮车：一直发同一句（也可以和选好的表情轮流发）
MODE_STORY = "story"  # 说书人：把一段长文字按顺序分成多条发
MODE_NOTIFY = "notify"  # 只提醒：开播、下播时弹通知、打开直播页，不发弹幕
MODE_CHOICES = {MODE_REPEAT: "独轮车模式", MODE_STORY: "说书人模式", MODE_NOTIFY: "只提醒"}

# 发送时段的默认值
DEFAULT_WINDOW = ("20:00", "23:00")


def parse_hhmm(text: str) -> int | None:
    """“20:30”→ 一天里的第几分钟；格式不对返回 None。"""
    try:
        hours, minutes = (int(part) for part in str(text).split(":"))
    except ValueError:
        return None
    return hours * 60 + minutes if 0 <= hours < 24 and 0 <= minutes < 60 else None


BROWSER_EDGE = "edge"
BROWSER_CHROME = "chrome"
BROWSER_DEFAULT = "default"
BROWSER_CHOICES = {BROWSER_EDGE: "Microsoft Edge", BROWSER_CHROME: "Google Chrome", BROWSER_DEFAULT: "系统默认浏览器"}

# 点窗口右上角 × 时怎么处理
CLOSE_ASK = "ask"
CLOSE_TRAY = "tray"
CLOSE_QUIT = "quit"
CLOSE_CHOICES = {CLOSE_ASK: "每次询问", CLOSE_TRAY: "最小化到托盘", CLOSE_QUIT: "退出程序"}

log = logging.getLogger(__name__)


def _program_dir() -> Path:
    if getattr(sys, "frozen", False):  # 打包成 exe 之后
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def fallback_data_dir() -> Path:
    """程序文件夹写不进去时（比如放在 C:\\Program Files）改用的数据文件夹。"""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_ID


def _writable(folder: Path) -> bool:
    """真的写一个文件试试。Windows 上 os.access 看不出文件夹权限。"""
    probe = folder / f".write-test-{os.getpid()}"
    try:
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def resolve_data_dir(program: Path, fallback: Path) -> tuple[Path, str]:
    """决定数据放在哪，返回（文件夹, 给日志看的说明）。

    优先用程序文件夹下的 data，换电脑时整个文件夹带走就行；
    写不进去就改用 fallback（%LOCALAPPDATA%\\BiliAutoDanmaku）。
    以前已经存在 fallback 里的数据会接着用，不会因为换了程序文件夹就“丢了”直播间。
    """
    portable = program / "data"
    if portable.is_dir():
        if _writable(portable):
            return portable, ""
        if fallback.exists():
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback, f"{portable} 不能写入，改用 {fallback}（这里面已有数据，没有从程序文件夹复制）"
        try:  # 第一次换位置：把程序文件夹里的旧数据复制过来，直播间、收藏夹照常可用
            shutil.copytree(portable, fallback, ignore=shutil.ignore_patterns("*.tmp", ".write-test-*"))
            return fallback, f"{portable} 不能写入，已把里面的数据复制到 {fallback}，以后保存在这里"
        except OSError as e:
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback, f"{portable} 不能写入，改用 {fallback}；复制旧数据失败：{e}"
    if fallback.is_dir():
        return fallback, ""
    try:
        portable.mkdir(parents=True)
    except OSError:
        pass
    else:
        if _writable(portable):
            return portable, ""
        shutil.rmtree(portable, ignore_errors=True)
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback, f"程序文件夹 {program} 不能写入，数据保存在 {fallback}"


def data_dir_for(program: Path) -> tuple[Path, str]:
    """program 这个程序文件夹对应的数据文件夹。设置环境变量 BILI_AUTO_DANMAKU_HOME 可以换位置（测试用）。"""
    override = os.environ.get("BILI_AUTO_DANMAKU_HOME")
    if override:
        path = Path(override)
        path.mkdir(parents=True, exist_ok=True)
        return path, ""
    return resolve_data_dir(program, fallback_data_dir())


_data_dir: Path | None = None
data_dir_note = ""  # 数据没放在程序文件夹时的说明，启动后写进日志


def data_dir() -> Path:
    """配置、收藏夹、历史记录和日志所在的文件夹。"""
    if os.environ.get("BILI_AUTO_DANMAKU_HOME"):
        return data_dir_for(_program_dir())[0]
    global _data_dir, data_dir_note
    if _data_dir is None:  # 只判断一次，免得运行中途换位置
        _data_dir, data_dir_note = data_dir_for(_program_dir())
    return _data_dir


def config_path() -> Path:
    return data_dir() / "config.json"


@dataclass
class RepeatItem:
    """独轮车要发的一条：一句文字，或者一个表情。"""

    text: str  # 文字本身；表情时是给人看的说法，比如“表情「打call」”
    emoticon: str = ""  # 表情 ID，空表示这条是文字


def emoticon_label(name: str) -> str:
    return f"表情「{name}」"


@dataclass
class RoomConfig:
    room_id: int  # 真实房间号（长号）
    uid: int = 0  # 主播 UID，用来批量查询开播状态
    uname: str = ""  # 主播昵称，只用于显示
    mode: str = MODE_REPEAT
    message: str = ""  # 独轮车模式要循环发送的弹幕
    # 独轮车模式和文字轮流发的表情，每个是 {"unique": 表情 ID, "name": 名字, "url": 图片地址}
    emoticons: list[dict] = field(default_factory=list)
    repeat_random: bool = False  # 独轮车：文字和表情随机顺序发（默认按“文字→表情1→表情2…”轮流）
    interval_sec: int = DEFAULT_SEND_INTERVAL  # 固定间隔；随机间隔时是最短间隔
    interval_random: bool = False
    interval_max_sec: int = 30  # 随机间隔时的最长间隔
    open_browser: bool = True  # 开播时是否打开直播页
    send_when_offline: bool = False  # 未开播时是否也发送
    enabled: bool = True
    # 说书人模式。文字本身存在 data\stories\<房间号>.txt，这里只记标题和指纹
    story_title: str = ""
    story_hash: str = ""  # 文字内容的指纹，文字换了进度就从头算
    story_chars: int = 0
    story_chunk_size: int = DANMAKU_MAX_LENGTH  # 每条发多少字
    story_by_line: bool = False  # True：按行说书；False：忽略换行
    story_inherit: bool = False  # 说书状态继承：下一场直播从上一场停下的地方接着说
    story_loop: bool = False  # 说完以后：True 从头循环，False 停止
    story_skip_filtered: bool = False  # 跳过以前被屏蔽过的段落（见 history.HistoryStore.is_filtered）
    # 发送限制
    limit_count: int = 0  # 每场最多发成功多少条，0 表示不限
    limit_minutes: int = 0  # 每场从发出第一条算起，最多发多少分钟，0 表示不限
    window_enabled: bool = False  # 只在每天的某个时段里发送
    window_start: str = DEFAULT_WINDOW[0]  # “时:分”，结束早于开始表示跨过半夜，比如 22:00～02:00
    window_end: str = DEFAULT_WINDOW[1]

    @property
    def sends(self) -> bool:
        """会不会发弹幕（“只提醒”的直播间不发）。"""
        return self.mode != MODE_NOTIFY

    def repeat_items(self) -> list[RepeatItem]:
        """独轮车轮流发的内容：先是文字（没填就没有），然后是选好的表情。"""
        items = [RepeatItem(self.message)] if self.message else []
        items += [RepeatItem(emoticon_label(e["name"]), e["unique"]) for e in self.emoticons]
        return items

    def repeat_text(self, max_names: int = 5) -> str:
        """独轮车的内容写成一句话，列表和历史记录里显示。"""
        if not self.emoticons:
            return self.message
        names = "、".join(e["name"] for e in self.emoticons[:max_names])
        if len(self.emoticons) > max_names:
            names += f"等 {len(self.emoticons)} 个"
        return f"{self.message} ＋ 表情：{names}" if self.message else f"表情：{names}"

    def next_delay(self) -> int:
        """下一条弹幕要等多少秒。"""
        if self.interval_random:
            return random.randint(self.interval_sec, max(self.interval_sec, self.interval_max_sec))
        return self.interval_sec

    def average_delay(self) -> float:
        return (self.interval_sec + max(self.interval_sec, self.interval_max_sec)) / 2 if self.interval_random else self.interval_sec

    def interval_text(self) -> str:
        if not self.sends:
            return "—"
        if self.interval_random:
            return f"{self.interval_sec}～{self.interval_max_sec} 秒"
        return f"{self.interval_sec} 秒"

    def in_window(self, moment: datetime) -> bool:
        """moment（本地时间）在不在发送时段里。没开时段限制时总是 True。"""
        if not self.window_enabled:
            return True
        start, end = parse_hhmm(self.window_start), parse_hhmm(self.window_end)
        if start is None or end is None or start == end:
            return True
        now = moment.hour * 60 + moment.minute
        return start <= now < end if start < end else (now >= start or now < end)

    def window_text(self) -> str:
        return f"{self.window_start}～{self.window_end}"

    def limits_text(self) -> str:
        """发送限制写成一句话，没有限制时返回空字符串。"""
        parts = []
        if self.limit_count:
            parts.append(f"每场最多 {self.limit_count} 条")
        if self.limit_minutes:
            parts.append(f"每场最多 {self.limit_minutes} 分钟")
        if self.window_enabled:
            parts.append(f"只在 {self.window_text()} 发送")
        return "，".join(parts)

    @classmethod
    def from_dict(cls, data: dict) -> RoomConfig:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class AppConfig:
    rooms: list[RoomConfig] = field(default_factory=list)
    poll_interval_sec: int = DEFAULT_POLL_INTERVAL
    auto_monitor: bool = True  # 程序启动后自动开始检测；关掉的话启动时先处于“暂停监控”
    browser: str = BROWSER_EDGE
    close_action: str = CLOSE_ASK
    notify: bool = True
    notice_shown: bool = False  # 第一次打开窗口时弹出的“使用须知”看过没有

    def find_room(self, room_id: int) -> RoomConfig | None:
        return next((room for room in self.rooms if room.room_id == room_id), None)


def _clamp(value, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def _clean_emoticons(raw) -> list[dict]:
    """只留下有表情 ID 的，去掉重复的，手改坏的配置也能读。"""
    result, seen = [], set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        unique = str(item.get("unique") or "").strip()
        if unique and unique not in seen:
            seen.add(unique)
            result.append({"unique": unique, "name": str(item.get("name") or unique), "url": str(item.get("url") or "")})
    return result[:MAX_EMOTICONS]


def _normalize(cfg: AppConfig) -> None:
    cfg.poll_interval_sec = _clamp(cfg.poll_interval_sec, MIN_POLL_INTERVAL, MAX_POLL_INTERVAL)
    if cfg.browser not in BROWSER_CHOICES:
        cfg.browser = BROWSER_EDGE
    if cfg.close_action not in CLOSE_CHOICES:
        cfg.close_action = CLOSE_ASK
    for room in cfg.rooms:
        if room.mode not in MODE_CHOICES:
            room.mode = MODE_REPEAT
        room.interval_sec = _clamp(room.interval_sec, MIN_SEND_INTERVAL, MAX_SEND_INTERVAL)
        room.interval_max_sec = _clamp(room.interval_max_sec, room.interval_sec, MAX_SEND_INTERVAL)
        room.message = room.message[:DANMAKU_MAX_LENGTH]
        room.emoticons = _clean_emoticons(room.emoticons)
        room.story_chunk_size = _clamp(room.story_chunk_size, 1, DANMAKU_MAX_LENGTH)
        room.limit_count = _clamp(room.limit_count, 0, MAX_LIMIT_COUNT)
        room.limit_minutes = _clamp(room.limit_minutes, 0, MAX_LIMIT_MINUTES)
        if parse_hhmm(room.window_start) is None or parse_hhmm(room.window_end) is None:
            room.window_start, room.window_end = DEFAULT_WINDOW


def load_config() -> AppConfig:
    path = config_path()
    if not path.exists():
        return AppConfig()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        cfg = AppConfig(rooms=[RoomConfig.from_dict(r) for r in raw.get("rooms", [])])
        for f in fields(AppConfig):
            if f.name != "rooms" and f.name in raw:
                setattr(cfg, f.name, raw[f.name])
        _normalize(cfg)
        return cfg
    except (OSError, ValueError, TypeError, AttributeError) as e:
        # 文件坏了就另存一份备份，用默认配置启动，不让程序起不来
        backup = path.with_name("config.broken.json")
        try:
            os.replace(path, backup)
        except OSError:
            pass
        log.warning("配置文件读取失败（%s），已备份为 %s，改用默认配置", e, backup)
        return AppConfig()


def save_config(cfg: AppConfig) -> None:
    path = config_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(cfg), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
