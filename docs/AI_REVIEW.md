# 外部 AI 审查入口

本仓库当前为 **public**。ChatGPT、Claude Chat 或其他外部审查者可直接读取，无需 GitHub 登录或授权凭据。请勿要求上传生产照片、原始 SQLite、完整恢复索引或 NAS 凭据。

本文件是外部 AI 审查的单一入口，不替代本地权威状态。运行时权威状态以 Codex 维护的 `reports/agent-coordination/STATE.json` 与 `PROJECT_STATUS.md` 为准；该目录不进入 Git。

最后核对：2026-11-11（America/Los_Angeles）；核对时 `main` 合并提交为 `8182720`（PR #7）。最新提交始终以 GitHub `main` HEAD 为准。

### 纯文本访问地址（推荐给 ChatGPT / Claude）

GitHub 页面若受脚本、登录或抓取限制，直接读取以下 raw 文件：

| 用途 | 地址 |
|---|---|
| AI 审查入口 | `https://raw.githubusercontent.com/dyxiguajun/NAS-Photo-Intelligence/main/docs/AI_REVIEW.md` |
| README | `https://raw.githubusercontent.com/dyxiguajun/NAS-Photo-Intelligence/main/README.md` |
| 架构 | `https://raw.githubusercontent.com/dyxiguajun/NAS-Photo-Intelligence/main/docs/ARCHITECTURE.md` |
| 当前状态 | `https://raw.githubusercontent.com/dyxiguajun/NAS-Photo-Intelligence/main/docs/maintenance/STATE.md` |
| 任务队列 | `https://raw.githubusercontent.com/dyxiguajun/NAS-Photo-Intelligence/main/docs/maintenance/TASKS.md` |
| DataRestore1 摘要 | `https://raw.githubusercontent.com/dyxiguajun/NAS-Photo-Intelligence/main/docs/datarestore-audit/README.md` |

raw 不可达时的镜像前缀：`https://cdn.jsdelivr.net/gh/dyxiguajun/NAS-Photo-Intelligence@main/`（例如追加 `docs/AI_REVIEW.md`）。

以上 GitHub/raw/jsDelivr 地址已于 2026-11-11 用匿名请求验证 HTTP 200，不需要 GitHub 登录。

公开范围包括源码、测试和说明文档；生产媒体、原始 SQLite、完整恢复索引、凭据和未脱敏日志不进入 Git。

## 1. 项目用途

NAS-Photo-Intelligence 是一个**只读照片证据引擎**，用于整理中美双 NAS 中大量历史照片：

- 读取元数据、时间证据和异常标签。
- 用完整 SHA-256 建立字节级重复组。
- 用规范化像素指纹建立严格像素等价组。
- 用 pHash/dHash 生成视觉相似候选，仅作为研究证据。
- 生成版本建议与正式照片库模拟，不执行删除、移动或 EXIF 写回。

核心原则：**先保留独有媒体；SHA-256 确认字节重复；视觉相似不等于删除授权；生产操作必须显式授权。**

## 2. 建议阅读顺序

| 文件 | 内容 |
|---|---|
| `README.md` | 项目用途、命令入口、运行条件 |
| `docs/ARCHITECTURE.md` | `nas-photo`、SQLite、Docker Czkawka、三 Agent 分工 |
| `docs/maintenance/STATE.md` | OpenCode 当前状态、CI、GitHub 限制 |
| `docs/maintenance/SECURITY_AUDIT.md` | 脱敏、排除边界、残余风险 |
| `docs/maintenance/TASKS.md` | OpenCode 任务队列与验收条件 |
| `docs/datarestore-audit/README.md` | DataRestore1 损坏容器恢复与去重审计摘要 |
| `.github/workflows/ci.yml` | 实际 CI 步骤 |
| `tests/` | 可运行回归测试（合成数据） |
| `config/example.json` | 脱敏配置模板 |

## 3. 当前可验证状态

- 仓库：`dyxiguajun/NAS-Photo-Intelligence`，public，默认分支 `main`。
- 核对时 `main`：`81827209198f1e48d5e39d6b7268bff2e8bec229`（PR #7）；PR #2 引入本审查入口，PR #3 增加 lockfile 恢复测试，PR #5 公开 DataRestore1 审计摘要，PR #6 完成 C 层 3 个对象像素验证，PR #7 修正公开访问说明。
- CI：Python 3.12 / 3.13 均通过（PR #6 run 43142942434；PR #7 run 43143234574）。
- 跟踪文件：96 个（基线 85 + AI 审查入口/`pre-push` + lockfile 测试 + 8 个 DataRestore1 说明）；无生产媒体、SQLite、CSV/JSONL、日志、报告数据目录或凭据。
- 回归测试：干净 worktree 中 `117` 项通过（含 5 个 lockfile 恢复用例）。
- 静态检查：`ruff check src tests tools` 通过。
- 密钥扫描：自研扫描器 clean（96 files）；`gitleaks` 20 commits no leaks。
- 已交付：`tests/test_locked_batch.py` 验证第二实例 exit 75、SIGKILL 后已提交产物保留、进程/子进程退出后解锁、待办重做。
- DataRestore1 摘要：205 个损坏容器 / 42.63 GB；16,974 个唯一媒体对象，15,449 个可解码，1,352 个高清；详细结论与 7 个缺陷复盘见 `docs/datarestore-audit/`。
- C 层复核（PR #6）：3 个“无候选”高清对象的最高 NCC 均 < 0.50（0.3834 / 0.4948 / 0.3863），确认参考池内无同内容候选；其中 1000×1000 具真实照片特征，仍建议人工目视确认。
- 正在处理：NAS-005 的断线、ENOSPC、重复启动与冻结清单对账；PNG 损坏/拼接/截断解析（P1）；DSH 高清索引（31,115 / 48,672）与 Q1 匹配率重算、参考池 SHA 去重；Codex 的整理/迁移生产前验证。

