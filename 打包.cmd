@echo off
rem 把程序打包成 exe：结果在 dist 文件夹里（B站弹幕自动化 文件夹，和发布用的 zip 压缩包）。
rem 打包用的环境放在 .build 里，和日常运行用的环境分开，只装需要的组件，打出来的包更小。
cd /d "%~dp0"
set "VENV=%~dp0.build\venv"
rem PyInstaller 和 pip 的缓存也放进 .build，不往用户目录里写
set "PYINSTALLER_CONFIG_DIR=%~dp0.build\pyinstaller"
set "PIP_CACHE_DIR=%~dp0.build\pip-cache"
if not exist "%VENV%\Scripts\python.exe" (
  echo 第一次打包，正在准备打包环境，请稍等……
  python -m venv "%VENV%" || goto :fail
)
"%VENV%\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt pyinstaller || goto :fail
"%VENV%\Scripts\python.exe" tools\build_exe.py || goto :fail
echo.
pause
exit /b 0
:fail
echo.
echo 打包失败，请把上面的报错信息发给我。
pause
