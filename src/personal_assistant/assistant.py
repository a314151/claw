from __future__ import annotations

import json
import re
from typing import Any

from .llm_client import LLMClient
from .mcp_client import MCPManager
from .app_logger import logger


class PersonalAssistant:
    def __init__(self, llm: LLMClient, mcp: MCPManager, max_turns: int = 8) -> None:
        self._llm = llm
        self._mcp = mcp
        self._max_turns = max_turns

    @staticmethod
    def _is_news_query(text: str) -> bool:
        t = text.strip().lower()
        if not t:
            return False
        keywords = [
            "新闻",
            "热点",
            "头条",
            "快讯",
            "资讯",
            "最新动态",
            "news",
            "headline",
            "breaking",
        ]
        return any(k in t for k in keywords)

    @staticmethod
    def _allow_playwright(text: str) -> bool:
        t = (text or "").strip().lower()
        if not t:
            return False
        hints = [
            "使用playwright",
            "浏览器自动化",
            "需要页面交互",
            "allow_playwright",
            "use playwright",
            "browser automation",
        ]
        return any(k in t for k in hints)

    def _select_tools(self, all_tools: list[dict[str, Any]], user_input: str) -> list[dict[str, Any]]:
        filtered: list[dict[str, Any]] = []
        allow_playwright = self._allow_playwright(user_input)
        for tool in all_tools:
            fn = tool.get("function", {}) if isinstance(tool, dict) else {}
            name = str(fn.get("name", "")).strip().lower()
            # 默认不启用 Playwright（常导致外部弹窗和额外开销），除非用户显式要求。
            if (name.startswith("playwright_") or name.startswith("playwright.")) and not allow_playwright:
                continue
            # 新闻场景即使允许工具，也优先非浏览器工具。
            if self._is_news_query(user_input) and (name.startswith("playwright_") or name.startswith("playwright.")):
                continue
            filtered.append(tool)
        return filtered or all_tools

    @staticmethod
    def _unwrap_raw_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
        """尝试把模型生成的 raw 包装参数恢复成可调用的结构化参数。"""
        if not isinstance(arguments, dict):
            return {"value": arguments}

        if "raw" not in arguments or not isinstance(arguments.get("raw"), str):
            return arguments

        cur: Any = arguments.get("raw")
        for _ in range(4):
            if isinstance(cur, str):
                try:
                    cur = json.loads(cur)
                except json.JSONDecodeError:
                    break
                continue

            if isinstance(cur, dict):
                # 常见场景：{"raw":"{...}"} 连续包裹。
                if "raw" in cur and isinstance(cur.get("raw"), str):
                    cur = cur["raw"]
                    continue
                return cur
            break

        return arguments

    @staticmethod
    def _looks_like_tool_error(text: str) -> bool:
        t = (text or "").lower()
        flags = [
            "调用工具失败",
            "调用工具超时",
            "工具不存在",
            "mcp 服务不存在",
            "error",
            "invalid_type",
            "invalid input",
            "validation",
            "traceback",
            "exception",
        ]
        return any(k in t for k in flags)

    @staticmethod
    def _merge_usage(base: dict[str, int], usage: dict[str, int] | None) -> None:
        if not usage:
            return
        base["prompt_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
        base["completion_tokens"] += int(usage.get("completion_tokens", 0) or 0)
        base["total_tokens"] += int(usage.get("total_tokens", 0) or 0)

    @staticmethod
    def _build_system_content(user_profile: str | None, skill_guidance: str | None) -> str:
        system_content = (
            "你是一个严谨、简洁的个人助手。"
            "当存在可用工具时，优先调用工具再回答；"
            "回答中不要编造工具执行结果。"
        )
        if user_profile:
            system_content += f"\n\n用户画像（简要，供个性化参考）：\n{user_profile.strip()}"
        if skill_guidance:
            system_content += f"\n\n{skill_guidance.strip()}"
        return system_content

    async def ask(
        self,
        user_input: str,
        provider: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        history: list[dict[str, Any]] | None = None,
        user_profile: str | None = None,
        skill_guidance: str | None = None,
    ) -> str:
        system_content = self._build_system_content(user_profile, skill_guidance)

        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": system_content,
            }
        ]

        if history:
            messages.extend(history)

        messages.append({"role": "user", "content": user_input})

        tools = self._select_tools(self._mcp.tools_for_llm(), user_input)
        logger.info(f"Received user input: {user_input[:50]}...")
        last_text = ""
        last_failed_tool = ""
        same_tool_fail_count = 0

        for _ in range(self._max_turns):
            response = await self._llm.complete(
                messages=messages,
                tools=tools,
                provider=provider,
                model=model,
                api_key=api_key,
            )
            if response.text:
                last_text = response.text

            if not response.tool_calls:
                if response.text:
                    messages.append({"role": "assistant", "content": response.text})
                return response.text or "我没有得到可用结果，请重试。"

            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": response.text or "",
                "tool_calls": [],
            }

            for call in response.tool_calls:
                assistant_message["tool_calls"].append(
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                )
            messages.append(assistant_message)

            for call in response.tool_calls:
                repaired_args = self._unwrap_raw_arguments(call.arguments)
                logger.info(f"Calling tool: {call.name} with args: {repaired_args}")
                tool_result = await self._mcp.call_tool(call.name, repaired_args)
                logger.debug(f"Tool {call.name} returned result: {str(tool_result)[:100]}...")

                if self._looks_like_tool_error(tool_result):
                    if last_failed_tool == call.name:
                        same_tool_fail_count += 1
                    else:
                        last_failed_tool = call.name
                        same_tool_fail_count = 1

                    if same_tool_fail_count >= 2:
                        return (
                            f"工具 {call.name} 连续两次调用失败，已自动中止本轮工具重试。"
                            "这通常是参数格式不匹配导致，请提供更明确的参数或更换工具。"
                        )
                else:
                    last_failed_tool = ""
                    same_tool_fail_count = 0

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": tool_result,
                    }
                )

        return last_text or "达到最大工具调用轮次，请换个问法再试。"

    async def ask_stream(
        self,
        user_input: str,
        provider: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        history: list[dict[str, Any]] | None = None,
        user_profile: str | None = None,
    ):
        async for evt in self.ask_stream_events(
            user_input=user_input,
            provider=provider,
            model=model,
            api_key=api_key,
            history=history,
            user_profile=user_profile,
        ):
            if evt.get("type") == "delta":
                yield str(evt.get("content", ""))

    async def ask_stream_events(
        self,
        user_input: str,
        provider: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        history: list[dict[str, Any]] | None = None,
        user_profile: str | None = None,
        skill_guidance: str | None = None,
    ):
        system_content = self._build_system_content(user_profile, skill_guidance)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_content}
        ]

        if history:
            messages.extend(history)

        messages.append({"role": "user", "content": user_input})

        tools = self._select_tools(self._mcp.tools_for_llm(), user_input)
        last_text = ""
        last_failed_tool = ""
        same_tool_fail_count = 0
        usage_totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        for _ in range(self._max_turns):
            response = await self._llm.complete(
                messages=messages,
                tools=tools,
                provider=provider,
                model=model,
                api_key=api_key,
            )
            self._merge_usage(usage_totals, response.usage)
            if response.text:
                last_text = response.text

            if not response.tool_calls:
                final_text = response.text or "我没有得到可用结果，请重试。"
                # 保留 SSE 的逐段体验，同时确保工具调用链已执行完成。
                step = 24
                for i in range(0, len(final_text), step):
                    yield {"type": "delta", "content": final_text[i : i + step]}
                yield {"type": "metrics", "usage": usage_totals}
                return

            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": response.text or "",
                "tool_calls": [],
            }
            for call in response.tool_calls:
                assistant_message["tool_calls"].append(
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                )
            messages.append(assistant_message)

            for call in response.tool_calls:
                repaired_args = self._unwrap_raw_arguments(call.arguments)
                yield {
                    "type": "tool_start",
                    "name": call.name,
                    "arguments": repaired_args,
                }
                tool_result = await self._mcp.call_tool(call.name, repaired_args)
                preview = str(tool_result).strip()
                if len(preview) > 240:
                    preview = preview[:240] + "..."
                yield {
                    "type": "tool_result",
                    "name": call.name,
                    "preview": preview,
                }

                if self._looks_like_tool_error(tool_result):
                    if last_failed_tool == call.name:
                        same_tool_fail_count += 1
                    else:
                        last_failed_tool = call.name
                        same_tool_fail_count = 1

                    if same_tool_fail_count >= 2:
                        hint = (
                            f"工具 {call.name} 连续两次调用失败，已自动中止本轮工具重试。"
                            "这通常是参数格式不匹配导致，请提供更明确的参数或更换工具。"
                        )
                        yield {"type": "tool_error", "name": call.name, "message": hint}
                        yield {"type": "delta", "content": hint}
                        yield {"type": "metrics", "usage": usage_totals}
                        return
                else:
                    last_failed_tool = ""
                    same_tool_fail_count = 0

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": tool_result,
                    }
                )

        fallback = last_text or "达到最大工具调用轮次，请换个问法再试。"
        step = 24
        for i in range(0, len(fallback), step):
            yield {"type": "delta", "content": fallback[i : i + step]}
        yield {"type": "metrics", "usage": usage_totals}

    async def ask_with_metrics(
        self,
        user_input: str,
        provider: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        history: list[dict[str, Any]] | None = None,
        user_profile: str | None = None,
        skill_guidance: str | None = None,
    ) -> dict[str, Any]:
        full_text: list[str] = []
        tool_events: list[dict[str, Any]] = []
        usage_totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        async for evt in self.ask_stream_events(
            user_input=user_input,
            provider=provider,
            model=model,
            api_key=api_key,
            history=history,
            user_profile=user_profile,
            skill_guidance=skill_guidance,
        ):
            evt_type = str(evt.get("type", ""))
            if evt_type == "delta":
                full_text.append(str(evt.get("content", "")))
            elif evt_type in {"tool_start", "tool_result", "tool_error"}:
                tool_events.append(evt)
            elif evt_type == "metrics":
                raw_usage = evt.get("usage")
                usage = raw_usage if isinstance(raw_usage, dict) else None
                self._merge_usage(usage_totals, usage)

        return {
            "reply": "".join(full_text).strip(),
            "tool_events": tool_events,
            "usage": usage_totals,
        }
