from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP


mcp = FastMCP("doc_export")


def _output_root() -> Path:
    root = Path(os.getenv("DOC_OUTPUT_ROOT", ".")).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _resolve_output_path(file_path: str, suffix: str) -> Path:
    if not file_path.strip():
        raise ValueError("file_path 不能为空")

    root = _output_root()
    raw = Path(file_path.strip())
    candidate = raw if raw.is_absolute() else root / raw
    resolved = candidate.resolve()

    if not resolved.suffix:
        resolved = resolved.with_suffix(suffix)

    if not resolved.is_relative_to(root):
        raise ValueError(f"输出路径必须位于 {root} 下")

    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


@mcp.tool()
def create_csv(
    file_path: str,
    headers: list[str],
    rows: list[list[Any]],
    delimiter: str = ",",
    encoding: str = "utf-8-sig",
) -> str:
    """根据表头与二维数组写入 CSV 文件。"""
    if not headers:
        raise ValueError("headers 不能为空")

    if not isinstance(rows, list):
        raise ValueError("rows 必须是数组")

    path = _resolve_output_path(file_path, ".csv")

    with path.open("w", newline="", encoding=encoding) as f:
        writer = csv.writer(f, delimiter=delimiter)
        writer.writerow([_to_text(h) for h in headers])
        for row in rows:
            if not isinstance(row, list):
                raise ValueError("rows 中每一项都必须是数组")
            writer.writerow([_to_text(v) for v in row])

    return f"CSV 已生成: {path}"


@mcp.tool()
def create_pdf(
    file_path: str,
    title: str,
    content: str,
) -> str:
    """根据标题与正文生成 PDF（A4 单栏自动换页）。"""
    if not title.strip():
        raise ValueError("title 不能为空")

    try:
        from reportlab.lib.pagesizes import A4  # type: ignore[import-not-found]
        from reportlab.pdfgen import canvas  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("生成 PDF 需要安装 reportlab，请先执行: uv pip install reportlab") from exc

    path = _resolve_output_path(file_path, ".pdf")

    c = canvas.Canvas(str(path), pagesize=A4)
    width, height = A4

    margin_x = 48
    top = height - 56
    line_h = 18

    c.setFont("Helvetica-Bold", 16)
    c.drawString(margin_x, top, title.strip())

    c.setFont("Helvetica", 11)
    y = top - 28
    for raw_line in content.splitlines() or [""]:
        line = raw_line.rstrip()
        if y <= 48:
            c.showPage()
            c.setFont("Helvetica", 11)
            y = height - 56
        c.drawString(margin_x, y, line)
        y -= line_h

    c.save()
    return f"PDF 已生成: {path}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
