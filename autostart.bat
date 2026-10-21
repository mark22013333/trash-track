@echo off
setlocal EnableExtensions
rem 供 Windows 工作排程器開機啟動使用：無視窗互動、不暫停，固定使用 5000 埠供 Cloudflare Tunnel 轉送。

cd /d "%~dp0"
if errorlevel 1 exit /b 1

if not exist "logs" mkdir "logs"
set "VENV_PYTHON=%CD%\.venv\Scripts\python.exe"
set "TIMESTAMP=session"
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss" 2^>nul') do set "TIMESTAMP=%%I"
set "LOG_FILE=%CD%\logs\autostart-%TIMESTAMP%.log"

if not exist "%VENV_PYTHON%" (
    >>"%LOG_FILE%" echo [錯誤] 找不到 .venv，請先執行一次 start.bat 建立虛擬環境。
    exit /b 1
)

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PORT=5000"

>>"%LOG_FILE%" echo [啟動] %DATE% %TIME% 由工作排程器啟動 TrashTrack（連接埠 %PORT%）
"%VENV_PYTHON%" "service_runner.py" >>"%LOG_FILE%" 2>&1
set "SERVER_EXIT=%ERRORLEVEL%"
>>"%LOG_FILE%" echo [停止] %DATE% %TIME% TrashTrack 結束（狀態碼：%SERVER_EXIT%）
exit /b %SERVER_EXIT%
