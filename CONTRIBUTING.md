# 贡献指南

感谢参与微信公众号控制台与文章设计 Skill 的改进。

## 开始之前

1. 搜索现有 Issue，确认问题尚未被报告。
2. 安全漏洞不要提交公开 Issue，请按 `SECURITY.md` 私下报告。
3. 功能改动应同时考虑控制台 API、Skill 工作流和升级兼容性。

## 本地开发

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
pytest -q
ruff check .
```

Windows PowerShell 使用 `.\.venv\Scripts\Activate.ps1` 激活环境。

## 提交要求

- 保持改动聚焦，不提交 `.env`、数据库、密钥、上传图片或 `.qa` 文件。
- 行为修改必须补充测试；API 契约变化必须同步 `API.zh-CN.md`。
- 数据库结构变化必须新增迁移，不得要求用户删除原数据库。
- 用户可见流程变化必须同步主 README 和 Skill README。
- 提交前运行 `pytest -q`、`ruff check .` 和 Skill 快速校验。

Pull Request 请说明问题、方案、验证结果、兼容性影响和必要的升级步骤。
