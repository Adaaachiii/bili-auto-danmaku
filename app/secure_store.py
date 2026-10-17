"""登录 Cookie 的保存：放在 Windows 凭据管理器里。

凭据管理器里的内容由 Windows 按你的系统账户加密，其他 Windows 用户读不到；
它也不在程序文件夹或配置文件里，不会被云盘同步，也不会随程序一起分享出去。
在“控制面板 → 凭据管理器 → Windows 凭据”里能看到一条名字带 BiliAutoDanmaku 的记录。
"""

from __future__ import annotations

import json
import logging

import keyring
from keyring.errors import PasswordDeleteError

from .bili_api import Credentials

SERVICE = "BiliAutoDanmaku"
ACCOUNT = "bilibili-cookie"

log = logging.getLogger(__name__)


def load_credentials() -> Credentials | None:
    try:
        raw = keyring.get_password(SERVICE, ACCOUNT)
    except Exception as e:  # 读不到就当没登录，不能因此让程序起不来
        log.warning("读取凭据管理器失败：%s", e)
        return None
    if not raw:
        return None
    try:
        return Credentials.from_dict(json.loads(raw))
    except (ValueError, KeyError, TypeError):
        log.warning("凭据管理器里保存的登录信息格式不对，已忽略")
        return None


def save_credentials(creds: Credentials) -> None:
    keyring.set_password(SERVICE, ACCOUNT, json.dumps(creds.to_dict()))


def clear_credentials() -> None:
    try:
        keyring.delete_password(SERVICE, ACCOUNT)
    except PasswordDeleteError:
        pass
