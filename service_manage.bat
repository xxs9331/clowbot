@echo off
:: ClawBot Windows 服务管理脚本
:: 需要以管理员权限运行

echo ========================================
echo  ClawBot Service Manager
echo ========================================
echo.

:: 检查管理员权限
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] 请以管理员权限运行此脚本！
    echo 右键 → 以管理员身份运行
    pause
    exit /b 1
)

set BOT_DIR=%~dp0
set PYTHON=D:\Users\Lenovo\AppData\Local\Programs\Python\Python313\python.exe
set CONDA_PYTHON=E:\Users\Lenovo\miniconda3\python.exe

:: 优先用 conda Python（有完整依赖）
if exist "%CONDA_PYTHON%" (
    set PYTHON=%CONDA_PYTHON%
)

if "%1"=="" goto :menu
if "%1"=="install" goto :install
if "%1"=="remove" goto :remove
if "%1"=="start" goto :start
if "%1"=="stop" goto :stop
if "%1"=="status" goto :status
if "%1"=="restart" goto :restart
goto :menu

:menu
echo 用法: %0 [install^|remove^|start^|stop^|restart^|status]
echo.
echo   install  - 安装为 Windows 服务（开机自启）
echo   remove   - 卸载 Windows 服务
echo   start    - 启动服务
echo   stop     - 停止服务
echo   restart  - 重启服务
echo   status   - 查看服务状态
goto :end

:install
echo [1/3] 安装 ClawBot 服务...
"%PYTHON%" "%BOT_DIR%service.py" install
if %errorlevel% neq 0 (
    echo [ERROR] 服务安装失败
    pause
    exit /b 1
)
echo [2/3] 设置服务为自动启动...
sc config ClawBot start=auto
echo [3/3] 启动服务...
net start ClawBot
echo.
echo [OK] ClawBot 服务已安装并启动 ✓
echo     开机将自动运行
echo     可在 services.msc 中管理
goto :end

:remove
echo [1/2] 停止服务...
net stop ClawBot 2>nul
echo [2/2] 卸载服务...
"%PYTHON%" "%BOT_DIR%service.py" remove
if %errorlevel% neq 0 (
    echo [ERROR] 服务卸载失败
    pause
    exit /b 1
)
echo.
echo [OK] ClawBot 服务已卸载 ✓
goto :end

:start
net start ClawBot
goto :end

:stop
net stop ClawBot
goto :end

:restart
net stop ClawBot 2>nul
timeout /t 2 /nobreak >nul
net start ClawBot
goto :end

:status
sc query ClawBot
goto :end

:end