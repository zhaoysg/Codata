"""MCP wire/schema tests use an injected SDK; live validation is a separate example."""

import asyncio
import copy
import json
import threading
import time
import unittest
from functools import wraps
from unittest.mock import AsyncMock, Mock

try:
    from mcp import Client
except ImportError:
    Client = None

from kk_smartbi import ConfigurationError, ParameterDefinition, ParameterError, TransportError
from kk_smartbi.mcp_server import create_http_app, create_server


class FakeSDK:
    def __init__(self):
        self.closed = False
        self.calls = []
        self.entries = [
            {"id": "report-a", "name": "同名日报", "path": "资源 / 甲 / 同名日报"},
            {"id": "report-b", "name": "同名日报", "path": "资源 / 乙 / 同名日报"},
        ]
        self.result = {
            "report_id": "report-a", "source": "smartbi_http", "truncated": False,
            "table_format": "records", "columns": ["日期", "运单"], "column_count": 2,
            "rows": [{"日期": "2026-10-20", "运单": "0001"}, {"日期": "2026-10-21", "运单": "0002"}],
            "totals": [{"日期": "合计", "运单": "2"}], "data_row_count": 2,
            "parameters": [{"name": "开始日期：", "value": "2026-10-20"}],
        }

    def close(self):
        self.closed = True

    def browse_catalog(self, folder_id=None):
        self.calls.append(("browse", folder_id))
        return self.entries

    def search_reports(self, keyword, **kwargs):
        self.calls.append(("search", keyword, kwargs))
        return self.entries

    def get_report_info(self, report_id):
        return next(item for item in self.entries if item["id"] == report_id)

    def get_parameters(self, report_id):
        return [ParameterDefinition(
            "param.date", "开始日期：", "开始日期：", None, "DATETIMEPICK", True,
            "2026-10-20", "2026-10-20", {"internal": "not exposed"},
        )]

    def describe_report(self, report_id):
        return {**self.get_report_info(report_id), "query_supported": True,
                "sheets": [{"index": 0, "name": "Sheet1", "visible": True}],
                "parameters": [p.to_dict() for p in self.get_parameters(report_id)]}

    def check_connection(self):
        return {"authenticated": True, "business_query_executed": False}

    def get_parameter_choices(self, report_id, parameter, *, params=None):
        self.calls.append(("choices", report_id, parameter, params))
        return [{"value": "a", "displayValue": "甲"}, {"value": "b", "displayValue": "乙"}]

    def query_report(self, report_id, params, **kwargs):
        self.calls.append(("query", report_id, params, kwargs))
        if "未知" in params:
            raise ParameterError("该报表不存在参数 '未知'，请先调用 get_parameters(report_id)。")
        if "crash" in params:
            raise RuntimeError("synthetic secret must not leak")
        return self.result


def mcp_session(test):
    @wraps(test)
    def run(self):
        async def scenario():
            self.sdk = FakeSDK()
            self.server = create_server("unused.ini", client_factory=lambda path: self.sdk)
            # AnyIO cancel scopes must enter and exit in the same task.
            async with Client(self.server) as self.client:
                await test(self)
            self.assertTrue(self.sdk.closed)
        asyncio.run(scenario())
    return run


