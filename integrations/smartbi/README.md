# Smartbi Python SDK 与 MCP

独立、可选的 Smartbi 报表连接器，版本 `0.5.1`。核心 SDK 仅依赖 Python 3.10+ 标准库；MCP 通过可选依赖安装，已使用 Python 3.12 和 `mcp==2.3.0` 验证。不会自动安装到 Codata，也不预置组织服务、账号、报表或业务数据。

当前适配范围是 Smartbi 7.2 的非分页电子表格分组表。目录可以发现其他资源类型，但不能据此认定查询支持仪表盘、分页清单表或其他版本。此实现使用该类部署的客户端协议，并非 Smartbi 官方 SDK。

## 安装与配置

从 Codata 仓库根目录运行：

```powershell
py -3.12 -m venv .venv-smartbi
.\.venv-smartbi\Scripts\python.exe -m pip install "./integrations/smartbi[mcp]"
Copy-Item integrations/smartbi/config.example.ini integrations/smartbi/config.ini
```

仅使用 Python SDK 时去掉 `[mcp]`。如果之前通过 `PYTHONPATH` 运行另一份源码，安装前先清除该变量，避免旧包元数据干扰版本识别。Linux/macOS 可用 `python3.12 -m venv`，并将 Python 路径改为 `.venv-smartbi/bin/python`。

在被忽略的本地 `config.ini` 中填写实际部署：

```ini
[smartbi]
base_url = https://smartbi.example.com/smartbi
username =
password =
login_method = clickLogin
timeout = 600
max_response_bytes = 16777216

[fixed_parameters]
```

`base_url` 必须替换为实际应用地址，账号由使用方配置，建议使用仅具备目标报表读取权限的账号。`SMARTBI_BASE_URL`、`SMARTBI_USERNAME`、`SMARTBI_PASSWORD` 可覆盖文件配置。INI 使用 UTF-8 无 BOM，密码中的 `%`、`#` 不做插值。

不读取浏览器 cookie，不重放门户 SSO，也不把访问链接中的临时密码视为可复用账号密码。`fixed_parameters` 仅填写报表元数据中存在的固定业务参数；查询方不能覆盖它们，固定参数不等于终端用户权限隔离。

## Python 使用方式

```python
from kk_smartbi import SmartBIClient

with SmartBIClient.from_config("integrations/smartbi/config.ini") as client:
    reports = client.search_reports("业务进度")
    for report in reports:
        print(report["name"], report["path"], report["id"])

    # 依据名称和完整路径选择，不默认取同名结果的第一项。
    candidates = [item for item in reports if item["name"] == "业务进度报表"]
    if len(candidates) != 1:
        raise ValueError("请根据名称、完整路径和 ID 明确选择报表")
    report_id = candidates[0]["id"]
    description = client.describe_report(report_id)
    if not description["query_supported"]:
        raise ValueError(description["unsupported_reason"])

    print(description["parameters"])
    print(description["sheets"])
    # 按此 ID 的实际参数定义提供条件；无参数时为 {}。
    result = client.query_report(report_id, params={}, result_format="auto")
    print(result["table_format"], result["rows"])
```

| SDK 方法 | 用途 |
| --- | --- |
| `browse_catalog(folder_id=None)` | 浏览一层目录，返回名称、完整路径、ID 和类型。 |
| `search_reports(keyword, ..., max_results=None)` | 搜索元数据；保留同名但不同路径的报表，可限定目录和类型。 |
| `get_report_info(report_id)` | 读取名称、完整路径、类型和定义修改时间。 |
| `describe_report(report_id)` | 读取支持范围、参数和工作表可见性，不刷新业务报表。 |
| `get_parameters(report_id)` | 获取真实参数 ID/name/alias、类型、必填状态和默认值。 |
| `get_parameter_choices(report_id, parameter, params=None)` | 读取备选值，`params` 可提供级联父条件。 |
| `query_report(report_id, params=None, ...)` | 查询一个可见工作表，返回记录或坐标单元格。 |
| `check_connection()` | 验证认证和目录读取，不刷新业务报表。 |

