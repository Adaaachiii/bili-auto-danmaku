@echo off
rem 运行环境放在用户目录下的 .venvs 里，不放在程序文件夹：
rem 有的文件夹带有“低完整性”标记，放在里面的 Python 只能以低权限运行，打开 Edge 会弹提权提示，托盘图标也不显示。
cd /d "%~dp0"
set "VENV=%USERPROFILE%\.venvs\bili-auto-danmaku"
if not exist "%VENV%\Scripts\pythonw.exe" (
  echo 第一次运行，正在安装需要的组件，请稍等……
  python -m venv "%VENV%" || goto :fail
  "%VENV%\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
)
start "" "%VENV%\Scripts\pythonw.exe" "%~dp0main.py"
exit /b 0

:fail
echo.
echo 安装失败，请把上面的报错信息发给我。
pause
