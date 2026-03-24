from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import secrets
import copy
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import boto3
import httpx
from botocore.config import Config
from botocore.exceptions import ClientError

from .app_logger import logger
from .skill_engine import load_default_skills, normalize_skills


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _now_ts() -> int:
    return int(time.time())


def _word_count(text: str) -> int:
    return len([x for x in re.split(r"\s+", text.strip()) if x])


def _clip_words(text: str, max_words: int) -> str:
    parts = [x for x in re.split(r"\s+", text.strip()) if x]
    if len(parts) <= max_words:
        return text.strip()
    return " ".join(parts[:max_words]).strip()


def _safe_json_load(text: str | None, default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def _session_title_from_text(text: str, max_chars: int = 24) -> str:
    cleaned = re.sub(r"\s+", " ", text.strip())
    if not cleaned:
        return "新对话"
    return cleaned if len(cleaned) <= max_chars else f"{cleaned[:max_chars]}..."


@dataclass(slots=True)
class StorageConfig:
    account_id: str
    api_token: str
    bucket: str
    endpoint: str
    access_key_id: str
    secret_access_key: str


class R2KVStore:
    """Cloudflare R2 object storage wrapper.

    Uses Cloudflare's REST API for simple JSON/text get/put.
    If R2 credentials are missing, falls back to local files to keep the app runnable.
    """

    def __init__(self) -> None:
        account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
        api_token = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
        bucket = os.getenv("CLOUDFLARE_R2_BUCKET", "").strip()
        endpoint = os.getenv("CLOUDFLARE_R2_ENDPOINT", "").strip()
        access_key_id = os.getenv("CLOUDFLARE_R2_ACCESS_KEY_ID", "").strip()
        secret_access_key = os.getenv("CLOUDFLARE_R2_SECRET_ACCESS_KEY", "").strip()

        use_s3 = bool(endpoint and bucket and access_key_id and secret_access_key)
        use_api = bool(account_id and api_token and bucket)

        self._r2_enabled = use_s3 or use_api
        self._cfg = StorageConfig(
            account_id=account_id,
            api_token=api_token,
            bucket=bucket,
            endpoint=endpoint,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
        )
        self._mode = "s3" if use_s3 else ("api" if use_api else "local")
        self._base = (
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}/r2/buckets/{bucket}/objects"
            if use_api
            else ""
        )
        self._s3 = (
            boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_access_key,
                region_name="auto",
                config=Config(signature_version="s3v4"),
            )
            if use_s3
            else None
        )
        self._local_root = Path("data")
        self._local_root.mkdir(parents=True, exist_ok=True)

    @property
    def is_r2_enabled(self) -> bool:
        return self._r2_enabled

    async def get_text(self, key: str) -> str | None:
        if self._r2_enabled:
            try:
                return await self._r2_get_text(key)
            except Exception as exc:
                logger.warning("R2 get failed, fallback to local. key=%s err=%s", key, exc)
        return self._local_get_text(key)

    async def put_text(self, key: str, content: str, content_type: str = "text/plain; charset=utf-8") -> None:
        if self._r2_enabled:
            try:
                await self._r2_put_text(key, content, content_type=content_type)
                return
            except Exception as exc:
                logger.warning("R2 put failed, fallback to local. key=%s err=%s", key, exc)
        self._local_put_text(key, content)

    async def delete(self, key: str) -> None:
        if self._r2_enabled:
            try:
                await self._r2_delete(key)
                return
            except Exception as exc:
                logger.warning("R2 delete failed, fallback to local. key=%s err=%s", key, exc)
        local_path = self._local_root.joinpath(key)
        if local_path.exists():
            local_path.unlink()

    async def _r2_get_text(self, key: str) -> str | None:
        if self._mode == "s3" and self._s3 is not None:
            return await asyncio.to_thread(self._s3_get_text_sync, key)
        encoded = quote(key, safe="/")
        url = f"{self._base}/{encoded}"
        headers = {"Authorization": f"Bearer {self._cfg.api_token}"}
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.text

    async def _r2_put_text(self, key: str, content: str, content_type: str) -> None:
        if self._mode == "s3" and self._s3 is not None:
            await asyncio.to_thread(self._s3_put_text_sync, key, content, content_type)
            return
        encoded = quote(key, safe="/")
        url = f"{self._base}/{encoded}"
        headers = {
            "Authorization": f"Bearer {self._cfg.api_token}",
            "Content-Type": content_type,
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.put(url, headers=headers, content=content.encode("utf-8"))
            resp.raise_for_status()

    async def _r2_delete(self, key: str) -> None:
        if self._mode == "s3" and self._s3 is not None:
            await asyncio.to_thread(self._s3_delete_sync, key)
            return
        encoded = quote(key, safe="/")
        url = f"{self._base}/{encoded}"
        headers = {"Authorization": f"Bearer {self._cfg.api_token}"}
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.delete(url, headers=headers)
            if resp.status_code not in (200, 204, 404):
                resp.raise_for_status()

    def _s3_get_text_sync(self, key: str) -> str | None:
        assert self._s3 is not None
        try:
            resp = self._s3.get_object(Bucket=self._cfg.bucket, Key=key)
            data = resp["Body"].read()
            return data.decode("utf-8")
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"NoSuchKey", "404", "NotFound"}:
                return None
            raise

    def _s3_put_text_sync(self, key: str, content: str, content_type: str) -> None:
        assert self._s3 is not None
        self._s3.put_object(
            Bucket=self._cfg.bucket,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType=content_type,
        )

    def _s3_delete_sync(self, key: str) -> None:
        assert self._s3 is not None
        self._s3.delete_object(Bucket=self._cfg.bucket, Key=key)

    def _local_get_text(self, key: str) -> str | None:
        local_path = self._local_root.joinpath(key)
        if not local_path.exists():
            return None
        return local_path.read_text(encoding="utf-8")

    def _local_put_text(self, key: str, content: str) -> None:
        local_path = self._local_root.joinpath(key)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(content, encoding="utf-8")


