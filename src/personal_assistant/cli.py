from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .assistant import PersonalAssistant
from .config import load_config
from .llm_client import LLMClient
from .mcp_client import MCPManager


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Personal LLM+MCP Assistant")
    parser.add_argument("--config", default=None, help="MCP 配置文件路径（JSON）")
    parser.add_argument("--once", default=None, help="单次提问模式")
    parser.add_argument("--provider", default=None, help="模型提供商，如 openai/deepseek/zhipu/qwen/gemini")
    parser.add_argument("--model", default=None, help="覆盖默认模型名")
    parser.add_argument("--debug", action="store_true", help="开启调试日志")
    return parser


async def _run(args: argparse.Namespace) -> int:
    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    try:
        config = load_config(args.config)
    except Exception as exc:
        logging.error("配置加载失败: %s", exc)
        return 2

    llm = LLMClient(config.llm_providers, config.default_provider)

    async with MCPManager(config.mcp_servers) as mcp:
        assistant = PersonalAssistant(llm=llm, mcp=mcp, max_turns=config.max_turns)

        if args.once:
            reply = await assistant.ask(args.once, provider=args.provider, model=args.model)
            print(reply)
            return 0

        print("个人助手已启动，输入 exit 退出。")
        print("可用 provider:", ", ".join([item["provider"] for item in llm.available_providers()]))
        while True:
            try:
                text = input("你> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n再见")
                return 0

            if not text:
                continue
            if text.lower() in {"exit", "quit", "q"}:
                print("再见")
                return 0

            try:
                reply = await assistant.ask(text, provider=args.provider, model=args.model)
            except Exception as exc:
                logging.exception("处理请求失败: %s", exc)
                print("助手> 出错了，请查看日志后重试。")
                continue

            print(f"助手> {reply}")


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    code = asyncio.run(_run(args))
    sys.exit(code)


if __name__ == "__main__":
    main()
