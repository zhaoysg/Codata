"""Command line entry point: metadata discovery and explicit report queries."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .client import SmartBIClient
from .errors import SmartBIError
from .models import TableLayout


def _parameters(args) -> dict:
    text = Path(args.params_file).read_text(encoding="utf-8") if args.params_file else (args.params_json or "{}")
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("参数 JSON 必须是对象。")
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Smartbi V7 独立 HTTP SDK；不会读取浏览器凭据。")
    parser.add_argument("--config", default="config.ini", help="UTF-8 INI 配置文件")
    commands = parser.add_subparsers(dest="command", required=True)
    browse = commands.add_parser("browse", help="浏览一层资源目录，默认资源定制根目录")
    browse.add_argument("--folder-id")
    search = commands.add_parser("search", help="按关键词搜索报表，返回名称、完整路径和 ID")
    search.add_argument("keyword")
    search.add_argument("--folder-id")
    search.add_argument("--type", dest="resource_types", action="append", help="可重复指定，例如 SPREADSHEET_REPORT")
    search.add_argument("--page-size", type=int, default=100)
    info = commands.add_parser("info", help="按报表 ID 获取名称、完整路径和详情")
    info.add_argument("report_id")
    describe = commands.add_parser("describe", help="获取参数、可见工作表与当前查询支持范围，不刷新报表")
    describe.add_argument("report_id")
    health = commands.add_parser("health", help="验证 Smartbi 登录与目录读取，不刷新业务报表")
    metadata = commands.add_parser("params", help="按资源 ID 获取参数定义")
    metadata.add_argument("report_id")
    metadata.add_argument("--include-choices", action="store_true")
    choices = commands.add_parser("choices", help="获取参数备选值，可传已选择的父级参数")
    choices.add_argument("report_id")
    choices.add_argument("parameter")
    query = commands.add_parser("query", help="查询非分页电子表格分组表")
    query.add_argument("report_id")
    query.add_argument("--sheet-index", type=int)
    query.add_argument("--header-start", type=int)
    query.add_argument("--header-rows", type=int)
    query.add_argument("--format", dest="result_format", choices=("auto", "records", "grid"), default="auto", help="auto 无法识别表头时返回坐标单元格；records 严格按字段返回")
    for command in (choices, query):
        source = command.add_mutually_exclusive_group()
        source.add_argument("--params-file", help="UTF-8 JSON 参数文件")
        source.add_argument("--params-json", help="JSON 对象，推荐改用参数文件避免终端转义")
    for command in (browse, search, info, describe, health, metadata, choices, query):
        command.add_argument("--output", help="保存 UTF-8 无 BOM JSON；已有文件会被替换")
    args = parser.parse_args(argv)
    try:
        with SmartBIClient.from_config(args.config) as client:
            if args.command == "browse":
                result = client.browse_catalog(args.folder_id)
            elif args.command == "search":
                result = client.search_reports(args.keyword, folder_id=args.folder_id, resource_types=args.resource_types, page_size=args.page_size)
            elif args.command == "info":
                result = client.get_report_info(args.report_id)
            elif args.command == "describe":
                result = client.describe_report(args.report_id)
            elif args.command == "health":
                result = client.check_connection()
            elif args.command == "params":
                result = [item.to_dict() for item in client.get_parameters(args.report_id, include_choices=args.include_choices)]
            elif args.command == "choices":
                result = client.get_parameter_choices(args.report_id, args.parameter, params=_parameters(args))
            else:
                layout = TableLayout(header_start=args.header_start, header_rows=args.header_rows)
                result = client.query_report(args.report_id, _parameters(args), layout=layout, sheet_index=args.sheet_index, result_format=args.result_format)
        encoded = json.dumps(result, ensure_ascii=False, indent=4) + "\n"
        if args.output:
            with Path(args.output).open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(encoded)
        else:
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8")
            print(encoded, end="")
        return 0
    except (SmartBIError, OSError, ValueError) as exc:
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
        # OSError can include paths, but credentials are never put in paths or URLs.
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
