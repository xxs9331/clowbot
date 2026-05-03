@echo off
REM ClawBot Windows service helper (ASCII-only for cmd.exe codepage safety)

echo ========================================
echo  ClawBot Service Manager
echo ========================================
echo.

REM Require elevated session
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Run this script as Administrator.
    echo Right-click -^> Run as administrator
    pause
    exit /b 1
)

set "BOT_DIR=%~dp0"
set "PYTHON="

REM 1) Project venv (preferred)
if exist "%BOT_DIR%venv\Scripts\python.exe" set "PYTHON=%BOT_DIR%venv\Scripts\python.exe"
if not defined PYTHON if exist "%BOT_DIR%.venv\Scripts\python.exe" set "PYTHON=%BOT_DIR%.venv\Scripts\python.exe"

REM 2) Windows Python launcher (py -3)
if not defined PYTHON (
    for /f "usebackq delims=" %%A in (`py -3 -c "import sys; print(sys.executable)" 2^>nul`) do set "PYTHON=%%A"
)

REM 3) First python on PATH
if not defined PYTHON (
    for /f "usebackq delims=" %%A in (`where python 2^>nul`) do (
        set "PYTHON=%%A"
        goto :python_found
    )
)
:python_found

if not defined PYTHON (
    echo [ERROR] Python not found. Either:
    echo   - Create venv: python -m venv venv
    echo   - Or add Python to PATH / install from python.org
    pause
    exit /b 1
)
echo [INFO] Using Python: %PYTHON%

if "%1"=="" goto :menu
if "%1"=="install" goto :install
if "%1"=="remove" goto :remove
if "%1"=="start" goto :start
if "%1"=="stop" goto :stop
if "%1"=="status" goto :status
if "%1"=="restart" goto :restart
if "%1"=="force-stop" goto :force-stop
goto :menu

:menu
echo Usage: %0 [install^|remove^|start^|stop^|restart^|status^|force-stop]
echo.
echo   install    - pip deps + register Windows service (auto-start)
echo   remove     - uninstall Windows service
echo   start      - start service
echo   stop       - stop service
echo   restart    - stop then start (waits for STOPPED)
echo   status     - sc.exe query ClawBot (+ PID line)
echo   force-stop - kill service process (recovery if STOP_PENDING forever)
echo.
echo In PowerShell, use sc.exe not sc (sc is Set-Content alias).
goto :end

:install
echo [0/4] pip install -r requirements.txt ...
"%PYTHON%" -m pip install -r "%BOT_DIR%requirements.txt"
if %errorlevel% neq 0 (
    echo [ERROR] pip install failed
    pause
    exit /b 1
)
echo [1/4] Register ClawBot service...
"%PYTHON%" "%BOT_DIR%service.py" install
if %errorlevel% neq 0 (
    echo [ERROR] service install failed
    pause
    exit /b 1
)
echo [2/4] Set service start type to AUTO...
sc.exe config ClawBot start= auto
echo [3/4] Start service...
net start ClawBot
echo.
echo [OK] ClawBot installed and started.
echo     Auto-start on boot; manage in services.msc
echo     If model replies work in bot.py but not as service: restart service after
echo     upgrading .clawbot; opencode stderr is drained to logs\acp-stderr.log
goto :end

:remove
echo [1/2] Stop service...
net stop ClawBot 2>nul
echo [2/2] Remove service...
"%PYTHON%" "%BOT_DIR%service.py" remove
if %errorlevel% neq 0 (
    echo [ERROR] service remove failed
    pause
    exit /b 1
)
echo.
echo [OK] ClawBot service removed.
goto :end

:start
net start ClawBot
goto :end

:stop
net stop ClawBot
goto :end

:restart
echo Stopping ClawBot...
net stop ClawBot 2>nul
set SW=0
:restart_wait
sc.exe query ClawBot | findstr /I "STOPPED" >nul
if %errorlevel%==0 goto :restart_go
set /a SW+=1
if %SW% gtr 60 goto :restart_fail
if %SW%==15 net stop ClawBot 2>nul
if %SW%==30 net stop ClawBot 2>nul
if %SW%==45 net stop ClawBot 2>nul
echo Waiting for STOPPED... (%SW%/60) STOP_PENDING can take a while.
timeout /t 2 /nobreak >nul
goto :restart_wait
:restart_go
echo Starting ClawBot...
net start ClawBot
goto :end
:restart_fail
echo [ERROR] Timed out waiting for STOPPED. Open services.msc, stop ClawBot manually,
echo or reboot. If bot.main blocks on long poll, stop can be slow until cancel works.
exit /b 1

:status
sc.exe query ClawBot
sc.exe queryex ClawBot
goto :end

:force-stop
echo [force-stop] Kill ClawBot service process (admin). Use when STOP_PENDING never finishes.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=Get-CimInstance Win32_Service -Filter 'Name=''ClawBot''' -ErrorAction SilentlyContinue; if($null -eq $s){Write-Host '[ERROR] Service ClawBot not found.'; exit 2}; $id=[int]$s.ProcessId; Write-Host ('State='+$s.State+' PID='+$id); if($id -le 0){Write-Host 'No process to kill.'; exit 0}; Write-Host ('Stop-Process -Id '+$id+' -Force'); Stop-Process -Id $id -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 2; $s2=Get-CimInstance Win32_Service -Filter 'Name=''ClawBot''' -ErrorAction SilentlyContinue; if($null -ne $s2){Write-Host ('After kill: State='+$s2.State+' PID='+[int]$s2.ProcessId)}"
if %errorlevel% neq 0 (
    echo [WARN] force-stop PowerShell returned non-zero.
)
echo Done. Check: %0 status   then: %0 start
goto :end

:end
