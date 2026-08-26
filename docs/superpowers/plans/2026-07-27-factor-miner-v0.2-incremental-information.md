# Factor Miner V0.2 增量信息评价实施计划

> **代理执行要求：**实施时必须使用 `superpowers:executing-plans`，逐项完成本计划。步骤使用复选框（`- [ ]`）跟踪。项目明确禁止使用子代理执行。

**目标：**在不修改 V0.1 历史身份的前提下，实现内容寻址参考因子库、截面排名 OLS 正交化、残差 RankIC/HAC/Bonferroni 和不可跳过的增量信息闸门。

**架构：**V0.2 使用新的评价政策版本引用冻结参考因子库。工作流在打开标签前从候选、参考库和同批次更早候选生成确定性正交残差；打开标签后分别评价原始因子与残差，并将完整结果原子发布。V0.1 入口继续使用原有政策和产物结构。

**技术栈：**Python 3.12、Pydantic 2、Polars 1、NumPy、statsmodels、Typer、unittest。

## 全局约束

- 所有 Markdown 标题和正文使用中文。
- 原始因子、正交化预处理、评价标签和回测严格分层。
- 真实数据、个股样本、完整结果、运行配置、账本和密钥不得进入 Git。
- Mac 只运行程序生成的纯合成测试；真实正交化、IC 和统计推断只在公司 Linux 服务器运行。
- 参考库、正交化政策、研究族、候选顺序和阈值必须在打开任何 outcome 前冻结。
- V0.2 输出只能称为“通过可见增量信息验证的候选因子”。
- V0.1 文件身份、运行语义和测试必须保持兼容。
- 每个任务遵循测试先行，并在测试通过后单独提交。

---

### 任务 1：冻结参考因子库和 V0.2 评价政策

**文件：**
- 修改：`src/factor_miner/schema.py`
- 修改：`src/factor_miner/policy.py`
- 修改：`src/factor_miner/ledger.py`
- 修改：`tests/helpers.py`
- 修改：`tests/test_schema.py`
- 修改：`tests/test_ledger.py`

**接口：**
- 产出：`ReferenceFactorLibrarySpec`
- 产出：`RegisteredReferenceFactorLibrary`
- 产出：`OrthogonalizationPolicySpec`
- 产出：`IncrementalEvaluationPolicySpec`
- 产出：`reference_factor_library_id(spec) -> str`
- 产出：`registered_reference_factor_library(spec) -> RegisteredReferenceFactorLibrary`
- 产出：`validate_incremental_policy_library(policy, library) -> None`
- 产出：`company_a_share_incremental_policy(library_id) -> IncrementalEvaluationPolicySpec`

- [x] **步骤 1：编写失败测试**

测试以下行为：参考因子 ID 必须非空、有序且唯一；库 ID 与内容哈希一致；V0.2 政策必须包含正交化参数；政策与库的清单和因子顺序不一致时拒绝；账本在独立 `state/reference_libraries` 目录不可变登记参考库；V0.1 政策 ID 不发生变化。

- [x] **步骤 2：运行测试并确认因缺少 V0.2 类型和登记接口而失败**

```bash
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest \
  tests.test_schema tests.test_ledger -v
```

- [x] **步骤 3：实现最小 schema、内容 ID、内置政策和账本登记**

正交化政策固定使用：

```python
OrthogonalizationPolicySpec(
    method="cross_sectional_rank_ols",
    include_intercept=True,
    min_reference_coverage=0.8,
    min_cross_sectional_excess_names=20,
    max_condition_number=1e8,
    min_median_residual_variance_ratio=0.05,
    min_abs_mean_residual_rank_ic=0.01,
)
```

`IncrementalEvaluationPolicySpec` 继承 V0.1 的全部评价字段，只把 `policy_version` 固定为 `"2"`，并增加 `reference_factor_library_id` 与 `orthogonalization`。旧 `EvaluationPolicySpec` 的序列化字节不得变化。

- [x] **步骤 4：运行目标测试与完整回归测试**

```bash
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest \
  tests.test_schema tests.test_ledger -v
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest discover -s tests -v
```

- [x] **步骤 5：提交**

```bash
git add src/factor_miner/schema.py src/factor_miner/policy.py \
  src/factor_miner/ledger.py tests/helpers.py tests/test_schema.py tests/test_ledger.py
git commit -m "feat: freeze v0.2 incremental policy"
```

### 任务 2：实现确定性截面正交化

**文件：**
- 新增：`src/factor_miner/incremental.py`
- 新增：`tests/test_incremental.py`
- 修改：`src/factor_miner/errors.py`

**接口：**
- 依赖：`OrthogonalizationPolicySpec`
- 产出：`DailyOrthogonalization`
- 产出：`OrthogonalizationSummary`
- 产出：`OrthogonalizedFactor`
- 产出：`orthogonalize_candidate(candidate_frame, reference_frames, eligibility_frame, campaign, policy) -> OrthogonalizedFactor`

- [x] **步骤 1：编写线性组合的失败测试**

