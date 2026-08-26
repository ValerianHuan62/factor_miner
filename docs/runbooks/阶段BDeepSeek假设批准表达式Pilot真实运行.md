# 阶段 B：DeepSeek 假设、人工批准与三候选 Pilot

## 研究边界

阶段 B 的输出仍然只能称为“通过冻结可见协议计算的候选因子诊断”。DeepSeek 生成的是
待审阅的经济假设和三个 typed AST 设计，不是有效 Alpha、认证因子或因果机制证明。
即使三候选全部表现良好，也不能据此扩大研究结论；机制状态固定为
`mechanism_unverified`，需要独立机制检验。

DeepSeek 只能收到公开研究简报、已核验来源标识、字段能力摘要、允许算子和冻结输出
Schema。请求不得包含 QuantLake、个股、原始数值、标签、IC、收益、Sharpe、回撤、Barra
或服务器路径。真实表达式计算、IC、组合回测和发布只在公司 Linux 执行；Mac 只能做合成
测试和 SSH 隧道访问界面。

本阶段不读取、不修改、不追加旧的 `llmfamily_1bae19965638a6ac9620e0b0`，不使用
正式 120 槽研究族，不生成 generation seal。所有阶段 B 控制产物写到独立的显式
`FM_PILOT_STAGE_B_ROOT`。

## 两个批准闸门

“外发授权”和“研究批准”是两个不同动作：

1. 外发授权只允许固定请求哈希对应的脱敏字节发送给 DeepSeek。
2. 研究批准由人工审阅假设的主张、机制、预期方向、代理、独立验证、竞争解释、失效
   方式和证伪路径后完成。未批准的假设不能生成表达式。

表达式请求批准后，程序固定生成恰好三个槽位；每个响应先经过字段白名单、typed AST、
lookback、可得性、未来函数和 compiler 硬校验，再交给阶段 A 的同一个
`run_fixed_pilot`。模型不能修改评价政策、交易日历、预算或结果；semantic lint 只保留
为非阻塞诊断。

## 服务器私有文件

以下文件必须在服务器私有目录生成或保存，不能进入 Git：

- 公开研究简报：包含 `public_brief` 和已核验的 `verified_source_refs`；
- 公司 DeepSeek 外发政策；
- 每个请求对应的精确 `LLMExportAuthorization`；
- 服务器字段可用性注册表；
- 阶段 A 的 `PilotServerConfig`，包含显式 QuantLake、交易日历、CSI300、状态表、产物
  根目录、可见区间、代码提交和配置哈希；
- `DEEPSEEK_API_KEY`，只从服务器私有环境读取，不写入命令行和日志。

`PilotServerConfig` 的 JSON 结构为：

```json
{
  "paths": {
    "quantlake_root": "<服务器 QuantLake 根目录>",
    "release_manifest_uri": "<显式 release 清单>",
    "state_manifest_uri": "<显式状态清单>",
    "market_uri": "<显式行情入口>",
    "state_uri": "<显式状态表>",
    "field_registry_uri": "<显式字段注册表>",
    "benchmark_root": "<显式基准派生根目录>",
    "calendar_root": "<显式日历派生根目录>",
    "calendar_uri": "<显式交易日历>",
    "calendar_manifest_uri": "<日历发布 manifest，含 source_sha256 与日期分区>",
    "calendar_version": "<冻结日历版本>",
    "benchmark_uri": "<显式 CSI300 开盘价>",
    "benchmark_manifest_uri": "<基准发布 manifest，含 source_sha256、date_column 与日期分区>",
    "benchmark_schema_version": "<冻结基准 Schema>",
    "universe_root": "<可选历史成分根目录>",
    "universe_uri": "<可选历史成分>",
    "market_open_root": "<可选持有期开盘价根目录>",
    "market_open_uri": "<可选持有期开盘价>",
    "market_open_manifest_uri": "<启用持有期开盘价时必填的发布 manifest>",
    "barra_root": "<可选 Barra 派生数据根目录>",
    "barra_manifest_uri": "<启用 Barra 时必填的发布 manifest>",
    "barra_max_exposure_staleness_days": "<冻结的暴露陈旧天数上限>",
    "barra_exposure_uri": "<可选 Barra 暴露>",
    "barra_factor_returns_uri": "<可选 Barra 因子收益>",
    "barra_benchmark_weights_uri": "<可选 CSI300 Barra 基准权重>"
  },
  "request": {
    "visible_start": "<冻结可见起点>",
    "visible_end": "<冻结可见终点>",
    "candidate_file_sha256": "<先填 64 位占位哈希，运行前由程序替换>",
    "evaluation_policy_id": "<冻结评价政策 ID>",
    "data_release_id": "<服务器 release ID>",
    "code_commit": "<40 位代码提交>",
    "config_hash": "<配置内容 SHA-256>",
    "artifact_root": "<独立产物根目录>"
  }
}
```

普通 2021 至 2026 研究只打开上述发布 manifest 中与请求窗口重叠的 Parquet
分区。日历、基准或可选持有期开盘价缺少 manifest 时硬失败，不得通过遍历全历史
文件计算身份哈希，也不得回退读取 2012 分区。启用 Barra 时，`barra_manifest_uri` 必须以
`barra-source-manifest-v1` 显式绑定暴露、因子收益和基准权重的相对路径与
SHA256。方向冻结前只读该 manifest metadata；所有候选方向文件原子落盘并
回读后，才允许打开 Barra 真实文件并执行存在性、流式哈希与 Schema 合同检查。
正式复算部署目录必须保留只读 `.git` metadata；命令执行前核对真实 40 位
`HEAD`，并要求 `src`、`dashboard`、
`pyproject.toml`、`uv.lock` 和 `migrations` 相关树干净。缺少 `.git` 时硬失败。

