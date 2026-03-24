from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI

from .config import LLMConfig


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    usage: dict[str, int] | None = None


class LLMClient:
    def __init__(self, providers: dict[str, LLMConfig], default_provider: str) -> None:
        if not providers:
            raise ValueError("providers 不能为空")
        if default_provider not in providers:
            raise ValueError(f"默认 provider 不存在: {default_provider}")

        self._providers = providers
        self._default_provider = default_provider
        self._clients: dict[str, AsyncOpenAI] = {}

    def available_providers(self) -> list[dict[str, str]]:
        return [
            {
                "provider": name,
                "model": cfg.model,
                "base_url": cfg.base_url,
            }
            for name, cfg in self._providers.items()
        ]

    def _resolve(self, provider: str | None, model: str | None) -> tuple[str, LLMConfig, str]:
        provider_name = (provider or self._default_provider).strip().lower()
        cfg = self._providers.get(provider_name)
        if cfg is None:
            available = ", ".join(self._providers.keys())
            raise ValueError(f"不支持的 provider: {provider_name}，可用: {available}")

        actual_model = (model or cfg.model).strip()
        if not actual_model:
            raise ValueError(f"provider {provider_name} 的模型为空")

        return provider_name, cfg, actual_model

    def _client_for(self, provider_name: str, cfg: LLMConfig, api_key: str) -> AsyncOpenAI:
        cache_key = f"{provider_name}:{hash(api_key)}"
        if cache_key not in self._clients:
            self._clients[cache_key] = AsyncOpenAI(
                api_key=api_key,
                base_url=cfg.base_url,
            )
        return self._clients[cache_key]

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        provider: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
    ) -> LLMResponse:
        provider_name, cfg, actual_model = self._resolve(provider, model)
        resolved_key = (api_key or cfg.api_key).strip()
        if not resolved_key:
            raise ValueError(
                f"provider {provider_name} 未配置 API Key。请在网页中填写，或在 .env 设置 {provider_name.upper()}_API_KEY"
            )
        client = self._client_for(provider_name, cfg, resolved_key)

        payload: dict[str, Any] = {
            "model": actual_model,
            "messages": messages,
            "temperature": cfg.temperature,
            "max_tokens": cfg.max_tokens,
            "timeout": cfg.timeout_s,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        result = await client.chat.completions.create(**payload)
        if not result.choices:
            return LLMResponse(text="", tool_calls=[], usage=None)

        message = result.choices[0].message
        text = message.content or ""

        tool_calls: list[ToolCall] = []
        if message.tool_calls:
            for index, call in enumerate(message.tool_calls):
                name = call.function.name
                raw_args = call.function.arguments or "{}"
                args: dict[str, Any]
                try:
                    loaded = json.loads(raw_args)
                    args = loaded if isinstance(loaded, dict) else {"value": loaded}
                except json.JSONDecodeError:
                    args = {"raw": raw_args}
                tool_calls.append(
                    ToolCall(
                        id=call.id or f"call_{index}",
                        name=name,
                        arguments=args,
                    )
                )

        usage: dict[str, int] | None = None
        raw_usage = getattr(result, "usage", None)
        if raw_usage is not None:
            prompt_tokens = int(getattr(raw_usage, "prompt_tokens", 0) or 0)
            completion_tokens = int(getattr(raw_usage, "completion_tokens", 0) or 0)
            total_tokens = int(getattr(raw_usage, "total_tokens", prompt_tokens + completion_tokens) or 0)
            usage = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
            }

        return LLMResponse(text=text, tool_calls=tool_calls, usage=usage)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        provider: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
    ):
        provider_name, cfg, actual_model = self._resolve(provider, model)
        resolved_key = (api_key or cfg.api_key).strip()
        if not resolved_key:
            raise ValueError(
                f"provider {provider_name} 未配置 API Key。请在网页中填写，或在 .env 设置 {provider_name.upper()}_API_KEY"
            )
        client = self._client_for(provider_name, cfg, resolved_key)

        payload: dict[str, Any] = {
            "model": actual_model,
            "messages": messages,
            "temperature": cfg.temperature,
            "max_tokens": cfg.max_tokens,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        
        response = await client.chat.completions.create(**payload)
        async for chunk in response:
            yield chunk
