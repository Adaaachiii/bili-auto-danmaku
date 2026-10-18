"""监控引擎：后台线程里轮询开播状态，按各直播间的设置发送弹幕。

两种发送模式：
- 独轮车：按间隔一直发同一句；
- 说书人：把一段长文字按顺序切成多条，一条接一条地发，进度记在 data\\progress.json。

界面调用 set_xxx / start_session 等方法改设置（线程安全），
引擎通过 Qt 信号把状态变化、日志和通知告诉界面。
每次“开始发弹幕 → 停止发弹幕”的统计写进历史记录（history.py）。
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass, replace
from enum import Enum

from PySide6.QtCore import QObject, Signal

from . import bili_api, story
from .bili_api import BiliApiError, Credentials, SendOutcome, SendResult
from .config import MODE_REPEAT, MODE_STORY, ON_START_SEND_AND_OPEN, ON_START_WAIT, RoomConfig
from .history import HistoryStore
from .story import StoryText

log = logging.getLogger(__name__)

# 连续几次查到“未开播”才认定下播，防止主播断流重连时误判
OFFLINE_CONFIRM_COUNT = 2
# 连续失败这么多次就停掉本场发送，免得一直刷错误
MAX_CONSECUTIVE_FAILURES = 5


class Session(Enum):
    """一场直播（或一段未开播期间）里的发送状态。"""

    IDLE = "idle"  # 不发送（未开播，且没设置“未开播时也发送”）
    SENDING = "sending"  # 正在按间隔发送
    WAIT_NEXT = "wait_next"  # 启动时已在播，按设置本次直播不发送
    STOPPED = "stopped"  # 本场已停发（手动停，或连续失败）
    MUTED = "muted"  # 本场被禁言
    FINISHED = "finished"  # 说书人模式：整段文字已经说完


@dataclass(frozen=True)
class RoomView:
    """给界面显示用的直播间状态快照。"""

    room_id: int
    uname: str
    title: str
    live: bool | None  # None 表示还没检测出结果
    live_status: int
    session: Session
    sent_count: int  # 本场已发送条数
    last_result: str
    last_ok: bool
    error: str  # 检测开播状态失败时的原因
    mode: str
    story_pos: int  # 说书进度：已经说到第几个字
    story_len: int  # 说书文字的总字数（整理后）
    story_missing: bool  # 说书人模式但文字文件不见了


@dataclass
class _Pending:
    """这一轮要发的一条弹幕。"""

    room_id: int
    name: str
    text: str
    story_from: int | None = None  # 说书人模式：这条从哪个位置开始
    story_to: int | None = None  # 说书人模式：发完后进度移到哪里


@dataclass
class _RoomState:
    cfg: RoomConfig
    uname: str = ""
    title: str = ""
    live: bool | None = None
    live_status: int = 0
    offline_streak: int = 0
    session: Session = Session.IDLE
    next_send_at: float = 0.0
    sent_count: int = 0
    failures: int = 0
    last_result: str = ""
    last_ok: bool = True
    error: str = ""
    story: StoryText | None = None
    story_pos: int = 0
    generation: int = 0  # 每开始一轮发送加 1，用来判断历史记录是不是该另起一条
    end_reason: str = ""  # 这一轮为什么停下，写进历史记录
    record_id: int | None = None  # 正在记的那条历史记录
    record_gen: int = 0  # 那条记录属于第几轮

    @property
    def name(self) -> str:
        return self.uname or self.cfg.uname or f"房间 {self.cfg.room_id}"

    @property
    def is_story(self) -> bool:
        return self.cfg.mode == MODE_STORY

    def reset(self) -> None:
        """回到“还没检测过”的状态，下一次检测按“启动时已在直播”的规则处理。"""
        self.live = None
        self.offline_streak = 0
        self.session = Session.IDLE
        self.error = ""

    def begin_session(self, new_live: bool = False) -> None:
        """开始发送。new_live 表示这是新的一场直播：没开“说书状态继承”时，说书从头开始。"""
        self.session = Session.SENDING
        self.generation += 1
        self.sent_count = 0
        self.failures = 0
        self.next_send_at = time.monotonic()
        if new_live and self.is_story and not self.cfg.story_inherit:
            self.story_pos = 0

    def can_send_now(self) -> bool:
        """直播中，或者未开播但设置了“未开播时也发送”。"""
        return self.live is True or (self.live is False and self.cfg.send_when_offline)

    def view(self) -> RoomView:
        return RoomView(
            room_id=self.cfg.room_id,
            uname=self.uname or self.cfg.uname,
            title=self.title,
            live=self.live,
            live_status=self.live_status,
            session=self.session,
            sent_count=self.sent_count,
            last_result=self.last_result,
            last_ok=self.last_ok,
            error=self.error,
            mode=self.cfg.mode,
            story_pos=self.story_pos,
            story_len=self.story.length if self.story else self.cfg.story_chars,
            story_missing=self.is_story and self.story is None,
        )


class Engine(QObject):
    room_changed = Signal(int)  # 房间号
    log_message = Signal(str, str)  # (级别 info / warn / error, 内容)
    notify = Signal(str, str)  # (标题, 内容)
    open_page = Signal(int)  # 房间号
    auth_invalid = Signal(str)  # B 站返回的原因
    history_changed = Signal()  # 有历史记录开始或结束

    def __init__(self, poll_interval: int, on_start_live: str, history: HistoryStore | None = None):
        super().__init__()
        self._history = history
        self._lock = threading.RLock()
        self._rooms: dict[int, _RoomState] = {}
        self._poll_interval = poll_interval
        self._on_start_live = on_start_live
        self._creds: Credentials | None = None
        self._paused = False
        self._poll_requested = True
        self._next_poll_at = 0.0
        self._progress = story.load_progress()  # {房间号: (位置, 文字指纹)}
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=3)

    # ------------------------------------------------------------ 界面调用（线程安全）

    def set_rooms(self, rooms: list[RoomConfig]) -> None:
        events: list[tuple] = []
        with self._lock:
            updated: dict[int, _RoomState] = {}
            for cfg in rooms:
                cfg = replace(cfg)  # 复制一份，界面那边再改也不影响引擎
                state = self._rooms.get(cfg.room_id)
                if state is None:
                    state = _RoomState(cfg=cfg)
                    self._poll_requested = True
                else:
                    was_enabled = state.cfg.enabled
                    was_offline_send = state.cfg.send_when_offline
                    state.cfg = cfg
                    if not cfg.enabled:
                        state.reset()
                    elif not was_enabled:
                        state.reset()
                        self._poll_requested = True
                    else:
                        # 间隔改短了立刻生效，不用等上一个长间隔走完
                        state.next_send_at = min(state.next_send_at, time.monotonic() + cfg.interval_sec)
                        # 未开播期间改了“未开播时也发送”，马上按新设置开始或停止
                        if state.live is False and cfg.send_when_offline != was_offline_send:
                            if cfg.send_when_offline:
                                state.begin_session()
                            else:
                                state.session = Session.IDLE
                                state.end_reason = "关闭了“未开播时也发送”"
                self._sync_story(state, events)
                updated[cfg.room_id] = state
            for room_id, state in self._rooms.items():
                if room_id not in updated and state.record_id is not None:
                    self._close_record(state, time.time(), events, "删除了直播间")
            self._rooms = updated
        self._dispatch(events)
        self._wake.set()

    def set_poll_interval(self, seconds: int) -> None:
        with self._lock:
            if seconds != self._poll_interval:
                self._poll_interval = seconds
                self._next_poll_at = min(self._next_poll_at, time.monotonic() + seconds)
        self._wake.set()

    def set_on_start_live(self, mode: str) -> None:
        with self._lock:
            self._on_start_live = mode

    def set_credentials(self, creds: Credentials | None) -> None:
        with self._lock:
            self._creds = creds
        self._wake.set()

    def has_credentials(self) -> bool:
        return self._creds is not None

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            self._paused = paused
            if not paused:
                self._poll_requested = True
        self._wake.set()

    def is_paused(self) -> bool:
        return self._paused

    def request_poll(self) -> None:
        self._poll_requested = True
        self._wake.set()

    def seconds_until_poll(self) -> int:
        return max(0, math.ceil(self._next_poll_at - time.monotonic()))

    def start_session(self, room_id: int) -> bool:
        """手动开始本场发送。主播没在直播、又没开“未开播时也发送”时返回 False。"""
        with self._lock:
            state = self._rooms.get(room_id)
            if state is None or not state.cfg.enabled or not state.can_send_now():
                return False
            if state.session is Session.SENDING:
                state.end_reason = "手动重新开始本场发送"
            state.begin_session()
            name = state.name
        self._emit_log("info", f"[{name}] 手动开始本场发送")
        self.room_changed.emit(room_id)
        self._wake.set()
        return True

    def stop_session(self, room_id: int) -> bool:
        """手动停止本场发送，下次开播会照常自动发送。"""
        with self._lock:
            state = self._rooms.get(room_id)
            if state is None or state.session is not Session.SENDING:
                return False
            state.session = Session.STOPPED
            state.end_reason = "手动停止"
            name, count = state.name, state.sent_count
        self._emit_log("info", f"[{name}] 手动停止本场发送（本场已发 {count} 条）")
        self.room_changed.emit(room_id)
        return True

    def reset_story(self, room_id: int) -> bool:
        """说书进度回到开头。已经说完的话，能发的时候马上从头再说。"""
        with self._lock:
            state = self._rooms.get(room_id)
            if state is None or not state.is_story:
                return False
            state.story_pos = 0
            if state.session is Session.FINISHED:
                if state.cfg.enabled and state.can_send_now():
                    state.begin_session()
                else:
                    state.session = Session.IDLE
            self._record_progress(state)
            name = state.name
        self._emit_log("info", f"[{name}] 说书进度已重置，从头开始")
        self.room_changed.emit(room_id)
        self._wake.set()
        return True

    def view(self, room_id: int) -> RoomView | None:
        with self._lock:
            state = self._rooms.get(room_id)
            return state.view() if state else None

    # ------------------------------------------------------------ 说书文字与进度

    def _sync_story(self, state: _RoomState, events: list[tuple]) -> None:
        """说书文字有变化时重新读文件；文字换了，进度从头算。调用时已持有锁。"""
        cfg = state.cfg
        if cfg.mode != MODE_STORY or not cfg.story_hash:
            state.story = None
            return
        if state.story is not None and state.story.hash == cfg.story_hash:
            return
        raw = story.load_room_story(cfg.room_id)
        if raw is None:
            state.story = None
            events.append(("log", "error", f"[{state.name}] 找不到说书文字文件，请编辑这个直播间重新设置说书内容"))
            return
        text = StoryText(raw)
        saved = self._progress.get(cfg.room_id)
        state.story = text
        state.story_pos = saved[0] if saved and saved[1] == text.hash and saved[0] <= text.length else 0

    def _record_progress(self, state: _RoomState) -> None:
        """把说书进度写进文件，程序重启后也能接着说。调用时已持有锁。"""
        if state.story is None:
            return
        self._progress[state.cfg.room_id] = (state.story_pos, state.story.hash)
        live_ids = set(self._rooms)
        self._progress = {rid: item for rid, item in self._progress.items() if rid in live_ids}
        try:
            story.save_progress(self._progress)
        except OSError as e:
            log.warning("保存说书进度失败：%s", e)

    # ------------------------------------------------------------ 后台线程

    def _run(self) -> None:
        session = bili_api.new_session()
        bili_api.init_anonymous_cookies(session)
        while not self._stop.is_set():
            try:
                if not self._paused:
                    if self._poll_requested or time.monotonic() >= self._next_poll_at:
                        self._poll_requested = False
                        self._next_poll_at = time.monotonic() + self._poll_interval
                        self._poll(session)
                        self._next_poll_at = time.monotonic() + self._poll_interval
                    self._send_due(session)
                self._check_records()
            except Exception:  # 后台线程不能因为一个意外错误就整个停掉
                log.exception("监控线程出错")
                self._emit_log("error", "监控出现意外错误，详情见日志文件；程序会继续运行")
            self._wake.wait(0.5)
            self._wake.clear()

    def _poll(self, session) -> None:
        with self._lock:
            targets = {rid: state.cfg.uid for rid, state in self._rooms.items() if state.cfg.enabled}
        if not targets:
            return

        statuses: dict[int, bili_api.LiveStatus] = {}
        uids = [uid for uid in targets.values() if uid]
        if uids:
            try:
                statuses = bili_api.get_live_status_by_uids(session, uids)
            except BiliApiError as e:
                self._emit_log("warn", f"批量查询开播状态失败，改为逐个查询：{e}")

        for room_id, uid in targets.items():
            status = statuses.get(uid)
            if status is not None:
                self._apply_status(room_id, status.live_status, status.uname, status.title)
                continue
            try:
                info = bili_api.get_room_info(session, room_id)
            except BiliApiError as e:
                self._apply_error(room_id, str(e))
                continue
            self._apply_status(room_id, info.live_status, "", "")

    def _apply_error(self, room_id: int, message: str) -> None:
        with self._lock:
            state = self._rooms.get(room_id)
            if state is None:
                return
            first_time = state.error != message
            state.error = message
            name = state.name
        if first_time:  # 同样的错误只记一次，网络断开时不刷屏
            self._emit_log("error", f"[{name}] 检测开播状态失败：{message}")
        self.room_changed.emit(room_id)

    def _apply_status(self, room_id: int, live_status: int, uname: str, title: str) -> None:
        events: list[tuple] = []
        with self._lock:
            state = self._rooms.get(room_id)
            if state is None or not state.cfg.enabled:
                return
            if uname:
                state.uname = uname
            if title:
                state.title = title
            name = state.name
            if state.error:
                state.error = ""
                events.append(("log", "info", f"[{name}] 开播状态检测已恢复"))
            state.live_status = live_status
            previous = state.live

            if live_status == bili_api.LIVE_ON:
                state.offline_streak = 0
                state.live = True
                if previous is None:  # 刚开始监控时主播已经在播
                    if self._on_start_live == ON_START_WAIT:
                        state.session = Session.WAIT_NEXT
                        events.append(("log", "info", f"[{name}] 正在直播，按设置本次直播不发送"))
                    else:
                        state.begin_session(new_live=True)
                        events.append(("log", "info", f"[{name}] 正在直播，开始发送弹幕{self._story_hint(state)}"))
                        if self._on_start_live == ON_START_SEND_AND_OPEN and state.cfg.open_browser:
                            events.append(("open", room_id))
                elif previous is False:  # 开播
                    if state.session is Session.SENDING:
                        state.end_reason = "开播了，另起一条记录"
                    state.begin_session(new_live=True)
                    events.append(("log", "info", f"[{name}] 开播了，开始发送弹幕{self._story_hint(state)}"))
                    events.append(("notify", f"{name} 开播了", "已开始自动发送弹幕"))
                    if state.cfg.open_browser:
                        events.append(("open", room_id))
            elif previous is True:
                state.offline_streak += 1
                if state.offline_streak >= OFFLINE_CONFIRM_COUNT:  # 下播
                    state.live = False
                    count = state.sent_count
                    state.end_reason = "下播"
                    if state.cfg.send_when_offline:
                        state.end_reason = "下播了，按设置继续发送（另起一条记录）"
                        state.begin_session()
                        events.append(("log", "info", f"[{name}] 下播了（本场共发 {count} 条），按设置未开播时继续发送"))
                        events.append(("notify", f"{name} 下播了", "按设置未开播时继续发送弹幕"))
                    else:
                        state.session = Session.IDLE
                        events.append(("log", "info", f"[{name}] 下播了，停止发送（本场共发 {count} 条）"))
                        events.append(("notify", f"{name} 下播了", f"已停止发送，本场共发 {count} 条"))
            elif previous is None:  # 刚开始监控时主播没在播
                state.live = False
                if state.cfg.send_when_offline:
                    state.begin_session()
                    events.append(("log", "info", f"[{name}] 当前未开播，按设置未开播时也发送，开始发送弹幕"))
                else:
                    state.session = Session.IDLE
                    events.append(("log", "info", f"[{name}] 当前未开播，等待开播"))
        events.append(("changed", room_id))
        self._dispatch(events)

    @staticmethod
    def _story_hint(state: _RoomState) -> str:
        if not state.is_story or state.story is None:
            return ""
        if state.story_pos == 0:
            return "（说书从头开始）"
        return f"（说书接着上次，从 {state.story_pos / state.story.length:.1%} 处继续）"

    def _send_due(self, session) -> None:
        creds = self._creds
        if creds is None:
            return
        events: list[tuple] = []
        pending: list[_Pending] = []
        now = time.monotonic()
        with self._lock:
            for rid, state in self._rooms.items():
                if not (
                    state.cfg.enabled
                    and state.can_send_now()
                    and state.session is Session.SENDING
                    and now >= state.next_send_at
                ):
                    continue
                if state.cfg.mode == MODE_REPEAT:
                    if state.cfg.message:
                        pending.append(_Pending(rid, state.name, state.cfg.message))
                    continue
                item = self._next_story_item(state, events)
                if item is not None:
                    pending.append(item)
        self._dispatch(events)

        for item in pending:
            if self._stop.is_set() or self._paused or self._creds is not creds:
                return
            sent_at = time.time()
            try:
                result = bili_api.send_danmaku(session, creds, item.room_id, item.text)
                if result.outcome is SendOutcome.ERROR and bili_api.get_account_info(session, creds) is None:
                    # Cookie 坏掉时发送接口不一定返回“未登录”，查一下账号才能确定
                    result = SendResult(SendOutcome.AUTH_INVALID, result.message, result.code)
            except BiliApiError as e:
                result = SendResult(SendOutcome.ERROR, str(e))
            self._handle_send_result(item, result, sent_at)

    def _next_story_item(self, state: _RoomState, events: list[tuple]) -> _Pending | None:
        """说书人模式：切出下一条。说完了按设置停止或从头循环。调用时已持有锁。"""
        text = state.story
        if text is None or text.length == 0:
            return None
        if state.story_pos >= text.length:
            if not state.cfg.story_loop:
                self._finish_story(state, events)
                return None
            state.story_pos = 0
            self._record_progress(state)
            events.append(("log", "info", f"[{state.name}] 说完一遍了，从头再说"))
        chunk, end = text.next_chunk(state.story_pos, state.cfg.story_chunk_size, state.cfg.story_by_line)
        return _Pending(state.cfg.room_id, state.name, chunk, story_from=state.story_pos, story_to=end)

    def _finish_story(self, state: _RoomState, events: list[tuple]) -> None:
        state.session = Session.FINISHED
        state.end_reason = "说书说完了"
        events.append(("log", "info", f"[{state.name}] 说书说完了，停止发送（可以右键“说书从头开始”）"))
        events.append(("notify", f"{state.name} 的说书说完了", "整段文字已经全部发完"))

    def _handle_send_result(self, item: _Pending, result: SendResult, sent_at: float | None = None) -> None:
        events: list[tuple] = []
        stamp = time.strftime("%H:%M:%S")
        outcome = result.outcome
        name = item.name
        with self._lock:
            state = self._rooms.get(item.room_id)
            if state is None:
                return
            self._record_result(state, item, result, sent_at or time.time(), events)
            delay = state.cfg.next_delay()
            state.next_send_at = time.monotonic() + delay
            state.last_ok = outcome is SendOutcome.OK

            # 说书进度：发成功了往后走；被过滤的那段也跳过，免得一直卡在同一段上
            advanced = False
            if item.story_from is not None and outcome in (SendOutcome.OK, SendOutcome.FILTERED):
                if state.story_pos == item.story_from:  # 发送期间没人动过进度
                    state.story_pos = item.story_to
                    self._record_progress(state)
                    advanced = True

            if outcome is SendOutcome.OK:
                state.sent_count += 1
                state.failures = 0
                state.last_result = f"{stamp}  已发送"
                if item.story_from is not None and state.story:
                    events.append(("log", "info", f"[{name}] 已发送（说书 {state.story_pos / state.story.length:.1%}）：{item.text}"))
                else:
                    events.append(("log", "info", f"[{name}] 已发送：{item.text}"))
            elif outcome is SendOutcome.AUTH_INVALID:
                state.last_result = f"{stamp}  登录失效"
                self._creds = None
                events.append(("auth_invalid", result.message))
            elif outcome is SendOutcome.MUTED:
                state.session = Session.MUTED
                state.end_reason = "被禁言"
                state.last_result = f"{stamp}  被禁言"
                events.append(("log", "error", f"[{name}] 发送失败：{result.message}，本场停止发送"))
                events.append(("notify", f"{name} 的直播间发送失败", f"{result.message}，本场已停止发送"))
            else:
                state.failures += 1
                if outcome is SendOutcome.FILTERED:
                    state.last_result = f"{stamp}  被过滤（{result.message}）"
                    skipped = "，这段已跳过" if advanced else ""
                    events.append(
                        ("log", "warn", f"[{name}] 弹幕被 B 站过滤了（返回：{result.message}），可能含屏蔽词{skipped}：{item.text}")
                    )
                elif outcome is SendOutcome.RATE_LIMITED:
                    state.next_send_at = time.monotonic() + delay * 2
                    state.last_result = f"{stamp}  被限流"
                    events.append(
                        ("log", "warn", f"[{name}] 被限流：{result.message}。下一条推迟到 {delay * 2} 秒后，建议把间隔调长")
                    )
                else:
                    state.last_result = f"{stamp}  失败：{result.message}"
                    events.append(("log", "error", f"[{name}] 发送失败：{result.message}"))
                if state.failures >= MAX_CONSECUTIVE_FAILURES:
                    state.session = Session.STOPPED
                    state.end_reason = f"连续 {state.failures} 次发送失败，自动停发"
                    events.append(("log", "error", f"[{name}] 连续 {state.failures} 次发送失败，本场停止发送"))
                    events.append(("notify", f"{name} 的直播间发送失败", f"连续 {state.failures} 次失败，本场已停止发送"))

            # 刚发完最后一段：不循环的话马上标记说完
            if (
                advanced
                and state.session is Session.SENDING
                and state.story
                and state.story_pos >= state.story.length
                and not state.cfg.story_loop
            ):
                self._finish_story(state, events)
        events.append(("changed", item.room_id))
        self._dispatch(events)

    # ------------------------------------------------------------ 历史记录

    def _record_result(self, state: _RoomState, item: _Pending, result: SendResult, at: float, events: list[tuple]) -> None:
        """把一次发送结果记进历史记录；这一轮的第一条发送时新开一条记录。调用时已持有锁。"""
        if self._history is None:
            return
        try:
            if state.record_id is not None and state.record_gen != state.generation:
                self._close_record(state, at, events)  # 已经换成新的一轮了
            if state.record_id is None:
                content = state.cfg.story_title if state.is_story else state.cfg.message
                state.record_id = self._history.open_session(state.cfg.room_id, state.name, state.cfg.mode, content, at)
                state.record_gen = state.generation
                state.end_reason = ""
                events.append(("history",))
            self._history.add_result(
                state.record_id, state.cfg.room_id, state.name, at, result.outcome.value, result.message, item.text
            )
        except Exception:  # 记录写不进去不能影响发弹幕
            log.exception("写历史记录失败")
            return
        if result.outcome is SendOutcome.FILTERED:
            events.append(("filtered_csv",))

    def _record_active(self, state: _RoomState) -> bool:
        return (
            state.cfg.enabled
            and state.session is Session.SENDING
            and state.generation == state.record_gen
            and not self._paused
            and self._creds is not None
        )

    def _end_reason(self, state: _RoomState) -> str:
        if state.end_reason:
            return state.end_reason
        if not state.cfg.enabled:
            return "停用了这个直播间"
        if self._paused:
            return "暂停监控"
        if self._creds is None:
            return "登录失效或退出登录"
        return "停止发送"

    def _close_record(self, state: _RoomState, at: float, events: list[tuple], reason: str = "") -> None:
        """结束这个直播间正在记的历史记录。调用时已持有锁。"""
        record_id, state.record_id = state.record_id, None
        try:
            self._history.close_session(record_id, at, reason or self._end_reason(state))
        except Exception:
            log.exception("写历史记录失败")
        state.end_reason = ""
        events.append(("history",))

    def _check_records(self) -> None:
        """已经停止发送的直播间，把它的历史记录结束掉。后台线程每一轮都检查一次。"""
        if self._history is None:
            return
        events: list[tuple] = []
        with self._lock:
            now = time.time()
            for state in self._rooms.values():
                if state.record_id is not None and not self._record_active(state):
                    self._close_record(state, now, events)
        self._dispatch(events)

    def close_records(self, reason: str) -> None:
        """程序退出或更新前调用：结束所有还在记的历史记录。"""
        if self._history is None:
            return
        events: list[tuple] = []
        with self._lock:
            now = time.time()
            for state in self._rooms.values():
                if state.record_id is not None:
                    self._close_record(state, now, events, reason)
        self._dispatch(events)

    # ------------------------------------------------------------ 信号

    def _emit_log(self, level: str, text: str) -> None:
        getattr(log, {"warn": "warning"}.get(level, level))(text)
        self.log_message.emit(level, text)

    def _dispatch(self, events: list[tuple]) -> None:
        """在锁外发信号，避免界面回调里再调用引擎时卡住。"""
        for event in events:
            kind = event[0]
            if kind == "log":
                self._emit_log(event[1], event[2])
            elif kind == "notify":
                self.notify.emit(event[1], event[2])
            elif kind == "open":
                self.open_page.emit(event[1])
            elif kind == "changed":
                self.room_changed.emit(event[1])
            elif kind == "auth_invalid":
                self._emit_log("error", f"登录已失效（{event[1]}），已停止发送，请重新登录")
                self.auth_invalid.emit(event[1])
            elif kind == "history":
                self.history_changed.emit()
            elif kind == "filtered_csv":
                self._write_filtered_csv()

    def _write_filtered_csv(self) -> None:
        try:
            self._history.write_filtered_csv()
        except PermissionError:
            pass  # 整理表正被 Excel 打开着，下次有新的被过滤弹幕、或在历史记录页打开整理表时再写
        except OSError as e:
            log.warning("更新屏蔽词整理表失败：%s", e)
