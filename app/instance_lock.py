"""单实例运行锁(内核级文件锁)。

守护程序漏判(如 Docker / NSSM / Windows 任务计划程序未设 PROCESS_SUPERVISED)
或重启接力竞态可能导致两个实例同时运行,抢 Telegram getUpdates
(409 Conflict 死循环)并抢 Web 端口。

用 fcntl.flock(POSIX) / msvcrt.locking(Windows) 对 data/bot.lock 加排他锁:
- 锁由内核强制互斥,不存在"读 PID → 查存活 → 写 PID"的 TOCTOU 竞态;
- 锁随进程退出(含 os._exit、崩溃)由内核自动释放,无陈旧锁;
- 不依赖 PID 存活性判断,PID 复用不会误判。
文件里写入 PID 仅供人工排查,不作为存活依据。
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_LOCK_FD: int | None = None  # 持锁 fd,必须活到进程退出,防 GC 提前关闭放锁


def ensure_single_instance(lock_file: Path) -> None:
    """对 lock_file 加内核级排他锁;已被其他实例持锁则退出(exit 3)。

    锁文件无法打开时降级为记日志继续运行(宁可无保护,不误挡启动)。
    """
    global _LOCK_FD
    try:
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as exc:
        logger.warning("打开实例锁文件失败(继续运行,无单实例保护): %s", exc)
        return
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        logger.critical(
            "检测到另一个实例正在运行(锁文件 %s),本实例退出以避免双实例冲突",
            lock_file,
        )
        print("FATAL: 已有实例在运行,本实例退出", file=sys.stderr)
        raise SystemExit(3)
    _LOCK_FD = fd
    # 写 PID 仅供人工排查;存活与否由内核锁保证,不读这个值
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, str(os.getpid()).encode("ascii"))
    except OSError:
        pass
