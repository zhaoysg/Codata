"""Live query example. Requires a real account in configuration; no fixture fallback."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kk_smartbi import ReportParseError, SmartBIClient, SmartBIError, TableLayout


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(Path(__file__).resolve().parents[1] / "config.ini"))
    parser.add_argument("--report-id", default="example-shipping-report", help="替换为实际报表 ID")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        with SmartBIClient.from_config(args.config) as client:
            result = client.query_report(
                report_id=args.report_id,
                params={"开始日期：": "2026-10-20", "结束日期：": "2026-10-20"},
                layout=TableLayout(header_start=1, header_rows=3),
            )
        if not result["rows"] or any(row.get("日期") != "2026-10-20" for row in result["rows"]):
            raise ReportParseError("查询结果没有目标日期，拒绝将默认日期、空结果或其他日期视为本次验证成功。")
        result["verification"] = {"expected_date": "2026-10-20", "result_date_verified": True}
        text = json.dumps(result, ensure_ascii=False, indent=4) + "\n"
        if args.output:
            with args.output.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
        else:
            sys.stdout.reconfigure(encoding="utf-8")
            print(text, end="")
        return 0
    except (SmartBIError, OSError) as exc:
        sys.stderr.reconfigure(encoding="utf-8")
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
