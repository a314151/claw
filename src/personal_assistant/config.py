from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


_SERVER_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


@dataclass(slots=True)
class LLMConfig:
    provider: str
    api_key: str
    base_url: str
    model: str
    temperature: float = 0.2
    max_tokens: int = 1024
    timeout_s: float = 60.0


class ProviderName(StrEnum):
    OPENAI = "openai"
    DEEPSEEK = "deepseek"
    ZHIPU = "zhipu"
    QWEN = "qwen"
    GEMINI = "gemini"


@dataclass(slots=True)
class ProviderPreset:
    base_url: str
    default_model: str
    models: list[str]


PROVIDER_PRESETS: dict[ProviderName, ProviderPreset] = {
    ProviderName.OPENAI: ProviderPreset(
        base_url="https://api.openai.com/v1",
        default_model="gpt-5-mini",
        models=["gpt-5", "gpt-5-mini", "gpt-4.1", "gpt-4o"],
    ),
    ProviderName.DEEPSEEK: ProviderPreset(
        base_url="https://api.deepseek.com/v1",
        default_model="deepseek-v3",
        models=["deepseek-v3", "deepseek-r1", "deepseek-chat", "deepseek-reasoner"],
    ),
    ProviderName.ZHIPU: ProviderPreset(
        base_url="https://open.bigmodel.cn/api/paas/v4",
        default_model="glm-4.7",
        models=["glm-4.7", "glm-4-plus", "glm-4-air", "glm-4-flash"],
    ),
    ProviderName.QWEN: ProviderPreset(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_model="qwen3-max",
        models=["qwen3-max", "qwen3-plus", "qwen-max", "qwen-plus"],
    ),
    ProviderName.GEMINI: ProviderPreset(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        default_model="gemini-3-pro",
        models=["gemini-3-pro", "gemini-3-flash", "gemini-2.5-pro", "gemini-2.5-flash"],
    ),
}


@dataclass(slots=True)
class MCPServerConfig:
    name: str
    command: str
    args: list[str]
    env: dict[str, str]
    startup_timeout_s: float = 15.0


@dataclass(slots=True)
class AppConfig:
    default_provider: str
    llm_providers: dict[str, LLMConfig]
    mcp_servers: list[MCPServerConfig]
    max_turns: int = 8


def _get_float(name: str, default: float, low: float, high: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"环境变量 {name} 不是合法浮点数: {raw}") from exc
    if not (low <= value <= high):
        raise ValueError(f"环境变量 {name} 超出范围 [{low}, {high}]: {value}")
    return value


def _get_int(name: str, default: int, low: int, high: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"环境变量 {name} 不是合法整数: {raw}") from exc
    if not (low <= value <= high):
        raise ValueError(f"环境变量 {name} 超出范围 [{low}, {high}]: {value}")
    return value


def _load_mcp_servers(config_file: Path) -> list[MCPServerConfig]:
    if not config_file.exists():
        return []

    data: Any = json.loads(config_file.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("MCP 配置文件必须是 JSON 数组")

    servers: list[MCPServerConfig] = []
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"MCP 配置第 {idx} 项必须是对象")

        name = str(item.get("name", "")).strip()
        if not name or not _SERVER_NAME_RE.match(name):
            raise ValueError(f"MCP 服务名不合法: {name!r}，仅允许字母数字下划线和中划线")

        command = str(item.get("command", "")).strip()
        if not command:
            raise ValueError(f"MCP 服务 {name} 缺少 command")

        args_raw = item.get("args", [])
        if not isinstance(args_raw, list):
            raise ValueError(f"MCP 服务 {name} 的 args 必须是数组")
        args = [str(x) for x in args_raw]

        env_raw = item.get("env", {})
        if not isinstance(env_raw, dict):
            raise ValueError(f"MCP 服务 {name} 的 env 必须是对象")
        env = {str(k): str(v) for k, v in env_raw.items()}

        startup_timeout_s = float(item.get("startup_timeout_s", 15.0))
        if startup_timeout_s <= 0:
            raise ValueError(f"MCP 服务 {name} 的 startup_timeout_s 必须大于 0")

        servers.append(
            MCPServerConfig(
                name=name,
                command=command,
                args=args,
                env=env,
                startup_timeout_s=startup_timeout_s,
            )
        )

    return servers


def _env_name_for_provider(provider: ProviderName, field: str) -> str:
    return f"{provider.value.upper()}_{field}"


def _get_provider_config(provider: ProviderName) -> LLMConfig:
    preset = PROVIDER_PRESETS[provider]
    api_key = os.getenv(_env_name_for_provider(provider, "API_KEY"), "").strip()
    base_url = os.getenv(_env_name_for_provider(provider, "BASE_URL"), preset.base_url).strip()
    model = os.getenv(_env_name_for_provider(provider, "MODEL"), preset.default_model).strip()
    if not model:
        raise ValueError(f"{provider.value} 的模型名不能为空")

    return LLMConfig(
        provider=provider.value,
        api_key=api_key,
        base_url=base_url,
        model=model,
        temperature=_get_float("LLM_TEMPERATURE", 0.2, 0.0, 2.0),
        max_tokens=_get_int("LLM_MAX_TOKENS", 1024, 64, 8192),
        timeout_s=_get_float("LLM_TIMEOUT_S", 60.0, 1.0, 300.0),
    )


def list_provider_options(configured: dict[str, LLMConfig]) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for provider in ProviderName:
        key = provider.value
        preset = PROVIDER_PRESETS[provider]
        cfg = configured[key]
        options.append(
            {
                "provider": key,
                "base_url": cfg.base_url or preset.base_url,
                "default_model": cfg.model or preset.default_model,
                "models": list(preset.models),
                "has_api_key": bool(cfg.api_key),
            }
        )
    return options


def load_config(config_path: str | None = None) -> AppConfig:
    load_dotenv(override=False)

    providers: dict[str, LLMConfig] = {}
    for provider in ProviderName:
        cfg = _get_provider_config(provider)
        providers[provider.value] = cfg

    # Backward compatibility: allow old single-provider variables.
    legacy_api_key = os.getenv("LLM_API_KEY", "").strip()
    if legacy_api_key:
        legacy_base_url = os.getenv("LLM_BASE_URL", PROVIDER_PRESETS[ProviderName.OPENAI].base_url).strip()
        legacy_model = os.getenv("LLM_MODEL", PROVIDER_PRESETS[ProviderName.OPENAI].default_model).strip()
        providers[ProviderName.OPENAI.value] = LLMConfig(
            provider=ProviderName.OPENAI.value,
            api_key=legacy_api_key,
            base_url=legacy_base_url,
            model=legacy_model,
            temperature=_get_float("LLM_TEMPERATURE", 0.2, 0.0, 2.0),
            max_tokens=_get_int("LLM_MAX_TOKENS", 1024, 64, 8192),
            timeout_s=_get_float("LLM_TIMEOUT_S", 60.0, 1.0, 300.0),
        )

    default_provider = os.getenv("LLM_PROVIDER", ProviderName.OPENAI.value).strip().lower()
    if default_provider not in providers:
        default_provider = ProviderName.DEEPSEEK.value

    mcp_path = Path(config_path or os.getenv("MCP_CONFIG", "./mcp_servers.json"))
    mcp_servers = _load_mcp_servers(mcp_path)

    max_turns = _get_int("ASSISTANT_MAX_TURNS", 8, 1, 30)
    return AppConfig(
        default_provider=default_provider,
        llm_providers=providers,
        mcp_servers=mcp_servers,
        max_turns=max_turns,
    )