参数名保留原始标点；可以使用参数 ID、真实名称或唯一别名。未知参数、重复指定、缺少已知必填值及覆盖固定参数均报错。需要真实值/显示值分开的控件可传 `{"value": "内部值", "displayValue": "显示值"}`。多选字符串、树控件和动态规则仍需按实际部署验证，SDK 不猜格式。

显式 `outputParameters: null` 或空列表可表示无参数；定义缺失、与非空参数布局冲突或重复 ID 仍报错。没有日期参数时不能人为添加日期条件筛选历史数据。报表定义修改时间不等于数据更新时间；“当前数据”需要按新查询的实际结果及报表显示的更新时间说明。

## 在 Codata 中使用 MCP

调用链是 `Codata → MCP → SDK → Smartbi`。不需要改动 Codata 后端依赖或业务代码；在 MCP 管理页按使用方环境配置连接器。此连接器提供自己的报表查询工具，不承诺兼容 Codata 的 SQL `run_query` 或自动刷新看板协议。

本地 stdio：

```powershell
.\.venv-smartbi\Scripts\python.exe -m kk_smartbi.mcp_server --config "C:/path/to/smartbi/config.ini"
```

通常由支持 stdio MCP 的客户端启动该进程。参考 [客户端配置示例](examples/mcp-client.example.json)，将 Python 和配置文件路径替换为实际绝对路径；密码只由服务端读取，不放进工具参数。

需要局域网 Streamable HTTP 时，绑定实际网卡 IP 并逐个列出调用方 IP：

```text
python -m kk_smartbi.mcp_server --config /absolute/path/config.ini \
  --transport streamable-http --host <server-lan-ip> --port 8765 \
  --allow-client <client-ip>
```

这是跨平台命令示意，PowerShell 可写成一行。Codata 中填写 `http://<server-lan-ip>:8765/mcp`，同时按实际调用方设置入站防火墙。此内网入口用实际 TCP 对端 IP 和 Host/Origin 检查限制访问，不接受 `X-Forwarded-For` 冒充；不提供 TLS 或 Bearer Token 验证。外部访问需要使用方部署合适的鉴权和传输保护，不能把无鉴权入口直接作为公网服务。

### 11 个只读工具

| 工具 | 用途 |
| --- | --- |
| `browse_catalog` | 按文件夹 ID 浏览目录。 |
| `search_reports` | 按关键词搜索名称、路径和 ID。 |
| `get_report_info` | 获取报表基础详情。 |
| `get_parameters` | 获取真实参数定义。 |
| `get_parameter_choices` | 获取下拉或级联备选值。 |
| `describe_report` | 一次获取支持范围、参数和工作表名单。 |
| `health_check` | 区分 MCP 本身与上游 Smartbi 的状态。 |
| `query_report` | 同步查询，返回快照 `query_id`。 |
| `start_query` | 提交异步任务，立即返回 `query_id`。 |
| `get_query_status` | 查看任务状态，可等待 0..25 秒。 |
| `get_query_result` | 从快照分页和裁剪行列，不重新执行报表。 |

推荐慢查询流程：

```text
search_reports → describe_report → start_query
  → get_query_status → get_query_result
```

用户可以只说“查询业务进度报表的当前数据”，由 AI 内部解析 ID；多个同名结果需按路径选择。先确认 `query_supported=true` 和工作表可见性，参数来自 `describe_report`。无参数时传 `{}`，不能猜日期字段。`get_query_status` 返回 `queued/running` 时等待，不重复提交；相同输入的进行中异步任务会去重。

后续分页使用同一 `query_id` 和 `next_offset`，不要重新调用 `query_report`。`source_requeried=false`、相同的 `snapshot_created_at` 表示同一快照。同步查询被另一业务查询占用时返回 `BUSY`，可使用异步队列。元数据使用独立 SDK 会话，本地状态及快照读取不依赖业务查询会话。

### 输出与限制

