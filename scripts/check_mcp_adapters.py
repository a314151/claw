from __future__ import annotations

import asyncio
import json
from pathlib import Path

from personal_assistant.config import MCPServerConfig
from personal_assistant.mcp_client import MCPManager


async def check_one(cfg: MCPServerConfig) -> dict[str, object]:
    try:
        async with MCPManager([cfg]) as mcp:
            tools = mcp.tools_for_llm()
            return {
                "name": cfg.name,
                "ok": True,
                "tool_count": len(tools),
                "sample_tools": [t.get("function", {}).get("name", "") for t in tools[:5]],
            }
    except Exception as exc:
        return {
            "name": cfg.name,
            "ok": False,
            "error": str(exc),
        }


async def check_one_with_timeout(cfg: MCPServerConfig, timeout_s: float = 20.0) -> dict[str, object]:
    try:
        return await asyncio.wait_for(check_one(cfg), timeout=timeout_s)
    except asyncio.TimeoutError:
        return {
            "name": cfg.name,
            "ok": False,
            "error": f"timeout>{timeout_s}s",
        }


async def main() -> None:
    root = Path(__file__).resolve().parents[1]
    uv_exe = str(root / ".venv" / "Scripts" / "uv.exe")

    checks = [
        MCPServerConfig(
            name="filesystem",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", str(root)],
            env={},
            startup_timeout_s=20.0,
        ),
        MCPServerConfig(
            name="fetch",
            command=uv_exe,
            args=["tool", "run", "mcp-server-fetch"],
            env={},
            startup_timeout_s=20.0,
        ),
        MCPServerConfig(
            name="doc_export",
            command=uv_exe,
            args=["run", "python", "-m", "personal_assistant.mcp_document_server"],
            env={},
            startup_timeout_s=20.0,
        ),
        MCPServerConfig(
            name="playwright",
            command="npx",
            args=["-y", "@playwright/mcp", "--headless"],
            env={"PLAYWRIGHT_MCP_HEADLESS": "1"},
            startup_timeout_s=25.0,
        ),
    ]

    results = []
    for cfg in checks:
        results.append(await check_one_with_timeout(cfg))

    ok_count = sum(1 for x in results if x.get("ok"))
    out = {
        "ok": ok_count == len(results),
        "total": len(results),
        "ok_count": ok_count,
        "results": results,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))

    required = {"filesystem", "fetch", "doc_export"}
    required_ok = all((x.get("name") in required and x.get("ok")) or (x.get("name") not in required) for x in results)
    if not required_ok:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
