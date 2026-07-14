@echo off
cd /d "%~dp0"

..\.venv\Scripts\python.exe send_payload.py
pause
