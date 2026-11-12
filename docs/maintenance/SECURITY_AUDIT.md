# OpenCode 安全审计记录

日期：2026-11-11
范围：`<PROJECT_ROOT>` 全目录，重点为进入 Git 的文件。
方法：默认拒绝 `.gitignore` 计算候选；扫描凭据模式、私网地址、本机绝对路径、真实媒体文件名、文件体积；检查暂存区与 Git 历史，而不只检查工作目录。

## 结论

- **未发现**私钥、GitHub token、AWS access key、Slack/OpenAI token 或密码赋值。
- **发现 18 个 Markdown 文档**曾含 NAS 私网 IP、Tailscale 地址、挂载路径、本机绝对路径或真实样本文件名；已完成脱敏。
- 生产数据、原始/恢复媒体、完整索引、日志和报告目录已排除出版本控制。
- 仓库 `dyxiguajun/NAS-Photo-Intelligence` 当前为 **public**；首次基线经 PR #1 合并到 `main`（merge commit `b5c8010`）。
- 当前 `main`：96 个跟踪文件（88 + 8 个 DataRestore1 脱敏说明）；无禁止路径；无 >1 MiB blob；自研扫描器 clean（96 files）；`gitleaks` 20 commits no leaks。

## 发现与处置

| # | 严重度 | 发现 | 处置 |
|---|---|---|---|
| 1 | 高 | 项目初始不是 Git 仓库，无历史可审计 | 本地初始化、独立分支审计、默认拒绝忽略规则；仓库与 PR #1 已完成首次受控推送 |
| 2 | 高 | `HANDOFF.md`、`PROJECT_INSPECTION_2026-10-17.md`、`README.md` 及 `docs/` 中 18 个文件含 Tailscale IP、LAN IP、挂载路径、本机路径 | 替换为 `<TAILSCALE_NAS_IP>`、`<LAN_NAS_IP>`、`<NAS_SHARE>`、`<PROJECT_ROOT>`、`<HOME>` 等占位符；原文件备份 |
| 3 | 高 | `docs/phases/evidence/` 含真实样本绝对路径、文件名、机器环境与读取统计 | 整目录排除（`.gitignore`） |
| 4 | 高 | `docs/phases/PHASE0_REPORT.md` 等含真实照片文件名与样本 SHA-256 | 文件名替换为 `<restore-01>` 等占位符；样本哈希替换为占位符 |
| 5 | 高 | `config/local.json`、`config/phase*.json` 含真实 NAS 挂载根与来源布局 | 保持忽略；仅提交 `config/example.json`、`config/manifest.example.json` |
| 6 | 中 | `reports/` 约 1.3 GB，含 DSH 研究、协调消息、索引与日志 | 整目录忽略；协调消息保留在本地 |
| 7 | 中 | `tools/exiftool/` 约 45 MB，上游二进制 | 忽略；由 `tools/install_exiftool.py` 在本地安装并核验 |
| 8 | 中 | `tests/test_evidence_job.py` 曾从被忽略的 `reports/datarestore-audit/` 导入模块 | Codex 已将 worker/intake 发布到 `tools/nas-evidence/`；测试在干净 checkout 可运行 |
| 9 | 低 | `tools/nas-evidence/run-container.sh` 含 NAS 卷路径与固定镜像摘要 | 属部署参数而非凭据；已加隔离契约测试 |
| 10 | 低 | 生产 SQLite、备份与 CSV/JSONL 可能被误加入 | `.gitignore` 增加 `*.sqlite3`、`*.db`、`*.csv`、`*.jsonl`、`*.log` 规则；pre-commit 双重拦截 |
| 11 | 中 | 仓库当前为 public，但分支保护/rulesets、secret scanning/push protection 尚未启用 | 采用 PR + CI + 本地 pre-push 防护；建议启用 public 仓库的安全设置，需确认不阻断 Codex 集成 |
| 12 | 低 | 本审计文档曾含本机绝对路径 | 替换为 `<PROJECT_ROOT>`；`scan_secrets.py` 增加 `/Users/<name>/` 本机路径检测，防止复发 |

## 脱敏映射（示例）

| 原值类型 | 占位符 |
|---|---|
| Tailscale NAS 地址 | `<TAILSCALE_NAS_IP>` |
| 局域网 NAS 地址 | `<LAN_NAS_IP>` |
| Tailscale 客户端地址 | `<TAILSCALE_CLIENT_IP>` |
| NAS 设备名 | `<NAS_NAME>` |
| 共享挂载路径 | `/Volumes/<NAS_SHARE>` |
| 本机项目绝对路径 | `<PROJECT_ROOT>` |
| 本机用户目录 | `<HOME>` |
| 真实样本文件名 | `<restore-01>.jpg`、`<mobile-02>.jpeg` 等 |
| 真实样本 SHA-256 | `<sha256-of-private-sample>` |

原始文档备份：`private/backups/docs-pre-sanitize-20261111.tar.gz`（`private/` 已被忽略）。

## 版本控制边界

纳入：源码、测试、工具脚本、Docker 配置、文档、示例配置、CI 配置。

排除：原始/恢复媒体、生产 SQLite、CSV/JSONL、日志、报告、缓存、虚拟环境、ExifTool 二进制、真实样本清单、本地凭据、协调消息、未合并的本地改动。

## 自动验证

```sh
python tools/maintenance/scan_secrets.py
gitleaks git --no-banner --redact
.venv/bin/python -m unittest discover -s tests
.venv/bin/ruff check src tests tools
```

`scan_secrets.py` 检查：私钥块、GitHub token、AWS access key、Slack/OpenAI token、通用密码/密钥赋值、Tailscale CGNAT 地址、本机 `/Users/<name>/` 路径。

`test_repository_policy.py` 检查：受禁路径/扩展名未被跟踪、`.gitignore` 关键项存在、单文件不超过 2 MiB。

## 残余风险

- 仓库当前公开全部源码、测试和说明文档；如果只需公开“项目概览”，应改为私有代码 + 独立公开文档仓库/Pages，需用户/Codex 决策。
- public 仓库尚未启用分支保护与 secret scanning；本地 `pre-push`、PR 约定和 CI 是补偿措施，不是服务端强制。建议在确认不阻断集成后启用。
- DataRestore1 公开文档仍含 1 个示例容器名与 3 个对象 SHA-256 前缀；已通知 DSH 复核，是否替换为占位符由 DSH/Codex 决定。
- 公开源码不包含生产媒体、原始 SQLite、完整索引或凭据；但源码和审计统计本身已公开，外部 AI 的结论仍需回到本地权威状态复核。
- 历史文档已脱敏，但原始备份仍在本机 `private/`；该目录必须保持忽略且不进入任何同步工具。
- 若未来将 `reports/`、`docs/phases/evidence/` 或真实索引纳入版本控制，必须先重新审计并取得用户确认。
- 正式照片库构建、双 NAS 迁移和生产媒体操作不在本审计授权范围内。
