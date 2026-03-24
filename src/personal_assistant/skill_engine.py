from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class Skill:
    skill_id: str
    title: str
    summary: str
    keywords: list[str]
    instruction: str
    source: str
    enabled: bool = True


# 来源参考（经典实践）：
# - openai/openai-cookbook: function calling loop & schema-first
# - modelcontextprotocol/servers: tool annotations/read-only hints
# - microsoft/playwright-mcp: tool capability boundaries and browser constraints
DEFAULT_SKILLS: list[Skill] = [
    Skill(
        skill_id="tool_schema_first",
        title="工具参数先验校验",
        summary="先检查工具参数是否满足 schema，再执行，减少 invalid_type 和重试浪费。",
        keywords=["参数", "schema", "工具", "invalid", "json", "调用失败"],
        instruction=(
            "调用工具前先做参数最小化与字段校验。"
            "如果参数明显缺失，优先向用户追问一个关键参数。"
            "当工具返回校验错误时，最多再尝试一次修正，不要无限重试。"
        ),
        source="https://github.com/openai/openai-cookbook",
    ),
    Skill(
        skill_id="mcp_readonly_first",
        title="读优先写后置",
        summary="先使用只读工具获取上下文，再决定是否执行写操作，降低误操作风险。",
        keywords=["读取", "分析", "文件", "只读", "mcp", "安全"],
        instruction=(
            "遇到文件或系统任务时，先读后写。"
            "优先使用 read/list 类工具确认目标存在与上下文。"
            "只有在证据充分且用户意图明确时再执行写操作。"
        ),
        source="https://github.com/modelcontextprotocol/servers",
    ),
    Skill(
        skill_id="browser_context_guard",
        title="浏览器上下文边界",
        summary="避免把 Node/后端代码当成页面脚本执行，区分浏览器页面与服务端环境。",
        keywords=["playwright", "浏览器", "document", "blob", "window", "页面"],
        instruction=(
            "若使用浏览器工具，只在页面上下文执行 DOM 相关逻辑。"
            "不要在非页面上下文调用 document/window/Blob。"
            "可由专用导出工具处理 CSV/PDF，不强依赖 browser_run_code。"
        ),
        source="https://github.com/microsoft/playwright-mcp",
    ),
    Skill(
        skill_id="cost_aware_routing",
        title="成本敏感工具路由",
        summary="先尝试低成本工具和更直接路径，减少无效探索与 token 消耗。",
        keywords=["成本", "token", "路由", "优化", "新闻", "抓取"],
        instruction=(
            "优先选择可直接返回结果的低成本工具。"
            "对于新闻/网页问题，先 fetch，再考虑浏览器自动化。"
            "同类失败连续出现时，及时降级或中止并解释原因。"
        ),
        source="https://github.com/openai/openai-cookbook",
    ),
]


def skill_to_dict(skill: Skill) -> dict[str, Any]:
    return {
        "id": skill.skill_id,
        "title": skill.title,
        "summary": skill.summary,
        "keywords": list(skill.keywords),
        "instruction": skill.instruction,
        "source": skill.source,
        "enabled": bool(skill.enabled),
    }


def load_default_skills() -> list[dict[str, Any]]:
    return [skill_to_dict(x) for x in DEFAULT_SKILLS]


def normalize_skills(skills: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    raw = skills or []
    normalized: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        skill_id = str(item.get("id", "")).strip()
        title = str(item.get("title", "")).strip()
        if not skill_id or not title:
            continue
        keywords_raw = item.get("keywords", [])
        keywords = [str(x).strip().lower() for x in keywords_raw if str(x).strip()] if isinstance(keywords_raw, list) else []
        normalized.append(
            {
                "id": skill_id,
                "title": title,
                "summary": str(item.get("summary", "")).strip(),
                "keywords": keywords,
                "instruction": str(item.get("instruction", "")).strip(),
                "source": str(item.get("source", "")).strip(),
                "enabled": bool(item.get("enabled", True)),
            }
        )
    return normalized


def match_skills(user_input: str, skills: list[dict[str, Any]], limit: int = 2) -> list[dict[str, Any]]:
    text = (user_input or "").strip().lower()
    if not text:
        return []

    scored: list[tuple[int, dict[str, Any]]] = []
    for skill in normalize_skills(skills):
        if not skill.get("enabled", True):
            continue
        hit = 0
        for kw in skill.get("keywords", []):
            if isinstance(kw, str) and kw and kw in text:
                hit += 1
        if hit > 0:
            scored.append((hit, skill))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [x[1] for x in scored[: max(1, limit)]]


def build_skill_guidance(user_input: str, skills: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    matched = match_skills(user_input, skills)
    if not matched:
        return [], ""

    lines: list[str] = ["本轮已匹配的执行技能（先遵循以下策略再决定是否调用工具）："]
    for item in matched:
        lines.append(f"- {item.get('title', '')}: {item.get('instruction', '')}")

    return matched, "\n".join(lines).strip()
