@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================
echo  手机自动化工作台
echo  目录：%CD%
echo ============================================

python -c "import webview" >nul 2>nul
if errorlevel 1 (
  echo [启动] 正在安装 pywebview...
  python -m pip install pywebview
)

echo [启动] 正在启动主程序（实时输出已开启）...
echo [提示] 日志同时写入 runtime\workbench.log
if not exist runtime mkdir runtime

python -u launch_workbench.py

set EXIT_CODE=%ERRORLEVEL%
echo [退出] 工作台已关闭，返回码：%EXIT_CODE%
endlocal
pause