所有路径必须显式提供且通过输入合同；缺状态表、交易日历、CSI300、字段映射或 cutoff
不一致时硬失败，不提供本地行情、`fillna(0)` 或默认路径兜底。

## 命令流程

先在服务器安装锁定依赖：

```bash
uv sync --frozen --extra dashboard
```

准备假设请求，不访问网络：

```bash
factor-miner pilot prepare-hypothesis \
  <public_brief.json> \
  --artifact-root <FM_PILOT_STAGE_B_ROOT> \
  --output <hypothesis_request.json>
```

人工核对命令输出的 DeepSeek 请求哈希后，使用现有精确授权命令：

```bash
factor-miner llm authorize-export \
  <hypothesis_request.json> \
  <corporate_external_policy.json> \
  --approved-request-sha256 <DeepSeek请求哈希> \
  --approver-role <角色> \
  --output <hypothesis_authorization.json>
```

执行假设生成。命令只输出调用 ID、响应哈希和状态，不输出响应正文：

```bash
factor-miner pilot generate-hypothesis \
  <hypothesis_request.json> \
  <corporate_external_policy.json> \
  <hypothesis_authorization.json> \
  --artifact-root <FM_PILOT_STAGE_B_ROOT> \
  --record-root <私有调用录制根目录> \
  --output <hypothesis_response.json>
```

人工审阅响应后，批准会写入不可变记录：

```bash
factor-miner pilot approve-hypothesis \
  <hypothesis_response.json> \
  --artifact-root <FM_PILOT_STAGE_B_ROOT> \
  --output <approved_hypothesis.json> \
  --approver-role <角色>
```

由批准假设构造三表达式请求，不访问网络：

```bash
factor-miner pilot prepare-expression \
  <approved_hypothesis.json> \
  <field_registry.json> \
  --artifact-root <FM_PILOT_STAGE_B_ROOT> \
  --output <expression_request.json>
```

再次为精确表达式请求哈希执行 `llm authorize-export`。然后用一条服务器命令完成“生成
三个表达式 → 本地硬校验 → 阶段 A 因子计算、IC、组合、Barra 可选状态 → 原子发布”：

```bash
factor-miner pilot run-approved \
  <expression_request.json> \
  <approved_hypothesis.json> \
  <corporate_external_policy.json> \
  <expression_authorization.json> \
  <field_registry.json> \
  <pilot_config.json> \
  --artifact-root <FM_PILOT_STAGE_B_ROOT> \
  --record-root <私有调用录制根目录> \
  --dsn <可选 Dashboard DSN>
```

数据库投影失败不能删除本地产物；恢复后使用阶段 A 的 `factor-miner pilot project` 单独
重试。DeepSeek 故障只保留请求摘要和失败状态，不生成伪候选；同一请求恢复时只能重放
同一请求哈希。

## 可视化操作台

只读结果 Dashboard 不提供批准、修改政策或重新回测按钮。阶段 B 使用独立的受控操作台：

```bash
streamlit run dashboard/stage_b_console.py --server.address 127.0.0.1 --server.port 8502
```

操作台必须显式配置以下服务器环境变量：

```text
FM_PILOT_STAGE_B_ROOT
FM_PILOT_POLICY_PATH
FM_PILOT_FIELD_REGISTRY_PATH
FM_PILOT_RECORD_ROOT
FM_PILOT_HYPOTHESIS_REQUEST_PATH
FM_PILOT_HYPOTHESIS_AUTHORIZATION_PATH
FM_PILOT_HYPOTHESIS_RESPONSE_PATH
FM_PILOT_APPROVAL_PATH
FM_PILOT_EXPRESSION_REQUEST_PATH
FM_PILOT_EXPRESSION_AUTHORIZATION_PATH
FM_PILOT_CONFIG_PATH
FM_PILOT_APPROVER_ROLE
```

操作台只通过上述正式 CLI 执行写操作，展示阶段状态、假设内容、批准身份、调用哈希、
三个表达式的硬校验结果和最终 `pilot_run_id`。浏览器不展示 DeepSeek 原始响应，也不
连接 QuantLake。

Mac 通过 SSH 隧道访问服务器操作台和只读 Dashboard；不要把密码写入脚本、`.env`、Git
或 Streamlit 配置。结果仍以服务器不可变 JSON/Parquet 产物为正式主存储。

## 完成判定

阶段 B 只有同时满足以下条件才算完成：

1. 一个全新的阶段 B `pilotstage_...` 身份完成假设请求、外发授权、人工批准和表达式请求；
2. DeepSeek 响应恰好包含三个槽位，三个设计均通过字段、lookback、前视、typed AST 和
   compiler 硬闸门；
3. 阶段 A 的 `run_fixed_pilot` 生成完整 IC、十组、多空、成本、日历年化指标和 Barra
   `not_available` 或合法归因；
4. `run_manifest.json` 通过逐文件哈希核验，PostgreSQL 可幂等重投影，操作台可展示状态；
5. 旧污染 family 的账本字节、QuantLake、固定 CSI300 和服务器更新脚本均未改变。

即使候选诊断表现很好，最终报告也只能写“通过冻结可见协议计算的候选因子诊断”，不能
写“有效 Alpha”“认证因子”或“生产结论”。
