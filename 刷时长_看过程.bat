@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 汽水音乐刷时长 - 实时过程

set PYTHONPATH=%~dp0src
rem 与工作台任务列表里的 qishui_time.json 是同一份任务；修改 JSON 即改这里的行为
set MOBILEAUTO_TASK_KEY=qishui_time
rem 轮数上限：任务 JSON 默认 30 轮；这里可覆盖，0=无限（识别到"明日再来"仍自动停）
if not defined QISHUI_MAX_ROUNDS set QISHUI_MAX_ROUNDS=30
set MOBILEAUTO_MAX_LOOPS=%QISHUI_MAX_ROUNDS%

echo ============================================================
echo   汽水音乐刷时长（最多 %QISHUI_MAX_ROUNDS% 轮，0=无限）
echo   任务来源：tasks\qishui_time.json （工作台任务列表可编辑/看流程图）
echo   领完当月自动识别"明日再来"并输出畅听权益截止时间
echo   每一步实时显示在这里，同时保存到 runtime\flow_live.log
echo   停止：按 Ctrl+C，或直接关闭本窗口
echo ============================================================
echo.

if not exist runtime mkdir runtime
if not exist runtime\debug_clicks mkdir runtime\debug_clicks

python -u -m mobileauto.app_task

echo.
echo 脚本已停止。日志在 runtime\flow_live.log；任务 JSON 在 tasks\qishui_time.json
pause
