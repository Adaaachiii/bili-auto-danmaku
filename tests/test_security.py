"""安全相关的测试：登录 Cookie 不跟着跳转发到别的网站；B 站来的文字（直播标题等）不会被当成 HTML 显示。

运行：python tests/test_security.py（不联网，网络请求换成假的）。
"""
import ast
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import requests  # noqa: E402
from PySide6.QtGui import QTextDocument  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from app import bili_api  # noqa: E402
from app.ui_common import hint_label, plain_tip, set_colored  # noqa: E402

app = QApplication(sys.argv)
creds = bili_api.Credentials("SECRET_SESSDATA", "SECRET_JCT", "123")


class Redirector(requests.adapters.BaseAdapter):
    """B 站的地址一律回 302 跳到 evil.example；记下每个请求带的 Cookie。"""

    def __init__(self):
        super().__init__()
        self.seen = []

    def send(self, request, **kwargs):
        self.seen.append((request.url, request.headers.get("Cookie", "")))
        response = requests.Response()
        response.request, response.url, response.raw = request, request.url, io.BytesIO(b"")
        if "bilibili.com" in request.url:
            response.status_code = 302
            response.headers["Location"] = "https://evil.example/collect"
            response._content = b""
        else:
            response.status_code = 200
            response._content = b'{"code":0,"data":{}}'
        return response

    def close(self):
        pass


def fake_session():
    session = bili_api.new_session()
    adapter = Redirector()
    session.mount("https://", adapter)
    return session, adapter


# ---- 带 Cookie 的接口：遇到跳转就报错，不跟过去
calls = [
    lambda s: bili_api.get_account_info(s, creds),
    lambda s: bili_api.send_danmaku(s, creds, 1, "测试"),
    lambda s: bili_api.send_emoticon(s, creds, 1, "official_1"),
    lambda s: bili_api.get_emoticons(s, creds, 1),
    lambda s: bili_api.logout(s, creds),
]
for call in calls:
    session, adapter = fake_session()
    try:
        call(session)
    except bili_api.BiliApiError as e:
        assert "跳转" in str(e), e
    else:
        raise AssertionError("遇到跳转应该报错")
    assert len(adapter.seen) == 1 and "bilibili.com" in adapter.seen[0][0], adapter.seen
    assert "SECRET_SESSDATA" in adapter.seen[0][1], "发给 B 站的请求里应该有 Cookie"
print("带 Cookie 的请求不跟跳转 OK")

# ---- 就算哪里跟了跳转，Cookie 也只发给 *.bilibili.com
session, adapter = fake_session()
session.request("GET", "https://api.live.bilibili.com/x", cookies=creds.cookies(), allow_redirects=True)
(first_url, first_cookie), (second_url, second_cookie) = adapter.seen
assert "SECRET_SESSDATA" in first_cookie and "evil.example" in second_url and "SECRET" not in second_cookie, adapter.seen
session, adapter = fake_session()
session.request("GET", "https://evil.example/direct", cookies=creds.cookies())
assert "SECRET" not in adapter.seen[0][1], "Cookie 不该发给别的网站"
print("Cookie 只发给 B 站域名 OK")

# ---- 不带 Cookie 的请求（查开播状态等）照常跟跳转，反正没有登录信息可泄露
session, adapter = fake_session()
assert bili_api._request_json(session, "GET", "https://api.live.bilibili.com/x") == {"code": 0, "data": {}}
assert all("SECRET" not in cookie for _url, cookie in adapter.seen)
print("不带 Cookie 的请求照常跟跳转 OK")

# ---- 鼠标提示：转义成普通文字，<img> 不会去读文件
png = ROOT / "tests" / "__probe__.png"
payload = f"<img src='{png.as_uri()}'><b>粗</b> & 第二行\n第二行"
doc = QTextDocument()
doc.setHtml(plain_tip(payload))
assert doc.toPlainText().replace(" ", "\n") == payload, doc.toPlainText()
assert plain_tip("") == ""
print("鼠标提示纯文字 OK")

# ---- 标签：hint_label 和 set_colored 都按纯文字显示
from PySide6.QtCore import Qt  # noqa: E402

label = hint_label("<b>x</b>")
assert label.textFormat() is Qt.TextFormat.PlainText
other = QLabel()
set_colored(other, "<img src='x'>", "#000")
assert other.textFormat() is Qt.TextFormat.PlainText
print("标签纯文字 OK")

# ---- 代码检查：界面里设置鼠标提示，除了写死的文字，都要经过 plain_tip
STATIC_NAMES = {"SESSION_HINT", "APP_TITLE"}


def _safe_tip(part) -> bool:
    """写死的文字（包括只拼了大写常量的 f-string）、已知的常量，或者经过 plain_tip 的。"""
    if isinstance(part, ast.IfExp):  # a if 条件 else b：两边都要安全
        return _safe_tip(part.body) and _safe_tip(part.orelse)
    if isinstance(part, ast.JoinedStr):
        return all(
            isinstance(v, ast.Constant) or (isinstance(v.value, ast.Name) and v.value.id.isupper()) for v in part.values
        )
    return (
        isinstance(part, ast.Constant)
        or (isinstance(part, ast.Name) and part.id in STATIC_NAMES)
        or (isinstance(part, ast.Call) and getattr(part.func, "id", "") == "plain_tip")
    )


def unsafe_tooltips(source: str) -> list[int]:
    """setToolTip 的参数不安全的行号。help_icon 里的说明文字都是写死的，不查。"""
    lines = []
    for func in ast.walk(ast.parse(source)):
        if not isinstance(func, ast.FunctionDef) or func.name == "help_icon":
            continue
        for node in ast.walk(func):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "setToolTip" and not _safe_tip(node.args[0]):
                lines.append(node.lineno)
    return lines


# 先确认检查本身管用
bad = (
    "def f(item, title):\n"
    "    item.setToolTip(title)\n"
    "    item.setToolTip(f'直播标题：{title}')\n"
    "    item.setToolTip(plain_tip(title) if title else title)\n"
    "    item.setToolTip(plain_tip(title))\n"
    "    item.setToolTip('写死的')\n"
)
assert unsafe_tooltips(bad) == [2, 3, 4], unsafe_tooltips(bad)
problems = [f"{path.name}:{line}" for path in sorted((ROOT / "app").glob("ui_*.py"))
            for line in unsafe_tooltips(path.read_text(encoding="utf-8"))]
assert not problems, f"这些鼠标提示没有经过 plain_tip：{problems}"
print("鼠标提示都经过 plain_tip OK")

print("全部通过")
