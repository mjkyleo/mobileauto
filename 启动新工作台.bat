@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/"

echo ============================================
echo  手机自动化工作台（scrcpy / Electron 版）
echo ============================================

cd next
if not exist node_modules (
  echo [启动] 首次运行，正在安装依赖...
  npm install --no-audit --no-fund
)
if not exist node_modules\electron\dist\electron.exe (
  echo [启动] 正在下载 Electron 运行时，请稍候...
  node node_modules\electron\install.js
)
if not exist node_modules\electron\dist\electron.exe (
  echo [错误] Electron 运行时下载失败，请检查网络后重试
  pause
  exit /b 1
)
echo [启动] 正在编译...
npm run build
if errorlevel 1 (
  echo [错误] 编译失败
  pause
  exit /b 1
)
echo [启动] 正在打开工作台...
npx electron .
set EXIT_CODE=%ERRORLEVEL%
echo [退出] 工作台已关闭，返回码：%EXIT_CODE%
pause
