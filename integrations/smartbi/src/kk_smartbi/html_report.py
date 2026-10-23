"""Parse static Smartbi HTML without executing JavaScript or Excel formulas."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser

from .errors import ReportParseError, ResourceLimitError
from .models import TableLayout


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    parent: _Node | None = field(default=None, repr=False)
    children: list = field(default_factory=list, repr=False)

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, _Node):
                yield from child.walk()


class _Document(HTMLParser):
    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {})
        self.current = self.root
        self.depth = 0
        self.node_count = 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.node_count += 1
        if self.node_count > 500_000 or self.depth >= 128:
            raise ResourceLimitError("报表 HTML 节点数量或嵌套深度超出支持范围。")
        node = _Node(tag, {key: value or "" for key, value in attrs}, self.current)
        self.current.children.append(node)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}:
            self.current = node
            self.depth += 1

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.current.tag == tag:
            self.current = self.current.parent or self.root
            self.depth = max(0, self.depth - 1)

    def handle_endtag(self, tag):
        node = self.current
        distance = 0
        while node is not self.root:
            if node.tag == tag:
                self.current = node.parent or self.root
                self.depth = max(0, self.depth - distance - 1)
                return
            distance += 1
            node = node.parent or self.root

    def handle_data(self, data):
        self.current.children.append(data)


def _raw_text(node: _Node) -> str:
    return "".join(child if isinstance(child, str) else _raw_text(child) for child in node.children)


def _hidden_classes(document: _Document) -> set[str]:
    classes = set()
    for node in document.root.walk():
        if node.tag != "style":
            continue
        for selectors, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", _raw_text(node)):
            if not re.search(r"(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse))", declarations, re.I):
                continue
            for selector in selectors.split(","):
                match = re.fullmatch(r"\s*(?:td|th|tr|span|div)?\.([\w-]+)\s*", selector)
                if match:
                    classes.add(match[1])
    return classes


def _hidden(node: _Node, classes: set[str]) -> bool:
    current = node
    while current:
        if "hidden" in current.attrs:
            return True
        if classes.intersection(current.attrs.get("class", "").split()):
            return True
        if re.search(r"(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse))", current.attrs.get("style", ""), re.I):
            return True
        current = current.parent
    return False


def _text(node: _Node, hidden_classes: set[str]) -> str:
    if node.tag in {"script", "style"} or _hidden(node, hidden_classes):
        return ""
    parts = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
        elif child.tag == "br":
            parts.append(" ")
        else:
            parts.append(_text(child, hidden_classes))
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _nearest_table(node: _Node) -> _Node | None:
    current = node.parent
    while current:
        if current.tag == "table":
            return current
        current = current.parent
    return None


def _table_rows(document: _Document, table_id: str):
    tables = [node for node in document.root.walk() if node.tag == "table" and node.attrs.get("id") == table_id]
    if len(tables) != 1:
        raise ReportParseError(f"未找到唯一的数据表 {table_id!r}；响应可能是登录页、加载页或错误页。")
    table = tables[0]
    hidden_classes = _hidden_classes(document)
    rows = []
    for node in table.walk():
        if node.tag != "tr" or _nearest_table(node) is not table or _hidden(node, hidden_classes):
            continue
        cells = []
        for child in node.children:
            if not isinstance(child, _Node) or child.tag not in {"td", "th"} or _hidden(child, hidden_classes):
                continue
            try:
                rowspan = int(child.attrs.get("rowspan", "1"))
                colspan = int(child.attrs.get("colspan", "1"))
            except ValueError as exc:
                raise ReportParseError("表格包含非法 rowspan/colspan。") from exc
            if not 1 <= rowspan <= 10000 or not 1 <= colspan <= 10000:
                raise ReportParseError("表格合并范围超出支持范围。")
            cells.append({"text": _text(child, hidden_classes), "rowspan": rowspan, "colspan": colspan, "header": child.tag == "th"})
        rows.append(cells)
    return rows


def _expand(rows):
    grid: list[list[str | None]] = [[] for _ in rows]
    allocated = 0
    for row_index, cells in enumerate(rows):
        column_index = 0
        for cell in cells:
            while column_index < len(grid[row_index]) and grid[row_index][column_index] is not None:
                column_index += 1
            cell["column_index"] = column_index
            if row_index + cell["rowspan"] > len(rows):
                raise ReportParseError("rowspan 超出了数据表范围。")
            for offset in range(cell["rowspan"]):
                target = grid[row_index + offset]
                end = column_index + cell["colspan"]
                increase = max(0, end - len(target))
                allocated += increase
                if allocated > 250_000:
                    raise ResourceLimitError("展开后的表格超过 250000 个单元格，请缩小查询范围。")
                target.extend([None] * increase)
                if any(target[index] is not None for index in range(column_index, end)):
                    raise ReportParseError("表格合并单元格重叠，无法可靠对应表头。")
                target[column_index:end] = [cell["text"]] * cell["colspan"]
            column_index += cell["colspan"]
    return grid


def parse_report_html(html: str, layout: TableLayout | None = None, table_id: str = "mainTable", *, allow_heuristic: bool = True) -> dict:
    """Return display strings, never silently coerce tracking IDs or percentages."""
    layout = layout or TableLayout()
    raw_rows = _table_rows(_Document(html), table_id)
    if not raw_rows:
        raise ReportParseError("数据表没有任何行。")
    grid = _expand(raw_rows)
    start = layout.header_start
    count = layout.header_rows
    if start is None:
        start = next((index for index, cells in enumerate(raw_rows) if any(cell["header"] for cell in cells)), None)
        if start is not None:
            count = count or next((index - start for index in range(start + 1, len(raw_rows)) if not any(c["header"] for c in raw_rows[index])), len(raw_rows) - start)
        else:
            if not allow_heuristic:
                raise ReportParseError("表格没有语义化表头且未指定布局，改为返回原始坐标单元格，避免把数据中的合并行当作表头。")
            start = next((index for index, cells in enumerate(raw_rows) if any(cell["rowspan"] > 1 for cell in cells)), None)
            if start is None:
                raise ReportParseError("无法可靠识别表头，请传入 TableLayout(header_start=..., header_rows=...)。")
            count = count or max(cell["rowspan"] for cell in raw_rows[start])
    count = count or 1
    if start >= len(grid) or start + count > len(grid):
        raise ReportParseError("指定表头范围超出了数据表。")
    width = max(len(row) for row in grid[start:start + count])
    if layout.columns is not None:
        columns = list(layout.columns)
        if len(columns) != width:
            raise ReportParseError("手工指定的列名数量与数据表不一致。")
    else:
        columns = []
        for column in range(width):
            parts = []
            for row in grid[start:start + count]:
                value = row[column] if column < len(row) else None
                if value and (not parts or value != parts[-1]):
                    parts.append(value)
            columns.append("/".join(parts))
    if not all(columns) or len(set(columns)) != len(columns):
        raise ReportParseError("表头存在空列名或重复名称，请通过 TableLayout(columns=...) 明确指定。")
    begin = layout.data_start if layout.data_start is not None else start + count
    end = layout.data_end if layout.data_end is not None else len(grid)
    if begin < start + count or end < begin or end > len(grid):
        raise ReportParseError("指定数据范围非法。")
    records, totals, notes = [], [], []
    for index in range(begin, end):
        values = grid[index]
        nonempty = list(dict.fromkeys(value for value in values if value))
        if not nonempty:
            continue
        if len(nonempty) == 1 and re.match(r"^(?:注[：:]|备注[：:]|说明[：:])", nonempty[0]):
            notes.append(nonempty[0])
            continue
        if len(values) != width:
            raise ReportParseError("数据行与表头列数不符，拒绝返回可能错位的数据。")
        record = dict(zip(columns, (value or "" for value in values)))
        if record[columns[0]] in {"合计", "总计", "小计"}:
            totals.append(record)
        else:
            records.append(record)
    title_candidates = [cell["text"] for row in raw_rows[:start] for cell in row if cell["text"]]
    return {
        "title": max(title_candidates, key=len, default=""),
        "columns": columns,
        "column_count": width,
        "data_row_count": len(records),
        "rows": records,
        "totals": totals,
        "notes": notes,
    }


def parse_report_grid(html: str, *, layout_warning: str | None = None, table_id: str = "mainTable") -> dict:
    """Return real cell coordinates/spans when a table has no reliable record header.

    This deliberately does not classify business detail rows, totals, or empty data.
    A valid unique table and non-overlapping spans are still required.
    """
    raw_rows = _table_rows(_Document(html), table_id)
    if not raw_rows:
        raise ReportParseError("数据表没有任何行。")
    grid = _expand(raw_rows)
    return {
        "table_format": "grid",
        "coordinate_system": "visible_html_table_zero_based",
        "title": "",
        "columns": [],
        "column_count": max(map(len, grid), default=0),
        "data_row_count": None,
        "table_row_count": len(raw_rows),
        "rows": [{
            "row_index": index,
            "cells": [{key: cell[key] for key in ("column_index", "text", "rowspan", "colspan")} for cell in cells],
        } for index, cells in enumerate(raw_rows)],
        "totals": [],
        "notes": [],
        "layout_warning": layout_warning or "已按请求返回坐标单元格，未推断业务表头。",
        "interpretation_note": "行列坐标从 0 开始；保留合并范围和原始显示字符串。表格行数不等于业务明细数，空单元格不等于零；未自动识别合计或数据是否为空。不要猜列名或反复试布局。",
    }


def result_redirect(html: str) -> str | None:
    """Read only literal navigation instructions; never eval server JavaScript."""
    document = _Document(html)
    if any(node.tag == "table" and node.attrs.get("id") == "mainTable" for node in document.root.walk()):
        return None
    for node in document.root.walk():
        if node.tag == "meta" and node.attrs.get("http-equiv", "").lower() == "refresh":
            match = re.search(r"(?:^|;)\s*url\s*=\s*(.+)", node.attrs.get("content", ""), re.I)
            if match:
                return unescape(match[1].strip().strip("\"'"))
    pattern = r"(?:\blocation(?:\.href)?\s*=\s*|\blocation\.(?:replace|assign)\(\s*)([\"'])([^\"'\r\n]+)\1"
    for match in re.finditer(pattern, html):
        target = match[2].replace("\\/", "/").replace("\\u0026", "&")
        if "ssreport/htmls/" in target:
            return unescape(target)
    return None
