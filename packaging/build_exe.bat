@echo off
chcp 65001 >nul
cd /d "%~dp0.."
title 打包 MobileAuto.exe

if not exist venv (
    echo 创建虚拟环境 venv...
    python -m venv venv
)

call venv\Scripts\activate.bat
python -m pip install -r requirements.txt -r packaging\requirements-dev.txt

echo 开始 PyInstaller 打包...
pyinstaller --noconfirm --clean --windowed --name MobileAuto --paths src --add-data "src\mobileauto\web;src\mobileauto\web" --collect-submodules webview main.py

echo 打包完成，输出目录：dist\MobileAuto
pause
