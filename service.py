"""ClawBot Windows Service — 开机自启、后台运行

用法:
  # 安装服务（需管理员权限）
  python service.py install
  
  # 卸载服务
  python service.py remove
  
  # 调试模式（前台运行，看日志）
  python service.py debug
  
  # 启动/停止（也可在 services.msc 中操作）
  net start ClawBot
  net stop ClawBot
"""

import os
import sys
import asyncio
import servicemanager
import win32serviceutil
import win32service
import win32event


# ─── 服务配置 ───
SERVICE_NAME = "ClawBot"
SERVICE_DISPLAY = "ClawBot - 微信生活日志助手"
SERVICE_DESC = "微信 ClawBot 生活日志助手，自动记录生活事件到 Obsidian Vault"
BOT_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.py")


class ClawBotService(win32serviceutil.ServiceFramework):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = SERVICE_DISPLAY
    _svc_description_ = SERVICE_DESC
    _svc_deps_ = ["Tcpip"]  # 依赖网络
    _svc_start_type_ = win32service.SERVICE_AUTO_START  # 自动启动

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
        self._loop = None
        self._task = None

    def SvcStop(self):
        """SCM 在另一线程调用；禁止用 _loop.is_running()（在 SCM 线程里几乎恒为 False，导致从不 cancel，永远 STOP_PENDING）。"""
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.hWaitStop)
        loop = self._loop
        if loop is None:
            return

        def _request_cancel():
            try:
                t = self._task
                if t is not None and not t.done():
                    t.cancel()
            except Exception:
                pass

        try:
            loop.call_soon_threadsafe(_request_cancel)
        except RuntimeError:
            pass

    def SvcDoRun(self):
        """服务启动时调用"""
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        self.main()

    def main(self):
        """在独立线程中运行 bot.py 的 asyncio 主循环"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._task = self._loop.create_task(self._run_bot())
            self._loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            servicemanager.LogErrorMsg(f"ClawBot 异常退出: {e}")
        finally:
            self._loop.close()

    async def _run_bot(self):
        """导入并运行 bot.py 的 main()"""
        # 将 bot.py 的目录加入 path
        bot_dir = os.path.dirname(BOT_SCRIPT)
        if bot_dir not in sys.path:
            sys.path.insert(0, bot_dir)

        # 动态导入 bot 模块
        import importlib.util
        spec = importlib.util.spec_from_file_location("bot", BOT_SCRIPT)
        bot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bot)

        # 运行 main()，同时监听停止事件
        stop_event = asyncio.Event()

        def on_stop():
            if not stop_event.is_set():
                stop_event.set()

        # 注册 Windows 停止事件的回调
        self._loop.run_in_executor(None, lambda: (
            win32event.WaitForSingleObject(self.hWaitStop, win32event.INFINITE),
            on_stop()
        ))

        # 在后台线程等停止，在看门狗线程检查
        import threading

        def wait_for_stop():
            win32event.WaitForSingleObject(self.hWaitStop, win32event.INFINITE)
            stop_event.set()

        stop_thread = threading.Thread(target=wait_for_stop, daemon=True)
        stop_thread.start()

        # 运行 bot main，停止事件触发时取消
        bot_task = asyncio.create_task(bot.main())
        stop_waiter = asyncio.create_task(stop_event.wait())

        done, pending = await asyncio.wait(
            [bot_task, stop_waiter],
            return_when=asyncio.FIRST_COMPLETED,
        )

        for t in pending:
            t.cancel()
        for t in done:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass


def run_debug():
    """前台调试模式，直接运行 bot.py"""
    print(f"[ClawBot] 调试模式 - 直接运行 {BOT_SCRIPT}")
    # 直接导入运行
    bot_dir = os.path.dirname(BOT_SCRIPT)
    if bot_dir not in sys.path:
        sys.path.insert(0, bot_dir)
    asyncio.run(__import__("bot").main())


if __name__ == "__main__":
    if len(sys.argv) == 1:
        # 无参数 → 作为服务运行（由 Windows SCM 调用）
        win32serviceutil.HandleCommandLine(ClawBotService)
    elif sys.argv[1].lower() == "debug":
        run_debug()
    else:
        # install / remove / start / stop 等命令
        win32serviceutil.HandleCommandLine(ClawBotService)