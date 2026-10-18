"""B站弹幕自动化：程序入口。

运行：打包好的版本双击“B站弹幕自动化.exe”；源码版双击“启动.cmd”
（运行环境在 %USERPROFILE%\\.venvs\\bili-auto-danmaku，原因见启动.cmd 开头的说明）。
加上 --minimized 参数会直接缩小到托盘（开机自启用的就是这个）。
--install-update 是“从压缩包更新”时内部用的（见 app/updater.py）。
"""

import os
import sys
import traceback
from pathlib import Path


def _report_startup_error() -> None:
    """没有命令行窗口，出错会悄无声息。把错误存成文件并弹窗提示。"""
    if getattr(sys, "frozen", False):  # 打包成 exe 之后，存到 exe 旁边
        folder = Path(sys.executable).resolve().parent
    else:
        folder = Path(__file__).resolve().parent
    # 程序文件夹写不进去（比如在 C:\Program Files）就存到 %LOCALAPPDATA%\BiliAutoDanmaku
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "BiliAutoDanmaku"
    where = traceback.format_exc(limit=3)
    for candidate in (folder, local):
        error_file = candidate / "启动错误.txt"
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            error_file.write_text(traceback.format_exc(), encoding="utf-8")
        except OSError:
            continue
        where = f"错误信息已保存到：\n{error_file}\n\n反馈问题时请附上这个文件。"
        break
    import ctypes

    ctypes.windll.user32.MessageBoxW(None, f"程序启动失败。{where}", "B站弹幕自动化", 0x10)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--install-update"]:  # 从压缩包更新：解压出来的新版本在替换程序文件，不打开界面
        from app.updater import install_main

        sys.exit(install_main(sys.argv[2:]))
    try:
        from app.ui_main import run

        exit_code = run(sys.argv)
    except Exception:
        _report_startup_error()
        exit_code = 1
    sys.exit(exit_code)
