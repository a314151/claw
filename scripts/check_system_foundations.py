from __future__ import annotations

import json
from pathlib import Path

from personal_assistant.skill_engine import load_default_skills, normalize_skills
from personal_assistant.time_system import TimeSystem, build_local_time_reply, is_time_query


def check_time_system() -> list[str]:
    errs: list[str] = []
    ts = TimeSystem()
    node = ts.build_time_memory_node()
    required = ["clock_source", "timezone", "unix_ts", "iso", "date", "time", "weekday"]
    for k in required:
        if k not in node:
            errs.append(f"time_memory_node 缺少字段: {k}")
    if not isinstance(node.get("unix_ts"), int):
        errs.append("time_memory_node.unix_ts 不是 int")

    reply = build_local_time_reply(ts)
    if "当前时间" not in reply and "服务器本地时钟" not in reply:
        errs.append("时间回复文本不符合预期")

    if not is_time_query("现在几点了"):
        errs.append("时间问题识别失败: 现在几点了")
    if is_time_query("帮我写个总结"):
        errs.append("时间问题识别误判")
    return errs


def check_skills() -> list[str]:
    errs: list[str] = []
    skills = normalize_skills(load_default_skills())
    if len(skills) < 15:
        errs.append(f"默认 skill 数量不足: {len(skills)} < 15")

    seen_ids: set[str] = set()
    for s in skills:
        sid = s.get("id", "")
        if sid in seen_ids:
            errs.append(f"存在重复 skill id: {sid}")
        seen_ids.add(sid)

        source = str(s.get("source", ""))
        if not source.startswith("https://github.com/"):
            errs.append(f"skill {sid} source 非 GitHub: {source}")

        mcp_servers = s.get("mcp_servers", [])
        if not isinstance(mcp_servers, list) or not mcp_servers:
            errs.append(f"skill {sid} 缺少 mcp_servers 适配")

        instruction = str(s.get("instruction", "")).strip()
        if not instruction:
            errs.append(f"skill {sid} 缺少 instruction")

    return errs


def main() -> None:
    errors = []
    errors.extend(check_time_system())
    errors.extend(check_skills())

    out = {
        "ok": not errors,
        "error_count": len(errors),
        "errors": errors,
        "skill_count": len(load_default_skills()),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))

    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
