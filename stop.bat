@echo off
setlocal EnableExtensions
chcp 65001 >nul

cd /d "%~dp0"
if errorlevel 1 (
    echo [錯誤] 無法進入專案目錄：%~dp0
    pause
    exit /b 1
)

where powershell >nul 2>&1
if errorlevel 1 (
    echo [錯誤] 找不到 PowerShell，無法停止服務。請在啟動視窗按 Ctrl+C。
    pause
    exit /b 1
)

set "TRASHTRACK_VENV=%CD%\.venv\"
echo [停止] 正在尋找 TrashTrack 網站與 GPS 蒐集器…

powershell -NoProfile -ExecutionPolicy Bypass -Command "$procs = @(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($env:TRASHTRACK_VENV, [StringComparison]::OrdinalIgnoreCase) }); if ($procs.Count -eq 0) { Write-Output '[資訊] 沒有執行中的 TrashTrack 服務。'; exit 0 }; foreach ($p in $procs) { try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; Write-Output ('[通過] 已停止 PID ' + $p.ProcessId) } catch { if (Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue) { Write-Output ('[錯誤] 無法停止 PID ' + $p.ProcessId + '：' + $_.Exception.Message); $failed = $true } } }; if ($failed) { exit 1 }"
set "STOP_EXIT=%ERRORLEVEL%"

if "%STOP_EXIT%"=="0" (
    echo [停止] TrashTrack 服務已停止
) else (
    echo [錯誤] 部分程序無法停止，請以系統管理員身分重新執行。
)
echo.
pause
exit /b %STOP_EXIT%
