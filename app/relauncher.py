"""Windows 裸跑场景的自重启接力启动器。

背景:Windows 上 os.execv 无法干净替换正在运行的 uvicorn 进程,
所以更新/回滚/手动重启时,父进程(旧 bot)通过 _spawn_detached 启动本模块,
然后立即退出。本进程等父进程把 Web 端口真正释放后,再启动新的 bot 实例,
避免新进程 bind() 撞上 Address already in use(含 TIME_WAIT 残留连接)
直接死掉、服务永久掉线。

探测按 uvicorn 实际会绑的地址族进行(--host 与主程序 WEB_HOST 一致):
- 0.0.0.0 / 普通 IPv4:只测 AF_INET
- dual / :::AF_INET6 + AF_INET 同时可绑才算释放(与 main.py run_web 一致)
- 其他 IPv6 地址:只测 AF_INET6

用法(由 app.updater 内部调用,不需要手工执行):

    python app/relauncher.py --port 8000 [--host 0.0.0.0] [--timeout 60] [--delay 1.0] -- <实际启动命令...>
"""
from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import time
import os


def _probe_targets(host: str) -> list[tuple[int, str]]:
    """按 uvicorn 实际绑定逻辑(main.py run_web)确定要探测的地址族。"""
    host = (host or "0.0.0.0").strip()
    if host in {"dual", "::"}:
        return [(socket.AF_INET6, "::"), (socket.AF_INET, "0.0.0.0")]
    if ":" in host:
        return [(socket.AF_INET6, host)]
    return [(socket.AF_INET, host)]


def _try_bind_all(targets: list[tuple[int, str]], port: int) -> bool:
    """尝试同时 bind 全部目标地址;全部成功才算端口真正释放。"""
    socks: list[socket.socket] = []
    try:
        for family, addr in targets:
            sock = socket.socket(family, socket.SOCK_STREAM)
            if os.name != "nt":
                # 对齐 uvicorn 的 SO_REUSEADDR 语义,避免 Linux 上 TIME_WAIT
                # 期间误报"占用"拖满超时;Windows 不可设(会允许重复绑定,
                # 探测结果失真)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                # dual 场景 v4/v6 分开显式绑定,关掉 v4-mapped 防互相挤占
                try:
                    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                except (AttributeError, OSError):
                    pass
            sock.bind((addr, port))
            socks.append(sock)
        return True
    except OSError:
        return False
    finally:
        for sock in socks:
            sock.close()


def wait_for_port_free(
    port: int,
    host: str = "0.0.0.0",
    timeout: float = 60.0,
    interval: float = 0.5,
) -> bool:
    """轮询直到目标地址族全部可 bind(父进程退出且残留连接被内核回收)。

    返回 True 表示端口已可绑定;超时返回 False(调用方自行决定是否仍尝试启动)。
    """
    targets = _probe_targets(host)
    deadline = time.monotonic() + timeout
    while True:
        if _try_bind_all(targets, port):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="tg-group-guard 重启接力器")
    parser.add_argument("--port", type=int, default=0, help="等待释放的 Web 端口,0 表示不检测")
    parser.add_argument("--host", default="0.0.0.0", help="与主程序 WEB_HOST 一致,决定探测的地址族")
    parser.add_argument("--timeout", type=float, default=60.0, help="等待端口释放的超时秒数")
    parser.add_argument("--delay", type=float, default=1.0, help="启动前的固定延迟秒数")
    parser.add_argument("cmd", nargs=argparse.REMAINDER, help="-- 之后的实际启动命令")
    args = parser.parse_args(argv)

    cmd = list(args.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("relauncher: 缺少实际启动命令", file=sys.stderr, flush=True)
        return 2

    # 固定延迟:给父进程留出执行 os._exit 的时间
    time.sleep(max(0.0, args.delay))

    if args.port > 0:
        if wait_for_port_free(args.port, host=args.host, timeout=args.timeout):
            print(f"relauncher: 端口 {args.port}({args.host}) 已释放,启动新实例", flush=True)
        else:
            # 超时不等于必然失败(例如本实例没开 Web 端口),仍尝试启动;
            # 若真的仍被占用,uvicorn 报错会原样输出到控制台,便于排查
            print(
                f"relauncher: 等待端口 {args.port} 释放超时({args.timeout}s),仍尝试启动",
                flush=True,
            )
    else:
        print("relauncher: 未指定端口,固定延迟后直接启动新实例", flush=True)

    print(f"relauncher: 启动命令: {' '.join(cmd)}", flush=True)
    # 标准流继承:新实例的日志继续输出到原控制台窗口
    subprocess.Popen(cmd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
