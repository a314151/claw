from __future__ import annotations

import argparse
import json
import os
import smtplib
import time
import csv
import io
from contextlib import asynccontextmanager
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse, Response
from pydantic import BaseModel, Field

from .app_logger import logger
from .assistant import PersonalAssistant
from .config import AppConfig, MCPServerConfig, list_provider_options, load_config
from .llm_client import LLMClient
from .mcp_client import MCPManager
from .user_store import MCP_CATALOG, R2KVStore, UserIdentity, UserStore
from .skill_engine import build_skill_guidance
from .time_system import TimeSystem, build_local_time_reply, is_time_query


_TIME_SYSTEM = TimeSystem()


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    session_id: str | None = None
    provider: str | None = None
    model: str | None = None
    api_key: str | None = None


class ChatResponse(BaseModel):
    reply: str
    provider: str
    model: str
    elapsed_ms: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_cny: float = 0.0


class AuthCodeRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class AuthVerifyRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    code: str = Field(min_length=4, max_length=12)


class MCPConfigRequest(BaseModel):
    servers: list[dict[str, Any]] = Field(default_factory=list)


class PricingConfigRequest(BaseModel):
    pricing: dict[str, Any] = Field(default_factory=dict)


class UserMemoryUpdateRequest(BaseModel):
    profile: str = ""
    habits: dict[str, Any] = Field(default_factory=dict)
    skills: list[dict[str, Any]] = Field(default_factory=list)
    pricing: dict[str, Any] = Field(default_factory=dict)
    reason: str = "manual_update"


class ExportRequest(BaseModel):
    format: str = Field(pattern="^(csv|pdf)$")
    content: str = Field(min_length=1, max_length=200000)
    title: str = Field(default="assistant_answer", min_length=1, max_length=200)


class AppState:
    def __init__(self, config: AppConfig, llm: LLMClient, store: UserStore) -> None:
        self.config = config
        self.llm = llm
        self.store = store


DEFAULT_MODEL_PRICE_USD_PER_1M: dict[str, list[tuple[str, float, float]]] = {
    "openai": [
        ("gpt-5", 1.25, 10.0),
        ("gpt-5-mini", 0.25, 2.0),
        ("gpt-4.1", 2.0, 8.0),
        ("gpt-4o", 5.0, 15.0),
    ],
    "deepseek": [
        ("deepseek-v3", 0.27, 1.10),
        ("deepseek-chat", 0.27, 1.10),
        ("deepseek-r1", 0.55, 2.19),
        ("deepseek-reasoner", 0.55, 2.19),
    ],
    "zhipu": [
        ("glm-4.7", 0.80, 0.80),
        ("glm-4-plus", 0.70, 0.70),
        ("glm-4-air", 0.25, 0.25),
        ("glm-4-flash", 0.0, 0.0),
    ],
    "qwen": [
        ("qwen3-max", 0.80, 2.40),
        ("qwen3-plus", 0.40, 1.20),
        ("qwen-max", 0.80, 2.40),
        ("qwen-plus", 0.40, 1.20),
    ],
    "gemini": [
        ("gemini-3-pro", 1.25, 5.0),
        ("gemini-2.5-pro", 1.25, 5.0),
        ("gemini-3-flash", 0.10, 0.40),
        ("gemini-2.5-flash", 0.10, 0.40),
    ],
}


def _find_model_price(provider: str, model: str) -> tuple[float, float, bool]:
    provider_key = (provider or "").strip().lower()
    model_key = (model or "").strip().lower()
    cards = DEFAULT_MODEL_PRICE_USD_PER_1M.get(provider_key, [])
    for prefix, in_usd, out_usd in cards:
        if model_key.startswith(prefix):
            return in_usd, out_usd, True
    return 0.0, 0.0, False


def _normalize_price_table(raw_table: dict[str, Any] | None) -> dict[str, list[tuple[str, float, float]]]:
    normalized: dict[str, list[tuple[str, float, float]]] = {}
    if not isinstance(raw_table, dict):
        return normalized
    for provider, rows in raw_table.items():
        if not isinstance(rows, list):
            continue
        parsed_rows: list[tuple[str, float, float]] = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) != 3:
                continue
            prefix = str(row[0]).strip().lower()
            if not prefix:
                continue
            try:
                in_usd = float(row[1])
                out_usd = float(row[2])
            except (TypeError, ValueError):
                continue
            parsed_rows.append((prefix, in_usd, out_usd))
        if parsed_rows:
            normalized[str(provider).strip().lower()] = parsed_rows
    return normalized


