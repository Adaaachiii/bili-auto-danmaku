"""发送结果识别测试：B 站返回的 code 和 msg 分到成功 / 系统屏蔽 / 主播屏蔽 / 限流 / 禁言 / 登录失效 / 出错。

运行：python tests/test_send_result.py（不联网）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.bili_api import SendOutcome as O, classify_send_response, filter_kind  # noqa: E402

cases = [
    # (code, msg, 应该分到, 结果里的说明要包含)
    (0, "", O.OK, ""),
    (0, "ok", O.OK, ""),
    (0, "f", O.FILTERED, "f"),
    (0, "k", O.FILTERED, "k"),
    (0, "msg in 1s", O.RATE_LIMITED, "msg in 1s"),  # 旧接口发太快：code 0 也不能算成被屏蔽
    (0, "msg repeat", O.RATE_LIMITED, "msg repeat"),
    (10031, "您发送弹幕的频率过快", O.RATE_LIMITED, "频率过快"),
    (10031, "", O.RATE_LIMITED, "发送频率过快"),  # 只认返回码
    (-101, "账号未登录", O.AUTH_INVALID, "未登录"),
    (-111, "csrf 校验失败", O.AUTH_INVALID, "csrf"),
    (-111, "", O.AUTH_INVALID, "登录已失效"),
    (1003212, "超出限制长度", O.ERROR, "调小"),
    (1003212, "", O.ERROR, "1003212"),
    (-400, "参数错误", O.ERROR, "参数错误"),
    (12345, "你被禁言啦", O.MUTED, "禁言"),
    (12345, "你已被主播拉黑", O.MUTED, "拉黑"),
    (12345, "", O.ERROR, "错误码 12345"),
]
for code, msg, expect, contains in cases:
    result = classify_send_response(code, msg)
    assert result.outcome is expect, (code, msg, result)
    assert contains in result.message, (code, msg, result.message)
print(f"返回码识别 OK（{len(cases)} 种情况）")

assert filter_kind("f") == "系统屏蔽" and filter_kind("K") == "主播屏蔽" and filter_kind("x") == "被 B 站过滤"
print("系统屏蔽 / 主播屏蔽 OK")
print("全部通过")
