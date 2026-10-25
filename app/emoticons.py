"""表情图片的本地缓存。

表情包列表每次打开“选择表情”时现查（换了账号、买了新表情马上就能看到），
图片下载一次后存进数据文件夹的 cache\\emoticons，以后直接读。这个文件夹删掉也没关系，用到时会重新下载。
"""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path
from urllib.parse import urlparse

from . import bili_api
from .config import data_dir

_SUFFIXES = (".png", ".gif", ".jpg", ".jpeg", ".webp")


def cache_dir() -> Path:
    return data_dir() / "cache" / "emoticons"


def cache_path(url: str) -> Path:
    suffix = Path(urlparse(url).path).suffix.lower()
    name = hashlib.sha1(url.encode("utf-8")).hexdigest() + (suffix if suffix in _SUFFIXES else ".img")
    return cache_dir() / name


def load_image(url: str) -> bytes:
    """表情图片：有缓存读缓存，没有就下载再存起来。会联网，要放在后台线程调用。"""
    path = cache_path(url)
    try:
        return path.read_bytes()
    except OSError:
        pass
    content = bili_api.fetch_image(url, "表情图片")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")  # 几个线程同时下同一张也不会互相覆盖一半
        tmp.write_bytes(content)
        os.replace(tmp, path)
    except OSError:
        pass  # 存不下就下次再下载
    return content
