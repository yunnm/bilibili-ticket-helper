@echo off
chcp 65001 >nul
cd /d "%~dp0"
python start_gui.py
pause