@unittest.skipIf(Client is None, "Optional MCP dependency is not installed")
class MCPTests(unittest.TestCase):

    async def call(self, name, arguments):
        result = await self.client.call_tool(name, arguments)
        self.assertFalse(result.is_error, result.content)
        self.assertIsInstance(result.structured_content, dict)
        return result.structured_content

    @mcp_session
    async def test_eleven_read_only_tools_have_no_credential_inputs(self):
        listed = await self.client.list_tools()
        self.assertEqual({tool.name for tool in listed.tools}, {
            "browse_catalog", "search_reports", "get_report_info", "get_parameters", "get_parameter_choices", "query_report",
            "describe_report", "health_check", "start_query", "get_query_status", "get_query_result",
        })
        for tool in listed.tools:
            self.assertTrue(tool.annotations.read_only_hint)
            self.assertFalse(tool.annotations.destructive_hint)
            fields = set(tool.input_schema["properties"])
            self.assertFalse(fields & {"config", "config_path", "username", "password", "base_url", "url"})
            self.assertIsNotNone(tool.output_schema)

    @mcp_session
    async def test_catalog_and_search_preserve_duplicate_names_and_page(self):
        first = await self.call("search_reports", {"keyword": "同名", "limit": 1})
        second = await self.call("search_reports", {"keyword": "同名", "offset": first["next_offset"], "limit": 1})
        self.assertTrue(first["has_more"])
        self.assertTrue(second["truncated"])
        self.assertFalse(second["has_more"])
        self.assertNotEqual(first["items"][0]["id"], second["items"][0]["id"])
        browse = await self.call("browse_catalog", {})
        self.assertEqual(browse["total"], 2)
        self.assertFalse(browse["truncated"])

    @mcp_session
    async def test_partial_search_does_not_claim_an_exact_total(self):
        self.sdk.entries.append({"id": "report-c", "name": "同名日报", "path": "资源 / 丙 / 同名日报"})
        result = await self.call("search_reports", {"keyword": "同名", "limit": 2})
        self.assertTrue(result["has_more"])
        self.assertIsNone(result["total"])
        self.assertEqual(result["total_at_least"], 3)
        self.assertFalse(result["total_is_exact"])

    @mcp_session
    async def test_parameters_preserve_exact_names_without_raw_control_definition(self):
        result = await self.call("get_parameters", {"report_id": "report-a"})
        parameter = result["parameters"][0]
        self.assertEqual(result["parameter_count"], 1)
        self.assertTrue(result["has_parameters"])
        self.assertEqual(parameter["name"], "开始日期：")
        self.assertTrue(parameter["required"])
        self.assertNotIn("component_definition", parameter)
        self.assertNotIn("choices", parameter)

    @mcp_session
    async def test_choices_pass_parent_context_and_keep_value_display_pair(self):
        result = await self.call("get_parameter_choices", {
            "report_id": "report-a", "parameter": "仓库", "params": {"项目": "内部值"}, "limit": 1,
        })
        self.assertEqual(result["items"], [{"value": "a", "displayValue": "甲"}])
        self.assertEqual(self.sdk.calls[-1][-1], {"项目": "内部值"})

    @mcp_session
    async def test_query_reports_source_truncation_and_full_query_totals_without_mutation(self):
        before = copy.deepcopy(self.sdk.result)
        result = await self.call("query_report", {
            "report_id": "report-a", "params": {"开始日期：": "2026-10-20"}, "limit": 1,
            "layout": {"header_start": 1, "header_rows": 3},
        })
        self.assertTrue(result["truncated"])
        self.assertEqual(result["next_offset"], 1)
        self.assertEqual(result["data_row_count"], 2)
        self.assertEqual(result["returned_row_count"], 1)
        self.assertEqual(result["rows"][0]["运单"], "0001")
        self.assertEqual(result["totals_scope"], "entire_query")
        self.assertEqual(result["totals"], before["totals"])
        self.assertEqual(result["report_path"], self.sdk.entries[0]["path"])
        self.assertEqual(self.sdk.calls[-1][-1]["layout"].header_rows, 3)
        self.assertEqual(self.sdk.result, before)

    @mcp_session
    async def test_invalid_limits_layout_and_types_fail_before_query(self):
        for extra in ({"limit": 201}, {"row_offset": -1}, {"layout": {"header_rows": 0}}, {"sheet_index": -1}, {"layout": {"typo": 3}}, {"limit": "abc"}):
            result = await self.client.call_tool("query_report", {"report_id": "report-a", "params": {}, **extra})
            self.assertTrue(result.is_error, extra)
        self.assertEqual(self.sdk.calls, [])

    @mcp_session
    async def test_sdk_errors_flag_failure_and_unexpected_errors_hide_details(self):
        result = await self.client.call_tool("query_report", {"report_id": "report-a", "params": {"未知": "x"}})
        self.assertTrue(result.is_error)
        self.assertIn("get_parameters", json.dumps([item.model_dump() for item in result.content]))
        result = await self.client.call_tool("query_report", {"report_id": "report-a", "params": {"crash": "x"}})
        self.assertTrue(result.is_error)
        self.assertNotIn("synthetic secret", json.dumps([item.model_dump() for item in result.content]))

    @mcp_session
    async def test_output_character_limit_returns_error_instead_of_partial_json(self):
        sdk = FakeSDK()
        server = create_server("unused.ini", max_output_chars=10, client_factory=lambda path: sdk)
        async with Client(server) as client:
            result = await client.call_tool("get_report_info", {"report_id": "report-a"})
            self.assertTrue(result.is_error)
            self.assertIsNone(result.structured_content)
        self.assertTrue(sdk.closed)

    @mcp_session
    async def test_custom_caps_have_usable_defaults(self):
        sdk = FakeSDK()
        server = create_server("unused.ini", max_rows=1, catalog_page_limit=1, client_factory=lambda path: sdk)
        async with Client(server) as client:
            result = await client.call_tool("query_report", {"report_id": "report-a", "params": {}})
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content["returned_row_count"], 1)
            result = await client.call_tool("browse_catalog", {})
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content["returned"], 1)

    @mcp_session
    async def test_empty_parameter_list_is_success_and_query_can_omit_params(self):
        self.sdk.get_parameters = lambda report_id: []
        definitions = await self.call("get_parameters", {"report_id": "report-a"})
        self.assertFalse(definitions["has_parameters"])
        self.assertEqual(definitions["parameter_count"], 0)
        self.assertEqual(definitions["parameters"], [])
        await self.call("query_report", {"report_id": "report-a"})
        self.assertEqual(self.sdk.calls[-1][2], {})

    @mcp_session
    async def test_grid_pagination_never_claims_table_rows_are_business_records(self):
        self.sdk.result.update(table_format="grid", columns=[], totals=[], data_row_count=None, table_row_count=2)
        self.sdk.result["rows"] = [
            {"row_index": 0, "cells": [{"column_index": 0, "text": "报表标题", "rowspan": 1, "colspan": 2}]},
            {"row_index": 1, "cells": [{"column_index": 0, "text": "001", "rowspan": 1, "colspan": 1}]},
        ]
        result = await self.call("query_report", {"report_id": "report-a", "limit": 1})
        self.assertEqual(result["table_format"], "grid")
        self.assertIsNone(result["data_row_count"])
        self.assertEqual(result["table_row_count"], 2)
        self.assertEqual(result["pagination_scope"], "table_rows")
        self.assertEqual(result["totals_scope"], "unclassified")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["next_offset"], 1)

    @mcp_session
    async def test_snapshot_can_be_paged_without_a_second_query(self):
        first = await self.call("query_report", {"report_id": "report-a", "limit": 1})
        second = await self.call("get_query_result", {"query_id": first["query_id"], "row_offset": first["next_offset"], "limit": 1})
        self.assertEqual(second["rows"][0]["运单"], "0002")
        self.assertEqual(sum(call[0] == "query" for call in self.sdk.calls), 1)
        self.assertFalse(second["source_requeried"])

    @mcp_session
    async def test_describe_and_health_are_read_only_and_explicit(self):
        report = await self.call("describe_report", {"report_id": "report-a"})
        self.assertTrue(report["sheets"][0]["visible"])
        local = await self.call("health_check", {})
        self.assertEqual(local["smartbi"], {"checked": False})
        checked = await self.call("health_check", {"check_smartbi": True})
        self.assertTrue(checked["smartbi"]["authenticated"])
        self.assertFalse(checked["smartbi"]["business_query_executed"])

    @mcp_session
    async def test_health_distinguishes_upstream_failure_from_mcp_failure(self):
        def unavailable():
            raise TransportError("连接不可用")
        self.sdk.check_connection = unavailable
        health = await self.call("health_check", {"check_smartbi": True})
        self.assertTrue(health["mcp_ready"])
        self.assertFalse(health["smartbi"]["ok"])
        self.assertEqual(health["smartbi"]["error"]["code"], "TRANSPORT_ERROR")

    @mcp_session
    async def test_async_errors_have_stable_codes_without_automatic_retry(self):
        job = await self.call("start_query", {"report_id": "report-a", "params": {"未知": "x"}})
        state = await self.call("get_query_status", {"query_id": job["query_id"], "wait_seconds": 1})
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["error"]["code"], "INVALID_PARAMETERS")
        self.assertFalse(state["error"]["retryable"])
        self.assertEqual(sum(call[0] == "query" for call in self.sdk.calls), 1)

    @mcp_session
    async def test_long_query_does_not_block_metadata_and_control_tools(self):
        entered, release = threading.Event(), threading.Event()

        class SerializedFake(FakeSDK):
            def __init__(self):
                super().__init__()
                self.lock = threading.RLock()

            def get_parameters(self, report_id):
                with self.lock:
                    return super().get_parameters(report_id)

            def query_report(self, *args, **kwargs):
                with self.lock:
                    entered.set()
                    release.wait(3)
                    return super().query_report(*args, **kwargs)

        created = []

        def factory(path):
            sdk = SerializedFake()
            created.append(sdk)
            return sdk

        server = create_server("unused.ini", client_factory=factory)
        async with Client(server) as client:
            try:
                response = await client.call_tool("start_query", {"report_id": "report-a"})
                self.assertFalse(response.is_error)
                query_id = response.structured_content["query_id"]
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                before = time.monotonic()
                metadata = await client.call_tool("get_parameters", {"report_id": "report-a"})
                health = await client.call_tool("health_check", {})
                self.assertFalse(metadata.is_error)
                self.assertTrue(health.structured_content["runtime"]["query_busy"])
                self.assertLess(time.monotonic() - before, 1)
                sync = await client.call_tool("query_report", {"report_id": "report-a"})
                self.assertTrue(sync.is_error)
                self.assertIn('"code": "BUSY"', sync.content[0].text)
            finally:
                release.set()
            state = await client.call_tool("get_query_status", {"query_id": query_id, "wait_seconds": 1})
            self.assertEqual(state.structured_content["status"], "succeeded")
        self.assertEqual(len(created), 2)
        self.assertTrue(all(sdk.closed for sdk in created))


