"""版本检查与自助更新。

- check_latest_release(): 调 GitHub Releases API 拿最新版本与中文更新日志。
- run_update(): 更新到最新 Release 并准备重启：
  - git 部署：`git pull --ff-only` + `pip install -r requirements.txt`
  - 非 git 部署：下载 Release 源码压缩包，备份现有代码后覆盖（保留 data/、.env、ssl/）
  成功后由调用方触发进程重启（POSIX 用 os.execv 自我替换；Windows 裸跑经
  relauncher 接力器等端口释放后拉起新实例）。
- run_rollback(): 回滚到更新前状态（git reset --hard / 还原代码备份）后重启。
- 更新前自动备份 data/ 目录到 backups/（保留最近 5 份）。
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Optional

import aiohttp

from .version import APP_VERSION, github_repo

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKUPS_DIR = PROJECT_ROOT / "backups"
UPDATE_STATE_FILE = PROJECT_ROOT / "data" / "update_state.json"
GITHUB_API = "https://api.github.com"
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)
_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=300)
_MAX_BACKUPS = 5

# 覆盖代码 / 打快照时跳过的目录与文件
_PRESERVE_NAMES = {"data", ".env", "ssl", ".venv", "venv", ".git", "backups", "__pycache__"}


def parse_version(text: str) -> tuple[int, ...]:
    """把 'v1.2.3' / '1.2.3' 解析为可比较的元组；解析失败返回 (0,)。"""
    cleaned = text.strip().lstrip("vV")
    parts: list[int] = []
    for piece in cleaned.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        if digits == "":
            break
        parts.append(int(digits))
    return tuple(parts) if parts else (0,)


def is_newer(latest: str, current: str) -> bool:
    a, b = parse_version(latest), parse_version(current)
    length = max(len(a), len(b))
    a += (0,) * (length - len(a))
    b += (0,) * (length - len(b))
    return a > b


def is_git_deploy() -> bool:
    return (PROJECT_ROOT / ".git").exists()


def _github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"tg-group-guard/{APP_VERSION}",
    }
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def check_latest_release(repo: Optional[str] = None) -> dict[str, Any]:
    """返回版本比对快照；任何失败都体现在 error 字段，不抛异常。"""
    repo = repo or github_repo()
    result: dict[str, Any] = {
        "current": APP_VERSION,
        "latest": None,
        "update_available": False,
        "changelog": "",
        "release_url": None,
        "tarball_url": None,
        "published_at": None,
        "repo": repo,
        "deploy_method": "git" if is_git_deploy() else "tarball",
        "checked_at": int(time.time()),
        "error": None,
    }
    url = f"{GITHUB_API}/repos/{repo}/releases/latest"
    try:
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.get(url, headers=_github_headers()) as resp:
                if resp.status == 404:
                    result["error"] = "仓库还没有发布任何 Release"
                    return result
                if resp.status == 403:
                    result["error"] = "GitHub API 限流，稍后再试（可配置 GITHUB_TOKEN 提高限额）"
                    return result
                if resp.status != 200:
                    result["error"] = f"GitHub API 返回 HTTP {resp.status}"
                    return result
                data = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        result["error"] = f"无法连接 GitHub：{exc.__class__.__name__}"
        return result
    except ValueError:
        result["error"] = "GitHub 返回了无法解析的数据"
        return result

    tag = str(data.get("tag_name") or "").strip()
    if not tag:
        result["error"] = "最新 Release 缺少 tag_name"
        return result
    result["latest"] = tag
    result["changelog"] = str(data.get("body") or "").strip()
    result["release_url"] = data.get("html_url")
    result["tarball_url"] = data.get("tarball_url")
    result["published_at"] = data.get("published_at")
    result["update_available"] = is_newer(tag, APP_VERSION)
    return result


# ===== 备份 =====

def _prune_backups(pattern: str) -> None:
    backups = sorted(BACKUPS_DIR.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[_MAX_BACKUPS:]:
        try:
            old.unlink()
        except OSError:
            pass


def backup_data_dir(log: list[str]) -> Optional[Path]:
    """把 data/ 打包到 backups/data-<ts>.zip；无 data 目录时跳过。"""
    data_dir = PROJECT_ROOT / "data"
    if not data_dir.exists():
        return None
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    target = BACKUPS_DIR / f"data-{ts}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as zf:
        for file in data_dir.rglob("*"):
            if file.is_file():
                zf.write(file, file.relative_to(PROJECT_ROOT))
    log.append(f"已备份 data/ → {target.name}")
    _prune_backups("data-*.zip")
    return target


def backup_code_snapshot(log: list[str]) -> Path:
    """非 git 部署时，更新前给现有代码打快照，供回滚还原。"""
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    target = BACKUPS_DIR / f"code-{ts}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as zf:
        for file in PROJECT_ROOT.rglob("*"):
            if not file.is_file():
                continue
            rel = file.relative_to(PROJECT_ROOT)
            if any(part in _PRESERVE_NAMES for part in rel.parts):
                continue
            zf.write(file, rel)
    log.append(f"已备份现有代码 → {target.name}")
    _prune_backups("code-*.zip")
    return target


# ===== 更新状态（供回滚） =====

def read_update_state() -> Optional[dict[str, Any]]:
    try:
        raw = json.loads(UPDATE_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _write_update_state(state: dict[str, Any]) -> None:
    UPDATE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    UPDATE_STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


async def _git_current_commit() -> Optional[str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "HEAD",
            cwd=str(PROJECT_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        if proc.returncode == 0:
            return out.decode().strip() or None
    except OSError:
        pass
    return None


# ===== 命令执行 =====

async def _run_cmd(args: list[str], log: list[str]) -> tuple[int, str]:
    """在事件循环里跑子进程，返回 (returncode, 输出摘要)。"""
    log.append(f"$ {' '.join(args)}")
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(PROJECT_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            log.append("（命令超时，已终止）")
            return -1, "timeout"
        text = (out or b"").decode("utf-8", errors="replace").strip()
        if text:
            log.append(text[-3000:])
        return proc.returncode or 0, text
    except OSError as exc:
        log.append(f"无法执行命令：{exc}")
        return -1, str(exc)


async def _pip_install(log: list[str]) -> bool:
    requirements = PROJECT_ROOT / "requirements.txt"
    if not requirements.exists():
        return True
    code, _ = await _run_cmd(
        [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], log
    )
    return code == 0


# ===== 压缩包更新 =====

def _safe_extract_tar(data: bytes, dest: Path, log: list[str]) -> Path:
    """解压 GitHub tarball（内含一层 <repo>-<sha>/ 目录），返回该根目录。"""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        members = tf.getmembers()
        if not members:
            raise RuntimeError("Release 压缩包为空")
        root_name = members[0].name.split("/")[0]
        dest_resolved = str(dest.resolve()) + os.sep
        for member in members:
            # 防路径穿越：必须严格位于 dest 之内（含分隔符，排除同前缀兄弟目录）
            member_path = (dest / member.name).resolve()
            if not str(member_path).startswith(dest_resolved):
                raise RuntimeError("压缩包包含非法路径，已中止")
        tf.extractall(dest, filter="data")
    return dest / root_name


def _copy_code_over(src_root: Path, log: list[str]) -> int:
    """把解压出的代码覆盖到项目目录，跳过保留目录。返回复制文件数。"""
    count = 0
    for file in src_root.rglob("*"):
        if not file.is_file():
            continue
        rel = file.relative_to(src_root)
        if any(part in _PRESERVE_NAMES for part in rel.parts):
            continue
        target = PROJECT_ROOT / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file, target)
        count += 1
    log.append(f"已覆盖 {count} 个代码文件（data/、.env、ssl/ 等已保留）")
    return count


def _remove_stale_code(
    snapshot: Path,
    src_root: Path,
    log: list[str],
    dest_root: Optional[Path] = None,
) -> int:
    """删除"旧快照里有、新版本里没有"的 .py 文件及其 __pycache__。

    _copy_code_over 只增不删:新版本删除/改名的模块残留后,
    下次启动可能被误 import 遮蔽新代码。snapshot 是更新前
    backup_code_snapshot 打的旧代码快照(文件清单在 zip namelist 里)。
    """
    dest_root = dest_root or PROJECT_ROOT
    resolved_root = str(dest_root.resolve()) + os.sep
    new_files = {
        file.relative_to(src_root).as_posix()
        for file in src_root.rglob("*")
        if file.is_file()
    }
    try:
        with zipfile.ZipFile(snapshot) as zf:
            old_names = zf.namelist()
    except (OSError, zipfile.BadZipFile) as exc:
        log.append(f"⚠️ 读取代码快照失败，跳过废弃文件清理：{exc}")
        return 0
    removed = 0
    for name in old_names:
        rel = Path(name)
        if rel.suffix != ".py" or rel.as_posix() in new_files:
            continue
        if any(part in _PRESERVE_NAMES for part in rel.parts):
            continue
        # 与 tar 解压/回滚同款防护:删除前校验目标严格位于 dest_root 之内
        # (快照是自生成可信文件,此处为纵深防御)
        target = (dest_root / rel).resolve()
        if not str(target).startswith(resolved_root):
            log.append(f"⚠️ 快照包含越界路径 {name}，已跳过")
            continue
        try:
            target.unlink(missing_ok=True)
            removed += 1
            pycache = target.parent / "__pycache__"
            if pycache.is_dir():
                for pyc in pycache.glob(target.stem + ".*.pyc"):
                    pyc.unlink(missing_ok=True)
                try:
                    pycache.rmdir()  # 仅在已清空时移除目录
                except OSError:
                    pass
        except OSError as exc:
            log.append(f"⚠️ 删除废弃文件失败 {rel.as_posix()}：{exc}")
    if removed:
        log.append(f"已清理 {removed} 个新版本中已移除的废弃代码文件")
    return removed


async def _download_tarball(url: str, log: list[str]) -> bytes:
    log.append(f"正在下载 Release 源码包：{url}")
    async with aiohttp.ClientSession(timeout=_DOWNLOAD_TIMEOUT) as session:
        async with session.get(url, headers=_github_headers()) as resp:
            if resp.status != 200:
                raise RuntimeError(f"下载源码包失败：HTTP {resp.status}")
            return await resp.read()


# ===== 更新 / 回滚 =====

async def run_update(status: dict[str, Any], release: Optional[dict[str, Any]] = None) -> bool:
    """执行更新，进度写入 status dict。成功返回 True（调用方随后重启）。"""
    log: list[str] = status.setdefault("log", [])
    method = "git" if is_git_deploy() else "tarball"
    status["method"] = method

    # 1) 更新前备份 data/
    try:
        backup_data_dir(log)
    except OSError as exc:
        log.append(f"⚠️ data/ 备份失败（继续更新）：{exc}")

    if method == "git":
        prev_commit = await _git_current_commit()
        status["state"] = "pulling"
        code, _ = await _run_cmd(["git", "pull", "--ff-only"], log)
        if code != 0:
            status["state"] = "failed"
            status["error"] = (
                "git pull 失败：本地可能有未提交改动或产生了分叉，"
                "请在服务器上手动处理（git status 查看）后重试。"
            )
            return False
        _write_update_state(
            {
                "updated_at": int(time.time()),
                "from_version": APP_VERSION,
                "to_version": (release or {}).get("latest"),
                "method": "git",
                "git_prev_commit": prev_commit,
            }
        )
    else:
        tarball_url = (release or {}).get("tarball_url")
        if not tarball_url:
            status["state"] = "failed"
            status["error"] = "该 Release 没有可用的源码包下载地址。"
            return False
        status["state"] = "downloading"
        try:
            payload = await _download_tarball(tarball_url, log)
        except (RuntimeError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            status["state"] = "failed"
            status["error"] = f"下载更新包失败：{exc}"
            return False
        try:
            snapshot = backup_code_snapshot(log)
        except OSError as exc:
            status["state"] = "failed"
            status["error"] = f"更新前备份现有代码失败：{exc}"
            return False
        status["state"] = "installing"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                src_root = _safe_extract_tar(payload, Path(tmp), log)
                _copy_code_over(src_root, log)
                _remove_stale_code(snapshot, src_root, log)
        except (RuntimeError, tarfile.TarError, OSError) as exc:
            status["state"] = "failed"
            status["error"] = f"解压或覆盖代码失败：{exc}"
            return False
        _write_update_state(
            {
                "updated_at": int(time.time()),
                "from_version": APP_VERSION,
                "to_version": (release or {}).get("latest"),
                "method": "tarball",
                "code_backup": str(snapshot),
            }
        )

    status["state"] = "installing"
    if not await _pip_install(log):
        status["state"] = "failed"
        status["error"] = "依赖安装失败，请检查上方日志后手动执行 pip install -r requirements.txt。"
        return False

    status["state"] = "ready_to_restart"
    return True


async def run_rollback(status: dict[str, Any]) -> bool:
    """按 data/update_state.json 记录回滚到更新前状态。成功返回 True。"""
    log: list[str] = status.setdefault("log", [])
    state = read_update_state()
    if not state:
        status["state"] = "failed"
        status["error"] = "没有找到可回滚的更新记录。"
        return False

    if state.get("method") == "git":
        prev = state.get("git_prev_commit")
        if not prev:
            status["state"] = "failed"
            status["error"] = "更新记录中缺少回滚目标提交。"
            return False
        status["state"] = "rolling_back"
        code, _ = await _run_cmd(["git", "reset", "--hard", prev], log)
        if code != 0:
            status["state"] = "failed"
            status["error"] = "git 回滚失败，请在服务器上手动执行 git reset --hard。"
            return False
    else:
        backup = state.get("code_backup")
        if not backup or not Path(backup).exists():
            status["state"] = "failed"
            status["error"] = "代码备份文件不存在，无法自动回滚。"
            return False
        status["state"] = "rolling_back"
        try:
            # 还原快照中的文件；更新新引入的文件不会被删除，
            # 但旧代码不会引用它们，不影响回滚后的运行
            with zipfile.ZipFile(backup) as zf:
                root_prefix = str(PROJECT_ROOT) + os.sep
                for name in zf.namelist():
                    target = (PROJECT_ROOT / name).resolve()
                    if not str(target).startswith(root_prefix):
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(name) as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
            log.append(f"已从 {Path(backup).name} 还原代码")
        except (OSError, zipfile.BadZipFile) as exc:
            status["state"] = "failed"
            status["error"] = f"还原代码备份失败：{exc}"
            return False

    if not await _pip_install(log):
        log.append("⚠️ 依赖安装失败，回滚后如启动异常请手动执行 pip install -r requirements.txt")

    try:
        UPDATE_STATE_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    status["state"] = "ready_to_restart"
    return True


def schedule_shutdown(delay_seconds: float = 1.5, exit_code: int = 0) -> None:
    """延迟后退出进程（用于管理员主动关停）。退出前执行关闭钩子。"""

    async def _shutdown() -> None:
        await asyncio.sleep(delay_seconds)
        logger.warning("管理员请求关停服务，进程即将退出 (exit=%s)", exit_code)
        await _run_shutdown_hooks()
        os._exit(exit_code)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_shutdown())
    except RuntimeError:  # pragma: no cover
        import threading

        def _threaded() -> None:
            try:
                asyncio.run(_run_shutdown_hooks())
            except Exception:
                pass
            os._exit(exit_code)

        threading.Timer(delay_seconds, _threaded).start()


def _looks_like_container() -> bool:
    """Docker / 容器环境检测:/.dockerenv、cgroup 特征、PID 1。"""
    if os.name == "nt":
        return False
    try:
        if os.path.exists("/.dockerenv"):
            return True
    except OSError:
        pass
    try:
        with open("/proc/1/cgroup", "r", encoding="utf-8", errors="ignore") as fh:
            content = fh.read()
        if any(tag in content for tag in ("docker", "containerd", "kubepods")):
            return True
    except OSError:
        pass
    try:
        return os.getpid() == 1
    except OSError:
        return False


def _is_under_process_supervisor() -> bool:
    """检测进程守护/容器环境(systemd / pm2 / k8s / Docker)或显式标记。

    NSSM、Windows 任务计划程序等无可靠环境标记,
    需在 .env 中显式设置 PROCESS_SUPERVISED=true。
    漏判后果:守护程序拉起旧实例的同时本进程又接力拉起新实例 → 双实例
    抢 getUpdates(409 Conflict 死循环)并抢 Web 端口。
    """
    if os.getenv("PROCESS_SUPERVISED", "").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    return bool(
        os.getenv("INVOCATION_ID")            # systemd
        or os.getenv("PM2_USAGE")             # pm2
        or os.getenv("PM2_HOME")              # pm2
        or os.getenv("KUBERNETES_SERVICE_HOST")  # k8s
        or _looks_like_container()            # Docker / 容器
    )


# ===== 关闭钩子(优雅退出) =====
# os._exit / execv 会绕过 main.py 的资源收尾(store/bot/redis close)。
# main.py 启动时注册收尾协程,退出前限时执行,把数据丢失窗口压到最小。
_SHUTDOWN_HOOKS: list[Any] = []  # list[Callable[[], Awaitable[Any]]]


def register_shutdown_hook(hook: Any) -> None:
    """注册退出前执行的收尾协程(无参、返回 awaitable)。"""
    _SHUTDOWN_HOOKS.append(hook)


async def _run_shutdown_hooks(timeout: float = 3.0) -> None:
    """限时执行全部关闭钩子;单个失败/超时只记日志,不阻塞退出。"""
    for hook in _SHUTDOWN_HOOKS:
        try:
            await asyncio.wait_for(hook(), timeout=timeout)
        except Exception as exc:
            logger.warning("关闭钩子执行失败(忽略,继续退出): %r", exc)


def _spawn_detached(cmd: list[str]) -> None:
    """以独立进程方式启动新实例（Windows 裸跑场景）。

    标准流保持继承：父进程退出后控制台窗口仍在，子进程可继续输出日志。
    """
    kwargs: dict[str, Any] = {"close_fds": True}
    if os.name == "nt":
        # 脱离进程组，避免父进程退出/收到 Ctrl+C 时子进程被一起终止
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)


def _windows_relaunch_command(port: int = 0, host: str = "0.0.0.0") -> list[str]:
    """构造 Windows 裸跑重启命令:接力器先等端口释放,再拉起新实例。

    直接 _spawn_detached([python] + argv) 会有端口竞争:子进程毫秒级内
    尝试 bind,而父进程的监听 socket(及浏览器会话留下的 TIME_WAIT 连接)
    尚未被内核回收,子进程撞上 Address already in use 后无重试直接死掉,
    服务永久掉线。接力器(app/relauncher.py)按 WEB_HOST 实际地址族
    等端口真正可 bind 后再启动。
    """
    relauncher = Path(__file__).resolve().parent / "relauncher.py"
    cmd = [sys.executable, str(relauncher)]
    if port > 0:
        cmd += ["--port", str(port), "--host", host or "0.0.0.0"]
    return cmd + ["--", sys.executable] + sys.argv


def _restart_process(port: int = 0, host: str = "0.0.0.0") -> None:
    cmd = [sys.executable] + sys.argv
    logger.info("正在重启进程：%s", cmd)
    if os.name != "nt":
        # POSIX：execv 原地替换，PID 不变，systemd/pm2 无感知，最干净
        os.execv(sys.executable, cmd)
    if _is_under_process_supervisor():
        # Windows + 守护进程：直接退出，由守护进程拉起新实例
        os._exit(0)
    # Windows 裸跑：os.execv 无法干净移交事件循环与 socket 句柄;
    # 经接力器等端口释放后再拉起新实例,本进程随即退出
    _spawn_detached(_windows_relaunch_command(port, host))
    os._exit(0)


def schedule_restart(delay_seconds: float = 2.0, port: int = 0, host: str = "0.0.0.0") -> None:
    """延迟后重启进程。延迟是为了让 HTTP 响应先送回浏览器。

    port/host 是 Web 监听地址(WEB_PORT/WEB_HOST),用于 Windows 裸跑场景
    由接力器按实际地址族等待端口释放;port 传 0 表示不检测。
    """

    async def _restart() -> None:
        await asyncio.sleep(delay_seconds)
        await _run_shutdown_hooks()
        _restart_process(port, host)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_restart())
    except RuntimeError:  # pragma: no cover - 非事件循环环境
        import threading

        def _threaded() -> None:
            try:
                asyncio.run(_run_shutdown_hooks())
            except Exception:
                pass
            _restart_process(port, host)

        threading.Timer(delay_seconds, _threaded).start()
