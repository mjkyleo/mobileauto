@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  手机自动化工作台（scrcpy / Electron 版）
echo ============================================
cd next
if not exist node_modules (
  echo [启动] 首次运行，正在安装依赖...
  npm install --no-audit --no-fund
)
echo [启动] 正在编译...
npm run build
if errorlevel 1 (
  echo [错误] 编译失败
  pause
  exit /b 1
)
echo [启动] 正在打开工作台...
npm start
pause
