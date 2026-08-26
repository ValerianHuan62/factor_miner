# 公司 A 股数据合同

本文件定义第一个部署 profile 的稳定合同，不保存服务器秘密或可变解析路径。真实 URI 只允许通过服务器私有配置显式提供；缺失即失败。

## 1. 运行位置

- 真实运行平台：公司 Linux 服务器。
- 上游根目录：`/data/quantlake`，研究身份只读。
- 若 QuantLake 原始分区不直接满足标准面板，可在 QuantLake 外创建只读的服务器侧派生发布。必须显式配置 `FM_DERIVED_RELEASE_ROOT`，且不得位于本次运行的 artifact root 或冒烟输入根目录内。
- artifact root：必须位于 `/data` 下且不得等于或位于 `/data/quantlake` 内。
- Mac 路径、仓库内数据目录和个人后复权数据均不合法。

## 2. 必需数据来源

在读取第一行真实数据前冻结并记录：

```text
data_origin = server_quantlake
resolved_release_id
release_manifest_path
release_manifest_sha256
schema_version
market_cutoff
adjustment_convention
calendar_version
state_table_version
state_table_cutoff
config_path
config_hash
code_commit
reference_manifest_path
reference_manifest_sha256
```

这些值不在 Git 中设置默认值。服务器私有配置必须显式提供；任何字段缺失时 `doctor` 和真实运行都失败。`release_manifest_sha256` 必须与实际发布清单字节一致；`config_hash` 是配置文件中除自引用 `FM_CONFIG_HASH` 外全部 `FM_` 字段的规范 JSON SHA-256；`code_commit` 必须等于干净工作区的实际 Git HEAD。

## 3. 规范面板

真实适配器输出至少包含：

```text
date, asset
open, high, low, close, volume, amount
is_st, is_newly_listed, is_suspended, can_buy, can_sell
valid_for_factor_compute, valid_for_factor_rank, valid_for_trading
label_o2o_5d
```

`(date, asset)` 必须唯一，按 asset/date 可稳定排序；行情与状态表 cutoff 必须一致。

## 4. 状态语义

```python
valid_for_factor_compute = ~(is_st | is_newly_listed)
valid_for_factor_rank = valid_for_factor_compute & ~is_suspended
valid_for_trading = valid_for_factor_compute & can_buy & can_sell
```

adapter 必须验证提供的扩展 mask 与上述语义一致，不能信任任意同名列。

## 5. 可见验证标签

V0 公司 A 股 profile 固定使用：

```python
label_o2o_5d = open.shift(-6) / open.shift(-1) - 1
```

标签只供评价器使用，禁止进入 CandidateFactorSpec 的必需字段或 DSL。

## 6. 强制闸门

以下任一情况必须停止：release/manifest 不一致、复权口径未知、L2 落后、状态 mask 语义错误、主键重复、字段缺失、cutoff 不一致、全无有效值、QuantLake 可写、artifact root 不可写或位于 QuantLake 内。

V0 不规定服务器真实 L1/L2/标签文件名；它们必须由服务器私有配置显式解析。禁止猜测路径或借用 `huan_quant` 运行时代码。

可见验证的行情和状态 URI 必须与发布清单一致并位于清单声明的发布根目录；标签 URI 必须位于独立的标签发布根目录。`/data/factor_miner_artifacts/inputs` 只允许冒烟测试使用，可见验证不得读取其中任何文件。冒烟测试不得配置、构造或读取标签数据源。

## 7. 参考因子清单

参考因子必须通过服务器私有 JSON 清单冻结，配置项为 `FM_REFERENCE_MANIFEST_PATH` 和 `FM_REFERENCE_MANIFEST_SHA256`。清单最小结构如下：

```json
{
  "manifest_id": "company_a_share_reference_v1",
  "data_cutoff": "YYYY-MM-DD",
  "factors": [
    {
      "factor_id": "reference_momentum_20d",
      "parquet_path": "/data/.../reference_momentum_20d.parquet",
      "parquet_sha256": "64 位十六进制 SHA-256",
      "compiled_plan": {}
    }
  ]
}
```

`factors` 的顺序必须与评价政策完全一致。`compiled_plan` 用于结果打开前的结构冗余检查，其 `candidate_id` 必须等于 `factor_id`。冒烟运行只核验清单哈希和编译元数据，不读取 `parquet_path`；可见运行还会核验每个 Parquet 的实际哈希、结构、主键和截止日期。

## 8. QuantLake 服务器侧派生发布

当 QuantLake 保存的是分区原始字段时，标准化 market/state/label 可以写入独立的派生发布根目录，但发布清单必须满足以下条件：

- `release_kind` 固定为 `quantlake_derived_v1`；
- `transform_id` 固定为 `factor_miner_standard_panel_v1`；
- `upstream_quantlake_root` 必须解析为只读 `/data/quantlake`；
- `upstream_files` 非空，并逐文件记录 QuantLake 内绝对路径和实际 SHA-256；
- `derived_release_root` 与私有配置一致；
- market、state、label 的路径和实际 SHA-256 全部写入清单；
- 派生文件仍需通过标准 schema、主键、cutoff、状态语义和标签合同检查。

这不是对任意本地文件的放行。缺少上游清单、哈希不匹配、文件位于 QuantLake 之外却未声明派生发布、或派生根目录与运行产物混用时都必须失败。
