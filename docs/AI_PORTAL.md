# 外部 AI 审查入口（静态索引 / Markdown 镜像）

本文件是面向 ChatGPT、Claude 与普通网页抓取工具的**纯文本索引**，无需 GitHub 登录。
渲染版见 `docs/index.html`（GitHub Pages 入口），可机读版本见 `docs/ai-portal/manifest.json`，访问实测见 `docs/ai-portal/ACCESS_REPORT.md`。

- 公开仓库：https://github.com/dyxiguajun/NAS-Photo-Intelligence
- 可见性：Public；默认分支：`main`
- 本页链接固定的审查快照 SHA：`81827209198f1e48d5e39d6b7268bff2e8bec229`（PR #7 合并，2026-11-12T02:45:00Z）
- 把链接中的 SHA 换成 `main` 可读最新版；最新 HEAD 以 https://api.github.com/repos/dyxiguajun/NAS-Photo-Intelligence/commits/main 为准
- 本页位于独立分支 `agent/public-ai-review-portal`，通过 PR 提交，未直接修改 `main`

**权威说明**：仓库内维护的审查入口是 `docs/AI_REVIEW.md`；运行方式的权威说明是 `README.md`。本页只是文件目录与直链索引，不替代二者，也不代表运行时权威状态（本地 `reports/agent-coordination/STATE.json`，按设计不进入 Git）。

---

## 1. 纯文本地址前缀

```
RAW  = https://raw.githubusercontent.com/dyxiguajun/NAS-Photo-Intelligence/81827209198f1e48d5e39d6b7268bff2e8bec229/
BLOB = https://github.com/dyxiguajun/NAS-Photo-Intelligence/blob/81827209198f1e48d5e39d6b7268bff2e8bec229/
API  = https://api.github.com/repos/dyxiguajun/NAS-Photo-Intelligence
```

把 `RAW` 与下表路径拼接即可匿名获取纯文本。示例：

```
https://raw.githubusercontent.com/dyxiguajun/NAS-Photo-Intelligence/81827209198f1e48d5e39d6b7268bff2e8bec229/src/nas_photo/cli.py
```

备用镜像前缀：`https://cdn.jsdelivr.net/gh/dyxiguajun/NAS-Photo-Intelligence@main/`（匿名实测 HTTP 200）。
完整文件树（匿名）：`API/git/trees/main?recursive=1`（HTTP 200，匿名限额 60 次/小时）。

## 2. 关键文件目录

### 文档与交接

| 路径（接在 RAW 后） | 内容 |
|---|---|
| `README.md` | 用途、CLI 命令、运行条件、配置与限制 |
| `HANDOFF.md` | Phase 3.1 交接与保留边界 |
| `docs/AI_REVIEW.md` | 项目维护的外部 AI 审查入口 |
| `docs/ARCHITECTURE.md` | 数据库结构、时间规则、恢复协议、L1/L2 规范化与缓存边界 |
| `docs/PHASE3_LIBRARY_REVIEW.md` | 视觉库选型与颜色/透明/方向规则 |
| `docs/phases/README.md` | Phase 0–3 要求与报告索引 |
| `docs/datarestore-audit/README.md` | DataRestore1 恢复与去重审计摘要（脱敏） |
| `docs/datarestore-audit/03-defect-log.md` | 7 个静默数据缺陷复盘 |
| `docs/datarestore-audit/06-open-items.md` | 未完成事项与已知风险 |
| `docs/maintenance/SECURITY_AUDIT.md` | 脱敏映射、版本控制边界、残余风险 |
| `docs/maintenance/STATE.md` / `docs/maintenance/TASKS.md` | 维护状态与任务队列 |

### 核心源码

| 路径（接在 RAW 后） | 作用 |
|---|---|
| `nas_photo_cli.py` | CLI 启动入口 |
| `src/nas_photo/cli.py` | scan / duplicates / similarity / organize 命令解析 |
| `src/nas_photo/engine.py` | 元数据扫描、任务队列、恢复与超时隔离 |
| `src/nas_photo/metadata.py` | 路径安全、文件身份、ExifTool 只读调用 |
| `src/nas_photo/store.py` | SQLite schema、WAL、事务与迁移 |
| `src/nas_photo/config.py` / `time_rules.py` | 配置、限额与时间异常规则 |
| `src/nas_photo/worker.py` / `process.py` | 隔离 worker 与子进程封装 |
| `src/nas_photo/hashing.py` / `duplicates.py` | 分块哈希与 SHA-256 精确重复组 |
| `src/nas_photo/similarity.py` / `imaging.py` | L1/L2 视觉证据、解码与指纹 |
| `src/nas_photo/migrations.py` / `visual_migration.py` | schema 兼容升级 |
| `src/nas_photo/organize.py` 及 `organize_*.py` | 独立整理工作区、只读预检与 rclone 演练 |

### 工具、配置与 CI

| 路径（接在 RAW 后） | 作用 |
|---|---|
| `tools/czkawka_docker_batch.py` | Czkawka Docker 只读扫描批次 |
| `tools/agent_sync.py` | 三 Agent 本地消息交换 |
| `tools/locked_batch.py` | 固定 lockfile 单实例批次 |
| `tools/maintenance/scan_secrets.py` | 凭据与私网标识扫描 |
| `tools/nas-evidence/Dockerfile` / `nas_evidence_worker.py` | 证据 worker 容器与实现 |
| `tools/install_exiftool.py` | 固定版本 ExifTool 获取与核验 |
| `.github/workflows/ci.yml` | CI 步骤（Python 3.12/3.13） |
| `.githooks/pre-commit` / `.githooks/pre-push` | 本地防护钩子 |
| `.gitignore` | 默认拒绝白名单 |
| `pyproject.toml` / `requirements-visual.txt` / `requirements-dev.txt` / `ruff.toml` | 依赖与静态检查 |
| `config/example.json` / `config/manifest.example.json` | 脱敏配置示例 |

