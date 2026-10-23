"""V7 HTTP workflow observed in this deployment's SpreadsheetReport.js.

This module uses authenticated RPC to discover each report's actual parameters,
then submits the same read-only refresh form used by the spreadsheet viewer.
"""

from __future__ import annotations

import http.cookiejar
import json
import socket
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urljoin, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener

from .catalog import REPORT_TYPES, entry, validate_elements
from .errors import AuthenticationError, ParameterError, ProtocolError, QueryTimeoutError, ReportParseError, ResourceLimitError, ResourceNotFoundError, SmartBIError, TransportError, UnsupportedReportError
from .html_report import parse_report_grid, parse_report_html, result_redirect
from .models import ParameterDefinition, SmartBIConfig, TableLayout, find_parameter, resolve_parameters


_SESSION_RECOVERABLE_RPCS = {
    ("CatalogService", "getRootElements"),
    ("CatalogService", "getChildElementsWithPurviewTypeRead"),
    ("CatalogService", "getCatalogElementById"),
    ("CatalogService", "getCatalogElementPath"),
    ("MetadataService", "searchByKeywords"),
    ("SpreadsheetReportModule", "openQueryInPage"),
}


def _origin(url: str):
    parts = urlsplit(url)
    return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


class _ApplicationRedirects(HTTPRedirectHandler):
    def __init__(self, base_url: str):
        self.base_url = base_url

    def validate(self, url: str) -> str:
        parts = urlsplit(url)
        decoded = parts.path
        for _ in range(5):
            plain = unquote(decoded)
            if plain == decoded:
                break
            decoded = plain
        else:
            raise ProtocolError("拒绝过度编码的 Smartbi 响应地址。")
        segments = decoded.replace("\\", "/").split("/")
        application = urlsplit(self.base_url).path.rstrip("/") + "/"
        if (
            _origin(url) != _origin(self.base_url)
            or parts.username is not None
            or parts.password is not None
            or not parts.path.startswith(application)
            or ".." in parts.path.split("/")
            or any(segment.split(";", 1)[0] == ".." for segment in segments)
            or any(ord(character) < 32 for character in decoded)
        ):
            raise ProtocolError("拒绝跟随离开已配置 Smartbi 应用的响应地址。")
        return url

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.validate(urljoin(req.full_url, newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class SmartBIClient:
    """One configuration, one independent cookie jar. Public calls are serialized.

    Supported initially: one worksheet of a non-paginated V7 spreadsheet report.
    Browser sessions, portal SSO tokens and JavaScript macros are not replayed.
    """

    def __init__(self, config: SmartBIConfig):
        self.config = config
        self._cookies = http.cookiejar.CookieJar()
        self._redirects = _ApplicationRedirects(config.base_url)
        self._opener = build_opener(HTTPCookieProcessor(self._cookies), self._redirects)
        self._vision_url = config.base_url + "/vision/"
        self._authenticated = False
        self._lock = threading.RLock()

    @classmethod
    def from_config(cls, path: str | Path) -> SmartBIClient:
        return cls(SmartBIConfig.from_file(path))

    def __enter__(self) -> SmartBIClient:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._authenticated:
                try:
                    self._rpc("UserService", "logout", [], timeout=min(10, self.config.timeout))
                except SmartBIError:
                    pass
            self._cookies.clear()
            self._authenticated = False

    def _request(self, url: str, form: dict | None = None, timeout: float | None = None) -> tuple[str, str]:
        self._redirects.validate(url)
        data = urlencode(form).encode("utf-8") if form is not None else None
        headers = {"User-Agent": "kk-smartbi-sdk/0.5.1", "Accept": "application/json,text/html"}
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"
        try:
            with self._opener.open(Request(url, data=data, headers=headers), timeout=timeout or self.config.timeout) as response:
                final_url = self._redirects.validate(response.geturl())
                charset = response.headers.get_content_charset() or "utf-8"
                raw = response.read(self.config.max_response_bytes + 1)
                if len(raw) > self.config.max_response_bytes:
                    raise ResourceLimitError("Smartbi 单次响应超过 max_response_bytes 上限，请缩小查询范围。")
                return raw.decode(charset), final_url
        except HTTPError as exc:
            if exc.code in {401, 403}:
                self._authenticated = False
                raise AuthenticationError(f"Smartbi 拒绝访问，HTTP {exc.code}。") from None
            raise TransportError(f"Smartbi 请求失败，HTTP {exc.code}。") from None
        except (TimeoutError, socket.timeout):
            raise QueryTimeoutError("Smartbi 请求超时；执行状态未知，SDK 不自动重复查询。") from None
        except URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise QueryTimeoutError("Smartbi 请求超时；执行状态未知，SDK 不自动重复查询。") from None
            raise TransportError("无法连接 Smartbi，请检查地址、网络或 TLS 证书。") from None
        except (UnicodeError, LookupError):
            raise ProtocolError("Smartbi 响应字符编码无法解码。") from None
        except OSError:
            raise TransportError("Smartbi 连接被中断；请求执行状态未知。") from None

    def _rpc(self, service: str, method: str, params: list, timeout: float | None = None):
        for attempt in range(2):
            try:
                return self._rpc_once(service, method, params, timeout)
            except AuthenticationError:
                self._authenticated = False
                if attempt or (service, method) not in _SESSION_RECOVERABLE_RPCS:
                    raise
                # Only pre-refresh metadata operations may be replayed. Session
                # renewal cannot resurrect an existing report/parameter context.
                self._login(timeout=timeout)

    def _rpc_once(self, service: str, method: str, params: list, timeout: float | None = None):
        text, _ = self._request(
            self._vision_url + "RMIServlet",
            {"className": service, "methodName": method, "params": json.dumps(params, ensure_ascii=False)},
            timeout=timeout,
        )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            self._authenticated = False
            raise AuthenticationError("RPC 未返回 JSON，可能是登录失效、单点登录跳转或错误页面。") from None
        if not isinstance(payload, dict) or "retCode" not in payload:
            raise ProtocolError("RPC 响应缺少 retCode。")
        if isinstance(payload["retCode"], bool) or payload["retCode"] not in (0, "0"):
            code = str(payload["retCode"])
            if any(value in code.upper() for value in ("NOT_LOGIN", "NOT_LOGGED", "SESSION", "LOGIN_REQUIRED")):
                self._authenticated = False
                raise AuthenticationError("Smartbi 会话失效；当前步骤不能安全恢复，或一次续登后仍然失败。")
            # Do not echo arbitrary server text: it may contain the submitted password.
            raise ProtocolError(f"Smartbi 调用 {service}.{method} 失败；请由管理员核对服务端日志。")
        return payload.get("result")

    def _login(self, timeout: float | None = None) -> None:
        if not self._authenticated:
            result = self._rpc("UserService", self.config.login_method, [self.config.username, self.config.password], timeout=timeout)
            if result is not True:
                raise AuthenticationError("Smartbi 登录返回非成功结果。请核对真实账号密码及认证方式；门户 URL 中的临时凭据不可视为固定密码。")
            self._authenticated = True

    @staticmethod
    def _resource_id(value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ParameterError("资源 ID 必须是非空字符串。")
        return value

    def _catalog_element(self, resource_id: str) -> dict:
        value = self._rpc("CatalogService", "getCatalogElementById", [resource_id])
        if value is None:
            raise ResourceNotFoundError("资源不存在或当前账号无权访问。")
        if not isinstance(value, dict) or value.get("id") != resource_id:
            raise ProtocolError("目录详情响应未返回目标资源。")
        return value

    def _catalog_path(self, resource_id: str) -> list[dict]:
        path = validate_elements(self._rpc("CatalogService", "getCatalogElementPath", [resource_id]))
        if not path or path[-1]["id"] != resource_id:
            raise ProtocolError("服务端未返回包含目标资源的完整目录路径。")
        return path

    def get_report_info(self, report_id: str) -> dict:
        """Resolve a report ID to its display name, full Smartbi path and type."""
        self._resource_id(report_id)
        with self._lock:
            self._login()
            element = self._catalog_element(report_id)
            if element.get("type") not in REPORT_TYPES:
                raise UnsupportedReportError("该资源不是当前目录接口识别的报表类型；文件夹请使用 browse_catalog。")
            return entry(element, self._catalog_path(report_id))

    def check_connection(self) -> dict:
        """Authenticate and read catalog roots; never execute business report SQL."""
        with self._lock:
            timeout = min(10, self.config.timeout)
            self._login(timeout=timeout)
            roots = validate_elements(self._rpc("CatalogService", "getRootElements", [], timeout=timeout))
            return {"authenticated": True, "catalog_root_count": len(roots), "business_query_executed": False}

    @staticmethod
    def _paging(settings: dict) -> bool:
        value = settings.get("paging", False)
        if isinstance(value, str):
            value = value.lower()
        if value in (None, False, 0, "0", "false"):
            return False
        if value in (True, 1, "1", "true"):
            return True
        raise ProtocolError("无法识别报表分页标志，拒绝将局部结果当作完整数据。")

    @staticmethod
    def _sheets(context: dict) -> list[dict]:
        names = context.get("sheetNames") or []
        visible = context.get("visibleSheetNames")
        if not isinstance(names, list) or any(not isinstance(name, str) or not name for name in names):
            raise ProtocolError("工作表名称响应不是有效列表。")
        if visible is not None and (not isinstance(visible, list) or any(name not in names for name in visible)):
            raise ProtocolError("可见工作表响应与工作表名单不一致。")
        return [{"index": index, "name": name, "visible": name in visible if visible is not None else None,
                 "active": index == context.get("activeSheetIndex", 0)} for index, name in enumerate(names)]

    def describe_report(self, report_id: str) -> dict:
        """Describe query support, parameters and worksheet visibility without refreshing data."""
        with self._lock:
            info = self.get_report_info(report_id)
            if info["resource_type"] != "SPREADSHEET_REPORT":
                return {**info, "query_supported": False, "unsupported_reason": "当前仅支持非分页电子表格分组表。",
                        "parameters_available": False, "parameters": None, "sheets": [], "business_query_executed": False}
            with self._report(report_id) as context:
                settings = context.get("settings") or {}
                supported = str(settings.get("reportType")) == "0" and not self._paging(settings)
                definitions = [item.to_dict() for item in self._definitions(context)]
                for item in definitions:
                    item.pop("component_definition", None)
                    item.pop("choices", None)
                return {**info, "query_supported": supported,
                        "unsupported_reason": None if supported else "分页表或其他电子表格类型尚未适配。",
                        "parameters_available": True, "parameters": definitions, "parameter_count": len(definitions),
                        "has_parameters": bool(definitions), "sheets": self._sheets(context),
                        "business_query_executed": False, "result_formats": ["auto", "records", "grid"]}

    def browse_catalog(self, folder_id: str | None = None) -> list[dict]:
        """List one folder level. None means the 资源定制 resource root.

        Only CatalogService's read-permission child listing is used. This never
        opens spreadsheet contexts, fetches parameters or executes reports.
        """
        if folder_id is not None:
            self._resource_id(folder_id)
        with self._lock:
            self._login()
            if folder_id is None:
                roots = validate_elements(self._rpc("CatalogService", "getRootElements", []))
                root = next((item for item in roots if item["id"] == "DEFAULT_TREENODE"), None)
                if root is None:
                    raise ResourceNotFoundError("当前账号目录中没有资源定制根节点。")
                folder_id, parent_path = root["id"], [root]
            else:
                folder = self._catalog_element(folder_id)
                if not str(folder.get("type", "")).endswith("TREENODE"):
                    raise ParameterError("folder_id 必须是目录节点 ID，而不是报表 ID。")
                parent_path = self._catalog_path(folder_id)
            children = validate_elements(self._rpc("CatalogService", "getChildElementsWithPurviewTypeRead", [folder_id]))
            if len({item["id"] for item in children}) != len(children):
                raise ProtocolError("目录子节点响应包含重复资源 ID。")
            return [entry(child, parent_path + [child]) for child in children]

    def search_reports(
        self,
        keyword: str,
        *,
        folder_id: str | None = None,
        resource_types: list[str] | tuple[str, ...] | None = None,
        page_size: int = 100,
        max_results: int | None = None,
    ) -> list[dict]:
        """Search metadata using the deployment's start=0/maxRows interface.

        Grow the requested prefix when it fills, preserving same-name IDs/paths.
        max_results bounds matching results; None collects up to the 10000 guard.
        """
        if not isinstance(keyword, str) or not keyword.strip():
            raise ParameterError("搜索关键词不能为空；浏览目录请使用 browse_catalog。")
        if not isinstance(page_size, int) or isinstance(page_size, bool) or not 1 <= page_size <= 1000:
            raise ParameterError("page_size 必须是 1 到 1000 的整数。")
        if max_results is not None and (type(max_results) is not int or not 1 <= max_results <= 10000):
            raise ParameterError("max_results 必须是 1..10000 的整数或 None。")
        types = list(REPORT_TYPES if resource_types is None else resource_types)
        if not types or any(value not in REPORT_TYPES for value in types):
            raise ParameterError("resource_types 必须是非空的已支持报表类型列表。")
        if folder_id is not None:
            self._resource_id(folder_id)
        with self._lock:
            self._login()
            if folder_id is not None:
                folder = self._catalog_element(folder_id)
                if not str(folder.get("type", "")).endswith("TREENODE"):
                    raise ParameterError("搜索范围必须使用目录节点 ID。")
            # This deployment clamps out-of-range offsets to an earlier tail.
            # Its own UI uses start=0/maxRows. Grow that supported prefix instead
            # of treating a repeated tail as either failure or end-of-results.
            window, previous, paths = page_size, set(), {}
            while True:
                rows = validate_elements(self._rpc("MetadataService", "searchByKeywords", [keyword.strip(), types, "+", 0, window]))
                identifiers = [item["id"] for item in rows]
                if len(rows) > window or len(set(identifiers)) != len(identifiers):
                    raise ProtocolError("搜索响应超过请求上限或包含重复资源 ID。")
                if not previous.issubset(identifiers):
                    raise ProtocolError("搜索结果在获取期间发生变化，不能确认完整结果。")
                reports = []
                for item in rows:
                    if item.get("type") not in types:
                        continue
                    if item["id"] not in paths:
                        paths[item["id"]] = self._catalog_path(item["id"])
                    path = paths[item["id"]]
                    if folder_id is not None and folder_id not in [node["id"] for node in path[:-1]]:
                        continue
                    reports.append(entry(item, path))
                    if max_results is not None and len(reports) >= max_results:
                        return reports
                if len(rows) < window:
                    return reports
                if window >= 10000:
                    raise ResourceLimitError("搜索达到 10000 项，无法确认全部结果，请限定目录或关键词。")
                previous = set(identifiers)
                window = min(10000, max(window * 2, max_results or 0))

    @contextmanager
    def _report(self, report_id: str):
        if not isinstance(report_id, str) or not report_id.strip():
            raise ParameterError("report_id 必须是非空资源 ID。")
        self._login()
        result = self._rpc("SpreadsheetReportModule", "openQueryInPage", [report_id, None])
        context = result[0] if isinstance(result, list) and result else result
        if not isinstance(context, dict) or not context.get("clientId"):
            raise ProtocolError("报表响应缺少 clientId；资源可能不是受支持的电子表格。")
        try:
            yield context
        finally:
            try:
                self._rpc("SpreadsheetReportModule", "closeQuery", [context["clientId"]], timeout=min(10, self.config.timeout))
            except SmartBIError:
                pass

    @staticmethod
    def _definitions(context: dict) -> list[ParameterDefinition]:
        if "outputParameters" in context and context["outputParameters"] is None:
            # This V7 deployment explicitly emits null for parameterless reports.
            # Missing/malformed fields remain protocol errors rather than no-params.
            layout = (context.get("settings") or {}).get("paramsLayout")
            if layout not in (None, "", [], "[]", "[[]]"):
                raise ProtocolError("参数定义为 null，但参数布局非空，不能确认这是无参数报表。")
            return []
        raw = context.get("outputParameters")
        if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
            raise ProtocolError("报表响应缺少有效 outputParameters，拒绝猜测参数定义。")
        hidden = (context.get("settings") or {}).get("hiddenParams") or []
        if not isinstance(hidden, list) or any(not isinstance(item, str) for item in hidden):
            raise ProtocolError("hiddenParams 不是有效列表。")
        identifiers = [item.get("id") for item in raw]
        if any(not isinstance(item, str) or not item for item in identifiers) or len(set(identifiers)) != len(identifiers):
            raise ProtocolError("参数定义缺少有效 ID 或包含重复 ID。")
        return [ParameterDefinition.from_server(item, hidden) for item in raw]

    def get_parameters(self, report_id: str, *, include_choices: bool = False) -> list[ParameterDefinition]:
        """Discover definitions for this ID. No report ID or date parameter is built in."""
        with self._lock, self._report(report_id) as context:
            definitions = self._definitions(context)
            if include_choices and definitions:
                self._apply_choice_context(context, definitions, {})
                controls = {"COMBOBOX", "LISTDIALOG", "LISTPANEL"}
                definitions = [replace(item, choices=self._choices(context, item)) if str(item.control_type).upper() in controls else item for item in definitions]
            return definitions

    def _choices(self, context: dict, definition: ParameterDefinition) -> list:
        panel = context.get("parameterPanelClientId")
        if not panel:
            raise ProtocolError("参数面板上下文不存在，无法获取备选值。")
        result = self._rpc("ParameterPanelService", "getParamStandbyValue", [panel, definition.id])
        if not isinstance(result, list):
            raise ProtocolError("参数备选值响应不是列表。")
        return result

    def _apply_choice_context(self, context: dict, definitions: list[ParameterDefinition], params: Mapping[str, Any]) -> None:
        resolved = resolve_parameters(definitions, params, self.config.fixed_parameters, check_required=False)
        if not context.get("parameterPanelClientId"):
            raise ProtocolError("报表未返回参数面板上下文。")
        supplied_ids = {find_parameter(definitions, key).id for key in (*self.config.fixed_parameters, *params)}
        for item in resolved:
            if item["id"] in supplied_ids:
                accepted = self._rpc("ParameterPanelService", "setParamValue", [context["parameterPanelClientId"], item["id"], item["value"], item["displayValue"]])
                if accepted is False:
                    raise ParameterError(f"服务器未接受父级参数 {item['name']!r}，拒绝返回上下文不确定的备选值。")

    def get_parameter_choices(self, report_id: str, parameter: str, *, params: Mapping[str, Any] | None = None) -> list:
        """Use already-selected parent values when asking for dependent choices."""
        with self._lock, self._report(report_id) as context:
            definitions = self._definitions(context)
            definition = find_parameter(definitions, parameter)
            self._apply_choice_context(context, definitions, params or {})
            return self._choices(context, definition)

    def query_report(
        self,
        report_id: str,
        params: Mapping[str, Any] | None = None,
        *,
        layout: TableLayout | None = None,
        sheet_index: int | None = None,
        result_format: str = "records",
    ) -> dict:
        if result_format not in {"auto", "records", "grid"}:
            raise ParameterError("result_format 仅支持 auto、records、grid。")
        if sheet_index is not None and (type(sheet_index) is not int or sheet_index < 0):
            raise ParameterError("sheet_index 必须是非负整数，不能是布尔值。")
        with self._lock, self._report(report_id) as context:
            settings = context.get("settings") or {}
            if str(settings.get("reportType")) != "0" or self._paging(settings):
                raise UnsupportedReportError("第一版仅支持非分页的电子表格分组表，避免将第一页误当完整数据。")
            index = context.get("activeSheetIndex", 0) if sheet_index is None else sheet_index
            sheets = self._sheets(context)
            names = [sheet["name"] for sheet in sheets]
            if type(index) is not int or index < 0 or (names and index >= len(names)):
                raise ParameterError("sheet_index 超出报表工作表范围。")
            if sheets and sheets[index]["visible"] is False:
                raise ParameterError("该工作表被标记为不可见，请通过 describe_report 选择可见工作表。")
            resolved = resolve_parameters(self._definitions(context), params if params is not None else {}, self.config.fixed_parameters)
            params_json = json.dumps(resolved, ensure_ascii=False)
            if resolved:
                if self._rpc("SpreadsheetReportModule", "setParamsInfo", [context["clientId"], params_json]) is False:
                    raise ParameterError("Smartbi 拒绝设置本次查询参数，未执行刷新。")
            html, url = self._request(self._vision_url + "ssreportServlet", {
                "resid": report_id,
                "clientId": context["clientId"],
                "sheetIndex": index,
                "pageId": 0,
                "refreshType": "refresh",
                "paramsInfo": params_json,
                "writeBackData": "",
            })
            for _ in range(5):
                target = result_redirect(html)
                if target is None:
                    break
                html, url = self._request(urljoin(url, target))
            else:
                raise ProtocolError("报表结果重定向超过支持次数。")
            if result_format == "grid":
                parsed = parse_report_grid(html)
            else:
                try:
                    parsed = {"table_format": "records", **parse_report_html(html, layout, allow_heuristic=result_format == "records")}
                except ReportParseError as exc:
                    if result_format == "records":
                        raise
                    parsed = parse_report_grid(html, layout_warning=str(exc))
            return {
                "report_id": report_id,
                "parameters": resolved,
                "sheet_index": index,
                "sheet_name": names[index] if names else None,
                "source": "smartbi_http",
                "truncated": False,
                **parsed,
            }
