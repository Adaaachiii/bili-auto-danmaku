"""检查更新和一键更新：查 GitHub 上最新发布的版本，下载并核对它的压缩包。

只连 GitHub：查版本用 api.github.com，下载用 github.com（会跳转到 *.githubusercontent.com）。
不带任何 B站 的登录信息，GitHub 能看到的只有你的 IP 和程序版本号（写在 User-Agent 里）。
下载完核对大小和 SHA-256，对不上就删掉、不更新；之后照样走 updater.prepare_from_zip 的检查
（是不是本程序、压缩包里的路径安不安全），再由 updater 替换文件，出错恢复旧版本。
所有函数都是阻塞调用，界面里要放到后台线程执行。
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

from .config import APP_ID, APP_VERSION
from .updater import version_tuple

REPO = "Adaaachiii/bili-auto-danmaku"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
DOWNLOAD_PREFIX = f"https://github.com/{REPO}/releases/download/"
ASSET_RE = re.compile(r"^BiliAutoDanmaku-v(\d+(?:\.\d+)*)-win-x64\.zip$")
VERSION_RE = re.compile(r"^\d+(?:\.\d+)*$")
# 发布说明里手写的校验值（GitHub 接口没给 digest 时用），比如“压缩包 SHA-256：`19c2…`”
NOTES_SHA_RE = re.compile(r"SHA-?256[^`\n]*`([0-9a-fA-F]{64})`")
TIMEOUT = 15
MAX_REDIRECTS = 5
MAX_SIZE = 300 * 2**20  # 压缩包现在 33 MB 左右，大得离谱的不下
CHUNK = 256 * 1024


class UpdateCheckError(Exception):
    """说明是给人看的。"""


class DownloadCancelled(UpdateCheckError):
    pass


@dataclass
class Release:
    version: str  # 比如“1.4.0”
    notes: str  # 发布说明（Markdown 原文）
    page_url: str  # 发布页面
    asset_name: str
    asset_url: str
    size: int
    sha256: str  # 小写十六进制；GitHub 接口给的，没有的话用发布说明里写的，都没有是空字符串

    def newer_than(self, version: str) -> bool:
        return version_tuple(self.version) > version_tuple(version)


def new_session() -> requests.Session:
    """和 B站 用的分开，里面没有任何 Cookie。"""
    session = requests.Session()
    session.headers.update({
        "User-Agent": f"{APP_ID}/{APP_VERSION}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    return session


def _network_error(e: requests.RequestException) -> UpdateCheckError:
    if isinstance(e, requests.Timeout):
        return UpdateCheckError("连接 GitHub 超时，请稍后再试")
    if isinstance(e, requests.ConnectionError):
        return UpdateCheckError("连不上 GitHub，请检查网络")
    return UpdateCheckError(f"网络请求失败：{e.__class__.__name__}")


def fetch_latest(session: requests.Session | None = None) -> Release:
    """查 GitHub 上最新发布的版本（不含草稿和预发布）。"""
    session = session or new_session()
    try:
        resp = session.get(LATEST_API, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise _network_error(e) from e
    if resp.status_code == 404:
        raise UpdateCheckError("GitHub 上还没有公开发布的版本")
    if resp.status_code in (403, 429):
        raise UpdateCheckError("GitHub 暂时不让查（短时间内查得太多），过一会再试")
    if resp.status_code != 200:
        raise UpdateCheckError(f"GitHub 返回 HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as e:
        raise UpdateCheckError("GitHub 返回的数据看不懂") from e
    return parse_release(data)


def parse_release(data: dict) -> Release:
    version = str(data.get("tag_name") or "").strip().lstrip("vV")
    if not VERSION_RE.match(version):
        raise UpdateCheckError(f"没认出最新版本的版本号：{data.get('tag_name')!r}")
    page_url = str(data.get("html_url") or "")
    if not page_url.startswith(RELEASES_PAGE + "/"):
        page_url = RELEASES_PAGE
    notes = str(data.get("body") or "")
    for asset in data.get("assets") or []:
        name = str(asset.get("name") or "")
        match = ASSET_RE.match(name)
        url = str(asset.get("browser_download_url") or "")
        if match and match.group(1) == version and url.startswith(DOWNLOAD_PREFIX):
            return Release(version, notes, page_url, name, url, int(asset.get("size") or 0), _sha256_of(asset, notes))
    raise UpdateCheckError(f"{version} 的发布里没有找到 Windows 压缩包")


def _sha256_of(asset: dict, notes: str) -> str:
    digest = str(asset.get("digest") or "")
    if digest.lower().startswith("sha256:") and re.fullmatch(r"[0-9a-fA-F]{64}", digest[7:]):
        return digest[7:].lower()
    match = NOTES_SHA_RE.search(notes)
    return match.group(1).lower() if match else ""


def _check_url(url: str) -> None:
    """只从 GitHub 下载：github.com 或者它存放文件用的 *.githubusercontent.com，而且必须是 https。"""
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not (host == "github.com" or host.endswith(".githubusercontent.com")):
        raise UpdateCheckError(f"下载地址不是 GitHub（{host or url}），已停止")


def download(
    release: Release,
    dest: Path,
    progress: Callable[[int, int], None] | None = None,
    cancel: threading.Event | None = None,
    session: requests.Session | None = None,
) -> Path:
    """把新版本的压缩包下载到 dest，核对大小和 SHA-256。对不上、取消或出错都会删掉下载了一半的文件。"""
    if not release.sha256:
        raise UpdateCheckError("这个版本没有提供校验值（SHA-256），为了安全没有自动下载，请到下载页手动下载")
    if not 0 < release.size <= MAX_SIZE:
        raise UpdateCheckError(f"压缩包大小不对劲（{release.size} 字节），没有下载")
    session = session or new_session()
    part = dest.with_name(dest.name + ".part")
    url = release.asset_url
    resp = None
    try:
        for _ in range(MAX_REDIRECTS + 1):  # 自己一跳一跳地跟，每一跳都检查是不是 GitHub
            _check_url(url)
            resp = session.get(url, stream=True, timeout=TIMEOUT, allow_redirects=False)
            if not resp.is_redirect:
                break
            url = urljoin(url, resp.headers["Location"])
            resp.close()
        else:
            raise UpdateCheckError("下载时跳转次数太多，已停止")
        if resp.status_code != 200:
            raise UpdateCheckError(f"下载失败：GitHub 返回 HTTP {resp.status_code}")
        sha, received = hashlib.sha256(), 0
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(part, "wb") as out:
            for chunk in resp.iter_content(CHUNK):
                if cancel is not None and cancel.is_set():
                    raise DownloadCancelled("已取消下载")
                received += len(chunk)
                if received > release.size:
                    raise UpdateCheckError("下载的文件比 GitHub 上写的大，已停止")
                sha.update(chunk)
                out.write(chunk)
                if progress is not None:
                    progress(received, release.size)
        if received != release.size:
            raise UpdateCheckError("下载不完整，请重试")
        if sha.hexdigest() != release.sha256:
            raise UpdateCheckError("下载的文件和 GitHub 上公布的校验值对不上，已经删掉，没有更新")
        os.replace(part, dest)
        return dest
    except requests.RequestException as e:
        raise _network_error(e) from e
    finally:
        if resp is not None:
            resp.close()
        part.unlink(missing_ok=True)
