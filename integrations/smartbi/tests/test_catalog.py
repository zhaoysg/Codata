"""Catalog behaviour tests; transport and report queries have separate HTTP tests."""

import contextlib
import importlib.util
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from kk_smartbi import ParameterError, ProtocolError, ResourceNotFoundError, SmartBIClient, SmartBIConfig, UnsupportedReportError
from kk_smartbi.__main__ import main


ROOT_ID = "DEFAULT_TREENODE"
ROOT = {"id": ROOT_ID, "name": ROOT_ID, "alias": "资源定制", "type": "DEFAULT_TREENODE", "hasChild": True}
FOLDER_A = {"id": "folder-a", "name": "甲项目", "alias": "甲项目", "type": "DEFAULT_TREENODE", "hasChild": True}
FOLDER_B = {"id": "folder-b", "name": "乙项目", "alias": "乙项目", "type": "DEFAULT_TREENODE", "hasChild": True}
REPORT_A = {"id": "report-a", "name": "示例日报", "alias": "示例日报", "type": "SPREADSHEET_REPORT", "desc": "甲项目说明", "lastModifiedDate": "2026-10-23"}
REPORT_B = {"id": "report-b", "name": "示例日报", "alias": "示例日报", "type": "SPREADSHEET_REPORT"}
REPORT_C = {"id": "report-c", "name": "示例看板", "alias": "示例看板", "type": "PAGE"}


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.repeat_page = False
        self.nodes = {item["id"]: item for item in (ROOT, FOLDER_A, FOLDER_B, REPORT_A, REPORT_B, REPORT_C)}
        self.paths = {
            ROOT_ID: [ROOT], "folder-a": [ROOT, FOLDER_A], "folder-b": [ROOT, FOLDER_B],
            "report-a": [ROOT, FOLDER_A, REPORT_A], "report-b": [ROOT, FOLDER_B, REPORT_B],
            "report-c": [ROOT, FOLDER_A, REPORT_C],
        }

    def rpc(self, service, method, params, **kwargs):
        self.calls.append((service, method, params))
        if service == "UserService":
            return True
        if method == "getRootElements":
            return [ROOT]
        if method == "getCatalogElementById":
            return self.nodes.get(params[0])
        if method == "getCatalogElementPath":
            return self.paths.get(params[0], [])
        if method == "getChildElementsWithPurviewTypeRead":
            return {ROOT_ID: [FOLDER_A, FOLDER_B], "folder-a": [REPORT_A, REPORT_C], "folder-b": [REPORT_B]}[params[0]]
        if service == "MetadataService" and method == "searchByKeywords":
            keyword, types, operator, offset, size = params
            self.assertEqual(operator, "+")
            rows = [dict(item, alias="<Font color='red'>示例</Font>" + item["alias"][2:]) for item in (REPORT_A, REPORT_B, REPORT_C) if item["type"] in types and keyword in item["name"]]
            if self.repeat_page:
                return [rows[0], rows[0]]
            # Mirrors the observed V7 behaviour: an offset beyond the end clamps
            # to an earlier tail rather than returning an empty page.
            offset = len(rows) // 2 if offset >= len(rows) else offset
            return rows[offset:offset + size]
        raise AssertionError(f"Unexpected operation: {service}.{method}")

    def client(self):
        client = SmartBIClient(SmartBIConfig("https://example.com/app", "synthetic-user", "synthetic-password"))
        client._rpc = self.rpc
        return client

    def test_browse_one_level_and_descend_by_folder_id(self):
        with self.client() as client:
            roots = client.browse_catalog()
            children = client.browse_catalog(roots[0]["id"])
        self.assertEqual([item["name"] for item in roots], ["甲项目", "乙项目"])
        self.assertTrue(roots[0]["is_folder"])
        self.assertEqual(children[0]["path"], "资源定制 / 甲项目 / 示例日报")
        self.assertEqual(children[0]["parent_id"], "folder-a")

    def test_detail_has_full_path_and_definition_modification_time(self):
        with self.client() as client:
            info = client.get_report_info("report-a")
        self.assertEqual(info["id"], "report-a")
        self.assertEqual(info["path_ids"], [ROOT_ID, "folder-a", "report-a"])
        self.assertEqual(info["description"], "甲项目说明")
        self.assertEqual(info["modified_at"], "2026-10-23")
        self.assertNotIn("data_updated_at", info)

    def test_search_keeps_same_names_and_cleans_highlights(self):
        with self.client() as client:
            reports = client.search_reports("示例", page_size=1)
        self.assertEqual(len(reports), 3)
        self.assertEqual(reports[0]["name"], reports[1]["name"])
        self.assertNotEqual(reports[0]["id"], reports[1]["id"])
        self.assertNotEqual(reports[0]["path"], reports[1]["path"])
        self.assertNotIn("<Font", json.dumps(reports))
        pages = [call[2][3] for call in self.calls if call[1] == "searchByKeywords"]
        self.assertEqual(pages, [0, 0, 0])
        limits = [call[2][4] for call in self.calls if call[1] == "searchByKeywords"]
        self.assertEqual(limits, [1, 2, 4])

    def test_folder_scope_uses_ancestor_ids(self):
        with self.client() as client:
            reports = client.search_reports("示例", folder_id="folder-a")
        self.assertEqual([item["id"] for item in reports], ["report-a", "report-c"])

    def test_bounded_search_stops_after_enough_matches_for_a_window(self):
        with self.client() as client:
            reports = client.search_reports("示例", max_results=1)
        self.assertEqual(len(reports), 1)
        self.assertEqual(sum(method == "searchByKeywords" for _, method, _ in self.calls), 1)
        self.assertEqual(sum(method == "getCatalogElementPath" for _, method, _ in self.calls), 1)

    def test_resource_type_filter_is_sent_to_server(self):
        with self.client() as client:
            reports = client.search_reports("示例", resource_types=["SPREADSHEET_REPORT"])
        self.assertEqual([item["id"] for item in reports], ["report-a", "report-b"])

    def test_duplicate_search_response_cannot_be_presented_as_complete(self):
        self.repeat_page = True
        with self.client() as client, self.assertRaises(ProtocolError):
            client.search_reports("示例")

    def test_unknown_report_and_folder_confusion_are_explicit(self):
        with self.client() as client:
            with self.assertRaises(ResourceNotFoundError):
                client.get_report_info("not-found")
            with self.assertRaises(UnsupportedReportError):
                client.get_report_info("folder-a")
            with self.assertRaises(ParameterError):
                client.browse_catalog("report-a")

    def test_invalid_search_arguments_fail_before_login(self):
        with self.client() as client:
            for kwargs in ({"keyword": " "}, {"keyword": "示例", "page_size": 0}, {"keyword": "示例", "resource_types": []}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ParameterError):
                    client.search_reports(**kwargs)
        self.assertEqual(self.calls, [])

    def test_discovery_never_opens_reports_or_ignores_permissions(self):
        with self.client() as client:
            client.browse_catalog()
            client.search_reports("示例")
            client.get_report_info("report-a")
        self.assertFalse(any(service == "SpreadsheetReportModule" or "IgnorePurview" in method for service, method, _ in self.calls))

    def test_malformed_paths_are_not_presented_as_complete(self):
        self.paths["report-a"] = [ROOT, FOLDER_A]
        with self.client() as client, self.assertRaises(ProtocolError):
            client.get_report_info("report-a")

    def test_cli_browse_search_and_info(self):
        commands = [(["browse"], list), (["search", "示例", "--type", "SPREADSHEET_REPORT"], list), (["info", "report-a"], dict)]
        for arguments, result_type in commands:
            with self.subTest(arguments=arguments), patch("kk_smartbi.__main__.SmartBIClient.from_config", return_value=self.client()), contextlib.redirect_stdout(io.StringIO()) as output:
                code = main(["--config", "synthetic.ini", *arguments])
            self.assertEqual(code, 0)
            self.assertIsInstance(json.loads(output.getvalue()), result_type)

    def test_example_requires_a_unique_search_result(self):
        path = Path(__file__).resolve().parents[1] / "examples" / "search_and_query_report.py"
        spec = importlib.util.spec_from_file_location("catalog_example", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with self.client() as client:
            reports = client.search_reports("示例")
        with self.assertRaises(ParameterError):
            module.choose_report(reports, "示例日报")
        self.assertEqual(module.choose_report(reports, "示例日报", "report-b")["id"], "report-b")
        with self.assertRaises(ParameterError):
            module.choose_report(reports, "示例日报", "not-in-search")


if __name__ == "__main__":
    unittest.main()
