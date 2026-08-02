"""单实例运行锁。

守护程序漏判(如 Docker / NSSM / Windows 任务计划程序未设 PROCESS_SUPERVISED)
或重启接力竞态可能导致两个实例同时运行,抢 Telegram getUpdates
(409 Conflict 死循环)并抢 Web 端口。启动时对 data/bot.lock 加锁:
发现锁被存活进程持有则拒绝启动;锁属已退出的进程(陈旧锁)则接管。
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def _pid_alive(pid: int) -> bool:
    """跨平台检测进程是否存活。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            # 进程对象可能因残留句柄而未销毁,OpenProcess 成功不代表存活;
            # 必须看退出码:STILL_ACTIVE(259) 才是真存活
            exit_code = ctypes.c_ulong(0)
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但属其他用户
    return True


def ensure_single_instance(lock_file: Path) -> None:
    """锁被存活实例持有时退出(exit 3);否则写入当前 PID。

    进程退出(包括 os._exit)后锁文件会残留,下次启动凭 PID 存活性
    判断为陈旧锁并接管,不会误挡正常重启。
    """
    old_pid = -1
    try:
        raw = lock_file.read_text(encoding="utf-8").strip()
        old_pid = int(raw) if raw else -1
    except (OSError, ValueError):
        pass
    if old_pid > 0 and old_pid != os.getpid() and _pid_alive(old_pid):
        logger.critical(
            "检测到另一个存活实例(pid=%s,锁文件 %s),本实例退出以避免双实例冲突",
            old_pid,
            lock_file,
        )
        print(f"FATAL: 已有实例在运行 (pid={old_pid}),本实例退出", file=sys.stderr)
        raise SystemExit(3)
    try:
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock_file.write_text(str(os.getpid()), encoding="utf-8")
    except OSError as exc:
        logger.warning("写入实例锁失败(继续运行): %s", exc)
