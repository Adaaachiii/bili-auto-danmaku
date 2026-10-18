"""配置文件读写。

直播间列表和各项设置保存在程序文件夹下的 data\\config.json，日志在 data\\logs。
说书用的长文字不放这里（这个文件每次改设置都要整体重写），见 story.py。
登录 Cookie 也不在这里，由 secure_store.py 存进 Windows 凭据管理器。
"""

from __future__ import annotations

import json
import logging
import os
import random
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

APP_ID = "BiliAutoDanmaku"  # 凭据管理器、开机启动项用这个名字，改了会丢登录，所以不跟着程序名改
APP_TITLE = "B站弹幕自动化"
APP_VERSION = "1.2.0"

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

# 弹幕发送模式
MODE_REPEAT = "repeat"  # 独轮车：一直发同一句
MODE_STORY = "story"  # 说书人：把一段长文字按顺序分成多条发
MODE_CHOICES = {MODE_REPEAT: "独轮车模式", MODE_STORY: "说书人模式"}

# 程序启动（或新添加、重新启用直播间）时主播已经在直播，怎么处理
ON_START_SEND_AND_OPEN = "send_and_open"
ON_START_SEND_ONLY = "send_only"
ON_START_WAIT = "wait"
ON_START_CHOICES = {
    ON_START_SEND_AND_OPEN: "立刻开始发送，并打开直播页",
    ON_START_SEND_ONLY: "立刻开始发送，不打开直播页",
    ON_START_WAIT: "本次直播不发送",
}

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


def data_dir() -> Path:
    """配置和日志所在的文件夹：程序文件夹下的 data。

    不放在 %APPDATA% 是因为有的环境不允许程序往用户目录写文件，放在自己文件夹里最稳。
    设置环境变量 BILI_AUTO_DANMAKU_HOME 可以换位置（测试用）。
    """
    override = os.environ.get("BILI_AUTO_DANMAKU_HOME")
    path = Path(override) if override else _program_dir() / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return data_dir() / "config.json"


@dataclass
class RoomConfig:
    room_id: int  # 真实房间号（长号）
    uid: int = 0  # 主播 UID，用来批量查询开播状态
    uname: str = ""  # 主播昵称，只用于显示
    mode: str = MODE_REPEAT
    message: str = ""  # 独轮车模式要循环发送的弹幕
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

    def next_delay(self) -> int:
        """下一条弹幕要等多少秒。"""
        if self.interval_random:
            return random.randint(self.interval_sec, max(self.interval_sec, self.interval_max_sec))
        return self.interval_sec

    def interval_text(self) -> str:
        if self.interval_random:
            return f"{self.interval_sec}～{self.interval_max_sec} 秒"
        return f"{self.interval_sec} 秒"

    @classmethod
    def from_dict(cls, data: dict) -> RoomConfig:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class AppConfig:
    rooms: list[RoomConfig] = field(default_factory=list)
    poll_interval_sec: int = DEFAULT_POLL_INTERVAL
    auto_monitor: bool = True  # 程序启动后自动开始检测；关掉的话启动时先处于“暂停监控”
    on_start_live: str = ON_START_SEND_AND_OPEN
    browser: str = BROWSER_EDGE
    close_action: str = CLOSE_ASK
    notify: bool = True

    def find_room(self, room_id: int) -> RoomConfig | None:
        return next((room for room in self.rooms if room.room_id == room_id), None)


def _clamp(value, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def _normalize(cfg: AppConfig) -> None:
    cfg.poll_interval_sec = _clamp(cfg.poll_interval_sec, MIN_POLL_INTERVAL, MAX_POLL_INTERVAL)
    if cfg.on_start_live not in ON_START_CHOICES:
        cfg.on_start_live = ON_START_SEND_AND_OPEN
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
        room.story_chunk_size = _clamp(room.story_chunk_size, 1, DANMAKU_MAX_LENGTH)


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
