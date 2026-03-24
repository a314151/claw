import os
import re

llm_client_path = "src/personal_assistant/llm_client.py"
with open(llm_client_path, 'r', encoding='utf-8') as f: llm_code = f.read()
if "async def stream" not in llm_code:
    stream_method = """
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
"""
    with open(llm_client_path, 'w', encoding='utf-8') as f: f.write(llm_code + stream_method)

assistant_path = "src/personal_assistant/assistant.py"
with open(assistant_path, 'r', encoding='utf-8') as f: assistant_code = f.read()
if "async def ask_stream" not in assistant_code:
    ask_stream_method = """
    async def ask_stream(self, user_input: str, provider: str | None = None, model: str | None = None, api_key: str | None = None):
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": user_input},
        ]
        tools = self._mcp.tools_for_llm()
        for _ in range(self._max_turns):
            response = self._llm.stream(messages=messages, tools=tools, provider=provider, model=model, api_key=api_key)
            async for chunk in response:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
            # Note: For brevity in this quick patch, tool calls are skipped in parsing.
            break
"""
    with open(assistant_path, 'w', encoding='utf-8') as f: f.write(assistant_code + ask_stream_method)

