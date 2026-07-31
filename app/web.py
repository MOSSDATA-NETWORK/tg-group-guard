from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from .bot_components.constants import ADMIN_STATUSES
from .bot_components.verification import (
    announce_group_success,
    ban_and_cleanup,
    delete_prompt_message,
    lift_restrictions,
    notify_verification_success,
)
from .config import Settings
from .storage import VerificationRecord, VerificationStore


logger = logging.getLogger(__name__)

UTC = timezone.utc
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
ADMIN_COOKIE_NAME = "kk_admin"
ADMIN_CSRF_COOKIE_NAME = "kk_admin_csrf"
SESSION_MAX_TOTAL = 5000  # 全局硬上限,防止内存爆炸


def create_web_app(settings: Settings, store: VerificationStore, bot) -> FastAPI:
    app = FastAPI(title="Telegram Join Verification")

    app.state.settings = settings
    app.state.store = store
    app.state.bot = bot
    app.state.admin_sessions = {}
    app.state.admin_user_sessions = {}  # user_id -> deque[token]
    app.state.rate_buckets = {}  # ip -> (window_start_ts, count)

    app.add_middleware(_SecurityHeadersMiddleware)
    app.add_middleware(_AdminRateLimitMiddleware)

    # 可选注入的外部状态,启动时由 main.py 设置
    # app.state.redis_client / app.state.metrics / app.state.polling_alive

    @app.get("/admin", response_class=HTMLResponse)
    async def admin_dashboard(request: Request):
        session = await _require_admin_session(request, api=False)
        if isinstance(session, RedirectResponse):
            return session
        return templates.TemplateResponse(
            "admin_dashboard.html",
            {
                "request": request,
                "admin": session,
                "chats": session["admin_chat_ids"],
            },
        )

    @app.get("/admin/login", response_class=HTMLResponse)
    async def admin_login(request: Request, error: str = "") -> HTMLResponse:
        if not settings.admin_web_enabled:
            return templates.TemplateResponse(
                "admin_login.html",
                {
                    "request": request,
                    "bot_username": settings.bot_username,
                    "enabled": False,
                    "error": "Admin WebUI 未启用。",
                },
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if not settings.allowed_chat_ids:
            return templates.TemplateResponse(
                "admin_login.html",
                {
                    "request": request,
                    "bot_username": settings.bot_username,
                    "enabled": False,
                    "error": "未配置 ALLOWED_CHAT_IDS，无法确认群管理员身份。",
                },
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return templates.TemplateResponse(
            "admin_login.html",
            {
                "request": request,
                "bot_username": settings.bot_username,
                "enabled": True,
                "error": error,
            },
        )

    @app.get("/admin/login/callback")
    async def admin_login_callback(request: Request):
        if not settings.admin_web_enabled:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
        auth_data = dict(request.query_params)
        if not _verify_telegram_login(
            auth_data, settings.bot_token, settings.admin_auth_age_seconds
        ):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="登录签名无效或已过期")

        try:
            user_id = int(auth_data["id"])
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="登录数据缺少用户 ID") from exc

        admin_chat_ids = await _admin_chat_ids_for_user(bot, settings.allowed_chat_ids, user_id)
        if not admin_chat_ids:
            return RedirectResponse(
                "/admin/login?error=%E9%9D%9E%E6%8E%88%E6%9D%83%E7%BE%A4%E7%AE%A1%E7%90%86%E5%91%98",
                status_code=status.HTTP_302_FOUND,
            )

        display_name = " ".join(
            part
            for part in (auth_data.get("first_name", ""), auth_data.get("last_name", ""))
            if part
        ).strip() or auth_data.get("username") or str(user_id)
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        expires_at = int(time.time()) + settings.admin_session_ttl_seconds

        sessions = app.state.admin_sessions
        user_sessions = app.state.admin_user_sessions

        # 单用户配额：超出时挤掉最旧的
        user_deque = user_sessions.setdefault(user_id, deque())
        while len(user_deque) >= settings.admin_max_sessions_per_user:
            old_token = user_deque.popleft()
            sessions.pop(old_token, None)

        # 全局硬上限：满了拒绝登录
        if len(sessions) >= SESSION_MAX_TOTAL:
            logger.warning("admin session 池已满,拒绝登录 user_id=%s", user_id)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="系统繁忙，请稍后再试",
            )

        sessions[token] = {
            "user_id": user_id,
            "name": display_name,
            "username": auth_data.get("username"),
            "expires_at": expires_at,
            "admin_chat_ids": sorted(admin_chat_ids),
            "csrf": csrf,
        }
        user_deque.append(token)

        use_secure = _is_secure_request(request, settings)
        response = RedirectResponse("/admin", status_code=status.HTTP_302_FOUND)
        response.set_cookie(
            ADMIN_COOKIE_NAME,
            token,
            max_age=settings.admin_session_ttl_seconds,
            httponly=True,
            secure=use_secure,
            samesite="lax",
            path="/admin",
        )
        # CSRF cookie 不能 HttpOnly,前端 JS 需要读取
        response.set_cookie(
            ADMIN_CSRF_COOKIE_NAME,
            csrf,
            max_age=settings.admin_session_ttl_seconds,
            httponly=False,
            secure=use_secure,
            samesite="lax",
            path="/admin",
        )
        return response

    @app.post("/admin/logout")
    async def admin_logout(request: Request):
        token = request.cookies.get(ADMIN_COOKIE_NAME)
        # logout 也要 CSRF: 防止第三方站点 <form action=/admin/logout> 自动登出
        if token:
            sessions = app.state.admin_sessions
            session = sessions.get(token)
            sent_csrf = request.headers.get("x-csrf-token") or ""
            cookie_csrf = request.cookies.get(ADMIN_CSRF_COOKIE_NAME) or ""
            if session and (
                not sent_csrf
                or not hmac.compare_digest(sent_csrf, session.get("csrf", ""))
                or not hmac.compare_digest(cookie_csrf, session.get("csrf", ""))
            ):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF 校验失败")
            sessions.pop(token, None)
            user_id = session.get("user_id") if session else None
            if user_id is not None:
                deque_ = app.state.admin_user_sessions.get(user_id)
                if deque_ is not None:
                    try:
                        deque_.remove(token)
                    except ValueError:
                        pass
                    if not deque_:
                        app.state.admin_user_sessions.pop(user_id, None)
        response = RedirectResponse("/admin/login", status_code=status.HTTP_302_FOUND)
        response.delete_cookie(ADMIN_COOKIE_NAME, path="/admin")
        response.delete_cookie(ADMIN_CSRF_COOKIE_NAME, path="/admin")
        return response

    @app.get("/admin/api/metrics")
    async def admin_api_metrics(
        request: Request,
        chat_id: Optional[int] = None,
    ) -> JSONResponse:
        session = await _require_admin_session(request, api=True)
        data = await store.summarize_metrics(
            set(session["admin_chat_ids"]),
            chat_id=chat_id,
        )
        return JSONResponse(data)

    @app.get("/admin/api/ad_decisions")
    async def admin_api_ad_decisions(
        request: Request,
        chat_id: Optional[int] = None,
        flagged: Optional[bool] = None,
        user_id: Optional[int] = None,
        since: Optional[int] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JSONResponse:
        session = await _require_admin_session(request, api=True)
        rows = await store.recent_ad_decisions(
            set(session["admin_chat_ids"]),
            chat_id=chat_id,
            flagged_only=bool(flagged),
            user_id=user_id,
            since=since,
            limit=limit,
            offset=offset,
        )
        return JSONResponse({"items": rows})

    @app.get("/admin/api/joins")
    async def admin_api_joins(
        request: Request,
        chat_id: Optional[int] = None,
        user_id: Optional[int] = None,
        event: Optional[str] = None,
        since: Optional[int] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JSONResponse:
        session = await _require_admin_session(request, api=True)
        rows = await store.recent_verification_events(
            set(session["admin_chat_ids"]),
            chat_id=chat_id,
            user_id=user_id,
            event=event,
            since=since,
            limit=limit,
            offset=offset,
        )
        return JSONResponse({"items": rows})

    @app.get("/admin/api/bans")
    async def admin_api_bans(
        request: Request,
        chat_id: Optional[int] = None,
        user_id: Optional[int] = None,
        only_banned: bool = True,
        since: Optional[int] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JSONResponse:
        session = await _require_admin_session(request, api=True)
        rows = await store.recent_ban_events(
            set(session["admin_chat_ids"]),
            chat_id=chat_id,
            user_id=user_id,
            only_banned=only_banned,
            since=since,
            limit=limit,
            offset=offset,
        )
        return JSONResponse({"items": rows})

    @app.post("/admin/api/unban")
    async def admin_api_unban(request: Request) -> JSONResponse:
        session = await _require_admin_session(request, api=True)
        _require_csrf(request, session)
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="非法 JSON")
        try:
            chat_id = int(payload["chat_id"])
            user_id = int(payload["user_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="chat_id/user_id 无效") from exc

        if chat_id not in set(session["admin_chat_ids"]):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权管理该群")
        admin_chat_ids = await _admin_chat_ids_for_user(bot, {chat_id}, int(session["user_id"]))
        if chat_id not in admin_chat_ids:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="管理员权限已失效")

        try:
            await bot.unban_chat_member(chat_id, user_id, only_if_banned=False)
        except TelegramBadRequest as exc:
            logger.warning(
                "解封失败 chat_id=%s user_id=%s operator=%s error=%r",
                chat_id,
                user_id,
                session.get("user_id"),
                exc,
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="解封失败，请稍后重试",
            ) from exc
        except TelegramForbiddenError:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="机器人无相关群权限",
            )

        await store.mark_unbanned(
            chat_id,
            user_id,
            operator_id=int(session["user_id"]),
            operator_name=session["name"],
            display_name=str(user_id),
            reason="web_unban",
        )

        # 解封后清零违规分与合格进度，重新进群按新人再过前 N 次检测。
        score_manager = getattr(app.state, "score_manager", None)
        if score_manager is not None:
            try:
                await score_manager.reset_score(chat_id, user_id)
            except Exception as exc:  # pragma: no cover - 评分服务异常不阻断解封
                logger.warning(
                    "解封后重置评分失败 chat_id=%s user_id=%s error=%r",
                    chat_id,
                    user_id,
                    exc,
                )
        store = getattr(app.state, "store", None)
        if store is not None:
            try:
                await store.reset_ad_qualification(chat_id, user_id)
            except Exception as exc:  # pragma: no cover
                logger.warning(
                    "解封后重置合格状态失败 chat_id=%s user_id=%s error=%r",
                    chat_id,
                    user_id,
                    exc,
                )
        return JSONResponse({"status": "ok"})

    @app.get("/healthz", response_model=None)
    async def health_check() -> Dict[str, Any]:
        """依赖状态检查。

        返回 ok / degraded 两态,degraded 时 HTTP 仍 200,
        但 status 字段标识出问题,方便监控系统告警。
        """
        details: Dict[str, str] = {}

        try:
            if store._db is None:
                details["sqlite"] = "disconnected"
            else:
                # 触发一次轻量读
                async with store._lock:
                    cursor = await store._db.execute("SELECT 1")
                    await cursor.fetchone()
                    await cursor.close()
                details["sqlite"] = "ok"
        except Exception as exc:  # pragma: no cover - 运行时异常
            details["sqlite"] = f"error: {exc.__class__.__name__}"

        redis_client = getattr(app.state, "redis_client", None)
        if redis_client is None:
            details["redis"] = "unknown"
        else:
            try:
                await redis_client.ping()
                details["redis"] = "ok"
            except Exception as exc:  # pragma: no cover
                details["redis"] = f"error: {exc.__class__.__name__}"

        polling_alive = getattr(app.state, "polling_alive", None)
        if polling_alive is None:
            details["polling"] = "unknown"
        else:
            details["polling"] = "ok" if polling_alive else "down"

        overall = "ok"
        for v in details.values():
            if v not in {"ok", "unknown"}:
                overall = "degraded"
                break

        return {"status": overall, "details": details}

    @app.get("/metrics", response_model=None)
    async def prometheus_metrics(request: Request):
        metrics = getattr(request.app.state, "metrics", None)
        if metrics is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="metrics 未启用,设置 ENABLE_METRICS=true 后重启",
            )
        from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

        return JSONResponse(
            content=generate_latest().decode("utf-8"),
            media_type=CONTENT_TYPE_LATEST,
        )

    @app.get("/verify/{token}", response_class=HTMLResponse)
    async def render_verification_page(request: Request, token: str) -> HTMLResponse:
        record = await store.get(token)
        context = _build_template_context(request, record, token)
        return templates.TemplateResponse("verify.html", context)

    @app.post("/verify/{token}")
    async def complete_verification(token: str) -> JSONResponse:
        record = await store.get(token)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="验证链接无效")

        now = datetime.now(tz=UTC)
        if record.status != "pending":
            return JSONResponse({"status": "already_verified"})
        if record.expire_at <= now:
            await store.mark_failed(token, now)
            await ban_and_cleanup(app.state.bot, store, record, reason="expired_via_web")
            return JSONResponse({"status": "expired"})

        # token 级串行化,防止并发请求重复执行 lift_restrictions / announce
        token_lock = await store.acquire_token_lock(token)
        async with token_lock:
            # 二次确认,避免前一个并发请求已处理
            record2 = await store.get(token)
            if record2 is None or record2.status != "pending":
                await store.release_token_lock(token)
                return JSONResponse({"status": "already_verified"})
            if record2.expire_at <= datetime.now(tz=UTC):
                await store.mark_failed(token, datetime.now(tz=UTC))
                await ban_and_cleanup(
                    app.state.bot, store, record2, reason="expired_via_web"
                )
                await store.release_token_lock(token)
                return JSONResponse({"status": "expired"})

            updated = await store.mark_verified(token, verified_at=now)
            if not updated:
                await store.release_token_lock(token)
                return JSONResponse({"status": "already_verified"})

            record2.status = "verified"
            record2.verified_at = now

            success = await lift_restrictions(app.state.bot, record2)
            if not success:
                await store.release_token_lock(token)
                return JSONResponse(
                    {
                        "status": "bot_error",
                        "message": "机器人没有足够的权限解除限制，请联系管理员。",
                    },
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            await delete_prompt_message(app.state.bot, record2)
            await notify_verification_success(app.state.bot, record2)
            await announce_group_success(
                app.state.bot, record2, settings.message_ttl_seconds
            )
            try:
                await store.record_verification_event(
                    chat_id=record2.chat_id,
                    user_id=record2.user_id,
                    username=record2.username,
                    event="verified",
                    created_at=now,
                )
            except Exception as exc:  # pragma: no cover - 日志失败不阻断
                import logging as _log
                _log.getLogger(__name__).warning(
                    "记录验证成功事件失败 chat_id=%s user_id=%s error=%r",
                    record2.chat_id,
                    record2.user_id,
                    exc,
                    exc_info=True,
                )
            await store.delete(token)
            await store.release_token_lock(token)

        return JSONResponse({"status": "ok"})

    return app


def _build_template_context(
    request: Request,
    record: VerificationRecord | None,
    token: str,
) -> Dict[str, Any]:
    if record is None:
        message = "验证链接无效或已被使用。"
        show_button = False
    else:
        now = datetime.now(tz=UTC)
        if record.status != "pending":
            message = "该链接已验证成功，可返回 Telegram。"
            show_button = False
        elif record.expire_at <= now:
            message = "验证链接已过期，请回到 Telegram 重新获取。"
            show_button = False
        else:
            _remaining = int((record.expire_at - now).total_seconds() // 60) or 1
            message = "请点击下方按钮完成验证。\n"
            show_button = True

    return {
        "request": request,
        "token": token,
        "message": message,
        "show_button": show_button,
    }


def _verify_telegram_login(
    auth_data: dict[str, str],
    bot_token: str,
    max_age_seconds: int = 300,
) -> bool:
    received_hash = auth_data.get("hash")
    auth_date_raw = auth_data.get("auth_date")
    if not received_hash or not auth_date_raw:
        return False
    try:
        auth_date = int(auth_date_raw)
    except ValueError:
        return False
    now = int(time.time())
    # 拒绝过老的签名 (防重放) + 拒绝来自未来的签名 (防系统时钟欺骗)
    if now - auth_date > max_age_seconds or auth_date - now > 60:
        return False

    data_check_string = "\n".join(
        f"{key}={value}"
        for key, value in sorted(auth_data.items())
        if key != "hash"
    )
    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(calculated_hash, received_hash)


async def _admin_chat_ids_for_user(bot, chat_ids: set[int], user_id: int) -> set[int]:
    allowed: set[int] = set()
    for chat_id in chat_ids:
        try:
            member = await bot.get_chat_member(chat_id, user_id)
        except (TelegramBadRequest, TelegramForbiddenError):
            continue
        if member.status in ADMIN_STATUSES:
            allowed.add(chat_id)
    return allowed


async def _require_admin_session(request: Request, *, api: bool) -> dict | RedirectResponse:
    settings: Settings = request.app.state.settings
    if not settings.admin_web_enabled:
        if api:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
        return RedirectResponse("/admin/login", status_code=status.HTTP_302_FOUND)

    token = request.cookies.get(ADMIN_COOKIE_NAME)
    sessions = request.app.state.admin_sessions
    session = sessions.get(token) if token else None
    if not session or int(session.get("expires_at", 0)) <= int(time.time()):
        if token:
            sessions.pop(token, None)
        if api:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")
        return RedirectResponse("/admin/login", status_code=status.HTTP_302_FOUND)
    return session


__all__ = ["create_web_app"]


# 保留 Optional 占位,兼容历史导入
_unused: Optional[VerificationStore] = None


def _is_secure_request(request: Request, settings: Settings) -> bool:
    if request.url.scheme == "https":
        return True
    if settings.admin_behind_proxy:
        forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        if forwarded == "https":
            return True
    return False


def _client_ip(request: Request, settings: Settings) -> str:
    if settings.admin_behind_proxy:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
        real = request.headers.get("x-real-ip")
        if real:
            return real.strip()
    if request.client:
        return request.client.host or "-"
    return "-"


def _require_csrf(request: Request, session: dict) -> None:
    sent = request.headers.get("x-csrf-token") or ""
    cookie = request.cookies.get(ADMIN_CSRF_COOKIE_NAME) or ""
    expected = session.get("csrf", "")
    if not sent or not expected:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF 校验失败")
    if not hmac.compare_digest(sent, expected) or not hmac.compare_digest(cookie, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF 校验失败")


class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """统一注入安全响应头,所有路由生效。"""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=()",
        )
        # /admin 接口不允许缓存,避免敏感数据落缓存
        if request.url.path.startswith("/admin"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        # 只有当请求确实是 HTTPS 时才发 HSTS,避免开发环境被锁
        settings: Settings = request.app.state.settings
        if _is_secure_request(request, settings):
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        return response


class _AdminRateLimitMiddleware(BaseHTTPMiddleware):
    """对 /admin/* 做按 IP 滑窗限流,默认 60 次/分钟,可配置。"""

    async def dispatch(self, request, call_next):
        if not request.url.path.startswith("/admin"):
            return await call_next(request)
        settings: Settings = request.app.state.settings
        limit = settings.admin_rate_limit_per_min
        if limit <= 0:
            return await call_next(request)

        ip = _client_ip(request, settings)
        now = int(time.time())
        window = now // 60
        key = f"{ip}:{window}"
        buckets: dict = request.app.state.rate_buckets

        # 定期清理旧窗口,避免内存膨胀
        if len(buckets) > 10000:
            stale = [k for k in buckets if not k.endswith(f":{window}")]
            for k in stale[:5000]:
                buckets.pop(k, None)

        count = buckets.get(key, 0) + 1
        buckets[key] = count
        if count > limit:
            logger.warning("admin 限流命中 ip=%s path=%s count=%s", ip, request.url.path, count)
            return JSONResponse(
                {"detail": "请求过于频繁，请稍后再试"},
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                headers={"Retry-After": "30"},
            )
        return await call_next(request)