@unittest.skipIf(Client is None, "Optional MCP dependency is not installed")
class HTTPAccessTests(unittest.TestCase):
    def test_network_bind_requires_an_explicit_ip_and_peer(self):
        server = Mock()
        for host, peers, port in (("0.0.0.0", ["192.0.2.20"], 8765), ("192.0.2.10", [], 8765), ("192.0.2.10", ["192.0.2.20"], 0)):
            with self.assertRaises(ConfigurationError):
                create_http_app(server, host=host, port=port, allowed_clients=peers)
        server.streamable_http_app.assert_not_called()

    def test_peer_check_ignores_forwarded_ip_and_blocks_before_mcp(self):
        async def scenario():
            inner = AsyncMock()
            server = Mock()
            server.streamable_http_app.return_value = inner
            app = create_http_app(server, host="192.0.2.10", port=8765, allowed_clients=["192.0.2.20"])
            messages = []

            async def receive():
                return {"type": "http.request", "body": b""}

            async def send(message):
                messages.append(message)

            await app({"type": "http", "client": ("192.0.2.21", 1234), "headers": [(b"x-forwarded-for", b"192.0.2.20")]}, receive, send)
            self.assertEqual(messages[0]["status"], 403)
            inner.assert_not_awaited()
            await app({"type": "http", "client": ("192.0.2.20", 1234)}, receive, send)
            inner.assert_awaited_once()
            settings = server.streamable_http_app.call_args.kwargs["transport_security"]
            self.assertTrue(settings.enable_dns_rebinding_protection)
            self.assertEqual(settings.allowed_hosts, ["192.0.2.10:8765"])
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