### 测试

`tests/` 下的 `test_engine.py`、`test_duplicates.py`、`test_similarity.py`、`test_organize.py`、`test_locked_batch.py`、`test_repository_policy.py`、`test_maintenance_integrity.py`、`test_maintenance_docker_contract.py`、`test_evidence_job.py`、`test_agent_sync.py`、`worker_probe.py`。
完整清单见 `docs/ai-portal/manifest.json`。

## 3. 架构速览

- **只读优先**：NAS 只提供原始文件；程序只写本地 SQLite、日志和报告。不删除、不移动、不改名、不写回 EXIF。
- **三层证据**：L0 完整 SHA-256 字节重复；L1 指定解码/规范化版本下完整 RGBA8 像素相同；L2 pHash/dHash 视觉候选，**仅作关联建议，永远不是删除授权**。
- **显式范围**：所有扫描/哈希/视觉任务都必须给 manifest，不自动全库扫描；读取字节预算跨 resume 累计，不因重启重置。
- **可恢复队列**：SQLite WAL + 每文件事务 + 独立 worker 进程组；`running` 条目在 resume 时重置，已提交结果不重做。
- **身份模型**：`source_id + relative_path` 是稳定逻辑身份，`mount_root` 只是可变挂载入口。
- 细节以 `docs/ARCHITECTURE.md` 为准。

## 4. 测试说明

CI 在 **Python 3.12 / 3.13** 运行：`pip check` → `scan_secrets.py` → `ruff check src tests tools` → `compileall` → `unittest discover -s tests -v`。测试使用合成图像与临时目录，**不读取真实 NAS**。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m pip install -r requirements-visual.txt -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests
.venv/bin/ruff check src tests tools
python tools/maintenance/scan_secrets.py
```

已知差异：`pyproject.toml` 声明 Python ≥3.11，而 numpy/scipy 视觉依赖要求 ≥3.12，CI 因此只跑 3.12/3.13。

## 5. 匿名访问结果与限制

2026-11-11 匿名实测：github.com 文件页、raw 域名、GitHub API、jsDelivr 镜像均为 **HTTP 200**；不存在或未发布的路径为 **HTTP 404**（不是 401/403）。以 `ChatGPT-User` / `GPTBot` / `OAI-SearchBot` / `Claude-User` / `ClaudeBot` 等 UA 请求，服务端也都返回 200，GitHub 未做 UA 层拦截。逐条记录见 `docs/ai-portal/ACCESS_REPORT.md`。

**但**：`github.com/robots.txt` 的 AI UA 分组包含 `Disallow: /*/tree/`、`Disallow: /*/raw/`、`Disallow: /*/blame/`。遵守 robots 的网页抓取工具可能拒绝打开 github.com 的目录页与 `/raw/` 页，**虽然这些 URL 用 curl 访问返回 200**。本入口因此统一使用 `raw.githubusercontent.com` 与 Pages 站点。

> 若 ChatGPT 的某个特定网页工具仍被阻止，应记录为**工具侧限制**（robots 策略、URL 白名单、渲染或网络策略）。**更改 GitHub 仓库权限、可见性、分支保护或账号授权，不能保证解决它**，不应声称权限调整必然修复。

匿名 API 限额 60 次/小时，超限返回 403 属限流，不是权限拒绝。

## 6. 发布边界

本入口只发布已进入版本控制且经脱敏检查的源码、测试、文档与示例配置。以下内容按 `.gitignore` 默认拒绝规则**不进入 Git，也无法通过任何匿名链接读取**（实测 404）：

- 生产/恢复照片与视频、`data/` 下的生产 SQLite 及备份/WAL/锁
- `reports/`：三 Agent 协调消息（`STATE.json`、`PROJECT_STATUS.md`、`TASK_BOARD.md` 等）、DSH 研究、约 3.1 GB 审计底稿、大型日志
- `config/local.json`、`config/phase*.json`：真实 NAS 挂载根与样本清单
- `docs/phases/evidence/`、`private/`、`logs/`、`cache/`、`.venv/`、`tools/exiftool/`
- `*.sqlite3`、`*.db`、`*.csv`、`*.jsonl`、`*.log` 与所有媒体扩展名
- SSH / Tailscale 密钥、NAS 凭据、私网地址与真实照片文件名（文档中已替换为占位符）

## 7. 与现有 Agent 成果的关系

本入口**不修改** README、`docs/AI_REVIEW.md`、`docs/maintenance/*`、`docs/datarestore-audit/*`、源码、测试或 CI。OpenCode 的 PR #7 已更正「私有仓库」表述并提供 raw/jsDelivr 纯文本地址；本入口补充的是**关键源码目录、固定 SHA 直链、Pages 静态站点与完整匿名访问实测**，属于增量而非覆盖。

维护提示：更新时替换 `manifest.json` 的 `snapshot_sha` 与 HTML/Markdown 中的 40 位 SHA；新增直链前必须确认目标文件已提交且不含真实路径、IP、文件名或凭据。
