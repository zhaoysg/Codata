"""Bounded in-memory query snapshots and one asynchronous query worker.

No automatic query retries, disk persistence, or business value conversion.
"""

from __future__ import annotations

import copy
import json
import math
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Callable

from .errors import (
    AuthenticationError, ConfigurationError, ParameterError, ProtocolError,
    QueryNotFoundError, QueryTimeoutError, ReportParseError, ResourceLimitError,
    ResourceNotFoundError, ServiceBusyError, SmartBIError, TransportError,
    UnsupportedReportError,
)


def error_payload(exc: Exception) -> dict:
    """Stable model-facing errors; never expose arbitrary exception details."""
    rules = (
        (QueryTimeoutError, "UPSTREAM_TIMEOUT", False, "执行状态未知，不自动重提。调整客户端等待时间或联系管理员核对。"),
        (AuthenticationError, "AUTHENTICATION_FAILED", False, "检查本地账号或会话，使用 health_check(check_smartbi=true) 验证。"),
        (ParameterError, "INVALID_PARAMETERS", False, "先 describe_report 获取参数和可见工作表，按真实定义修正输入。"),
        (QueryNotFoundError, "QUERY_NOT_FOUND", False, "结果已过期、被回收或 ID 不存在；确认是否需要重新查询，不自动重提。"),
        (ServiceBusyError, "BUSY", True, "查询通道正在使用或队列已满；查看已有 query_id 状态，不并行重提。"),
        (ResourceLimitError, "RESOURCE_LIMIT", False, "缩小范围或返回行列数；已有 query_id 时用 get_query_result 取局部，不重新执行。"),
        (UnsupportedReportError, "UNSUPPORTED_REPORT", False, "describe_report 检查类型及 query_supported，当前不支持分页清单或仪表盘。"),
        (ResourceNotFoundError, "RESOURCE_NOT_FOUND", False, "核对报表 ID 和配置账号的读取权限。"),
        (ReportParseError, "REPORT_PARSE_FAILED", False, "使用 auto/grid；登录页、结果表缺失或非法合并不能作为成功数据。"),
        (ProtocolError, "PROTOCOL_ERROR", False, "核对部署兼容性与服务端日志；不要用不同 layout 重复试探。"),
        (TransportError, "TRANSPORT_ERROR", False, "网络请求执行状态未知，请检查连接；不要自动重复业务查询。"),
        (ConfigurationError, "CONFIGURATION_ERROR", False, "检查服务端本地配置，不要向 AI 提交密码。"),
    )
    for kind, code, retryable, action in rules:
        if isinstance(exc, kind):
            message = str(exc)
            return {"code": code, "message": message[:2048], "message_truncated": len(message) > 2048,
                    "retryable": retryable, "next_action": action}
    if isinstance(exc, ValueError):
        return {"code": "INVALID_INPUT", "message": str(exc), "retryable": False, "next_action": "修正输入后再调用。"}
    return {"code": "INTERNAL_ERROR", "message": f"{type(exc).__name__}: 执行失败，未暴露内部异常内容。",
            "retryable": False, "next_action": "在服务端本地排查，不要重复提交相同查询。"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class QueryRuntime:
    """One worker + immutable snapshots, shared by sync and async MCP entry points."""

    def __init__(self, *, ttl_seconds: float = 900, max_entries: int = 8,
                 max_cache_bytes: int = 32 * 1024 * 1024, clock: Callable = time.monotonic):
        if (not isinstance(ttl_seconds, (int, float)) or isinstance(ttl_seconds, bool) or not math.isfinite(ttl_seconds)
            or ttl_seconds <= 0 or type(max_entries) is not int or max_entries < 1
            or type(max_cache_bytes) is not int or max_cache_bytes < 1):
            raise ConfigurationError("查询缓存 TTL、条目数和字节上限必须大于零。")
        self.ttl_seconds, self.max_entries, self.max_cache_bytes = ttl_seconds, max_entries, max_cache_bytes
        self._clock = clock
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._query_gate = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="smartbi-query")
        self._entries: dict[str, dict] = {}
        self._bytes = 0
        self._closed = False

    def _remove(self, query_id: str) -> None:
        self._bytes -= self._entries[query_id].get("size", 0)
        del self._entries[query_id]

    def _cleanup(self) -> None:
        now = self._clock()
        for query_id, entry in list(self._entries.items()):
            if entry.get("expires", float("inf")) <= now:
                self._remove(query_id)

    def _evict_one(self, exclude: str | None = None) -> bool:
        choices = [(entry["finished"], query_id) for query_id, entry in self._entries.items()
                   if query_id != exclude and entry["status"] in {"succeeded", "failed"}]
        if not choices:
            return False
        self._remove(min(choices)[1])
        return True

    def _admit(self, report_id: str, fingerprint: str | None = None) -> str:
        self._cleanup()
        if self._closed:
            raise ServiceBusyError("MCP 查询运行环境正在关闭。")
        while len(self._entries) >= self.max_entries:
            if not self._evict_one():
                raise ServiceBusyError("异步查询队列已满，请先查看已有查询状态。")
        query_id = uuid.uuid4().hex
        self._entries[query_id] = {"query_id": query_id, "report_id": report_id, "status": "queued",
                                   "created_at": _utc_now().isoformat(), "fingerprint": fingerprint}
        return query_id

    def _finish(self, query_id: str, action: Callable[[], dict]) -> None:
        with self._lock:
            self._entries[query_id]["status"] = "running"
        try:
            result = action()
            size = len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            if size > self.max_cache_bytes:
                raise ResourceLimitError("完整查询结果超过单个缓存可用字节上限，未保存局部数据。")
            snapshot = copy.deepcopy(result)
            with self._lock:
                while self._bytes + size > self.max_cache_bytes:
                    if not self._evict_one(exclude=query_id):
                        raise ResourceLimitError("缓存空间不足，未保存局部查询数据。")
                self._entries[query_id].update(result=snapshot, size=size)
                self._bytes += size
        except Exception as exc:
            with self._lock:
                self._entries[query_id].update(error=error_payload(exc))
        finally:
            with self._lock:
                self._entries[query_id].update(status="failed" if "error" in self._entries[query_id] else "succeeded",
                    finished=self._clock(), expires=self._clock() + self.ttl_seconds,
                    completed_at=_utc_now().isoformat(), snapshot_expires_at=(_utc_now() + timedelta(seconds=self.ttl_seconds)).isoformat())
                self._changed.notify_all()

    def run(self, report_id: str, action: Callable[[], dict]) -> str:
        """Synchronous execution fails fast if another business query is active."""
        if not self._query_gate.acquire(blocking=False):
            raise ServiceBusyError("已有业务查询正在运行。慢查询请使用 start_query，再查询状态和结果。")
        try:
            with self._lock:
                query_id = self._admit(report_id)
            self._finish(query_id, action)
            return query_id
        finally:
            self._query_gate.release()

    def start(self, report_id: str, action: Callable[[], dict], *, fingerprint: str) -> dict:
        with self._lock:
            self._cleanup()
            for entry in self._entries.values():
                if entry.get("fingerprint") == fingerprint and entry["status"] in {"queued", "running"}:
                    return {**self._status(entry), "deduplicated": True}
            query_id = self._admit(report_id, fingerprint)

            def work():
                with self._query_gate:
                    self._finish(query_id, action)

            self._executor.submit(work)
            return {**self._status(self._entries[query_id]), "deduplicated": False}

    @staticmethod
    def _status(entry: dict) -> dict:
        fields = ("query_id", "report_id", "status", "created_at", "completed_at", "snapshot_expires_at", "error")
        return {**{key: copy.deepcopy(entry[key]) for key in fields if key in entry},
                "poll_after_seconds": 5 if entry["status"] in {"queued", "running"} else None}

    def _find(self, query_id: str) -> dict:
        self._cleanup()
        if query_id not in self._entries:
            raise QueryNotFoundError("查询 ID 不存在、快照已过期或已按容量上限回收。")
        return self._entries[query_id]

    def status(self, query_id: str, *, wait_seconds: float = 0) -> dict:
        if not isinstance(wait_seconds, (int, float)) or not math.isfinite(wait_seconds) or not 0 <= wait_seconds <= 25:
            raise ParameterError("wait_seconds 必须在 0..25 秒之间。")
        with self._changed:
            entry = self._find(query_id)
            if wait_seconds and entry["status"] in {"queued", "running"}:
                self._changed.wait_for(lambda: query_id not in self._entries or self._entries[query_id]["status"] in {"succeeded", "failed"}, timeout=wait_seconds)
            return self._status(self._find(query_id))

    def health(self) -> dict:
        with self._lock:
            self._cleanup()
            return {"query_busy": self._query_gate.locked(), "queries": len(self._entries),
                    "active_queries": sum(e["status"] in {"queued", "running"} for e in self._entries.values()),
                    "snapshot_serialized_bytes": self._bytes, "max_cache_bytes": self.max_cache_bytes,
                    "max_entries": self.max_entries, "snapshot_ttl_seconds": self.ttl_seconds}

    def page(self, query_id: str, *, offset: int = 0, limit: int = 50, max_rows: int = 200,
             column_offset: int = 0, column_limit: int | None = None, include_totals: bool = True,
             omit_empty_cells: bool = True, max_chars: int = 40_000) -> dict:
        for name, value in (("offset", offset), ("column_offset", column_offset)):
            if type(value) is not int or value < 0:
                raise ParameterError(f"{name} 必须是非负整数。")
        if type(limit) is not int or not 1 <= limit <= max_rows:
            raise ParameterError(f"limit 必须在 1..{max_rows} 之间。")
        if column_limit is not None and (type(column_limit) is not int or column_limit < 1):
            raise ParameterError("column_limit 必须是正整数。")
        with self._lock:
            entry = self._find(query_id)
            if entry["status"] != "succeeded":
                return self._status(entry)
            result = entry["result"]
            grid = result.get("table_format") == "grid"
            width = result.get("column_count", len(result.get("columns", [])))
            end = width if column_limit is None else min(width, column_offset + column_limit)
            if column_offset >= width and width:
                raise ParameterError("column_offset 超过结果列数。")
            names = result.get("columns", [])[column_offset:end]

            def project(row):
                if not grid:
                    return {name: row[name] for name in names}
                cells = [cell for cell in row["cells"]
                         if cell["column_index"] < end and cell["column_index"] + cell["colspan"] > column_offset
                         and (not omit_empty_cells or cell["text"] or cell["rowspan"] > 1 or cell["colspan"] > 1)]
                return {"row_index": row["row_index"], "cells": cells}

            base = {key: value for key, value in result.items() if key not in {"rows", "totals", "columns"}}
            base.update(query_id=query_id, snapshot_created_at=entry["completed_at"], snapshot_expires_at=entry["snapshot_expires_at"],
                        source_requeried=False, columns=names, column_offset=column_offset, selected_column_count=max(0, end - column_offset),
                        columns_truncated=column_offset > 0 or end < width, totals_scope="unclassified" if grid else "entire_query",
                        pagination_scope="table_rows" if grid else "data_rows", row_offset=offset,
                        data_row_count=None if grid else len(result["rows"]),
                        totals=[project(row) for row in result.get("totals", [])] if include_totals else [], totals_included=include_totals)
            if grid:
                base["merged_context"] = [{"row_index": row["row_index"], "cells": [cell for cell in project(row)["cells"]
                    if row["row_index"] + cell["rowspan"] > offset]} for row in result["rows"][:offset]
                    if any(row["row_index"] + cell["rowspan"] > offset for cell in row["cells"])]
                base["merged_context"] = [row for row in base["merged_context"] if row["cells"]]
            actual = min(limit, max(0, len(result["rows"]) - offset))
            while True:
                has_more = offset + actual < len(result["rows"])
                value = {**base, "rows": [project(row) for row in result["rows"][offset:offset + actual]],
                         "returned_row_count": actual, "has_more": has_more,
                         "next_offset": offset + actual if has_more else None,
                         "truncated": bool(result.get("truncated")) or offset > 0 or has_more or base["columns_truncated"],
                         "response_limited": actual < min(limit, max(0, len(result["rows"]) - offset)),
                         "empty_cells_omitted": grid and omit_empty_cells}
                if len(json.dumps(value, ensure_ascii=False, allow_nan=False)) <= max_chars:
                    return copy.deepcopy(value)
                if actual <= 1:
                    raise ResourceLimitError(f"query_id={query_id} 的单行或合计超过输出上限；用 get_query_result 缩小列范围或 include_totals=false，不要重新执行报表。")
                actual = max(1, actual // 2)

    def close(self) -> None:
        with self._lock:
            self._closed = True
        # A running HTTP/SQL operation cannot be truthfully claimed cancelled.
        self._executor.shutdown(wait=True, cancel_futures=True)
        with self._query_gate, self._lock:
            self._entries.clear()
            self._bytes = 0
