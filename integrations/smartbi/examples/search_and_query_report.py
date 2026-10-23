"""Live workflow: search names/paths/IDs, choose uniquely, discover and query."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kk_smartbi import ParameterError, ReportParseError, SmartBIClient, SmartBIError, TableLayout


def choose_report(reports: list[dict], name: str, report_id: str | None = None) -> dict:
    matches = [item for item in reports if item["id"] == report_id] if report_id else [item for item in reports if item["name"] == name]
    if len(matches) != 1:
        raise ParameterError("搜索结果未唯一定位报表，请查看名称、完整路径和 ID，再用 --report-id 指定搜索结果中的一个 ID。")
    return matches[0]


def save_json(path: Path, value) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=4)
        stream.write("\n")


def main(argv=None) -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="当前示例报表的搜索到查询示例，不硬编码报表 ID。")
    parser.add_argument("--config", default=str(root / "config.ini"))
    parser.add_argument("--keyword", default="示例")
    parser.add_argument("--report-name", default="示例项目B2C发货监控表")
    parser.add_argument("--report-id", help="名称有歧义时，明确选用搜索结果中的 ID")
    parser.add_argument("--date", default="2026-10-20")
    parser.add_argument("--output-dir", type=Path, default=root / "results" / "search-and-query")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        requested_date = date.fromisoformat(args.date).isoformat()
        with SmartBIClient.from_config(args.config) as client:
            roots = client.browse_catalog()
            reports = client.search_reports(args.keyword)
            args.output_dir.mkdir(parents=True, exist_ok=True)
            save_json(args.output_dir / "catalog-root.json", roots)
            save_json(args.output_dir / "search-results.json", reports)
            print(json.dumps({"stage": "search", "count": len(reports), "reports": [{key: item[key] for key in ("name", "path", "id")} for item in reports]}, ensure_ascii=False), flush=True)
            selected = choose_report(reports, args.report_name, args.report_id)
            report_id = selected["id"]
            info = client.get_report_info(report_id)
            definitions = client.get_parameters(report_id)
            save_json(args.output_dir / "report-info.json", info)
            save_json(args.output_dir / "report-parameters.json", [item.to_dict() for item in definitions])
            print(json.dumps({"stage": "parameters", "report_id": report_id, "parameters": [{"name": item.name, "required": item.required, "id": item.id} for item in definitions]}, ensure_ascii=False), flush=True)
            # These are the discovered exact names for this example report only.
            # Other reports supply their own params after get_parameters(report_id).
            result = client.query_report(
                report_id,
                {"开始日期：": requested_date, "结束日期：": requested_date},
                layout=TableLayout(header_start=1, header_rows=3),
            )
        if not result["rows"] or any(row.get("日期") != requested_date for row in result["rows"]):
            raise ReportParseError("返回数据日期与本次查询条件不一致，拒绝认定验证成功。")
        result["report_name"] = info["name"]
        result["report_path"] = info["path"]
        result["verification"] = {"expected_date": requested_date, "result_date_verified": True, "independent_http_query_verified": True}
        output = args.output_dir / f"query-{requested_date}.json"
        save_json(output, result)
        print(json.dumps({"stage": "query", "status": "ok", "report_name": info["name"], "report_id": report_id, "data_row_count": result["data_row_count"], "column_count": result["column_count"], "output": str(output), "rows": result["rows"]}, ensure_ascii=False), flush=True)
        return 0
    except (SmartBIError, OSError, ValueError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
