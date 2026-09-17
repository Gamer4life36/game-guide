@echo off
cd /d "%~dp0"
rem Needs Python 3.12 (python.org installer, with the py launcher)
start "" pyw -3.12 server.py
