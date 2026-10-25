"""检查更新和一键更新的测试：认版本、只从 GitHub 下载、核对大小和 SHA-256、取消、交给“从压缩包更新”的流程。

运行：python tests/test_update_check.py（不联网，GitHub 换成假的）。
"""
import hashlib
import io
import os
import shutil
import sys
import tempfile
import threading
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
(ROOT / ".test").mkdir(exist_ok=True)
WORK = Path(tempfile.mkdtemp(dir=ROOT / ".test"))
os.environ["BILI_AUTO_DANMAKU_HOME"] = str(WORK / "data")
sys.path.insert(0, str(ROOT))
import requests  # noqa: E402

from app import update_check, updater  # noqa: E402
from app.config import APP_VERSION  # noqa: E402
from app.update_check import Release, UpdateCheckError  # noqa: E402

REPO = update_check.REPO
PAYLOAD = b"PK-fake-zip-" * 5000
SHA = hashlib.sha256(PAYLOAD).hexdigest()


def release_json(version="9.9.0", digest=f"sha256:{SHA}", body="", asset_name=None, url=None, size=len(PAYLOAD)):
    name = asset_name or f"BiliAutoDanmaku-v{version}-win-x64.zip"
    return {
        "tag_name": f"v{version}",
        "html_url": f"https://github.com/{REPO}/releases/tag/v{version}",
        "body": body,
        "assets": [
            {"name": "别的文件.txt", "browser_download_url": f"https://github.com/{REPO}/releases/download/v{version}/x.txt", "size": 1},
            {"name": name, "size": size, "digest": digest,
             "browser_download_url": url or f"https://github.com/{REPO}/releases/download/v{version}/{name}"},
        ],
    }


# ---- 认版本：版本号、压缩包、校验值
r = update_check.parse_release(release_json())
assert (r.version, r.size, r.sha256) == ("9.9.0", len(PAYLOAD), SHA), r
assert r.page_url.endswith("/releases/tag/v9.9.0") and r.asset_url.startswith(update_check.DOWNLOAD_PREFIX)
assert r.newer_than(APP_VERSION) and not r.newer_than("9.9.0") and not r.newer_than("10.0")
# 接口没给 digest 时，用发布说明里写的
r = update_check.parse_release(release_json(digest=None, body=f"更新内容……\n\n压缩包 SHA-256：`{SHA.upper()}`\n"))
assert r.sha256 == SHA
assert update_check.parse_release(release_json(digest=None)).sha256 == ""
# 压缩包名字里的版本和标签对不上、下载地址不是本仓库的发布：不认
for bad in (release_json(asset_name="BiliAutoDanmaku-v1.0.0-win-x64.zip"),
            release_json(url="https://evil.example/BiliAutoDanmaku-v9.9.0-win-x64.zip"),
            {**release_json(), "tag_name": "最新版"}):
    try:
        update_check.parse_release(bad)
    except UpdateCheckError:
        pass
    else:
        raise AssertionError(f"不该认：{bad}")
# 发布页地址不对劲时，改用仓库的发布列表
assert update_check.parse_release({**release_json(), "html_url": "https://evil.example/"}).page_url == update_check.RELEASES_PAGE
print("认版本 OK")


# ---- 假的 GitHub
class FakeGitHub(requests.adapters.BaseAdapter):
    def __init__(self, routes):
        super().__init__()
        self.routes = routes  # url -> (status, headers, body)
        self.seen = []

    def send(self, request, **kwargs):
        self.seen.append((request.url, dict(request.headers)))
        status, headers, body = self.routes.get(request.url, (404, {}, b""))
        response = requests.Response()
        response.status_code, response.url, response.request = status, request.url, request
        response.headers.update(headers)
        response.raw = io.BytesIO(body)
        return response

    def close(self):
        pass


def session_with(routes):
    session = update_check.new_session()
    adapter = FakeGitHub(routes)
    session.mount("https://", adapter)
    return session, adapter


import json  # noqa: E402