构造三个互不完全相关的参考截面，并令候选为其线性组合。断言逐对相关性可以低于 `0.8`，但正交残差逐值接近零、每日 `R²` 接近 1、残差方差比例接近 0。

- [x] **步骤 2：运行测试并确认 `factor_miner.incremental` 不存在**

```bash
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest \
  tests.test_incremental.IncrementalInformationTest.test_joint_linear_redundancy_has_zero_residual -v
```

- [x] **步骤 3：实现排名、样本标准化和带截距 OLS**

对每个日期使用完整有限值交集，按平均秩转换，使用样本标准差标准化，随后执行：

```python
coefficients, _, rank, singular_values = np.linalg.lstsq(
    design,
    candidate_values,
    rcond=None,
)
residual = candidate_values - design @ coefficients
```

要求设计矩阵满秩、条件数有限且不超过政策阈值。返回只包含 `date`、`asset`、`orthogonal_residual` 的残差面板和每日诊断。

- [x] **步骤 4：补充覆盖率、常数列、秩亏、条件数和确定性测试**

每项测试分别断言稳定失败代码：

```text
ORTHOGONALIZATION_COVERAGE_LOW
ORTHOGONALIZATION_RANK_DEFICIENT
ORTHOGONALIZATION_NUMERIC_UNSTABLE
```

同时证明修改任何标签值不会改变残差，因为该函数接口不接受 outcome。

- [x] **步骤 5：运行目标测试和完整回归测试**

```bash
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest \
  tests.test_incremental -v
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest discover -s tests -v
```

- [x] **步骤 6：提交**

```bash
git add src/factor_miner/incremental.py src/factor_miner/errors.py \
  tests/test_incremental.py
git commit -m "feat: orthogonalize candidate against factor library"
```

### 任务 3：实现残差增量信息评价

**文件：**
- 修改：`src/factor_miner/incremental.py`
- 修改：`tests/test_incremental.py`

**接口：**
- 依赖：`evaluate_rank_ic`
- 依赖：`hac_mean_test`
- 产出：`IncrementalInformationResult`
- 产出：`evaluate_incremental_information(orthogonalized, outcome_and_mask_frame, campaign, expected_sign, policy) -> IncrementalInformationResult`

- [x] **步骤 1：编写独立信号通过的失败测试**

构造候选为“参考线性组合 + 独立截面信号”，并让未来收益只跟独立信号同向。断言：

```python
result.residual_evaluation.mean_rank_ic > 0
result.residual_inference.bonferroni_p_value <= campaign.alpha
result.passed is True
```

- [x] **步骤 2：运行测试并确认评价接口缺失**

```bash
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest \
  tests.test_incremental.IncrementalInformationTest.test_independent_component_passes_residual_inference -v
```

- [x] **步骤 3：复用现有评价器和统计器实现增量结果**

将残差面板与只包含日期、证券、冻结 mask 和 label 的面板连接，调用现有 `evaluate_rank_ic` 和 `hac_mean_test`。通过条件同时检查：

```text
残差方差比例中位数
expected sign
平均残差 RankIC 最低效果量
Bonferroni p 值
有效日期与覆盖率
```

- [x] **步骤 4：补充方向错误、效果量不足、独立方差不足和有效日期不足测试**

合同执行成功但准入条件不满足时返回 `passed=false` 和有序原因，不抛出数值合同异常；输入结构或有效日期不足仍使用稳定领域错误。

- [x] **步骤 5：运行目标测试与完整回归测试**

```bash
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest \
  tests.test_incremental -v
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest discover -s tests -v
```

- [x] **步骤 6：提交**

```bash
git add src/factor_miner/incremental.py tests/test_incremental.py
git commit -m "feat: evaluate residual incremental information"
```

### 任务 4：接入不可跳过的 V0.2 工作流

**文件：**
- 修改：`src/factor_miner/workflow.py`
- 修改：`src/factor_miner/ledger.py`
- 修改：`tests/test_workflow.py`

**接口：**
- 依赖：任务 1 至任务 3 的全部接口
- 产出：`run_incremental_visible_campaign(...) -> RunResult`
- 产出：`CandidateTerminalStatus.INCREMENTAL_FAILED`
- 产出：`EventType.INCREMENTAL_FAILED`

- [x] **步骤 1：编写工作流失败测试**

构造逐对相关性均不过阈值、但联合空间完全解释候选的合成批次。断言终态为 `incremental_failed`，且产物包含 `incremental_information.json`，候选包标记 `incremental_information_evaluated=true`、`incremental_information_passed=false`。

- [x] **步骤 2：运行测试并确认 V0.2 工作流接口缺失**

```bash
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest \
  tests.test_workflow.WorkflowTest.test_v02_joint_redundancy_cannot_visible_pass -v
```

- [x] **步骤 3：实现结果前参考库校验、正交化和结果后双重评价**

执行顺序必须是：

```text
登记 V0.2 政策、参考库、family、campaign
→ 编译和结构冗余
→ input 与未来依赖探针
→ raw factor
→ 完整参考库检查与正交残差
→ 打开 outcome
→ 原始评价
→ 逐对冗余
→ 残差增量评价
→ 原子发布
→ 终态事件
```

