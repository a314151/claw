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
    mcp_servers: list[str]
    tool_hints: list[str]
    enabled: bool = True


# 来源参考（GitHub 高星项目）：
# - openai/openai-cookbook
# - modelcontextprotocol/servers
# - microsoft/playwright-mcp
# - github/github-mcp-server
# - cloudflare/mcp-server-cloudflare
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
        mcp_servers=["filesystem", "git", "fetch", "github", "postgres", "cloudflare"],
        tool_hints=["严格按 input schema 组装参数", "单次失败最多修复重试 1 次"],
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
        mcp_servers=["filesystem", "git", "memory", "fetch"],
        tool_hints=["默认只读", "写入前先给出影响面"],
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
        mcp_servers=["playwright", "fetch", "doc_export"],
        tool_hints=["默认 headless", "优先 fetch，必要时再浏览器自动化"],
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
        mcp_servers=["fetch", "filesystem", "playwright"],
        tool_hints=["先轻后重", "尽量减少无效工具轮次"],
    ),
    Skill(
        skill_id="repo_forensics",
        title="仓库取证链",
        summary="代码问题先做仓库证据链：目录、关键词、变更点，再下结论。",
        keywords=["仓库", "代码", "排查", "bug", "回归", "取证"],
        instruction=(
            "先列目录、再查符号/关键词、最后定位调用链。"
            "避免先入为主直接改代码。"
            "输出结论时附关键证据位置。"
        ),
        source="https://github.com/modelcontextprotocol/servers/tree/main/src/git",
        mcp_servers=["git", "filesystem", "memory"],
        tool_hints=["先证据后改动", "优先可复现路径"],
    ),
    Skill(
        skill_id="github_issue_flow",
        title="GitHub 问题闭环",
        summary="将缺陷整理为可追踪项，包含复现、影响、修复、验证。",
        keywords=["issue", "github", "缺陷", "追踪", "任务"],
        instruction=(
            "当用户要求持续跟踪时，按 issue 模板整理：复现步骤、预期/实际、影响范围、回归检查。"
            "修复后记录验证结果与残余风险。"
        ),
        source="https://github.com/github/github-mcp-server",
        mcp_servers=["github", "git"],
        tool_hints=["结构化 issue", "保留审计轨迹"],
    ),
    Skill(
        skill_id="web_fetch_pipeline",
        title="网页信息抽取流水线",
        summary="网页任务优先抓取文本结构化，再决定是否浏览器渲染。",
        keywords=["网页", "抓取", "链接", "文章", "摘要", "新闻"],
        instruction=(
            "优先 fetch 获取可读文本。"
            "当页面依赖脚本渲染且 fetch 不完整时再使用 playwright。"
            "输出时标注来源链接与抓取时间。"
        ),
        source="https://github.com/modelcontextprotocol/servers/tree/main/src/fetch",
        mcp_servers=["fetch", "playwright"],
        tool_hints=["先 fetch 后 playwright", "返回引用来源"],
    ),
    Skill(
        skill_id="doc_export_pipeline",
        title="文档导出流水线",
        summary="将回答结构化后导出 CSV/PDF，并给出失败兜底。",
        keywords=["导出", "pdf", "csv", "报表", "下载"],
        instruction=(
            "导出前先清洗文本结构。"
            "优先 doc_export 或后端导出接口。"
            "接口缺失时启用本地导出兜底并提示用户。"
        ),
        source="https://github.com/modelcontextprotocol/python-sdk",
        mcp_servers=["doc_export", "filesystem"],
        tool_hints=["导出前结构化", "失败自动兜底"],
    ),
    Skill(
        skill_id="postgres_safety",
        title="数据库安全查询",
        summary="数据库任务默认只读、限行数、禁危险写入。",
        keywords=["sql", "postgres", "数据库", "查询", "表结构"],
        instruction=(
            "优先 schema/样例查询。"
            "查询加 limit，避免全表扫描。"
            "未明确授权时不执行写操作。"
        ),
        source="https://github.com/modelcontextprotocol/servers/tree/main/src/postgres",
        mcp_servers=["postgres"],
        tool_hints=["默认只读", "查询限流"],
    ),
    Skill(
        skill_id="cloudflare_ops_guard",
        title="Cloudflare 变更护栏",
        summary="云资源操作先读取现状与计划，再执行最小改动。",
        keywords=["cloudflare", "workers", "kv", "r2", "d1"],
        instruction=(
            "先读取当前配置与资源状态。"
            "输出变更计划、影响范围与回滚方案。"
            "执行后回读确认结果。"
        ),
        source="https://github.com/cloudflare/mcp-server-cloudflare",
        mcp_servers=["cloudflare"],
        tool_hints=["先读取后变更", "必须可回滚"],
    ),
    Skill(
        skill_id="memory_profile_hygiene",
        title="记忆树卫生策略",
        summary="用户画像/习惯/技能更新保持简洁、可追溯、可回滚。",
        keywords=["画像", "记忆", "习惯", "skills", "存档", "回滚"],
        instruction=(
            "写入资料前先摘要化并限制长度。"
            "每次更新保留版本快照。"
            "涉及偏好变更时输出前后差异。"
        ),
        source="https://github.com/modelcontextprotocol/servers/tree/main/src/memory",
        mcp_servers=["memory", "filesystem"],
        tool_hints=["小步更新", "保持可回滚"],
    ),
    Skill(
        skill_id="compat_probe_first",
        title="能力探测优先",
        summary="新旧版本并行时先探测接口能力，避免直接报错中断。",
        keywords=["404", "兼容", "版本", "接口", "openapi"],
        instruction=(
            "先探测服务能力（如 openapi/health）。"
            "缺失能力走兜底路径并给出提示。"
            "不要把可降级问题升级为致命错误。"
        ),
        source="https://github.com/openai/openai-cookbook",
        mcp_servers=["fetch", "filesystem"],
        tool_hints=["先探测后调用", "始终给兜底"],
    ),
    Skill(
        skill_id="retry_budget_control",
        title="重试预算控制",
        summary="工具失败重试应有预算，避免无限循环浪费 token。",
        keywords=["重试", "失败", "超时", "token", "预算"],
        instruction=(
            "为每类失败设置最大重试次数。"
            "连续失败后快速降级并解释原因。"
            "记录失败摘要用于后续复盘。"
        ),
        source="https://github.com/openai/openai-cookbook",
        mcp_servers=["filesystem", "fetch", "playwright", "github", "postgres"],
        tool_hints=["失败预算", "降级策略"],
    ),
    Skill(
        skill_id="security_secret_hygiene",
        title="密钥与敏感信息卫生",
        summary="禁止在代码/日志中写入真实密钥与敏感信息。",
        keywords=["密钥", "token", "secret", "密码", "泄露", "安全"],
        instruction=(
            "输出中默认打码敏感字段。"
            "配置写入仅使用环境变量引用。"
            "变更前后检查日志是否泄露。"
        ),
        source="https://github.com/modelcontextprotocol/servers",
        mcp_servers=["filesystem", "git"],
        tool_hints=["敏感信息打码", "禁止硬编码密钥"],
    ),
    Skill(
        skill_id="incident_timeline",
        title="故障时间线复盘",
        summary="把故障过程整理为时间线，便于定位回归根因。",
        keywords=["故障", "时间线", "复盘", "回归", "根因"],
        instruction=(
            "按时间顺序记录症状、操作、观测结果。"
            "区分根因与表象。"
            "补充可自动化的回归检查。"
        ),
        source="https://github.com/github/github-mcp-server",
        mcp_servers=["memory", "git", "filesystem"],
        tool_hints=["时间线记录", "根因优先"],
    ),
    Skill(
        skill_id="time_system_first",
        title="时间系统优先",
        summary="时间问题统一走时间系统：时钟源、时区、格式、历史一致性。",
        keywords=["时间", "时区", "timestamp", "星期", "几点"],
        instruction=(
            "优先使用服务器本地时钟与统一时区策略。"
            "历史记录统一存储 Unix 时间戳并在展示层格式化。"
            "当上游时间来源冲突时，明确声明时钟来源。"
        ),
        source="https://github.com/openai/openai-cookbook",
        mcp_servers=["local_clock", "memory"],
        tool_hints=["统一时钟源", "存储与展示分离"],
    ),
    Skill(
        skill_id="verification_checklist",
        title="按钮与路径巡检",
        summary="修复后必须做登录到关键按钮的路径巡检并输出结果表。",
        keywords=["巡检", "按钮", "登录", "验证", "表格"],
        instruction=(
            "至少覆盖登录、设置、历史、资料库、发送、导出。"
            "每项给出 pass/fail 与证据。"
            "失败项要附下一步动作。"
        ),
        source="https://github.com/microsoft/playwright-mcp",
        mcp_servers=["playwright", "fetch", "filesystem"],
        tool_hints=["端到端验证", "结果表输出"],
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
        "mcp_servers": list(skill.mcp_servers),
        "tool_hints": list(skill.tool_hints),
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
                "mcp_servers": [
                    str(x).strip().lower()
                    for x in (item.get("mcp_servers", []) if isinstance(item.get("mcp_servers"), list) else [])
                    if str(x).strip()
                ],
                "tool_hints": [
                    str(x).strip()
                    for x in (item.get("tool_hints", []) if isinstance(item.get("tool_hints"), list) else [])
                    if str(x).strip()
                ],
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
        mcp_servers = item.get("mcp_servers", []) if isinstance(item.get("mcp_servers"), list) else []
        tool_hints = item.get("tool_hints", []) if isinstance(item.get("tool_hints"), list) else []
        lines.append(f"- {item.get('title', '')}: {item.get('instruction', '')}")
        if mcp_servers:
            lines.append(f"  适配 MCP: {', '.join(str(x) for x in mcp_servers)}")
        if tool_hints:
            lines.append(f"  执行提示: {'；'.join(str(x) for x in tool_hints)}")

    return matched, "\n".join(lines).strip()