def _find_model_price_from_table(
    provider: str,
    model: str,
    pricing_table: dict[str, list[tuple[str, float, float]]],
) -> tuple[float, float, bool]:
    provider_key = (provider or "").strip().lower()
    model_key = (model or "").strip().lower()
    cards = pricing_table.get(provider_key, [])
    for prefix, in_usd, out_usd in cards:
        if model_key.startswith(prefix):
            return in_usd, out_usd, True
    return 0.0, 0.0, False


def _estimate_cost(
    usage: dict[str, Any],
    provider: str,
    model: str,
    pricing_table: dict[str, list[tuple[str, float, float]]],
) -> dict[str, Any]:
    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    completion_tokens = int(usage.get("completion_tokens", 0) or 0)
    total_tokens = int(usage.get("total_tokens", prompt_tokens + completion_tokens) or 0)

    in_usd_per_1m, out_usd_per_1m, matched = _find_model_price_from_table(provider, model, pricing_table)
    cny_per_usd = float(os.getenv("CNY_PER_USD", "7.2") or 7.2)

    usd_cost = (prompt_tokens / 1_000_000.0) * in_usd_per_1m + (completion_tokens / 1_000_000.0) * out_usd_per_1m
    cost_cny = round(usd_cost * cny_per_usd, 6)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cost_cny": cost_cny,
        "pricing_matched": matched,
        "cny_per_usd": cny_per_usd,
    }


def _safe_file_name(title: str, ext: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in title.strip())
    cleaned = cleaned.strip("_") or "assistant_answer"
    return f"{cleaned}{ext}"


def _to_csv_bytes(content: str) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["line", "text"])
    for idx, line in enumerate(content.splitlines() or [content], start=1):
        writer.writerow([idx, line])
    return buf.getvalue().encode("utf-8-sig")


def _to_pdf_bytes(title: str, content: str) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    stream = io.BytesIO()
    c = canvas.Canvas(stream, pagesize=A4)
    width, height = A4
    x = 48
    y = height - 56
    line_h = 18

    c.setFont("Helvetica-Bold", 16)
    c.drawString(x, y, title[:80])
    y -= 28
    c.setFont("Helvetica", 11)

    for line in content.splitlines() or [content]:
        if y < 48:
            c.showPage()
            c.setFont("Helvetica", 11)
            y = height - 56
        c.drawString(x, y, line[:160])
        y -= line_h

    c.save()
    return stream.getvalue()


def _is_time_query(message: str) -> bool:
    return is_time_query(message)


def _build_local_time_reply() -> str:
    return build_local_time_reply(_TIME_SYSTEM)


def _send_email_code(to_email: str, code: str) -> str | None:
    host = os.getenv("SMTP_HOST", "").strip()
    user = (os.getenv("SMTP_USERNAME", "").strip() or os.getenv("SMTP_USER", "").strip())
    password = (os.getenv("SMTP_PASSWORD", "").strip() or os.getenv("SMTP_PASS", "").strip())
    sender = (os.getenv("SMTP_FROM", "").strip() or os.getenv("MAIL_FROM", "").strip() or user)
    port = int(os.getenv("SMTP_PORT", "587").strip() or "587")
    use_tls = os.getenv("SMTP_USE_TLS", "true").strip().lower() in {"1", "true", "yes"}
    use_ssl = os.getenv("SMTP_USE_SSL", "false").strip().lower() in {"1", "true", "yes"}

    debug_echo = os.getenv("SMTP_DEV_ECHO_CODE", "false").strip().lower() in {"1", "true", "yes"}
    if not host or not sender:
        if debug_echo:
            logger.warning("SMTP not configured, using debug echo mode.")
            return code
        raise RuntimeError("SMTP 配置不完整，请设置 SMTP_HOST/SMTP_FROM")

    msg = EmailMessage()
    msg["Subject"] = "登录验证码"
    msg["From"] = sender
    msg["To"] = to_email
    msg.set_content(f"你的登录验证码是: {code}\n5 分钟内有效。")

    # 465 常用隐式 SSL；587 常用 STARTTLS。
    if port == 465 and not use_ssl:
        use_ssl = True

    if use_ssl:
        with smtplib.SMTP_SSL(host, port, timeout=15) as smtp:
            if user and password:
                smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            if use_tls:
                smtp.starttls()
            if user and password:
                smtp.login(user, password)
            smtp.send_message(msg)
    return None


