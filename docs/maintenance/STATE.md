# OpenCode 维护状态

更新时间：2026-11-11 18:10（America/Los_Angeles）
角色：GitHub 版本管理、CI、独立代码审计、故障注入与数据完整性保障（三级）

本文件是 OpenCode 的短上下文恢复入口。每次恢复会话先读本文件与 `TASKS.md`，不要重新加载全部历史报告。

## 当前事实（以工作区实测为准）

- GitHub private 仓库：`dyxiguajun/NAS-Photo-Intelligence`，默认分支 `main`。
- 当前 `main`：`1db99b6eaa7906bca1584191270d91ef3e43f7d6`（PR #3 merge）；PR #1 基线 `b5c8010`，PR #2 外部 AI 审查入口 `c6023f4`。
- CI 在 Python 3.12 / 3.13 均通过（PR #3：1m47s / 1m45s）。
- 仓库跟踪 88 个文件（基线 85 + AI 审查入口/`pre-push` + lockfile 测试）；无 `data/`、`reports/`、媒体、SQLite、凭据；无 >1 MiB blob。
- 干净 worktree 全量测试：`117` 项通过，`76.316s`（含 5 个 lockfile 恢复用例）。
- 外部 AI 审查入口：`docs/AI_REVIEW.md`，README 与 `docs/ARCHITECTURE.md` 已链接。
- `reports/agent-coordination/STATE.json` 已存在（owner Codex，2026-11-11T22:05Z）；中央权威文件为 `STATE.json`、`PROJECT_STATUS.md`、`TASK_BOARD.md`、`DECISIONS.md`、`AGENT_REGISTRY.md`。
- DSH evidence worker/intake 已发布到 `tools/nas-evidence/`。
- 共享主工作区仍有 Codex/DSH 未提交改动；OpenCode 使用独立分支/worktree，不并发修改其文件。

## GitHub 保护限制

GitHub Free 私有仓库不支持服务端分支保护或 rulesets；secret scanning / push protection 也不可用。

| 能力 | 状态 | 证据 |
|---|---|---|
| 分支保护 / rulesets | 不可用 | API 403：`Upgrade to GitHub Pro or make this repository public` |
| Secret scanning | 不可用 | API 422：`Secret scanning is not available for this repository` |
| Push protection | 不可用 | 同上 |

补偿措施：

- 所有日常改动走独立分支 + PR；CI 每次 PR 运行。
- 本地 `.githooks/pre-push` 阻止直接推送 `main`；紧急情况才允许 `--no-verify`。
- 不擅自将私有仓库改为公开；升级 GitHub Pro 需用户/Codex 决策。

## 仓库边界

| 内容 | 处理 |
|---|---|
| `src/`、`tests/`、`tools/*.py`、`tools/*/Dockerfile`、`docs/`、示例配置 | 纳入 Git |
| `data/`、生产 SQLite、原始/恢复媒体 | 不纳入 |
| `reports/`（含协调消息、DSH 研究、大型报告） | 不纳入 |
| `logs/`、`cache/`、`private/`、`.venv/`、`tools/exiftool/` | 不纳入 |
| `config/local.json`、`config/phase*.json`、`docs/phases/evidence/` | 不纳入 |
| 真实样本文件名、私网 IP、Tailscale 地址、挂载路径 | 已在文档中脱敏为占位符 |

## 已完成

- 敏感信息审计与 18 个 Markdown 文档脱敏；原始文件备份于 `private/backups/docs-pre-sanitize-20261111.tar.gz`（已忽略）。
- 默认拒绝 `.gitignore`，仅白名单放行源码、测试、文档、示例配置与 CI/hook。
- `tools/maintenance/scan_secrets.py` 扫描高置信凭据与 Tailscale CGNAT 地址。
- `.github/workflows/ci.yml`：Python 3.12/3.13，安装 ExifTool/FFmpeg 与 `requirements-visual.txt`，执行依赖校验、密钥扫描、ruff、compileall、全量测试。
- `.githooks/pre-commit` 阻止 >1 MiB 文件、数据/媒体扩展名、敏感目录，并运行密钥扫描；`.githooks/pre-push` 阻止直接推送 `main`。
- 新增 `tests/test_repository_policy.py`、`tests/test_maintenance_integrity.py`、`tests/test_maintenance_docker_contract.py`。
- 新增 `tests/test_locked_batch.py`：5 个生成 fixture 用例，覆盖第二实例 exit 75、SIGKILL 后已提交产物保留、进程/子进程退出后解锁、待办重做。
- 外部 AI 审查入口 `docs/AI_REVIEW.md` 已合并（PR #2），README、架构、状态、安全与任务文档已刷新。
- 私有仓库、首次推送、PR #1/#2/#3 与 CI 已完成。

## 待确认 / 阻塞

- GitHub Pro 升级或保持 Free + 本地防护：需用户/Codex 决策。
- NAS-005 剩余：断线、ENOSPC、重复启动、冻结清单对账（P0/P1）；PNG 损坏/拼接/截断解析（P1）。固定 lockfile P0 已交付，等待 Codex 验收。
- DSH NAS-007 的 PNG 尺寸解析缺陷由 DSH 修正；OpenCode 只做独立生成 fixture 审计，不接管其生产恢复数据。
- `pyproject.toml` 的 `requires-python >=3.11` 与实际视觉依赖（numpy 2.5.4 / scipy 1.18.1 要求 >=3.12）不一致，建议 Codex 统一。

## 恢复命令

```sh
cd <PROJECT_ROOT>
git fetch origin
git status -sb
.venv/bin/python -m unittest discover -s tests
.venv/bin/ruff check src tests tools
python tools/maintenance/scan_secrets.py
```

相关文件：`docs/maintenance/TASKS.md`、`docs/maintenance/SECURITY_AUDIT.md`、`.github/workflows/ci.yml`、`.githooks/`。
