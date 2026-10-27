@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -c "import websocket" 2>nul || python -m pip install websocket-client
start "" pythonw "%~dp0app.pyw"