- Python SDK 默认 `records` 严格映射字段；MCP 默认 `auto`，无法可靠识别表头时用同一次查询返回 `grid`，不反复试布局。可显式选择 `grid`。
- 单元格保留显示字符串，不自动转换金额精度、前导零或百分比。`grid` 包含零起始 `row_index/column_index` 和 `text/rowspan/colspan`；坐标是可见 HTML 表坐标，不是 Excel 原生地址。
- `grid.data_row_count=null` 不表示无数据；`table_row_count` 包含标题、表头和页脚，不能当作业务明细数。空格不等于零。普通空白 1×1 格默认省略，可用 `omit_empty_cells=false` 保留；跨页父合并格在 `merged_context` 中返回。
- 默认请求 50 行，上限 200 行；字符预算默认 40000。超预算时减少返回行数并标记 `response_limited`，不截断单元格。`column_offset/column_limit` 支持裁剪列，`include_totals=false` 可省略合计。
- `totals_scope=entire_query` 表示合计来自完整查询，不是当前页小计。`truncated=true` 或列被裁剪时不能声称结果完整。目录/搜索/备选值每页上限 200 项；搜索未到末尾时总量为未知，不把局部计数当总数。
- 只运行一个业务查询 worker。默认有效快照 900 秒，最多 8 个任务/快照，序列化容量上限 32 MiB。过期、回收或进程重启返回 `QUERY_NOT_FOUND`，不静默重查。TTL 在后续访问时清理；序列化预算不是进程 RSS 上限。
- 快照反映查询时的数据和账号权限，不在每次读取时重新验权。共享账号模式不提供终端用户权限隔离；权限变更需清除快照或停止服务。
- 单次 HTTP 响应默认最多 16 MiB；展开表格最多 250000 个单元格；HTML 节点和深度有限制。达到上限明确失败，不返回貌似完整的局部数据。
- 会话过期时，仅目录/搜索/打开报表这些刷新前步骤允许一次续登后重试。参数上下文、业务刷新及网络超时均不自动重试，也不宣称能取消已运行的 SQL。
- 分页清单、仪表盘、不可见工作表、浏览器脚本及复杂控件不在当前已验证范围。服务器可能计算参数默认值；读取描述不等于运行报表刷新。

容量参数见 `python -m kk_smartbi.mcp_server --help`。工具直接执行失败以 `isError=true` 和 JSON `error.code/message/retryable/next_action` 表达。异步任务失败通过 `status=failed` 和 `error` 表达；读取到失败状态不等于业务查询成功。`health_check` 默认只检查 MCP；显式 `check_smartbi=true` 后还要看 `smartbi.ok`，不能把上游问题直接说成 MCP 进程故障。

## CLI 与验证

```powershell
kk-smartbi --config integrations/smartbi/config.ini search 业务进度
kk-smartbi --config integrations/smartbi/config.ini describe <report-id>
kk-smartbi --config integrations/smartbi/config.ini query <report-id> --params-file integrations/smartbi/examples/date-2026-10-20.params.json --format auto
```

独立示例：

```text
python examples/query_report.py --config config.ini --report-id <report-id>
python examples/search_and_query_report.py --config config.ini --keyword 示例 --report-name 示例项目B2C发货监控表
python examples/verify_mcp_async.py --url http://<server-lan-ip>:8765/mcp --report-id <report-id> --output results/query.json
```

这些示例需切到本模块目录，名称、ID、布局和参数都需按实际部署替换。日期文件只是演示，不代表每张报表都接受相同日期参数。

本地测试无需真实账号或网络服务：

```powershell
Set-Location integrations/smartbi
$env:PYTHONPATH = (Join-Path $PWD 'src')
$env:PYTHONDONTWRITEBYTECODE = '1'
python -m unittest discover -s tests -v
```

测试使用明确标注的合成 HTTP 响应和可注入 SDK，覆盖认证、参数、目录、表格、资源限制、快照、并发及 MCP 结构化返回。安装 `[mcp]` 后运行全部 84 项测试；未安装时跳过 19 项 MCP 测试。本地测试不能证明所有 Smartbi 部署均兼容，真实控件和版本仍需使用方验证。

代码按本仓库 [Apache-2.0](LICENSE) 许可。核心使用标准库；可选依赖为 [官方 MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)，通过安装依赖获取，本目录不包含其二进制或用户环境。
