"""Verify a persistent HTTP MCP: describe -> submit -> status -> snapshot pages."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from mcp import Client


async def verify(args, params: dict) -> dict:
    async with Client(args.url, read_timeout_seconds=45) as client:
        async def call(name, arguments):
            result = await client.call_tool(name, arguments)
            if result.is_error or not isinstance(result.structured_content, dict):
                raise ValueError(" ".join(item.text for item in result.content if hasattr(item, "text")))
            return result.structured_content

        tools = [tool.name for tool in (await client.list_tools()).tools]
        required = {"describe_report", "start_query", "get_query_status", "get_query_result", "health_check"}
        if not required <= set(tools):
            raise ValueError("MCP 工具定义尚未更新，请重新连接 v0.5 服务。")
        description = await call("describe_report", {"report_id": args.report_id})
        if not description["query_supported"]:
            raise ValueError("当前类型不支持查询：" + str(description.get("unsupported_reason")))
        request = {"report_id": args.report_id, "params": params, "result_format": "auto"}
        if args.sheet_index is not None:
            request["sheet_index"] = args.sheet_index
        started = time.monotonic()
        job = await call("start_query", request)
        query_id = job["query_id"]
        print(json.dumps({"stage": "submitted", "query_id": query_id, "seconds": round(time.monotonic() - started, 3)}, ensure_ascii=False), flush=True)
        health = await call("health_check", {})
        deadline = time.monotonic() + args.max_wait
        state = job
        while state["status"] in {"queued", "running"}:
            remaining = deadline - time.monotonic()
            if remaining < 1:
                raise ValueError(f"等待达到上限；query_id={query_id}，任务执行状态需继续查看，未自动重提。")
            state = await call("get_query_status", {"query_id": query_id, "wait_seconds": min(25, int(remaining))})
            print(json.dumps({"stage": "status", **state}, ensure_ascii=False), flush=True)
        if state["status"] != "succeeded":
            raise ValueError(json.dumps(state, ensure_ascii=False))
        pages = [await call("get_query_result", {"query_id": query_id, "limit": args.limit})]
        if pages[0]["has_more"]:
            pages.append(await call("get_query_result", {"query_id": query_id, "row_offset": pages[0]["next_offset"], "limit": args.limit}))
        if any(page["query_id"] != query_id or page["source_requeried"] for page in pages):
            raise ValueError("查询 ID 或快照来源不一致，拒绝认定验证成功。")
        if len({page["snapshot_created_at"] for page in pages}) != 1:
            raise ValueError("两页未使用同一个结果快照。")
        return {"tool_names": tools, "description": description, "job": job, "state": state,
                "health_after_submission": health, "pages": pages,
                "verification": {"same_snapshot": True, "source_requeried": False}}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="验证常驻 HTTP MCP 的异步查询和快照翻页；不读取浏览器凭据。")
    parser.add_argument("--url", required=True, help="运行中的 HTTP(S) MCP 服务地址")
    parser.add_argument("--report-id", required=True)
    parser.add_argument("--params-file", type=Path, help="UTF-8 JSON 参数对象；无参数报表可省略")
    parser.add_argument("--sheet-index", type=int)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--max-wait", type=int, default=720)
    parser.add_argument("--output", type=Path, default=Path("results/mcp-async.json"))
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        url = urlsplit(args.url)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("--url 必须是无凭据或查询串的 HTTP(S) MCP 地址。")
        if args.limit < 1 or args.max_wait < 1:
            raise ValueError("limit/max-wait 必须大于零。")
        params = json.loads(args.params_file.read_text(encoding="utf-8")) if args.params_file else {}
        if not isinstance(params, dict):
            raise ValueError("参数文件必须是 JSON 对象。")
        result = asyncio.run(verify(args, params))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=4)
            stream.write("\n")
        print(json.dumps({"status": "ok", "query_id": result["state"]["query_id"], "pages": len(result["pages"]), "output": str(args.output)}, ensure_ascii=False))
        return 0
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"{type(exc).__name__}: MCP 客户端连接或通信失败，请核对服务地址和连接状态。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
