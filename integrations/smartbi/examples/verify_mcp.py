"""Real stdio MCP verification: discover tools, search, select, inspect, query."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters


async def verify(args) -> dict:
    root = Path(__file__).resolve().parents[1]
    # A source checkout works without an editable install; installed wheels also work.
    env = {"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
    if (root / "src" / "kk_smartbi").is_dir():
        env["PYTHONPATH"] = str(root / "src")
    # Preserve only the explicitly documented SDK credential overrides if supplied.
    for key in ("SMARTBI_BASE_URL", "SMARTBI_USERNAME", "SMARTBI_PASSWORD"):
        if key in os.environ:
            env[key] = os.environ[key]
    transport = StdioServerParameters(
        command=sys.executable,
        args=["-m", "kk_smartbi.mcp_server", "--config", str(Path(args.config).resolve())],
        env=env,
    )
    async with Client(transport, read_timeout_seconds=660) as client:
        listed = await client.list_tools()

        async def call(name, arguments):
            result = await client.call_tool(name, arguments)
            if result.is_error or not isinstance(result.structured_content, dict):
                message = " ".join(item.text for item in result.content if hasattr(item, "text"))
                raise ValueError(f"MCP {name} 失败：{message}")
            return result.structured_content

        catalog = await call("browse_catalog", {})
        reports = []
        offset = 0
        while True:
            found = await call("search_reports", {"keyword": args.keyword, "offset": offset})
            reports.extend(found["items"])
            if not found["has_more"]:
                break
            offset = found["next_offset"]
        print(json.dumps({"stage": "search", "count": len(reports), "reports": [
            {key: item[key] for key in ("name", "path", "id")} for item in reports
        ]}, ensure_ascii=False), flush=True)
        matches = [item for item in reports if item["id"] == args.report_id] if args.report_id else [item for item in reports if item["name"] == args.report_name]
        if len(matches) != 1:
            raise ValueError("搜索未唯一定位目标，请查看名称、路径和 ID，用 --report-id 明确指定。")
        report_id = matches[0]["id"]
        info = await call("get_report_info", {"report_id": report_id})
        parameters = await call("get_parameters", {"report_id": report_id})
        names = {item["name"] for item in parameters["parameters"]}
        if not {"开始日期：", "结束日期："} <= names:
            raise ValueError("本示例的两个日期参数与报表元数据不符，拒绝猜测。")
        result = await call("query_report", {
            "report_id": report_id,
            "params": {"开始日期：": args.date, "结束日期：": args.date},
            "layout": {"header_start": 1, "header_rows": 3},
        })
        if result["truncated"] or not result["rows"] or any(row.get("日期") != args.date for row in result["rows"]):
            raise ValueError("结果为空、不完整或日期不一致，不能认定验证成功。")
        return {
            "verification": {"transport": "stdio", "expected_date": args.date, "result_date_verified": True},
            "tool_names": [tool.name for tool in listed.tools],
            "catalog": catalog,
            "search_results": reports,
            "report_info": info,
            "parameters": parameters,
            "result": result,
        }


def main(argv=None) -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="通过真正的 MCP stdio 子进程查询当前示例报表。")
    parser.add_argument("--config", default=str(root / "config.ini"))
    parser.add_argument("--keyword", default="示例")
    parser.add_argument("--report-name", default="示例项目B2C发货监控表")
    parser.add_argument("--report-id")
    parser.add_argument("--date", default="2026-10-20")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        args.date = date.fromisoformat(args.date).isoformat()
        result = asyncio.run(verify(args))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=4)
            stream.write("\n")
        print(json.dumps({"status": "ok", "transport": "stdio", "date": args.date,
            "tool_count": len(result["tool_names"]), "row_count": result["result"]["data_row_count"],
            "column_count": result["result"]["column_count"], "output": str(args.output)}, ensure_ascii=False))
        return 0
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