## 4. 关键源码路径

| 路径 | 作用 |
|---|---|
| `src/nas_photo/cli.py` | `nas-photo` 命令入口：scan、duplicates、similarity、organize |
| `src/nas_photo/engine.py` | 元数据扫描、任务队列、恢复与超时隔离 |
| `src/nas_photo/metadata.py` | 路径安全、文件身份、ExifTool 只读调用 |
| `src/nas_photo/store.py` | SQLite schema、WAL、事务与迁移 |
| `src/nas_photo/duplicates.py` | SHA-256 精确重复、预算、缓存与报告 |
| `src/nas_photo/similarity.py` | 视觉任务、L1/L2 证据、比较与报告 |
| `src/nas_photo/imaging.py` | 隔离 worker 中的解码、规范化和指纹 |
| `src/nas_photo/organize_index.py` | 独立整理工作区的版本化库存与证据 |
| `src/nas_photo/organize_build.py` | 只读整理预检与隔离演练 |
| `src/nas_photo/organize_transfer.py` | rclone 本地传输行为演练（不接触生产） |
| `tools/czkawka_docker_batch.py` | fnOS 上的 Czkawka Docker 扫描批次与检查点 |
| `tools/nas-evidence/` | NAS 侧可恢复 SHA-256/ExifTool 证据 worker |
| `tools/agent_sync.py` | 三 Agent 本地消息交换（原子写、去重、回执） |
| `tools/maintenance/scan_secrets.py` | CI/本地凭据与私网标识扫描 |

## 5. 主要设计决策

1. **字节重复与视觉相似分层。** SHA-256 相同才是字节级重复；L1 是同解码/规范化规则下的完整 RGBA8 像素相同；L2 仅是候选，不生成删除或保留授权。
2. **只读优先。** NAS 只提供原始文件；程序只写本地 SQLite、日志和报告。媒体写入、EXIF 写回和正式库执行需单独授权。
3. **显式范围。** 扫描/哈希/视觉任务都要求显式 manifest，不自动全库扫描；预算跨 resume 累计，不因重启重置。
4. **可恢复队列。** SQLite WAL + 每文件事务 + 独立 worker 进程组；`running` 条目在 resume 时重置，已提交 `done/skipped` 不重做。
5. **Docker 隔离。** Czkawka 只读扫描使用固定摘要镜像；不以容器删除或改写媒体。
6. **三 Agent 分工。** Codex 负责架构、主工程、权威数据库与最终集成；DSH 负责视觉匹配、媒体恢复与算法验证；OpenCode 负责 GitHub、CI、独立审计、故障注入与质量保障。OpenCode 不直接覆盖 Codex/DSH 的活跃文件。

## 6. 如何运行测试

需要 Python 3.12+（视觉依赖要求）和 ExifTool/FFmpeg：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m pip install -r requirements-visual.txt -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m unittest -v tests.test_locked_batch
.venv/bin/python tools/maintenance/scan_secrets.py
.venv/bin/ruff check src tests tools
```

测试使用合成图像和临时目录，不读取真实 NAS。`tests/test_locked_batch.py` 使用生成 fixture 验证固定 lockfile 的单实例、SIGKILL 恢复、进程/子进程解锁和待办重做。`config/example.json` 与 `config/manifest.example.json` 是脱敏示例；真实 `config/local.json` 和 `config/phase*.json` 不进入 Git。

## 7. 有意不进入 GitHub 的内容

- 生产/恢复媒体与私人照片。
- 生产 SQLite、原始媒体索引、完整 CSV/JSONL。
- `reports/`（三 Agent 协调消息、DSH 研究、大型证据与日志）。
- `data/`、`logs/`、`cache/`、`private/`、`.venv/`、`tools/exiftool/`。
- `docs/phases/evidence/`、本地配置、凭据、SSH/Tailscale 密钥。
- 共享工作区中 Codex/DSH 尚未提交的实时改动。

因此，GitHub 上的内容是可审查的源码、测试、文档和脱敏示例；生产进度、完整索引和未合并的本地改动可能比 GitHub 更新。

## 8. 已知风险与限制

- 仓库当前公开全部源码、测试和说明文档；如果只想公开“项目概览”而不公开详细代码，需要改为私有仓库 + 独立公开文档仓库/Pages，需用户/Codex 决策。
- 当前 public 仓库尚未启用服务端分支保护/rulesets 与 secret scanning（API 404）；依靠 PR 约定、CI 和本地 `pre-push` 防护。GitHub Free public 仓库可启用这些能力，等待确认后配置。
- `pyproject.toml` 声明 `requires-python >=3.11`，但 `requirements-visual.txt` 中 numpy/scipy 要求 Python >=3.12；CI 当前矩阵为 3.12/3.13。
- 视觉候选尚未人工确认；相似度不是概率，也不能作为删除授权。
- 正式照片库构建、双 NAS 迁移和生产媒体操作仍需 Codex/用户授权与验收。
- 外部 AI 的结论应回到本地权威状态和实际测试复核，不能仅凭本摘要修改生产数据。

## 9. 同步策略

在重要功能合并、CI 变更或版本更新后更新本文件；不把每次扫描进度、临时日志或研究中间结果提交到 Git。运行时状态继续由 Codex 在本地 `reports/agent-coordination/` 维护。