def _parse_bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="未登录")
    parts = authorization.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(status_code=401, detail="登录态格式错误")
    return parts[1].strip()


def _build_user_mcp_configs(base: list[MCPServerConfig], user_servers: list[dict[str, Any]]) -> list[MCPServerConfig]:
    merged = list(base)
    for item in user_servers:
        name = str(item.get("name", "")).strip()
        command = str(item.get("command", "")).strip()
        args = item.get("args", [])
        env = item.get("env", {})
        if not name or not command or not isinstance(args, list) or not isinstance(env, dict):
            continue
        normalized_name = name.strip().lower()
        normalized_args = [str(x) for x in args]
        normalized_env = {str(k): str(v) for k, v in env.items()}
        if normalized_name == "playwright":
            if "--headless" not in normalized_args:
                normalized_args.append("--headless")
            normalized_env.setdefault("PLAYWRIGHT_MCP_HEADLESS", "1")
        merged.append(
            MCPServerConfig(
                name=name,
                command=command,
                args=normalized_args,
                env=normalized_env,
                startup_timeout_s=15.0,
            )
        )
    return merged


async def _resolve_user(request: Request, authorization: str | None) -> UserIdentity:
    token = _parse_bearer_token(authorization)
    data: AppState = request.app.state.data
    try:
        return await data.store.resolve_token(token)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def create_app(config_path: str | None = None, debug: bool = False) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("Starting web app lifespan...")
        config = load_config(config_path)
        llm = LLMClient(config.llm_providers, config.default_provider)
        store = UserStore(R2KVStore())
        app.state.data = AppState(config=config, llm=llm, store=store)
        logger.info("Web app initialization complete.")
        yield
        logger.info("Shutting down web app.")

    app = FastAPI(title="Personal LLM+MCP Assistant", debug=debug, lifespan=lifespan)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        page = Path(__file__).with_name("web").joinpath("index.html")
        return HTMLResponse(
            content=page.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )

    @app.post("/api/auth/request-code")
    async def request_code(req: AuthCodeRequest) -> dict[str, Any]:
        # 兼容旧客户端：默认按登录流程发送验证码
        data: AppState = app.state.data
        try:
            code = await data.store.request_login_code(req.email)
            preview_code = _send_email_code(req.email.strip().lower(), code)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Request code failed: %s", exc)
            raise HTTPException(status_code=500, detail=f"发送验证码失败: {exc}") from exc
        result: dict[str, Any] = {"ok": True, "message": "验证码已发送，请查收邮箱"}
        if preview_code:
            result["debug_code"] = preview_code
        return result

    @app.post("/api/auth/verify")
    async def verify_code(req: AuthVerifyRequest) -> dict[str, Any]:
        # 兼容旧客户端：默认按登录流程校验
        data: AppState = app.state.data
        try:
            token = await data.store.verify_login_code(req.email, req.code)
            user = await data.store.resolve_token(token)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"token": token, "email": user.email, "user_id": user.user_id}

    @app.post("/api/auth/register/request-code")
    async def request_register_code(req: AuthCodeRequest) -> dict[str, Any]:
        data: AppState = app.state.data
        try:
            code = await data.store.request_register_code(req.email)
            preview_code = _send_email_code(req.email.strip().lower(), code)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Request code failed: %s", exc)
            raise HTTPException(status_code=500, detail=f"发送验证码失败: {exc}") from exc
        result: dict[str, Any] = {"ok": True, "message": "验证码已发送，请查收邮箱"}
        if preview_code:
            result["debug_code"] = preview_code
        return result

    @app.post("/api/auth/register/verify")
    async def verify_register_code(req: AuthVerifyRequest) -> dict[str, Any]:
        data: AppState = app.state.data
        try:
            token = await data.store.verify_register_code(req.email, req.code)
            user = await data.store.resolve_token(token)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"token": token, "email": user.email, "user_id": user.user_id}

    @app.post("/api/auth/login/request-code")
    async def request_login_code(req: AuthCodeRequest) -> dict[str, Any]:
        data: AppState = app.state.data
        try:
            code = await data.store.request_login_code(req.email)
            preview_code = _send_email_code(req.email.strip().lower(), code)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Request code failed: %s", exc)
            raise HTTPException(status_code=500, detail=f"发送验证码失败: {exc}") from exc
        result: dict[str, Any] = {"ok": True, "message": "验证码已发送，请查收邮箱"}
        if preview_code:
            result["debug_code"] = preview_code
        return result

    @app.post("/api/auth/login/verify")
    async def verify_login_code(req: AuthVerifyRequest) -> dict[str, Any]:
        data: AppState = app.state.data
        try:
            token = await data.store.verify_login_code(req.email, req.code)
            user = await data.store.resolve_token(token)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"token": token, "email": user.email, "user_id": user.user_id}

    @app.get("/api/me")
    async def me(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        user = await _resolve_user(request, authorization)
        return {"email": user.email, "user_id": user.user_id}

    @app.get("/api/providers")
    async def providers() -> dict[str, Any]:
        data: AppState = app.state.data
        return {
            "default_provider": data.config.default_provider,
            "providers": list_provider_options(data.config.llm_providers),
        }

    @app.get("/api/mcp/catalog")
    async def mcp_catalog() -> dict[str, Any]:
        return {"items": MCP_CATALOG}

    @app.get("/api/mcp/config")
    async def get_mcp_config(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        data: AppState = app.state.data
        user = await _resolve_user(request, authorization)
        servers = await data.store.get_mcp_config(user.user_id)
        return {"servers": servers}

    @app.post("/api/mcp/config")
    async def save_mcp_config(
        req: MCPConfigRequest,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        data: AppState = app.state.data
        user = await _resolve_user(request, authorization)
        await data.store.save_mcp_config(user.user_id, req.servers)
        return {"ok": True}

    @app.get("/api/pricing")
    async def get_pricing(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        data: AppState = app.state.data
        user = await _resolve_user(request, authorization)
        defaults: dict[str, Any] = {
            k: [[x[0], x[1], x[2]] for x in v] for k, v in DEFAULT_MODEL_PRICE_USD_PER_1M.items()
        }
        pricing = await data.store.get_pricing_config(user.user_id, defaults)
        return {"pricing": pricing, "defaults": defaults}

    @app.post("/api/pricing")
    async def save_pricing(
        req: PricingConfigRequest,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        data: AppState = app.state.data
        user = await _resolve_user(request, authorization)
        saved = await data.store.save_pricing_config(user.user_id, req.pricing)
        return {"ok": True, "pricing": saved}

    @app.get("/api/user/memory")
    async def get_user_memory(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        data: AppState = app.state.data
        user = await _resolve_user(request, authorization)
        defaults: dict[str, Any] = {
            k: [[x[0], x[1], x[2]] for x in v] for k, v in DEFAULT_MODEL_PRICE_USD_PER_1M.items()
        }
        hub = await data.store.get_memory_hub(user.user_id, pricing_defaults=defaults)
        versions = await data.store.list_memory_versions(user.user_id)
        return {"memory": hub, "versions": versions}

    @app.post("/api/user/memory")
    async def save_user_memory(
        req: UserMemoryUpdateRequest,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        data: AppState = app.state.data
        user = await _resolve_user(request, authorization)
        saved = await data.store.save_memory_hub(
            user_id=user.user_id,
            profile=req.profile,
            habits=req.habits,
            skills=req.skills,
            pricing=req.pricing,
            reason=req.reason,
        )
        versions = await data.store.list_memory_versions(user.user_id)
        return {"ok": True, "memory": saved, "versions": versions}

    @app.get("/api/user/memory/versions")
    async def list_user_memory_versions(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        data: AppState = app.state.data
        user = await _resolve_user(request, authorization)
        versions = await data.store.list_memory_versions(user.user_id)
        return {"items": versions}

    @app.post("/api/user/memory/restore/{version_id}")
    async def restore_user_memory_version(
        version_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        data: AppState = app.state.data
        user = await _resolve_user(request, authorization)
        restored = await data.store.restore_memory_version(user.user_id, version_id)
        versions = await data.store.list_memory_versions(user.user_id)
        return {"ok": True, "memory": restored, "versions": versions}

    @app.post("/api/export/answer")
    async def export_answer(
        req: ExportRequest,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> Response:
        _ = await _resolve_user(request, authorization)
        fmt = req.format.strip().lower()
        title = req.title.strip() or "assistant_answer"
        if fmt == "csv":
            data = _to_csv_bytes(req.content)
            filename = _safe_file_name(title, ".csv")
            return Response(
                content=data,
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": f"attachment; filename={filename}"},
            )

        data = _to_pdf_bytes(title, req.content)
        filename = _safe_file_name(title, ".pdf")
        return Response(
            content=data,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    @app.get("/api/history/sessions")
    async def list_history_sessions(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        data: AppState = app.state.data
        user = await _resolve_user(request, authorization)
        sessions = await data.store.list_history_sessions(user.user_id)
        return {"items": sessions}

    @app.get("/api/history/{session_id}")
    async def get_history(session_id: str, request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        data: AppState = app.state.data
        user = await _resolve_user(request, authorization)
        history = await data.store.get_history(user.user_id, session_id, include_meta=True)
        return {"session_id": session_id, "messages": history}

    @app.post("/api/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest, request: Request, authorization: str | None = Header(default=None)) -> ChatResponse:
        data: AppState = app.state.data
        user = await _resolve_user(request, authorization)

        provider = (req.provider or data.config.default_provider).strip().lower()
        provider_cfg = data.config.llm_providers.get(provider)
        if provider_cfg is None:
            raise HTTPException(status_code=400, detail=f"不支持的 provider: {provider}")

        model = (req.model or provider_cfg.model).strip()
        if not model:
            raise HTTPException(status_code=400, detail="model 不能为空")

        api_key = (req.api_key or "").strip() or None
        session_id = (req.session_id or "default").strip() or "default"

        await data.store.append_history_message(user.user_id, session_id, "user", req.message)
        if _is_time_query(req.message):
            reply = _build_local_time_reply()
            await data.store.append_history_message(
                user.user_id,
                session_id,
                "assistant",
                reply,
                meta={
                    "tool_events": [],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                    "cost": {"cost_cny": 0.0, "pricing_matched": True},
                    "provider": "local",
                    "model": "clock",
                    "matched_skills": [],
                    "skill_guidance": "",
                    "time_snapshot": _TIME_SYSTEM.build_time_memory_node(),
                },
            )
            await data.store.update_habits(
                user_id=user.user_id,
                user_message=req.message,
                provider="local",
                model="clock",
                tool_event_count=0,
            )
            return ChatResponse(
                reply=reply,
                provider="local",
                model="clock",
                elapsed_ms=1,
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                cost_cny=0.0,
            )

        history = await data.store.get_history(user.user_id, session_id)
        profile = await data.store.get_profile_text(user.user_id)
        defaults: dict[str, Any] = {
            k: [[x[0], x[1], x[2]] for x in v] for k, v in DEFAULT_MODEL_PRICE_USD_PER_1M.items()
        }
        pricing_raw = await data.store.get_pricing_config(user.user_id, defaults)
        pricing_table = _normalize_price_table(pricing_raw) or _normalize_price_table(defaults)
        skills = await data.store.get_skills(user.user_id)
        matched_skills, skill_guidance = build_skill_guidance(req.message, skills)
        user_mcp = await data.store.get_mcp_config(user.user_id)
        merged_mcp = _build_user_mcp_configs(data.config.mcp_servers, user_mcp)

        start = time.perf_counter()
        try:
            async with MCPManager(merged_mcp) as mcp:
                assistant = PersonalAssistant(llm=data.llm, mcp=mcp, max_turns=data.config.max_turns)
                result = await assistant.ask_with_metrics(
                    req.message,
                    provider=provider,
                    model=model,
                    api_key=api_key,
                    history=history[:-1],
                    user_profile=profile,
                    skill_guidance=skill_guidance,
                )
                reply = str(result.get("reply", "")).strip()
                raw_usage = result.get("usage")
                usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
                raw_tool_events = result.get("tool_events")
                tool_events: list[dict[str, Any]] = raw_tool_events if isinstance(raw_tool_events, list) else []
                cost_info = _estimate_cost(usage, provider=provider, model=model, pricing_table=pricing_table)
            logger.info("Chat request completed successfully.")
        except Exception as exc:
            logger.exception("Web Chat failed: %s", exc)
            raise HTTPException(status_code=500, detail=f"处理失败: {exc}") from exc

        await data.store.append_history_message(
            user.user_id,
            session_id,
            "assistant",
            reply,
            meta={
                "tool_events": tool_events,
                "usage": {
                    "prompt_tokens": cost_info["prompt_tokens"],
                    "completion_tokens": cost_info["completion_tokens"],
                    "total_tokens": cost_info["total_tokens"],
                },
                "cost": {
                    "cost_cny": cost_info["cost_cny"],
                    "pricing_matched": bool(cost_info["pricing_matched"]),
                },
                "provider": provider,
                "model": model,
                "matched_skills": [str(x.get("title", "")) for x in matched_skills],
                "skill_guidance": skill_guidance,
                "time_snapshot": _TIME_SYSTEM.build_time_memory_node(),
            },
        )
        updated_history = await data.store.get_history(user.user_id, session_id)
        await data.store.maybe_update_profile(user.user_id, req.message, updated_history)
        await data.store.update_habits(
            user_id=user.user_id,
            user_message=req.message,
            provider=provider,
            model=model,
            tool_event_count=len(tool_events),
        )

        elapsed_ms = int((time.perf_counter() - start) * 1000)
        return ChatResponse(
            reply=reply,
            provider=provider,
            model=model,
            elapsed_ms=elapsed_ms,
            prompt_tokens=cost_info["prompt_tokens"],
            completion_tokens=cost_info["completion_tokens"],
            total_tokens=cost_info["total_tokens"],
            cost_cny=cost_info["cost_cny"],
        )

    @app.post("/api/chat/stream")
    async def chat_stream(
        req: ChatRequest,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> StreamingResponse:
        data: AppState = app.state.data
        user = await _resolve_user(request, authorization)

        provider = (req.provider or data.config.default_provider).strip().lower()
        provider_cfg = data.config.llm_providers.get(provider)
        if provider_cfg is None:
            raise HTTPException(status_code=400, detail=f"不支持的 provider: {provider}")

        model = (req.model or provider_cfg.model).strip()
        if not model:
            raise HTTPException(status_code=400, detail="model 不能为空")

        api_key = (req.api_key or "").strip() or None
        session_id = (req.session_id or "default").strip() or "default"

        async def event_generator():
            await data.store.append_history_message(user.user_id, session_id, "user", req.message)
            if _is_time_query(req.message):
                final_text = _build_local_time_reply()
                yield "data: " + json.dumps({"type": "delta", "content": final_text}, ensure_ascii=False) + "\n\n"
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "type": "metrics",
                            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                            "cost": {"cost_cny": 0.0, "pricing_matched": True},
                            "provider": "local",
                            "model": "clock",
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
                await data.store.append_history_message(
                    user.user_id,
                    session_id,
                    "assistant",
                    final_text,
                    meta={
                        "tool_events": [],
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                        "cost": {"cost_cny": 0.0, "pricing_matched": True},
                        "provider": "local",
                        "model": "clock",
                        "matched_skills": [],
                        "skill_guidance": "",
                        "time_snapshot": _TIME_SYSTEM.build_time_memory_node(),
                    },
                )
                await data.store.update_habits(
                    user_id=user.user_id,
                    user_message=req.message,
                    provider="local",
                    model="clock",
                    tool_event_count=0,
                )
                yield "data: {\"type\":\"done\"}\n\n"
                return

            history = await data.store.get_history(user.user_id, session_id)
            profile = await data.store.get_profile_text(user.user_id)
            defaults: dict[str, Any] = {
                k: [[x[0], x[1], x[2]] for x in v] for k, v in DEFAULT_MODEL_PRICE_USD_PER_1M.items()
            }
            pricing_raw = await data.store.get_pricing_config(user.user_id, defaults)
            pricing_table = _normalize_price_table(pricing_raw) or _normalize_price_table(defaults)
            skills = await data.store.get_skills(user.user_id)
            matched_skills, skill_guidance = build_skill_guidance(req.message, skills)
            user_mcp = await data.store.get_mcp_config(user.user_id)
            merged_mcp = _build_user_mcp_configs(data.config.mcp_servers, user_mcp)

            full_reply: list[str] = []
            tool_events: list[dict[str, Any]] = []
            usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            try:
                async with MCPManager(merged_mcp) as mcp:
                    assistant = PersonalAssistant(llm=data.llm, mcp=mcp, max_turns=data.config.max_turns)
                    if matched_skills:
                        yield (
                            "data: "
                            + json.dumps(
                                {
                                    "type": "skill_match",
                                    "items": [str(x.get("title", "")) for x in matched_skills],
                                },
                                ensure_ascii=False,
                            )
                            + "\n\n"
                        )
                    async for evt in assistant.ask_stream_events(
                        req.message,
                        provider=provider,
                        model=model,
                        api_key=api_key,
                        history=history[:-1],
                        user_profile=profile,
                        skill_guidance=skill_guidance,
                    ):
                        if evt.get("type") == "delta":
                            chunk = str(evt.get("content", ""))
                            if chunk:
                                full_reply.append(chunk)
                        if evt.get("type") in {"tool_start", "tool_result", "tool_error"}:
                            tool_events.append(evt)
                        if evt.get("type") == "metrics":
                            raw_usage_obj = evt.get("usage")
                            raw_usage = raw_usage_obj if isinstance(raw_usage_obj, dict) else {}
                            usage = {
                                "prompt_tokens": int(raw_usage.get("prompt_tokens", 0) or 0),
                                "completion_tokens": int(raw_usage.get("completion_tokens", 0) or 0),
                                "total_tokens": int(raw_usage.get("total_tokens", 0) or 0),
                            }
                            cost_info = _estimate_cost(
                                usage,
                                provider=provider,
                                model=model,
                                pricing_table=pricing_table,
                            )
                            evt = {
                                "type": "metrics",
                                "usage": usage,
                                "cost": {
                                    "cost_cny": cost_info["cost_cny"],
                                    "pricing_matched": bool(cost_info["pricing_matched"]),
                                },
                                "provider": provider,
                                "model": model,
                            }
                        payload = json.dumps(evt, ensure_ascii=False)
                        yield f"data: {payload}\n\n"

                final_text = "".join(full_reply).strip()
                if final_text:
                    cost_info = _estimate_cost(
                        usage,
                        provider=provider,
                        model=model,
                        pricing_table=pricing_table,
                    )
                    await data.store.append_history_message(
                        user.user_id,
                        session_id,
                        "assistant",
                        final_text,
                        meta={
                            "tool_events": tool_events,
                            "usage": usage,
                            "cost": {
                                "cost_cny": cost_info["cost_cny"],
                                "pricing_matched": bool(cost_info["pricing_matched"]),
                            },
                            "provider": provider,
                            "model": model,
                            "matched_skills": [str(x.get("title", "")) for x in matched_skills],
                            "skill_guidance": skill_guidance,
                            "time_snapshot": _TIME_SYSTEM.build_time_memory_node(),
                        },
                    )
                    updated_history = await data.store.get_history(user.user_id, session_id)
                    await data.store.maybe_update_profile(user.user_id, req.message, updated_history)
                    await data.store.update_habits(
                        user_id=user.user_id,
                        user_message=req.message,
                        provider=provider,
                        model=model,
                        tool_event_count=len(tool_events),
                    )
                yield "data: {\"type\":\"done\"}\n\n"
            except Exception as exc:
                logger.exception("Web Chat stream failed: %s", exc)
                payload = json.dumps({"type": "error", "message": f"处理失败: {exc}"}, ensure_ascii=False)
                yield f"data: {payload}\n\n"

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    return app


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run local web UI for personal assistant")
    parser.add_argument("--config", default=None, help="MCP 配置文件路径（JSON）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8090, help="监听端口")
    parser.add_argument("--debug", action="store_true", help="开启调试模式")
    return parser


def main() -> None:
    args = _parser().parse_args()
    app = create_app(config_path=args.config, debug=args.debug)
    uvicorn.run(app, host=args.host, port=args.port, log_level="debug" if args.debug else "info")


if __name__ == "__main__":
    main()