@dataclass(slots=True)
class UserIdentity:
    user_id: str
    email: str


MCP_CATALOG: list[dict[str, Any]] = [
    {
        "id": "filesystem",
        "name": "Filesystem",
        "github": "https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem",
        "command": "npx",
        "args_template": ["-y", "@modelcontextprotocol/server-filesystem", "${WORKSPACE_PATH}"],
        "required_env": [],
        "description": "安全文件读写与目录操作。",
    },
    {
        "id": "git",
        "name": "Git",
        "github": "https://github.com/modelcontextprotocol/servers/tree/main/src/git",
        "command": "uvx",
        "args_template": ["mcp-server-git", "--repository", "${WORKSPACE_PATH}"],
        "required_env": [],
        "description": "仓库检索与 Git 操作工具。",
    },
    {
        "id": "memory",
        "name": "Memory",
        "github": "https://github.com/modelcontextprotocol/servers/tree/main/src/memory",
        "command": "npx",
        "args_template": ["-y", "@modelcontextprotocol/server-memory"],
        "required_env": [],
        "description": "知识图谱式持久记忆。",
    },
    {
        "id": "github",
        "name": "GitHub",
        "github": "https://github.com/github/github-mcp-server",
        "command": "npx",
        "args_template": ["-y", "@modelcontextprotocol/server-github"],
        "required_env": ["GITHUB_PERSONAL_ACCESS_TOKEN"],
        "description": "GitHub 官方 MCP，支持仓库/Issue/PR。",
    },
    {
        "id": "fetch",
        "name": "Fetch",
        "github": "https://github.com/modelcontextprotocol/servers/tree/main/src/fetch",
        "command": "uvx",
        "args_template": ["mcp-server-fetch"],
        "required_env": [],
        "description": "网页抓取与内容转换。",
    },
    {
        "id": "postgres",
        "name": "Postgres",
        "github": "https://github.com/modelcontextprotocol/servers/tree/main/src/postgres",
        "command": "npx",
        "args_template": ["-y", "@modelcontextprotocol/server-postgres", "${POSTGRES_DSN}"],
        "required_env": [],
        "description": "PostgreSQL 查询与结构读取。",
    },
    {
        "id": "playwright",
        "name": "Playwright",
        "github": "https://github.com/microsoft/playwright-mcp",
        "command": "npx",
        "args_template": ["-y", "@playwright/mcp", "--headless"],
        "required_env": [],
        "description": "浏览器自动化与页面抓取。",
    },
    {
        "id": "cloudflare",
        "name": "Cloudflare",
        "github": "https://github.com/cloudflare/mcp-server-cloudflare",
        "command": "npx",
        "args_template": ["-y", "@cloudflare/mcp-server-cloudflare"],
        "required_env": ["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"],
        "description": "Cloudflare 官方 MCP（Workers/KV/R2/D1）。",
    },
    {
        "id": "doc_export",
        "name": "Doc Export",
        "github": "https://github.com/modelcontextprotocol/python-sdk",
        "command": "uv",
        "args_template": ["run", "python", "-m", "personal_assistant.mcp_document_server"],
        "required_env": [],
        "description": "本地 CSV/PDF 生成工具（无需浏览器上下文）。",
    },
]


