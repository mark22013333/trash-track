@echo off
setlocal EnableExtensions
chcp 65001 >nul

cd /d "%~dp0"
if errorlevel 1 (
    echo [錯誤] 無法進入專案目錄：%~dp0
    pause
    exit /b 1
)

if not exist "logs" mkdir "logs"
if errorlevel 1 (
    echo [錯誤] 無法建立 logs 目錄。
    pause
    exit /b 1
)

set "TIMESTAMP=session"
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss" 2^>nul') do set "TIMESTAMP=%%I"
set "LOG_FILE=%CD%\logs\server-%TIMESTAMP%.log"
set "VENV_PYTHON=%CD%\.venv\Scripts\python.exe"

call :log "[啟動] TrashTrack 前置檢查開始"
call :log "[資訊] 專案目錄：%CD%"
call :log "[資訊] 本次 LOG：%LOG_FILE%"

if not exist "app.py" (
    call :fail "找不到 app.py，請確認 start.bat 位於專案根目錄。"
    exit /b 1
)
if not exist "requirements.txt" (
    call :fail "找不到 requirements.txt。"
    exit /b 1
)

set "PYTHON_CMD="
where py >nul 2>&1
if not errorlevel 1 (
    py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=py -3"
)

if defined PYTHON_CMD goto python_found

where python >nul 2>&1
if not errorlevel 1 (
    python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=python"
)

if defined PYTHON_CMD goto python_found

where python3 >nul 2>&1
if not errorlevel 1 (
    python3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=python3"
)

if not defined PYTHON_CMD (
    call :fail "找不到 Python 3.9 以上版本。請先從 python.org 安裝 Python 3，並勾選 Add Python to PATH。"
    exit /b 1
)

:python_found
for /f "delims=" %%I in ('%PYTHON_CMD% --version 2^>^&1') do set "PYTHON_VERSION=%%I"
call :log "[通過] 已找到 %PYTHON_VERSION%（%PYTHON_CMD%）"

if exist "%VENV_PYTHON%" goto venv_ready
if exist ".venv" (
    call :fail ".venv 已存在但不完整。請將它改名或刪除後再執行。"
    exit /b 1
)

call :log "[設定] 正在建立虛擬環境 .venv…"
%PYTHON_CMD% -m venv ".venv" >>"%LOG_FILE%" 2>&1
if errorlevel 1 (
    call :fail "虛擬環境建立失敗，詳細內容請查看 LOG。"
    exit /b 1
)
if not exist "%VENV_PYTHON%" (
    call :fail "虛擬環境建立後找不到 Python 執行檔。"
    exit /b 1
)

:venv_ready
call :log "[通過] 虛擬環境已就緒"

"%VENV_PYTHON%" -m pip --version >nul 2>&1
if errorlevel 1 (
    call :log "[設定] 虛擬環境缺少 pip，正在修復…"
    "%VENV_PYTHON%" -m ensurepip --upgrade >>"%LOG_FILE%" 2>&1
    if errorlevel 1 (
        call :fail "pip 建立失敗，詳細內容請查看 LOG。"
        exit /b 1
    )
)

"%VENV_PYTHON%" -c "import flask, requests" >nul 2>&1
if errorlevel 1 (
    call :log "[設定] 正在安裝必要套件，首次執行可能需要一些時間…"
    "%VENV_PYTHON%" -m pip install -r "requirements.txt" >>"%LOG_FILE%" 2>&1
    if errorlevel 1 (
        call :fail "套件安裝失敗。請檢查網路連線與 LOG 內的 pip 訊息。"
        exit /b 1
    )
) else (
    call :log "[通過] Flask 與 requests 已安裝"
)

set "APP_PORT=5000"
"%VENV_PYTHON%" -c "import socket,sys; s=socket.socket(); s.settimeout(0.5); used=s.connect_ex(('127.0.0.1',int(sys.argv[1]))) == 0; s.close(); sys.exit(0 if used else 1)" "%APP_PORT%" >nul 2>&1
if errorlevel 1 goto port_ready

call :log "[警告] 連接埠 5000 已被使用（Windows 常見原因是其他網站服務），改用 5001。"
set "APP_PORT=5001"
"%VENV_PYTHON%" -c "import socket,sys; s=socket.socket(); s.settimeout(0.5); used=s.connect_ex(('127.0.0.1',int(sys.argv[1]))) == 0; s.close(); sys.exit(0 if used else 1)" "%APP_PORT%" >nul 2>&1
if not errorlevel 1 (
    call :fail "連接埠 5000 與 5001 都已被使用。請先關閉既有服務，再重新執行。"
    exit /b 1
)

:port_ready
call :log "[通過] 連接埠 %APP_PORT% 可以使用"
call :log "[啟動] 網站網址：http://localhost:%APP_PORT%"
call :log "[提示] 按 Ctrl+C 可停止服務；伺服器輸出會持續寫入本次 LOG。"
echo.

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PORT=%APP_PORT%"
set "TRASHTRACK_PYTHON=%VENV_PYTHON%"
set "TRASHTRACK_LOG_FILE=%LOG_FILE%"

where powershell >nul 2>&1
if errorlevel 1 goto start_without_tee

powershell -NoProfile -ExecutionPolicy Bypass -Command "^& $env:TRASHTRACK_PYTHON 'app.py' 2^>^&1 ^| Tee-Object -FilePath $env:TRASHTRACK_LOG_FILE -Append; exit $LASTEXITCODE"
set "SERVER_EXIT=%ERRORLEVEL%"
goto server_stopped

:start_without_tee
call :log "[警告] 找不到 PowerShell，伺服器輸出只會顯示於目前視窗。"
"%VENV_PYTHON%" "app.py"
set "SERVER_EXIT=%ERRORLEVEL%"

:server_stopped
if "%SERVER_EXIT%"=="0" (
    call :log "[停止] TrashTrack 服務已停止"
) else (
    call :log "[錯誤] TrashTrack 非正常結束（狀態碼：%SERVER_EXIT%）。"
    echo 請查看 LOG：%LOG_FILE%
)
echo.
pause
exit /b %SERVER_EXIT%

:log
echo %~1
>>"%LOG_FILE%" echo %DATE% %TIME% %~1
exit /b 0

:fail
call :log "[錯誤] %~1"
echo.
echo 請查看 LOG：%LOG_FILE%
pause
exit /b 1
