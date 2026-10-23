"""Configuration and report parameter definitions; no network side effects."""

from __future__ import annotations

import configparser
import json
import math
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from .errors import ConfigurationError, ParameterError, ProtocolError


@dataclass(frozen=True)
class SmartBIConfig:
    base_url: str
    username: str
    password: str = field(repr=False)
    timeout: float = 180.0
    login_method: str = "clickLogin"
    fixed_parameters: Mapping[str, str] = field(default_factory=dict, repr=False)
    max_response_bytes: int = 16 * 1024 * 1024

    def __post_init__(self) -> None:
        parts = urlsplit(self.base_url)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or ".." in parts.path.split("/")
        ):
            raise ConfigurationError("base_url 必须是无账号、查询串和片段的 HTTP(S) 应用地址。")
        path = parts.path.rstrip("/")
        if path.endswith("/vision"):
            path = path[:-7]
        object.__setattr__(self, "base_url", urlunsplit((parts.scheme, parts.netloc, path, "", "")))
        if not self.username.strip() or not self.password:
            raise ConfigurationError("尚未配置 Smartbi 账号密码，请填写 config.ini 或 SMARTBI_USERNAME/SMARTBI_PASSWORD。")
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)) or not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ConfigurationError("timeout 必须是大于零的有限秒数。")
        if type(self.max_response_bytes) is not int or self.max_response_bytes < 1:
            raise ConfigurationError("max_response_bytes 必须是正整数。")
        if self.login_method not in {"clickLogin", "login"}:
            raise ConfigurationError("login_method 仅支持 clickLogin 或 login，不自动尝试其他认证方法。")

    @classmethod
    def from_file(cls, path: str | Path) -> SmartBIConfig:
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        try:
            with Path(path).open(encoding="utf-8") as stream:
                parser.read_file(stream)
            section = parser["smartbi"]
            timeout = float(section.get("timeout", "180"))
            max_response_bytes = int(section.get("max_response_bytes", str(16 * 1024 * 1024)))
        except (OSError, UnicodeError, configparser.Error, KeyError, ValueError) as exc:
            raise ConfigurationError("无法读取 UTF-8 INI 配置，或 [smartbi] 中的数值配置无效。") from exc
        fixed = dict(parser["fixed_parameters"]) if parser.has_section("fixed_parameters") else {}
        return cls(
            base_url=os.environ.get("SMARTBI_BASE_URL", section.get("base_url", "")),
            username=os.environ.get("SMARTBI_USERNAME", section.get("username", "")),
            password=os.environ.get("SMARTBI_PASSWORD", section.get("password", "")),
            timeout=timeout,
            login_method=section.get("login_method", "clickLogin"),
            fixed_parameters=fixed,
            max_response_bytes=max_response_bytes,
        )


def _boolean(value: Any) -> bool | None:
    if value is True or value == "true":
        return True
    if value is False or value == "false":
        return False
    return None