session, gh = session_with({update_check.LATEST_API: (200, {}, json.dumps(release_json()).encode())})
latest = update_check.fetch_latest(session)
assert latest.version == "9.9.0"
_url, headers = gh.seen[0]
assert "Cookie" not in headers and headers["User-Agent"] == f"BiliAutoDanmaku/{APP_VERSION}", headers
for status, words in ((404, "还没有公开发布"), (403, "过一会再试"), (500, "HTTP 500")):
    session, _ = session_with({update_check.LATEST_API: (status, {}, b"{}")})
    try:
        update_check.fetch_latest(session)
    except UpdateCheckError as e:
        assert words in str(e), e
    else:
        raise AssertionError(status)
print("查最新版本 OK")

# ---- 下载：跟着跳转到 githubusercontent，核对通过才留下文件
release = update_check.parse_release(release_json())
CDN = "https://release-assets.githubusercontent.com/some/path?sig=1"
ok_routes = {release.asset_url: (302, {"Location": CDN}, b""), CDN: (200, {}, PAYLOAD)}
dest = WORK / "dl" / "new.zip"
seen_progress = []
session, gh = session_with(ok_routes)
update_check.download(release, dest, lambda got, total: seen_progress.append((got, total)), session=session)
assert dest.read_bytes() == PAYLOAD and seen_progress[-1] == (len(PAYLOAD), len(PAYLOAD))
assert not dest.with_name("new.zip.part").exists()
assert all("Cookie" not in h for _u, h in gh.seen)
dest.unlink()
print("下载并核对 OK")


def must_fail(release_, routes, words, cancel=None):
    session_, _ = session_with(routes)
    try:
        update_check.download(release_, dest, cancel=cancel, session=session_)
    except UpdateCheckError as e:
        assert words in str(e), e
    else:
        raise AssertionError(words)
    assert not dest.exists() and not dest.with_name("new.zip.part").exists(), "失败时不能留下文件"


# 校验值对不上、大小对不上、跳到别的网站、http、没有校验值、取消
must_fail(Release(**{**release.__dict__, "sha256": "0" * 64}), ok_routes, "校验值对不上")
must_fail(Release(**{**release.__dict__, "size": len(PAYLOAD) + 10}), ok_routes, "不完整")
must_fail(Release(**{**release.__dict__, "size": len(PAYLOAD) - 10}), ok_routes, "比 GitHub 上写的大")
must_fail(release, {release.asset_url: (302, {"Location": "https://evil.example/x.zip"}, b"")}, "不是 GitHub")
must_fail(release, {release.asset_url: (302, {"Location": "http://objects.githubusercontent.com/x"}, b"")}, "不是 GitHub")
must_fail(Release(**{**release.__dict__, "sha256": ""}), ok_routes, "没有提供校验值")
must_fail(release, {release.asset_url: (302, {"Location": release.asset_url}, b"")}, "跳转次数太多")
stop = threading.Event()
stop.set()
try:
    session, _ = session_with(ok_routes)
    update_check.download(release, dest, cancel=stop, session=session)
except update_check.DownloadCancelled:
    assert not dest.exists() and not dest.with_name("new.zip.part").exists()
else:
    raise AssertionError("应该取消")
print("下载出错都不留文件 OK")

# ---- 下载好的压缩包交给“从压缩包更新”：一键更新把它放在程序文件夹里，解压后删掉；更新完 cleanup 会清理残留
target = WORK / "app"
target.mkdir()
fake_zip = target / updater.DOWNLOAD_NAME
with zipfile.ZipFile(fake_zip, "w") as z:
    z.writestr("B站弹幕自动化/B站弹幕自动化.exe", b"not really an exe")
    z.writestr("B站弹幕自动化/_internal/x.txt", b"x")
try:
    updater.prepare_from_zip(fake_zip, target)
except updater.UpdateError as e:
    assert "不是本程序" in str(e), e  # 假的 exe 过不了检查
else:
    raise AssertionError("假的 exe 应该过不了检查")
assert fake_zip.exists(), "prepare_from_zip 不能把要解压的压缩包删掉（下载的压缩包不在 _update 里）"
(target / f"{updater.DOWNLOAD_NAME}.part").write_bytes(b"x")
assert updater.cleanup(target)
assert not fake_zip.exists() and not (target / f"{updater.DOWNLOAD_NAME}.part").exists()
print("和从压缩包更新衔接 OK")

shutil.rmtree(WORK, ignore_errors=True)
print("全部通过")
