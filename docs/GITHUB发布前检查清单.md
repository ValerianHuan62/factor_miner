# GitHub 发布前检查清单

## 权利与边界

- 确认你有权公开源码、设计文档、字段注册表和研究协议。
- 如果权利范围不明确，先创建私有仓库，不要公开发布。
- 不上传公司行情、个股样本、研究账本、真实指标、模型、数据库备份或服务器配置。
- 仓库当前没有开源许可证；未确认授权前不要随意添加 MIT、Apache-2.0 等许可证。

## 自动与人工检查

```bash
git status --short
git ls-files | rg '(^|/)(\.env|data|artifacts|outputs|models|logs)(/|$)'
git grep -nE 'BEGIN (RSA |OPENSSH )?PRIVATE KEY|ghp_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}'
uv sync --frozen --extra dashboard
uv run python -m unittest discover -s tests
```

人工复核以下位置：

- `configs/` 和 `deploy/` 只能保存空值模板或通用路径。
- `docs/superpowers/` 是完整设计与实施历史；公开前确认其中没有不应披露的内部信息。
- `tests/` 可以包含明确标注的合成密钥字符串，但不能包含真实凭据。
- `git log -p --all` 中不能残留曾经提交后又删除的密钥或数据。

## 推荐发布方式

1. 首次先创建 GitHub 私有仓库。
2. 推送后开启 GitHub secret scanning 和 push protection。
3. CI 通过后再决定是否公开。
4. 公开前补充截图、架构说明和你有权披露的合成演示；不要使用真实公司数据截图。
