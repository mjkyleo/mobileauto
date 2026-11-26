@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 手机自动化工作台

python -c "import webview" >nul 2>nul
if errorlevel 1 (
    echo 缺少 pywebview，正在自动安装...
    python -m pip install pywebview
    if errorlevel 1 (
        echo 依赖安装失败，请手动执行：python -m pip install -r requirements.txt
        pause
        exit /b 1
    )
)

python main.py
pause
