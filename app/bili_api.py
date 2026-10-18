"""B 站接口封装。

这里用到的都是 B 站网页版自己在用的接口，不是官方开放接口，以后可能会变。
所有函数都是阻塞调用，界面里要放到后台线程执行。
登录 Cookie 只会发给 *.bilibili.com，不会发到别的地方；头像从 B 站图片服务器 *.hdslb.com 下载（不带 Cookie）。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from enum import Enum
from urllib.parse import quote, urlparse

import requests

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0"
)
TIMEOUT = 10

ROOM_INIT_URL = "https://api.live.bilibili.com/room/v1/Room/room_init"
STATUS_BY_UIDS_URL = "https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids"
SEND_URL = "https://api.live.bilibili.com/msg/send"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"
QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
LOGOUT_URL = "https://passport.bilibili.com/login/exit/v2"

# 直播间 live_status 的取值
LIVE_OFFLINE = 0
LIVE_ON = 1
LIVE_ROUND = 2  # 轮播录像，按未开播处理

LIVE_STATUS_TEXT = {LIVE_OFFLINE: "未开播", LIVE_ON: "直播中", LIVE_ROUND: "轮播中"}


class BiliApiError(Exception):
    """接口调用失败：网络错误，或者 B 站返回了错误码。"""


# ---------------------------------------------------------------- 登录凭据


@dataclass
class Credentials:
    """发弹幕需要的登录 Cookie。"""

    sessdata: str
    bili_jct: str
    dede_user_id: str = ""

    def cookies(self) -> dict[str, str]:
        cookies = {"SESSDATA": self.sessdata, "bili_jct": self.bili_jct}
        if self.dede_user_id:
            cookies["DedeUserID"] = self.dede_user_id
        return cookies

    def to_dict(self) -> dict[str, str]:
        return {"SESSDATA": self.sessdata, "bili_jct": self.bili_jct, "DedeUserID": self.dede_user_id}

    @classmethod
    def from_dict(cls, data: dict) -> Credentials:
        return cls(data["SESSDATA"], data["bili_jct"], data.get("DedeUserID", ""))

    @classmethod
    def from_cookie_text(cls, text: str) -> Credentials:
        """从粘贴进来的文本里取出 SESSDATA 和 bili_jct。

        支持三种写法：整段 Cookie（a=1; b=2）、每行一个 name=value、
        以及从浏览器开发者工具的 Cookie 表格里直接复制的行（名称和值之间是 Tab）。
        """
        text = re.sub(r"^\s*cookie\s*:\s*", "", text, flags=re.IGNORECASE)
        pairs: dict[str, str] = {}
        for part in re.split(r"[;\r\n]", text):
            part = part.strip()
            if "\t" in part:
                columns = part.split("\t")
                name, value = columns[0], columns[1]
            elif "=" in part:
                name, value = part.split("=", 1)
            else:
                continue
            pairs[name.strip()] = value.strip().strip('"')

        sessdata = pairs.get("SESSDATA", "")
        bili_jct = pairs.get("bili_jct", "")
        if not sessdata:
            raise ValueError("没有找到 SESSDATA，请检查粘贴的内容")
        if not bili_jct:
            raise ValueError("没有找到 bili_jct，请检查粘贴的内容")
        # 浏览器里的 SESSDATA 是编码过的（逗号写成 %2C）。有些工具会显示解码后的样子，这里统一编码回去。
        if "%" not in sessdata and ("," in sessdata or "*" in sessdata):
            sessdata = quote(sessdata, safe="")
        return cls(sessdata, bili_jct, pairs.get("DedeUserID", ""))


# ---------------------------------------------------------------- 通用请求


def new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Referer": "https://live.bilibili.com/",
            "Accept": "application/json, text/plain, */*",
        }
    )
    return session


def _request_json(session: requests.Session, method: str, url: str, **kwargs) -> dict:
    try:
        resp = session.request(method, url, timeout=TIMEOUT, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except requests.Timeout as e:
        raise BiliApiError("网络请求超时") from e
    except requests.ConnectionError as e:
        raise BiliApiError("无法连接到 B 站，请检查网络") from e
    except requests.HTTPError as e:
        raise BiliApiError(f"B 站返回 HTTP {e.response.status_code}") from e
    except requests.RequestException as e:
        raise BiliApiError(f"网络请求失败：{e.__class__.__name__}") from e
    except ValueError as e:
        raise BiliApiError("B 站返回的数据无法解析") from e


def _check(raw: dict, action: str):
    """code 不为 0 时抛出带说明的异常，否则返回 data 部分。"""
    if raw.get("code") != 0:
        message = raw.get("message") or raw.get("msg") or "未知错误"
        raise BiliApiError(f"{action}失败：{message}（错误码 {raw.get('code')}）")
    return raw.get("data") or {}


def init_anonymous_cookies(session: requests.Session) -> None:
    """领取游客标识 buvid3。部分接口缺少它时容易被风控，领取失败也不影响使用。"""
    try:
        data = _check(_request_json(session, "GET", SPI_URL), "获取游客标识")
    except BiliApiError:
        return
    if data.get("b_3"):
        session.cookies.set("buvid3", data["b_3"], domain=".bilibili.com")
    if data.get("b_4"):
        session.cookies.set("buvid4", data["b_4"], domain=".bilibili.com")


# ---------------------------------------------------------------- 直播间与开播状态

_ROOM_URL_RE = re.compile(r"live\.bilibili\.com/(?:h5/|blanc/)?(\d+)")


def parse_room_input(text: str) -> int | None:
    """从“房间号”或“直播间链接”里取出房间号，取不到返回 None。"""
    text = text.strip()
    if text.isdigit():
        return int(text)
    match = _ROOM_URL_RE.search(text)
    return int(match.group(1)) if match else None


@dataclass
class RoomInfo:
    room_id: int  # 真实房间号（长号）
    short_id: int  # 短号，没有则为 0
    uid: int  # 主播 UID
    live_status: int


def get_room_info(session: requests.Session, room_id: int) -> RoomInfo:
    """按房间号（长号或短号都行）查询直播间，不需要登录。"""
    data = _check(_request_json(session, "GET", ROOM_INIT_URL, params={"id": room_id}), "查询直播间")
    return RoomInfo(
        room_id=int(data["room_id"]),
        short_id=int(data.get("short_id") or 0),
        uid=int(data["uid"]),
        live_status=int(data.get("live_status") or 0),
    )


@dataclass
class LiveStatus:
    uid: int
    room_id: int
    uname: str
    title: str
    live_status: int


def get_live_status_by_uids(session: requests.Session, uids: list[int]) -> dict[int, LiveStatus]:
    """一次查询多个主播的开播状态，返回 {主播 UID: 状态}，不需要登录。"""
    params = [("uids[]", str(uid)) for uid in uids]
    data = _check(_request_json(session, "GET", STATUS_BY_UIDS_URL, params=params), "查询开播状态")
    result: dict[int, LiveStatus] = {}
    for item in data.values():
        uid = int(item.get("uid") or 0)
        result[uid] = LiveStatus(
            uid=uid,
            room_id=int(item.get("room_id") or 0),
            uname=str(item.get("uname") or ""),
            title=str(item.get("title") or ""),
            live_status=int(item.get("live_status") or 0),
        )
    return result


# ---------------------------------------------------------------- 账号


@dataclass
class AccountInfo:
    uid: int
    uname: str
    level: int
    face: str = ""  # 头像图片地址


def get_account_info(session: requests.Session, creds: Credentials) -> AccountInfo | None:
    """用 Cookie 查询当前登录的账号。Cookie 无效或已过期时返回 None。"""
    raw = _request_json(session, "GET", NAV_URL, cookies=creds.cookies())
    data = raw.get("data") or {}
    if raw.get("code") == -101 or not data.get("isLogin"):
        return None
    _check(raw, "查询账号信息")
    level = (data.get("level_info") or {}).get("current_level", 0)
    return AccountInfo(
        uid=int(data.get("mid") or 0),
        uname=str(data.get("uname") or ""),
        level=int(level),
        face=str(data.get("face") or ""),
    )


def fetch_avatar(url: str) -> bytes:
    """下载头像图片。只接受 B 站自己的图片服务器（hdslb.com），不会去别的网站。"""
    host = urlparse(url).hostname or ""
    if not (host == "hdslb.com" or host.endswith(".hdslb.com")):
        raise BiliApiError("头像地址不是 B 站的图片服务器")
    try:
        resp = requests.get(url.replace("http://", "https://", 1), headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise BiliApiError(f"下载头像失败：{e.__class__.__name__}") from e
    return resp.content


def logout(session: requests.Session, creds: Credentials) -> None:
    """让这份 Cookie 在 B 站服务器上失效。"""
    raw = _request_json(session, "POST", LOGOUT_URL, data={"biliCSRF": creds.bili_jct}, cookies=creds.cookies())
    _check(raw, "注销登录")


# ---------------------------------------------------------------- 发送弹幕


class SendOutcome(Enum):
    OK = "ok"
    FILTERED = "filtered"  # B 站收下了但没显示，通常是含屏蔽词
    RATE_LIMITED = "rate_limited"  # 发得太快，或短时间内重复内容被拦
    MUTED = "muted"  # 在这个直播间被禁言或被拉黑
    AUTH_INVALID = "auth_invalid"  # 登录失效
    ERROR = "error"


@dataclass
class SendResult:
    outcome: SendOutcome
    message: str = ""
    code: int | None = None


def send_danmaku(session: requests.Session, creds: Credentials, room_id: int, text: str) -> SendResult:
    form = {
        "bubble": "0",
        "msg": text,
        "color": "16777215",
        "mode": "1",
        "room_type": "0",
        "jumpfrom": "0",
        "fontsize": "25",
        "rnd": str(int(time.time())),
        "roomid": str(room_id),
        "csrf": creds.bili_jct,
        "csrf_token": creds.bili_jct,
    }
    headers = {"Origin": "https://live.bilibili.com", "Referer": f"https://live.bilibili.com/{room_id}"}
    raw = _request_json(session, "POST", SEND_URL, data=form, cookies=creds.cookies(), headers=headers)
    return classify_send_response(raw.get("code"), str(raw.get("message") or raw.get("msg") or ""))


# 发送弹幕接口的返回码。B 站没有公开文档，下面来自社区整理（bilibili-API-collect）和开源项目
CODE_NOT_LOGGED_IN = -101  # 账号未登录（Cookie 失效）
CODE_CSRF_FAILED = -111  # csrf 校验失败（Cookie 里的 bili_jct 不对）
CODE_TOO_FAST = 10031  # 发送频率过快
CODE_TOO_LONG = 1003212  # 超出长度限制
# code 是 0 但 msg 不为空：弹幕被拦下了，只有自己看得到。f 是系统（全站）屏蔽词，k 是主播设置的屏蔽词
FILTER_KINDS = {"f": "系统屏蔽", "k": "主播屏蔽"}

# 没有返回码可认时，再看提示文字。"msg in 1s"（1 秒内发了两条）和 "msg repeat"（短时间重复同一句）是旧接口的说法
_RATE_LIMIT_HINTS = ("频率", "过快", "重复", "repeat", "in 1s")
_MUTED_HINTS = ("禁言", "拉黑")


def filter_kind(message: str) -> str:
    """被过滤时 B 站返回的 msg 换成好懂的说法：f → 系统屏蔽，k → 主播屏蔽，认不出来的叫“被 B 站过滤”。"""
    return FILTER_KINDS.get(message.strip().lower(), "被 B 站过滤")


def classify_send_response(code, message: str) -> SendResult:
    lowered = message.lower()
    rate_limited = any(hint in lowered for hint in _RATE_LIMIT_HINTS)
    if code == 0:
        if lowered in ("", "ok", "success", "0"):
            return SendResult(SendOutcome.OK, "", 0)
        if rate_limited:  # 发太快、重复也可能带着 code 0 返回，这不是屏蔽词的问题
            return SendResult(SendOutcome.RATE_LIMITED, message, 0)
        return SendResult(SendOutcome.FILTERED, message, 0)
    if code in (CODE_NOT_LOGGED_IN, CODE_CSRF_FAILED):
        return SendResult(SendOutcome.AUTH_INVALID, message or "登录已失效", code)
    if code == CODE_TOO_FAST:
        return SendResult(SendOutcome.RATE_LIMITED, message or "发送频率过快", code)
    if code == CODE_TOO_LONG:
        return SendResult(SendOutcome.ERROR, f"弹幕超出长度限制，请把每条字数调小（{message or f'错误码 {code}'}）", code)
    if rate_limited:
        return SendResult(SendOutcome.RATE_LIMITED, message, code)
    if any(hint in message for hint in _MUTED_HINTS):
        return SendResult(SendOutcome.MUTED, message, code)
    return SendResult(SendOutcome.ERROR, message or f"错误码 {code}", code)


# ---------------------------------------------------------------- 扫码登录


class QrState(Enum):
    WAITING = "waiting"  # 还没扫
    SCANNED = "scanned"  # 扫了，等手机上确认
    EXPIRED = "expired"
    SUCCESS = "success"


def qr_generate(session: requests.Session) -> tuple[str, str]:
    """申请登录二维码，返回（二维码内容, 查询用的 key）。二维码约 3 分钟后过期。"""
    data = _check(_request_json(session, "GET", QR_GENERATE_URL), "获取登录二维码")
    return data["url"], data["qrcode_key"]


def qr_poll(session: requests.Session, qrcode_key: str) -> tuple[QrState, Credentials | None]:
    data = _check(_request_json(session, "GET", QR_POLL_URL, params={"qrcode_key": qrcode_key}), "查询扫码状态")
    code = data.get("code")
    if code == 86101:
        return QrState.WAITING, None
    if code == 86090:
        return QrState.SCANNED, None
    if code == 86038:
        return QrState.EXPIRED, None
    if code == 0:
        return QrState.SUCCESS, _credentials_after_qr_login(session, str(data.get("url") or ""))
    raise BiliApiError(f"扫码登录失败：{data.get('message') or code}")


def _credentials_after_qr_login(session: requests.Session, url: str) -> Credentials:
    # 登录成功后 Cookie 既在响应的 Set-Cookie 里，也在返回的跳转链接参数里，两边都取一下
    jar = {cookie.name: cookie.value for cookie in session.cookies}
    query = url.split("?", 1)[1] if "?" in url else ""
    from_url = dict(part.split("=", 1) for part in query.split("&") if "=" in part)
    sessdata = jar.get("SESSDATA") or from_url.get("SESSDATA", "")
    bili_jct = jar.get("bili_jct") or from_url.get("bili_jct", "")
    if not sessdata or not bili_jct:
        raise BiliApiError("登录成功了，但没有拿到 Cookie，请改用“粘贴 Cookie”登录")
    return Credentials(sessdata, bili_jct, jar.get("DedeUserID") or from_url.get("DedeUserID", ""))