- [x] **步骤 4：编写独立信息通过和参考库错配前置失败测试**

通过测试断言 `incremental_information.json`、运行清单和候选包哈希完整。错配测试使用会在调用时失败的 `OutcomeSource`，证明参考库错误发生在 outcome 打开前。

- [x] **步骤 5：运行目标测试与完整回归测试**

```bash
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest \
  tests.test_workflow -v
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest discover -s tests -v
```

- [x] **步骤 6：提交**

```bash
git add src/factor_miner/workflow.py src/factor_miner/ledger.py \
  tests/test_workflow.py
git commit -m "feat: enforce v0.2 incremental information gate"
```

### 任务 5：接入 CLI、示例和中文文档

**文件：**
- 修改：`src/factor_miner/cli.py`
- 修改：`tests/test_cli.py`
- 新增：`examples/policies/company_a_share_incremental_v2.json`
- 新增：`examples/reference_libraries/company_a_share_reference_library_v1.json`
- 新增：`docs/V0.2增量信息金融流程说明.md`
- 新增：`docs/migrations/v0.1-to-v0.2.md`
- 修改：`README.md`
- 修改：`docs/START_HERE.md`
- 修改：`docs/constraints/FACTOR_RESEARCH_PROTOCOL.md`
- 修改：`docs/constraints/RESEARCH_GOVERNANCE.md`

**接口：**
- 产出：`factor-miner register-reference-library`
- 修改：`factor-miner register-policy` 支持代码内置 V0.1 和 V0.2 政策
- 修改：`factor-miner run-visible` 按政策版本选择 V0.1 或 V0.2 工作流

- [x] **步骤 1：编写 CLI 失败测试**

测试参考库登记输出内容 ID，V0.2 政策无法在参考库缺失时登记批次，正式 CLI 不接受自定义正交化参数。

- [x] **步骤 2：运行测试并确认命令或路由缺失**

```bash
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest \
  tests.test_cli -v
```

- [x] **步骤 3：实现 CLI 与示例**

CLI 只允许登记代码内置 V0.1 政策，或与已登记参考库严格匹配的代码内置 V0.2 政策。`run-visible` 从账本读取政策和参考库，不接受绕过增量闸门的开关。

- [x] **步骤 4：编写中文金融流程、迁移和治理文档**

文档先说明原始信息、重叠信息和增量信息的区别，再说明程序入口、产物与失败边界。明确 V0.1 不自动升级；旧候选进入 V0.2 必须登记新 policy、family 和 campaign。

- [x] **步骤 5：运行 CLI、示例和完整测试**

```bash
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest \
  tests.test_cli -v
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run factor-miner --help
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest discover -s tests -v
```

- [x] **步骤 6：提交**

```bash
git add src/factor_miner/cli.py tests/test_cli.py examples docs README.md
git commit -m "docs: complete factor miner v0.2 local workflow"
```

### 任务 6：执行本地与公司服务器验收

当前状态：2026-07-28 本地与公司 Linux 服务器工程验收均已完成；真实样例候选终态为 `incremental_failed`。

**文件：**
- 新增：`docs/acceptance/v0.2-server-evidence-template.md`
- 修改：`docs/superpowers/plans/2026-07-27-factor-miner-v0.2-incremental-information.md`
- 修改：`README.md`
- 修改：`docs/START_HERE.md`

**接口：**
- 消费：正式 CLI、服务器私有配置和冻结参考因子库
- 产出：不含敏感信息的 V0.2 验收摘要

- [x] **步骤 1：执行最终本地验证**

```bash
git diff --check
UV_CACHE_DIR=/private/tmp/factor_miner_uv_cache uv run python -m unittest discover -s tests -v
```

- [x] **步骤 2：在公司 Linux 服务器核验干净提交和内容身份**

核验 Python、锁文件、Git commit、配置、QuantLake 派生发布、参考库清单和账本。真实路径、样本行和完整结果不复制进 Git。

- [x] **步骤 3：运行两次 input-only smoke**

两次运行必须 `outcomes_opened=false`，原始因子哈希一致。V0.2 smoke 不读取参考值或标签，只检查参考编译元数据与冻结政策身份。

- [x] **步骤 4：运行一次 V0.2 可见增量信息批次**

使用新的 policy、reference library、research family 和 campaign。无论候选通过或失败，都要求原始评价、逐对冗余、正交化诊断、残差评价、推断和明确终态完整。

- [x] **步骤 5：核验账本、产物哈希和工作区**

运行账本校验与完整服务器测试，确认最终工作区干净。证据模板只保存非敏感 ID、哈希和“通过/未通过/运行失败”，不保存公司个股数据或完整指标。

- [x] **步骤 6：提交验收状态**

```bash
git add docs/acceptance/v0.2-server-evidence-template.md \
  docs/superpowers/plans/2026-07-27-factor-miner-v0.2-incremental-information.md \
  README.md docs/START_HERE.md
git commit -m "docs: record v0.2 server acceptance"
```