class UserStore:
    def __init__(self, kv: R2KVStore) -> None:
        self._kv = kv

    async def get_skills(self, user_id: str) -> list[dict[str, Any]]:
        raw = await self._kv.get_text(f"skills/{user_id}.json")
        data = _safe_json_load(raw, None)
        if isinstance(data, list):
            normalized = normalize_skills(data)
            if normalized:
                return normalized
        defaults = load_default_skills()
        await self._kv.put_text(
            f"skills/{user_id}.json",
            json.dumps(defaults, ensure_ascii=False),
            content_type="application/json",
        )
        return defaults

    async def save_skills(self, user_id: str, skills: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized = normalize_skills(skills)
        if not normalized:
            normalized = load_default_skills()
        await self._kv.put_text(
            f"skills/{user_id}.json",
            json.dumps(normalized, ensure_ascii=False),
            content_type="application/json",
        )
        return normalized

    async def get_pricing_config(self, user_id: str, defaults: dict[str, Any]) -> dict[str, Any]:
        raw = await self._kv.get_text(f"pricing/{user_id}.json")
        data = _safe_json_load(raw, None)
        if isinstance(data, dict) and data:
            return data
        return copy.deepcopy(defaults)

    async def save_pricing_config(self, user_id: str, pricing: dict[str, Any]) -> dict[str, Any]:
        sanitized = pricing if isinstance(pricing, dict) else {}
        await self._kv.put_text(
            f"pricing/{user_id}.json",
            json.dumps(sanitized, ensure_ascii=False),
            content_type="application/json",
        )
        return sanitized

    async def get_habits(self, user_id: str) -> dict[str, Any]:
        raw = await self._kv.get_text(f"habits/{user_id}.json")
        data = _safe_json_load(raw, {})
        if isinstance(data, dict):
            return data
        return {}

    async def update_habits(
        self,
        user_id: str,
        user_message: str,
        provider: str,
        model: str,
        tool_event_count: int,
    ) -> dict[str, Any]:
        habits = await self.get_habits(user_id)
        habits["message_count"] = int(habits.get("message_count", 0)) + 1
        habits["avg_user_message_length"] = round(
            (
                float(habits.get("avg_user_message_length", 0.0)) * max(int(habits.get("message_count", 1)) - 1, 0)
                + len(user_message)
            ) / max(int(habits.get("message_count", 1)), 1),
            2,
        )
        habits["last_provider"] = provider
        habits["last_model"] = model
        habits["tool_event_total"] = int(habits.get("tool_event_total", 0)) + max(tool_event_count, 0)
        hour = int(time.localtime().tm_hour)
        active_hours = habits.get("active_hours", {})
        if not isinstance(active_hours, dict):
            active_hours = {}
        active_hours[str(hour)] = int(active_hours.get(str(hour), 0)) + 1
        habits["active_hours"] = active_hours
        habits["updated_at"] = _now_ts()

        await self._kv.put_text(
            f"habits/{user_id}.json",
            json.dumps(habits, ensure_ascii=False),
            content_type="application/json",
        )
        return habits

    async def get_memory_hub(self, user_id: str, pricing_defaults: dict[str, Any]) -> dict[str, Any]:
        profile = await self.get_profile_text(user_id)
        habits = await self.get_habits(user_id)
        skills = await self.get_skills(user_id)
        pricing = await self.get_pricing_config(user_id, pricing_defaults)
        return {
            "profile": profile,
            "habits": habits,
            "skills": skills,
            "pricing": pricing,
        }

    async def _push_memory_snapshot(self, user_id: str, reason: str) -> None:
        current = await self.get_memory_hub(user_id, pricing_defaults={})
        key = f"memory_versions/{user_id}.json"
        raw = await self._kv.get_text(key)
        items = _safe_json_load(raw, [])
        if not isinstance(items, list):
            items = []
        version_id = f"v_{_now_ts()}_{secrets.token_hex(4)}"
        items.append(
            {
                "version_id": version_id,
                "reason": reason,
                "created_at": _now_ts(),
                "snapshot": current,
            }
        )
        items = items[-50:]
        await self._kv.put_text(key, json.dumps(items, ensure_ascii=False), content_type="application/json")

    async def save_memory_hub(
        self,
        user_id: str,
        profile: str,
        habits: dict[str, Any],
        skills: list[dict[str, Any]],
        pricing: dict[str, Any],
        reason: str = "manual_update",
    ) -> dict[str, Any]:
        await self._push_memory_snapshot(user_id, reason=reason)
        profile_text = _clip_words(str(profile or "").strip(), 5000)
        await self._kv.put_text(f"profiles/{user_id}.md", profile_text)
        await self._kv.put_text(
            f"habits/{user_id}.json",
            json.dumps(habits if isinstance(habits, dict) else {}, ensure_ascii=False),
            content_type="application/json",
        )
        normalized_skills = await self.save_skills(user_id, skills)
        await self.save_pricing_config(user_id, pricing if isinstance(pricing, dict) else {})
        return {
            "profile": profile_text,
            "habits": habits if isinstance(habits, dict) else {},
            "skills": normalized_skills,
            "pricing": pricing if isinstance(pricing, dict) else {},
        }

    async def list_memory_versions(self, user_id: str) -> list[dict[str, Any]]:
        raw = await self._kv.get_text(f"memory_versions/{user_id}.json")
        items = _safe_json_load(raw, [])
        if not isinstance(items, list):
            return []
        result: list[dict[str, Any]] = []
        for item in reversed(items):
            if not isinstance(item, dict):
                continue
            result.append(
                {
                    "version_id": str(item.get("version_id", "")),
                    "reason": str(item.get("reason", "")),
                    "created_at": int(item.get("created_at", 0) or 0),
                }
            )
        return result

    async def restore_memory_version(self, user_id: str, version_id: str) -> dict[str, Any]:
        raw = await self._kv.get_text(f"memory_versions/{user_id}.json")
        items = _safe_json_load(raw, [])
        if not isinstance(items, list):
            raise ValueError("没有历史存档")
        target = None
        for item in items:
            if isinstance(item, dict) and str(item.get("version_id", "")) == version_id:
                target = item
                break
        if not isinstance(target, dict):
            raise ValueError("指定存档不存在")
        raw_snapshot = target.get("snapshot")
        snap: dict[str, Any] = raw_snapshot if isinstance(raw_snapshot, dict) else {}
        profile = str(snap.get("profile", ""))
        raw_habits = snap.get("habits")
        habits: dict[str, Any] = raw_habits if isinstance(raw_habits, dict) else {}
        raw_skills = snap.get("skills")
        skills: list[dict[str, Any]] = raw_skills if isinstance(raw_skills, list) else []
        raw_pricing = snap.get("pricing")
        pricing: dict[str, Any] = raw_pricing if isinstance(raw_pricing, dict) else {}
        await self.save_memory_hub(
            user_id=user_id,
            profile=profile,
            habits=habits,
            skills=skills,
            pricing=pricing,
            reason=f"restore:{version_id}",
        )
        return {
            "profile": profile,
            "habits": habits,
            "skills": normalize_skills(skills),
            "pricing": pricing,
        }

    def validate_email(self, email: str) -> str:
        normalized = email.strip().lower()
        if not EMAIL_RE.match(normalized):
            raise ValueError("邮箱格式不正确")
        return normalized

    def user_id_from_email(self, email: str) -> str:
        return hashlib.sha256(email.encode("utf-8")).hexdigest()[:24]

    async def user_exists(self, email: str) -> bool:
        normalized = self.validate_email(email)
        user_id = self.user_id_from_email(normalized)
        raw = await self._kv.get_text(f"users/{user_id}.json")
        return bool(raw)

    async def request_register_code(self, email: str, ttl_s: int = 300) -> str:
        return await self._request_auth_code(email, purpose="register", ttl_s=ttl_s)

    async def request_login_code(self, email: str, ttl_s: int = 300) -> str:
        return await self._request_auth_code(email, purpose="login", ttl_s=ttl_s)

    async def _request_auth_code(self, email: str, purpose: str, ttl_s: int = 300) -> str:
        normalized = self.validate_email(email)
        user_id = self.user_id_from_email(normalized)
        exists = await self.user_exists(normalized)
        if purpose == "register" and exists:
            raise ValueError("账号已存在，请直接登录")
        if purpose == "login" and not exists:
            raise ValueError("账号不存在，请先注册")

        code = f"{random.randint(0, 999999):06d}"
        payload = {
            "email": normalized,
            "code": code,
            "expires_at": _now_ts() + ttl_s,
            "purpose": purpose,
        }
        await self._kv.put_text(
            f"auth/codes/{purpose}/{user_id}.json",
            json.dumps(payload, ensure_ascii=False),
            content_type="application/json",
        )
        return code

    async def verify_register_code(self, email: str, code: str) -> str:
        return await self._verify_auth_code(email, code, purpose="register")

    async def verify_login_code(self, email: str, code: str) -> str:
        return await self._verify_auth_code(email, code, purpose="login")

    async def _verify_auth_code(self, email: str, code: str, purpose: str) -> str:
        normalized = self.validate_email(email)
        user_id = self.user_id_from_email(normalized)
        exists = await self.user_exists(normalized)
        if purpose == "register" and exists:
            raise ValueError("账号已存在，请直接登录")
        if purpose == "login" and not exists:
            raise ValueError("账号不存在，请先注册")

        raw = await self._kv.get_text(f"auth/codes/{purpose}/{user_id}.json")
        data = _safe_json_load(raw, {})
        if not data:
            raise ValueError("验证码不存在或已失效")
        if data.get("expires_at", 0) < _now_ts():
            raise ValueError("验证码已过期")
        if str(data.get("purpose", "")).strip() != purpose:
            raise ValueError("验证码类型不匹配，请重新获取")
        if str(data.get("code", "")).strip() != code.strip():
            raise ValueError("验证码错误")

        if purpose == "register":
            await self._ensure_user(user_id, normalized)

        token = secrets.token_urlsafe(32)
        session = {
            "user_id": user_id,
            "email": normalized,
            "created_at": _now_ts(),
            "expires_at": _now_ts() + 86400 * 14,
        }
        await self._kv.put_text(
            f"auth/sessions/{token}.json",
            json.dumps(session, ensure_ascii=False),
            content_type="application/json",
        )
        await self._kv.delete(f"auth/codes/{purpose}/{user_id}.json")
        return token

    async def resolve_token(self, token: str) -> UserIdentity:
        raw = await self._kv.get_text(f"auth/sessions/{token}.json")
        data = _safe_json_load(raw, {})
        if not data:
            raise ValueError("登录态无效，请重新登录")
        if int(data.get("expires_at", 0)) < _now_ts():
            raise ValueError("登录态已过期，请重新登录")
        return UserIdentity(user_id=str(data["user_id"]), email=str(data["email"]))

    async def get_history(
        self,
        user_id: str,
        session_id: str,
        max_turns: int = 12,
        include_meta: bool = False,
    ) -> list[dict[str, Any]]:
        raw = await self._kv.get_text(f"history/{user_id}/{session_id}.json")
        items = _safe_json_load(raw, [])
        if not isinstance(items, list):
            return []

        filtered: list[dict[str, Any]] = []
        for item in items[-max_turns * 2 :]:
            role = str(item.get("role", "")).strip()
            content = str(item.get("content", "")).strip()
            if role in {"user", "assistant", "tool"} and content:
                payload: dict[str, Any] = {"role": role, "content": content}
                if include_meta and isinstance(item.get("meta"), dict):
                    payload["meta"] = item["meta"]
                filtered.append(payload)
        return filtered

    async def append_history_message(
        self,
        user_id: str,
        session_id: str,
        role: str,
        content: str,
        meta: dict[str, Any] | None = None,
    ) -> int:
        key = f"history/{user_id}/{session_id}.json"
        raw = await self._kv.get_text(key)
        items = _safe_json_load(raw, [])
        if not isinstance(items, list):
            items = []
        message: dict[str, Any] = {"role": role, "content": content, "ts": _now_ts()}
        if isinstance(meta, dict) and meta:
            message["meta"] = meta
        items.append(message)
        await self._kv.put_text(key, json.dumps(items, ensure_ascii=False), content_type="application/json")

        await self._upsert_session_index(user_id, session_id, role, content)

        meta = await self.get_user_meta(user_id)
        meta["turn_count"] = int(meta.get("turn_count", 0)) + (1 if role == "assistant" else 0)
        await self._kv.put_text(f"users/{user_id}.json", json.dumps(meta, ensure_ascii=False), content_type="application/json")
        return int(meta["turn_count"])

    async def list_history_sessions(self, user_id: str) -> list[dict[str, Any]]:
        raw = await self._kv.get_text(f"history/{user_id}/_sessions.json")
        items = _safe_json_load(raw, [])
        if not isinstance(items, list):
            return []

        normalized: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            session_id = str(item.get("session_id", "")).strip()
            if not session_id:
                continue
            normalized.append(
                {
                    "session_id": session_id,
                    "title": str(item.get("title", "新对话")),
                    "updated_at": int(item.get("updated_at", 0)),
                    "last_role": str(item.get("last_role", "")),
                }
            )
        normalized.sort(key=lambda x: x.get("updated_at", 0), reverse=True)
        return normalized

    async def _upsert_session_index(self, user_id: str, session_id: str, role: str, content: str) -> None:
        key = f"history/{user_id}/_sessions.json"
        raw = await self._kv.get_text(key)
        items = _safe_json_load(raw, [])
        if not isinstance(items, list):
            items = []

        now_ts = _now_ts()
        title = _session_title_from_text(content)
        found = False
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("session_id", "")) != session_id:
                continue
            item["updated_at"] = now_ts
            item["last_role"] = role
            if role == "user" and (not str(item.get("title", "")).strip() or str(item.get("title")) == "新对话"):
                item["title"] = title
            found = True
            break

        if not found:
            items.append(
                {
                    "session_id": session_id,
                    "title": title if role == "user" else "新对话",
                    "updated_at": now_ts,
                    "last_role": role,
                }
            )

        await self._kv.put_text(key, json.dumps(items, ensure_ascii=False), content_type="application/json")

    async def get_profile_text(self, user_id: str) -> str:
        return (await self._kv.get_text(f"profiles/{user_id}.md")) or ""

    async def maybe_update_profile(
        self,
        user_id: str,
        user_message: str,
        recent_history: list[dict[str, Any]],
    ) -> None:
        meta = await self.get_user_meta(user_id)
        turn_count = int(meta.get("turn_count", 0))

        explicit = any(k in user_message for k in ["记录画像", "更新画像", "记住我", "写入记忆", "保存偏好"])
        periodic = turn_count > 0 and turn_count % 10 == 0
        if not (explicit or periodic):
            return

        note_source = []
        for item in recent_history[-6:]:
            role = item.get("role", "")
            content = str(item.get("content", "")).strip()
            if role and content:
                note_source.append(f"{role}: {content}")
        note = "\n".join(note_source)
        note = _clip_words(note, 200)
        if not note:
            return

        profile = await self.get_profile_text(user_id)
        profile_words = _word_count(profile)
        if profile_words >= 5000:
            return

        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        appended = f"\n- [{ts}] {note}"
        new_profile = (profile + appended).strip()
        new_profile = _clip_words(new_profile, 5000)
        await self._kv.put_text(f"profiles/{user_id}.md", new_profile)

    async def get_user_meta(self, user_id: str) -> dict[str, Any]:
        raw = await self._kv.get_text(f"users/{user_id}.json")
        data = _safe_json_load(raw, {})
        if isinstance(data, dict):
            return data
        return {}

    async def get_mcp_config(self, user_id: str) -> list[dict[str, Any]]:
        raw = await self._kv.get_text(f"mcp/{user_id}.json")
        data = _safe_json_load(raw, [])
        return data if isinstance(data, list) else []

    async def save_mcp_config(self, user_id: str, servers: list[dict[str, Any]]) -> None:
        sanitized: list[dict[str, Any]] = []
        for item in servers:
            name = str(item.get("name", "")).strip()
            command = str(item.get("command", "")).strip()
            args = item.get("args", [])
            env = item.get("env", {})
            if not name or not command or not isinstance(args, list) or not isinstance(env, dict):
                continue
            sanitized.append(
                {
                    "name": name,
                    "command": command,
                    "args": [str(x) for x in args],
                    "env": {str(k): str(v) for k, v in env.items()},
                }
            )
        await self._kv.put_text(
            f"mcp/{user_id}.json",
            json.dumps(sanitized, ensure_ascii=False),
            content_type="application/json",
        )

    async def _ensure_user(self, user_id: str, email: str) -> None:
        key = f"users/{user_id}.json"
        raw = await self._kv.get_text(key)
        if raw:
            return
        payload = {
            "user_id": user_id,
            "email": email,
            "created_at": _now_ts(),
            "turn_count": 0,
        }
        await self._kv.put_text(key, json.dumps(payload, ensure_ascii=False), content_type="application/json")
