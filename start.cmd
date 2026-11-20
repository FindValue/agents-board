@echo off
rem 启动看板（无控制台窗口）。调试请用：python board.py
cd /d "%~dp0"
start "" pythonw board.py
