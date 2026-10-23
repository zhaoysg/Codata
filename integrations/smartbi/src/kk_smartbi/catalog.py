"""Normalize permission-checked catalog metadata, including search highlights."""

from __future__ import annotations

import re
from html import unescape
from typing import Any

from .errors import ProtocolError


# Report types used by this V7 deployment's QuickSearchWrapper.js.
REPORT_TYPES = (
    "SIMPLE_REPORT", "Dashboard", "DashboardMap", "PAGE", "OLAP_REPORT",
    "SPREADSHEET_REPORT", "COMBINED_QUERY", "INSIGHT", "OFFICE_REPORT",
)


def plain_text(value: Any) -> str:
    if value is None:
        return ""
    return unescape(re.sub(r"<[^>]*>", "", str(value))).strip()


def validate_elements(value: Any) -> list[dict]:
    if not isinstance(value, list) or any(not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"] for item in value):
        raise ProtocolError("目录响应不是包含有效资源 ID 的列表，拒绝返回不完整目录。")
    return value


def entry(element: dict, path: list[dict]) -> dict:
    resource_id = element.get("id")
    if not resource_id or not path or path[-1].get("id") != resource_id:
        raise ProtocolError("目录路径未包含目标资源，无法确认完整路径。")
    identifiers = [node["id"] for node in path]
    if len(set(identifiers)) != len(identifiers):
        raise ProtocolError("目录路径出现循环资源 ID。")
    names = [plain_text(node.get("alias") or node.get("name") or node["id"]) for node in path]
    resource_type = str(element.get("type") or "")
    has_children = element.get("hasChild")
    if has_children not in (True, False, "true", "false"):
        has_children = None
    elif isinstance(has_children, str):
        has_children = has_children == "true"
    return {
        "id": resource_id,
        "name": plain_text(element.get("alias") or element.get("name") or resource_id),
        "internal_name": plain_text(element.get("name") or element.get("alias") or resource_id),
        "resource_type": resource_type,
        "is_folder": resource_type.endswith("TREENODE"),
        "is_report": resource_type in REPORT_TYPES,
        "has_children": has_children,
        "path": " / ".join(names),
        "path_ids": identifiers,
        "parent_id": identifiers[-2] if len(identifiers) > 1 else None,
        "description": plain_text(element.get("desc")),
        "created_at": element.get("createdDate"),
        "modified_at": element.get("lastModifiedDate"),
    }