@dataclass(frozen=True)
class ParameterDefinition:
    id: str
    name: str
    alias: str
    data_type: str | None
    control_type: str | None
    required: bool | None
    default_value: Any
    default_display_value: Any
    component_definition: dict[str, Any]
    hidden: bool = False
    choices: Any = None

    @classmethod
    def from_server(cls, raw: dict[str, Any], hidden_ids: list[str]) -> ParameterDefinition:
        definition = raw.get("componentDefine") or {}
        if isinstance(definition, str):
            try:
                definition = json.loads(definition)
            except json.JSONDecodeError as exc:
                raise ProtocolError("服务器返回的参数 componentDefine 不是有效 JSON。") from exc
        if not isinstance(definition, dict) or not raw.get("id"):
            raise ProtocolError("服务器返回了缺少 id 或 componentDefine 非对象的参数。")
        required = _boolean(raw.get("required"))
        # This V7 client's buildParamsInfo rejects an empty value when noNull is false.
        # Follow observed client behaviour, not the misleading property name.
        allow_empty = _boolean(definition.get("noNull"))
        if required is None and allow_empty is not None:
            required = not allow_empty
        parameter_id = str(raw["id"])
        name = str(raw.get("name") or parameter_id.rsplit(".", 1)[-1])
        return cls(
            id=parameter_id,
            name=name,
            alias=str(raw.get("alias") or name),
            data_type=raw.get("dataType") or raw.get("type"),
            control_type=raw.get("componentType"),
            required=required,
            default_value=raw.get("value"),
            default_display_value=raw.get("displayValue", raw.get("value")),
            component_definition=definition,
            hidden=parameter_id in hidden_ids or name in hidden_ids,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def find_parameter(definitions: list[ParameterDefinition], key: str) -> ParameterDefinition:
    if not isinstance(key, str) or not key:
        raise ParameterError("参数标识必须是非空字符串。")
    for attribute in ("id", "name", "alias"):
        matches = [item for item in definitions if getattr(item, attribute) == key]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            ids = ", ".join(item.id for item in matches)
            raise ParameterError(f"参数标识 {key!r} 有歧义，请使用内部 ID：{ids}")
    raise ParameterError(f"该报表不存在参数 {key!r}，请先调用 get_parameters(report_id)。")


def resolve_parameters(
    definitions: list[ParameterDefinition],
    supplied: Mapping[str, Any],
    fixed: Mapping[str, Any] | None = None,
    *,
    check_required: bool = True,
) -> list[dict[str, Any]]:
    if not isinstance(supplied, Mapping):
        raise ParameterError("params 必须是参数名称到值的映射。")
    chosen: dict[str, Any] = {}
    fixed_ids: set[str] = set()
    for mapping, is_fixed in ((fixed or {}, True), (supplied, False)):
        seen: set[str] = set()
        for key, value in mapping.items():
            definition = find_parameter(definitions, key)
            if definition.id in seen:
                raise ParameterError(f"参数 {definition.name!r} 被重复指定。")
            if not is_fixed and definition.id in fixed_ids:
                raise ParameterError(f"配置中的固定参数 {definition.name!r} 不允许被查询参数覆盖。")
            seen.add(definition.id)
            chosen[definition.id] = value
            if is_fixed:
                fixed_ids.add(definition.id)
    resolved = []
    for definition in definitions:
        value = chosen.get(definition.id, definition.default_value)
        display = definition.default_display_value
        if definition.id in chosen:
            if isinstance(value, dict):
                if "value" not in value or set(value) - {"value", "displayValue"}:
                    raise ParameterError("参数对象仅支持 value 和 displayValue。")
                display = value.get("displayValue", value["value"])
                value = value["value"]
            else:
                display = value
        if isinstance(value, (list, tuple, set, dict)) or isinstance(display, (list, tuple, set, dict)):
            raise ParameterError(f"参数 {definition.name!r} 的多选/复合格式需按该报表定义传入真实字符串。")
        if any(isinstance(item, float) and not math.isfinite(item) for item in (value, display)):
            raise ParameterError(f"参数 {definition.name!r} 不能是 NaN 或无穷大。")
        if value is None:
            value = ""
        if display is None:
            display = value
        if check_required and definition.required and str(value).strip() == "":
            raise ParameterError(f"缺少必填参数：{definition.alias} ({definition.name})")
        resolved.append({"id": definition.id, "name": definition.name, "value": str(value), "displayValue": str(display)})
    return resolved


@dataclass(frozen=True)
class TableLayout:
    """Zero-based layout override for spreadsheet reports without semantic HTML headers."""

    header_start: int | None = None
    header_rows: int | None = None
    data_start: int | None = None
    data_end: int | None = None
    columns: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        for value in (self.header_start, self.header_rows, self.data_start, self.data_end):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("表格行号和表头行数必须为非负整数，不能是布尔值。")
        if self.header_rows is not None and self.header_rows < 1:
            raise ValueError("header_rows 必须大于零。")
        if self.columns is not None and (
            not isinstance(self.columns, (tuple, list)) or not self.columns
            or any(not isinstance(name, str) or not name.strip() for name in self.columns)
            or len(set(self.columns)) != len(self.columns)
        ):
            raise ValueError("columns 必须是非空、无重复名称的字符串列表。")
