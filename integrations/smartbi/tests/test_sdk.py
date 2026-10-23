"""Local protocol tests. All credentials and HTTP responses here are synthetic."""

import contextlib
import io
import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

from kk_smartbi import (
    AuthenticationError, ConfigurationError, ParameterError, ProtocolError,
    ReportParseError, ResourceLimitError, SmartBIClient, SmartBIConfig, TableLayout, UnsupportedReportError,
)
from kk_smartbi.html_report import parse_report_grid, parse_report_html, result_redirect
from kk_smartbi.models import ParameterDefinition, resolve_parameters


REPORT_A = "example-shipping-report"
REPORT_B = "synthetic-warehouse-report"
PASSWORD = "synthetic-password%#"
ROOT = Path(__file__).resolve().parents[1]
FIXTURE = (Path(__file__).parent / "fixtures" / "spreadsheet.html").read_text(encoding="utf-8")


def parameter(identifier, name, alias, default="", control="TEXTINPUT", allow_empty=False):
    return {"id": identifier, "name": name, "alias": alias, "value": default, "displayValue": default,
            "componentType": control, "dataType": "STRING", "componentDefine": json.dumps({"noNull": allow_empty})}


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.state = {"contexts": {}, "closed": [], "refreshes": [], "stale": False, "login_calls": 0}
        state = self.state

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, body, *, cookie=False, content_type="application/json"):
                raw = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", content_type + ";charset=UTF-8")
                self.send_header("Content-Length", str(len(raw)))
                if cookie:
                    self.send_header("Set-Cookie", "JSESSIONID=synthetic-session; Path=/xdata-bi; HttpOnly")
                self.end_headers()
                self.wfile.write(raw)

            def rpc(self, result, code=0, cookie=False):
                self.send(json.dumps({"retCode": code, "result": result}), cookie=cookie)

            def do_POST(self):
                form = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"), keep_blank_values=True)
                if self.path.endswith("RMIServlet"):
                    service = form["className"][0]
                    method = form["methodName"][0]
                    args = json.loads(form["params"][0])
                    if service == "UserService" and method == "clickLogin":
                        state["login_calls"] += 1
                        passed = args == ["reader", PASSWORD]
                        self.rpc(passed, cookie=passed)
                        return
                    if "JSESSIONID=synthetic-session" not in self.headers.get("Cookie", ""):
                        self.rpc(None, "SESSION_EXPIRED")
                        return
                    if method == state.get("expire_once_method"):
                        state.pop("expire_once_method")
                        self.rpc(None, "SESSION_EXPIRED")
                        return
                    if method == state.get("expire_always_method"):
                        self.rpc(None, "SESSION_EXPIRED")
                        return
                    if method == "logout":
                        self.rpc(True)
                    elif method == "getRootElements":
                        self.rpc([{"id": "DEFAULT_TREENODE"}])
                    elif method == "openQueryInPage":
                        report_id = args[0]
                        if report_id == "bad-server":
                            self.send(json.dumps({"retCode": PASSWORD, "detail": PASSWORD}))
                            return
                        context_id = "ctx-" + str(len(state["contexts"]) + 1)
                        if report_id == "no-parameters":
                            definitions = None
                        elif report_id == REPORT_B:
                            definitions = [parameter("project-id", "project_code", "项目"), parameter("warehouse-id", "warehouse_code", "仓库", control="COMBOBOX")]
                        elif report_id == "ambiguous":
                            definitions = [parameter("one", "one_name", "筛选", "a"), parameter("two", "two_name", "筛选", "b")]
                        elif report_id == "required":
                            definitions = [parameter("required-id", "required_name", "必填项")]
                        else:
                            definitions = [parameter("start-id", "start_date", "开始日期：", "2026-10-21"), parameter("end-id", "end_date", "结束日期：", "2026-10-21")]
                        state["contexts"][context_id] = {"definitions": definitions, "params": [], "selected": {}}
                        self.rpc([{"clientId": context_id, "parameterPanelClientId": context_id,
                                   "outputParameters": definitions, "sheetNames": state.get("sheet_names", ["Sheet1"]), "activeSheetIndex": 0,
                                   "visibleSheetNames": state.get("visible_sheets", state.get("sheet_names", ["Sheet1"])),
                                   "settings": {"reportType": 1 if report_id == "paged" else 0, "paging": state.get("paging", False)}}, []])
                    elif method == "closeQuery":
                        state["closed"].append(args[0])
                        self.rpc(None)
                    elif method == "setParamsInfo":
                        if state.get("reject_params"):
                            self.rpc(False)
                            return
                        state["contexts"][args[0]]["params"] = json.loads(args[1])
                        self.rpc(None)
                    elif method == "setParamValue":
                        state["contexts"][args[0]]["selected"][args[1]] = args[2]
                        self.rpc(True)
                    elif method == "getParamStandbyValue":
                        parent = state["contexts"][args[0]]["selected"].get("project-id", "DEFAULT")
                        self.rpc([[parent + "-WAREHOUSE", "测试仓库"]])
                    else:
                        self.rpc(None, "UNEXPECTED_METHOD")
                elif self.path.endswith("ssreportServlet"):
                    state["refresh_requests"] = state.get("refresh_requests", 0) + 1
                    if state.get("expire_refresh"):
                        self.send_response(401)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    if "JSESSIONID=synthetic-session" not in self.headers.get("Cookie", ""):
                        self.rpc(None, "SESSION_EXPIRED")
                        return
                    context_id = form["clientId"][0]
                    parameters = json.loads(form["paramsInfo"][0])
                    if parameters != state["contexts"][context_id]["params"] or form["writeBackData"][0] != "":
                        self.rpc(None, "WRONG_PARAMETERS")
                        return
                    state["refreshes"].append(parameters)
                    self.send('<script>window.location.replace("ssreport/htmls/' + context_id + '/sheet_0_0.htm");</script>', content_type="text/html")
                else:
                    self.send_error(404)

            def do_GET(self):
                if "JSESSIONID=synthetic-session" not in self.headers.get("Cookie", ""):
                    self.send("<html>登录</html>", content_type="text/html")
                    return
                context_id = self.path.split("/")[-2]
                parameters = state["contexts"][context_id]["params"]
                date_value = "2026-10-21" if state["stale"] else next((item["value"] for item in parameters if item["name"] == "start_date"), "2026-10-20")
                self.send(state.get("html_override", FIXTURE).replace("$DATE$", date_value), content_type="text/html")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/xdata-bi"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def client(self, **kwargs):
        return SmartBIClient(SmartBIConfig(self.base_url, "reader", PASSWORD, timeout=3, **kwargs))

    def test_different_report_ids_discover_different_parameters(self):
        with self.client() as client:
            dates = client.get_parameters(REPORT_A)
            warehouses = client.get_parameters(REPORT_B)
        self.assertEqual([p.name for p in dates], ["start_date", "end_date"])
        self.assertEqual([p.alias for p in warehouses], ["项目", "仓库"])
        self.assertTrue(warehouses[0].required)
        self.assertEqual(self.state["login_calls"], 1)
        self.assertEqual(len(self.state["closed"]), 2)

    def test_parameterless_report_can_query_empty_and_rejects_invented_dates(self):
        with self.client() as client:
            self.assertEqual(client.get_parameters("no-parameters", include_choices=True), [])
            result = client.query_report("no-parameters")
            self.assertEqual(result["parameters"], [])
            with self.assertRaises(ParameterError):
                client.query_report("no-parameters", {"开始日期：": "2026-10-20"})
            with self.assertRaises(ParameterError):
                client.get_parameter_choices("no-parameters", "日期")
        self.assertEqual(self.state["refreshes"], [[]])
        self.assertEqual(len(self.state["closed"]), 4)

    def test_real_http_cookie_parameter_mapping_redirect_and_table(self):
        with self.client() as client:
            result = client.query_report(REPORT_A, {"开始日期：": "2026-10-20", "end-id": "2026-10-20"})
        row = result["rows"][0]
        self.assertEqual(row["日期"], "2026-10-20")
        self.assertEqual(row["数据监控/仓接单量"], "11931")
        self.assertEqual(row["时效监控/出库率"], "99.37%")
        self.assertEqual(row["异常管理/近1H/配送未揽"], "76")
        self.assertEqual(result["column_count"], 26)
        self.assertEqual(len(result["totals"]), 1)
        self.assertEqual(result["data_row_count"], 1)
        self.assertNotIn("999999", row.values())
        self.assertEqual(self.state["refreshes"][0][0]["id"], "start-id")
        self.assertEqual(len(self.state["closed"]), 1)

    def test_two_dates_are_new_queries_not_reused_results(self):
        with self.client() as client:
            first = client.query_report(REPORT_A, {"start_date": "2026-10-20", "end_date": "2026-10-20"})
            second = client.query_report(REPORT_A, {"start_date": "2026-10-21", "end_date": "2026-10-21"})
        self.assertEqual(first["rows"][0]["日期"], "2026-10-20")
        self.assertEqual(second["rows"][0]["日期"], "2026-10-21")
        self.assertEqual(len(self.state["refreshes"]), 2)

    def test_complex_sheet_returns_grid_instead_of_repeated_layout_errors(self):
        # Mirrors the real report: a title, plain TD headers, empty padding columns,
        # merged subtotal labels, and a page-number row. None may be invented away.
        self.state["html_override"] = '''<table id="mainTable">
            <tr><td colspan="4">测试抽检率</td></tr>
            <tr><td>仓库</td><td>数量</td><td></td><td></td></tr>
            <tr><td>001</td><td>12</td><td></td><td></td></tr>
            <tr><td>合计</td><td>12</td><td></td><td></td></tr>
            <tr><td>1</td><td></td><td></td><td></td></tr>
        </table>'''
        with self.client() as client:
            result = client.query_report(REPORT_A, {}, layout=TableLayout(header_start=1, header_rows=1), result_format="auto")
        self.assertEqual(result["table_format"], "grid")
        self.assertIsNone(result["data_row_count"])
        self.assertEqual(result["table_row_count"], 5)
        self.assertEqual(result["column_count"], 4)
        self.assertEqual(result["rows"][0]["cells"][0]["colspan"], 4)
        self.assertEqual(result["rows"][2]["cells"][0]["text"], "001")
        self.assertEqual(result["rows"][2]["cells"][1]["column_index"], 1)
        self.assertIn("表头", result["layout_warning"])
        self.assertEqual(len(self.state["refreshes"]), 1)

    def test_record_mode_still_rejects_ambiguous_layout(self):
        self.state["html_override"] = '<table id="mainTable"><tr><td>列名</td><td></td></tr><tr><td>001</td><td>12</td></tr></table>'
        with self.client() as client:
            with self.assertRaises(ReportParseError):
                client.query_report(REPORT_A, {}, layout=TableLayout(header_start=0), result_format="records")
            raw = client.query_report(REPORT_A, {}, result_format="grid")
            self.assertEqual(raw["rows"][1]["cells"][0]["text"], "001")

    def test_auto_does_not_mistake_merged_data_for_a_header(self):
        self.state["html_override"] = '<table id="mainTable"><tr><td colspan="2">仓库数量</td></tr><tr><td>仓库</td><td>数量</td></tr><tr><td rowspan="2">上海</td><td>12</td></tr><tr><td>13</td></tr></table>'
        with self.client() as client:
            raw = client.query_report(REPORT_A, {}, result_format="auto")
        self.assertEqual(raw["table_format"], "grid")
        self.assertEqual(raw["rows"][2]["cells"][0]["rowspan"], 2)
        self.assertEqual(raw["rows"][3]["cells"][0]["column_index"], 1)

    def test_auto_and_grid_do_not_turn_login_html_into_success(self):
        self.state["html_override"] = '<html><body>登录已过期</body></html>'
        with self.client() as client:
            for result_format in ("auto", "grid"):
                with self.subTest(result_format=result_format), self.assertRaises(ReportParseError):
                    client.query_report(REPORT_A, {}, result_format=result_format)

    def test_unknown_required_ambiguous_and_duplicate_parameters_fail(self):
        cases = [(REPORT_A, {"不存在": "x"}), ("required", {}), ("ambiguous", {"筛选": "x"}),
                 (REPORT_A, {"start_date": "a", "开始日期：": "b"})]
        with self.client() as client:
            for report_id, params in cases:
                with self.subTest(report_id=report_id, params=params), self.assertRaises(ParameterError):
                    client.query_report(report_id, params)
        self.assertEqual(self.state["refreshes"], [])
        self.assertEqual(len(self.state["closed"]), len(cases))

    def test_fixed_context_cannot_be_overridden_through_alias(self):
        with self.client(fixed_parameters={"start_date": "2026-10-20"}) as client:
            with self.assertRaises(ParameterError):
                client.query_report(REPORT_A, {"开始日期：": "2026-10-21"})

    def test_dependent_choices_apply_parent_without_requiring_child(self):
        with self.client() as client:
            choices = client.get_parameter_choices(REPORT_B, "仓库", params={"项目": "P1"})
        self.assertEqual(choices, [["P1-WAREHOUSE", "测试仓库"]])
        self.assertEqual(self.state["refreshes"], [])

    def test_include_choices_uses_fixed_parent_context(self):
        with self.client(fixed_parameters={"项目": "P2"}) as client:
            definitions = client.get_parameters(REPORT_B, include_choices=True)
        self.assertEqual(definitions[1].choices, [["P2-WAREHOUSE", "测试仓库"]])

    def test_authentication_false_never_opens_report(self):
        with SmartBIClient(SmartBIConfig(self.base_url, "reader", "wrong")) as client:
            with self.assertRaises(AuthenticationError):
                client.get_parameters(REPORT_A)
        self.assertEqual(self.state["contexts"], {})

    def test_expired_metadata_session_renews_once(self):
        self.state["expire_once_method"] = "getRootElements"
        with self.client() as client:
            result = client.check_connection()
        self.assertTrue(result["authenticated"])
        self.assertEqual(self.state["login_calls"], 2)
        self.assertEqual(self.state["refreshes"], [])

    def test_expired_open_renews_before_refresh_without_repeating_business_query(self):
        self.state["expire_once_method"] = "openQueryInPage"
        with self.client() as client:
            result = client.query_report(REPORT_A, {})
        self.assertEqual(result["data_row_count"], 1)
        self.assertEqual(self.state["login_calls"], 2)
        self.assertEqual(self.state["refresh_requests"], 1)
        self.assertEqual(len(self.state["contexts"]), 1)

    def test_expiry_after_context_open_never_reuses_context_after_login(self):
        self.state["expire_once_method"] = "setParamsInfo"
        with self.client() as client, self.assertRaises(AuthenticationError):
            client.query_report(REPORT_A, {})
        self.assertEqual(self.state["login_calls"], 1)
        self.assertEqual(self.state["refreshes"], [])

    def test_expired_refresh_is_not_retried(self):
        self.state["expire_refresh"] = True
        with self.client() as client, self.assertRaises(AuthenticationError):
            client.query_report(REPORT_A, {})
        self.assertEqual(self.state["login_calls"], 1)
        self.assertEqual(self.state["refresh_requests"], 1)

    def test_persistent_expiry_does_not_loop_login(self):
        self.state["expire_always_method"] = "getRootElements"
        with self.client() as client, self.assertRaises(AuthenticationError):
            client.check_connection()
        self.assertEqual(self.state["login_calls"], 2)

    def test_errors_do_not_echo_server_password_or_represent_config_secret(self):
        with self.client() as client:
            self.assertNotIn(PASSWORD, repr(client.config))
            with self.assertRaises(ProtocolError) as error:
                client.get_parameters("bad-server")
            self.assertNotIn(PASSWORD, str(error.exception))

    def test_paged_report_is_rejected_instead_of_returning_first_page(self):
        with self.client() as client, self.assertRaises(UnsupportedReportError):
            client.query_report("paged", {})
        self.assertEqual(self.state["refreshes"], [])

    def test_string_paging_flag_cannot_return_a_partial_report(self):
        self.state["paging"] = "1"
        with self.client() as client, self.assertRaises(UnsupportedReportError):
            client.query_report(REPORT_A, {})
        self.assertEqual(self.state["refreshes"], [])

    def test_rejected_parameter_application_never_refreshes(self):
        self.state["reject_params"] = True
        with self.client() as client, self.assertRaises(ParameterError):
            client.query_report(REPORT_A, {})
        self.assertEqual(self.state["refreshes"], [])

    def test_boolean_sheet_index_and_hidden_sheet_are_rejected(self):
        self.state["sheet_names"] = ["Sheet1", "Hidden"]
        self.state["visible_sheets"] = ["Sheet1"]
        with self.client() as client:
            for index in (False, True, 1):
                with self.subTest(index=index), self.assertRaises(ParameterError):
                    client.query_report(REPORT_A, {}, sheet_index=index)
        self.assertEqual(self.state["refreshes"], [])

    def test_response_size_limit_is_enforced_without_partial_parse(self):
        config = SmartBIConfig(self.base_url, "reader", PASSWORD, timeout=3, max_response_bytes=100)
        with SmartBIClient(config) as client, self.assertRaises(ResourceLimitError):
            client.query_report(REPORT_A, {})
        self.assertEqual(self.state["refreshes"], [])

    def test_describe_discovers_visible_sheets_without_business_refresh(self):
        self.state["sheet_names"] = ["Sheet1", "Hidden"]
        self.state["visible_sheets"] = ["Sheet1"]
        with self.client() as client:
            client.get_report_info = lambda rid: {"id": rid, "resource_type": "SPREADSHEET_REPORT", "name": "测试"}
            described = client.describe_report(REPORT_A)
        self.assertTrue(described["query_supported"])
        self.assertTrue(described["sheets"][0]["visible"])
        self.assertFalse(described["sheets"][1]["visible"])
        self.assertEqual(described["parameter_count"], 2)
        self.assertFalse(described["business_query_executed"])
        self.assertEqual(self.state["refreshes"], [])
        self.assertEqual(len(self.state["closed"]), 1)

    def test_describe_unsupported_resource_does_not_open_a_report(self):
        with self.client() as client:
            client.get_report_info = lambda rid: {"id": rid, "resource_type": "PAGE", "name": "看板"}
            described = client.describe_report("dashboard")
        self.assertFalse(described["query_supported"])
        self.assertIsNone(described["parameters"])
        self.assertEqual(self.state["contexts"], {})

    def test_redirect_guard_rejects_other_origins(self):
        with self.client() as client, self.assertRaises(ProtocolError):
            client._request("https://example.com/xdata-bi/vision/")

    def test_redirect_guard_rejects_encoded_parent_paths(self):
        with self.client() as client:
            for path in ("vision/%2e%2e/%2e%2e/other", "vision/%252e%252e/%252e%252e/other", "vision/%2e%2e%5c%2e%2e%5cother"):
                with self.subTest(path=path), self.assertRaises(ProtocolError):
                    client._redirects.validate(self.base_url + "/" + path)

    def test_example_checks_actual_returned_date(self):
        environment = {**os.environ, "SMARTBI_BASE_URL": self.base_url, "SMARTBI_USERNAME": "reader", "SMARTBI_PASSWORD": PASSWORD,
                       "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(ROOT / "src")}
        command = [sys.executable, "-B", str(ROOT / "examples" / "query_report.py"), "--config", str(ROOT / "config.example.ini")]
        good = subprocess.run(command, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertTrue(json.loads(good.stdout)["verification"]["result_date_verified"])
        self.state["stale"] = True
        stale = subprocess.run(command, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(stale.returncode, 2)
        self.assertIn("ReportParseError", stale.stderr)
        self.assertEqual(stale.stdout, "")


class ConfigurationAndParserTests(unittest.TestCase):
    def test_layout_rejects_booleans_floats_and_invalid_columns(self):
        for values in ({"header_start": True}, {"header_rows": 1.5}, {"data_start": "3"}, {"columns": ("a", "a")}, {"columns": ("",)}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                TableLayout(**values)
    def test_explicit_null_parameters_are_empty_but_missing_or_malformed_still_fail(self):
        self.assertEqual(SmartBIClient._definitions({"outputParameters": None, "settings": {"paramsLayout": "[]"}}), [])
        self.assertEqual(SmartBIClient._definitions({"outputParameters": []}), [])
        for context in ({}, {"outputParameters": "[]"}, {"outputParameters": {}},
                        {"outputParameters": None, "settings": {"paramsLayout": '[[{"id":"date"}]]'}}):
            with self.subTest(context=context), self.assertRaises(ProtocolError):
                SmartBIClient._definitions(context)

    def test_duplicate_parameter_ids_cannot_produce_ambiguous_submissions(self):
        raw = parameter("same", "日期", "日期")
        with self.assertRaises(ProtocolError):
            SmartBIClient._definitions({"outputParameters": [raw, raw]})

    def test_nonfinite_parameter_values_are_rejected(self):
        definition = ParameterDefinition.from_server(parameter("number", "数量", "数量"), [])
        for value in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value), self.assertRaises(ParameterError):
                resolve_parameters([definition], {"数量": value})

    def test_expansion_and_nesting_limits_fail_before_large_allocation(self):
        huge = '<table id="mainTable"><tr><td rowspan="100" colspan="3000">x</td></tr>' + '<tr></tr>' * 99 + '</table>'
        deep = '<table id="mainTable"><tr><td>' + '<div>' * 140 + 'x' + '</div>' * 140 + '</td></tr></table>'
        for html in (huge, deep):
            with self.subTest(kind="huge" if html == huge else "deep"), self.assertRaises(ResourceLimitError):
                parse_report_grid(html)

    def test_parameter_type_uses_smartbi_type_when_data_type_is_absent(self):
        raw = parameter("date-id", "日期", "日期")
        raw.pop("dataType")
        raw["type"] = "DATE"
        self.assertEqual(ParameterDefinition.from_server(raw, []).data_type, "DATE")

    def test_parameter_names_preserve_fullwidth_colon(self):
        raw = parameter("live-shaped-id", "开始日期：", "开始日期：")
        definitions = [ParameterDefinition.from_server(raw, [])]
        self.assertEqual(resolve_parameters(definitions, {"开始日期：": "2026-10-20"})[0]["value"], "2026-10-20")
        with self.assertRaises(ParameterError):
            resolve_parameters(definitions, {"开始日期": "2026-10-20"})

    def test_ini_preserves_password_and_case_of_business_parameter(self):
        text = f"[smartbi]\nbase_url=https://example.com/app\nusername=reader\npassword={PASSWORD}\n[fixed_parameters]\nContextUserId=user1\n"
        with patch("pathlib.Path.open", return_value=io.StringIO(text)), patch.dict(os.environ, {}, clear=True):
            config = SmartBIConfig.from_file("unused.ini")
        self.assertEqual(config.password, PASSWORD)
        self.assertEqual(config.fixed_parameters, {"ContextUserId": "user1"})

    def test_empty_configuration_fails_before_network_access(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ConfigurationError):
            SmartBIConfig.from_file(ROOT / "config.example.ini")

    def test_duplicate_headers_and_login_html_do_not_become_success(self):
        duplicate = '<table id="mainTable"><tr><th>日期</th><th>日期</th></tr><tr><td>a</td><td>b</td></tr></table>'
        for html in (duplicate, '<form><input type="password"></form>'):
            with self.subTest(html=html), self.assertRaises(ReportParseError):
                parse_report_html(html)

    def test_layout_override_keeps_leading_zeros_and_percentages(self):
        html = '<table id="mainTable"><tr><td>单号</td><td>比例</td></tr><tr><td>00123</td><td>99.00%</td></tr></table>'
        result = parse_report_html(html, TableLayout(header_start=0, header_rows=1))
        self.assertEqual(result["rows"], [{"单号": "00123", "比例": "99.00%"}])

    def test_javascript_is_not_executed_or_followed_inside_result_table(self):
        self.assertIsNone(result_redirect(FIXTURE + '<script>location="ssreport/htmls/other/sheet.htm";</script>'))
        self.assertIsNone(result_redirect('<script>location = make_url();</script>'))
        self.assertEqual(result_redirect('<script>location.replace("ssreport/htmls/context/sheet.htm")</script>'), "ssreport/htmls/context/sheet.htm")


if __name__ == "__main__":
    unittest.main()
