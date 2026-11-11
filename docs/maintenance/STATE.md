# OpenCode 维护状态

更新时间：2026-11-11（America/Los_Angeles）
角色：GitHub 版本管理、CI、独立代码审计、故障注入与数据完整性保障（三级）

本文件是 OpenCode 的短上下文恢复入口。每次恢复会话先读本文件与 `TASKS.md`，不要重新加载全部历史报告。

## 当前事实（以工作区实测为准）

- 项目此前**不是 Git 仓库**。2026-11-11 已在本地执行 `git init -b main`；**尚未配置 remote，尚未首次推送**。
- 维护前基线：`98` 项 unittest 通过，耗时 `61.737s`（`.venv/bin/python`，Python 3.14.0）。
- 当前全量：`112` 项 unittest 通过，耗时 `51.920s`（含 Codex/DSH 同期新增的同步、证据导入与库存测试）。
- 维护新增：`8` 项测试（仓库边界、库存幂等/计数、Docker 隔离契约），已通过。
- SQLite 生产库 schema 3；`data/`、`reports/`、`logs/`、`cache/`、`private/`、`.venv/`、`tools/exiftool/` 均不进入版本控制。
- Codex 与 DSH 正在同一工作区活跃（`src/nas_photo/organize_transfer.py`、`tools/nas-evidence/`、`reports/datarestore-audit/`）。OpenCode 不修改其实时文件。
- 共享协调中心尚未由 Codex 建立；OpenCode 只写自己的 `reports/agent-coordination/opencode/outbox/`。

## 仓库边界

| 内容 | 处理 |
|---|---|
| `src/`、`tests/`、`tools/*.py`、`tools/*/Dockerfile`、`docs/`、示例配置 | 纳入 Git |
| `data/`、生产 SQLite、原始/恢复媒体 | 不纳入 |
| `reports/`（含协调消息、DSH 研究、1.3G 报告） | 不纳入 |
| `logs/`、`cache/`、`private/`、`.venv/`、`tools/exiftool/` | 不纳入 |
| `config/local.json`、`config/phase*.json`、`docs/phases/evidence/` | 不纳入 |
| 真实样本文件名、私网 IP、Tailscale 地址、挂载路径 | 已在文档中脱敏为占位符 |

## 已完成

- 敏感信息审计与 18 个 Markdown 文档脱敏；原始文件备份于 `private/backups/docs-pre-sanitize-20261111.tar.gz`。
- 加固 `.gitignore`，新增 SQLite/CSV/JSONL/日志/私有目录规则。
- 新增 `tools/maintenance/scan_secrets.py`，CI 扫描高置信凭据与 Tailscale CGNAT 地址。
- 新增 `.github/workflows/ci.yml`：Python 3.11/3.12/3.13，安装 ExifTool/FFmpeg，依赖校验、密钥扫描、静态检查、变更文件格式检查、编译、全量测试。
- 新增 `tests/test_repository_policy.py`、`tests/test_maintenance_integrity.py`、`tests/test_maintenance_docker_contract.py`。
- `tests/test_evidence_job.py` 在 DSH 模块未发布时显式 skip，避免干净 checkout 的 CI 失败。

## 待确认 / 阻塞

- **首次 GitHub 推送需要用户明确确认**：仓库名、可见性（应为 private）、组织/个人账号、分支保护要求。
- DSH 需将 `nas_evidence_worker.py`、`run_evidence_job.py` 及依赖发布到受版本控制路径，否则该模块在 CI 中只能 skip。
- Codex 需建立 `reports/agent-coordination/` 中央状态与任务板；OpenCode 不抢先修改。

## 恢复命令

```sh
cd /Users/xigua/Desktop/Program_Projects/NAS-Photo-Intelligence
git status -sb
.venv/bin/python -m unittest discover -s tests
.venv/bin/ruff check src tests tools
python tools/maintenance/scan_secrets.py
```

相关文件：`docs/maintenance/TASKS.md`、`docs/maintenance/SECURITY_AUDIT.md`、`.github/workflows/ci.yml`。
