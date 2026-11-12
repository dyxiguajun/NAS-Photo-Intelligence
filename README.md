# NAS Photo Intelligence Engine

> 历史快照。现状以 [docs/AI_REVIEW.md](https://github.com/dyxiguajun/NAS-Photo-Intelligence/blob/main/docs/AI_REVIEW.md) 与本地 [STATE.json](reports/agent-coordination/STATE.json) 为准。

命令行照片证据引擎：只读元数据索引、异常标签、SHA-256 完全重复检测、规范化像素相同检测及视觉相似候选。SQLite、任务队列和输出均在 Mac 本地；不执行删除、照片移动或元数据修复。

> **外部 AI 审查入口 / External review entry:** [docs/AI_REVIEW.md](docs/AI_REVIEW.md)。本仓库当前为公开仓库，可直接读取；如 GitHub 页面受限，入口文档内提供 raw / jsDelivr 纯文本地址。生产媒体、原始 SQLite、完整恢复索引、凭据和未脱敏日志不进入 Git。

Phase 3 已完成 28 个 NAS 文件的有限验证：26 张图片生成指纹，2 个文件跳过，发现 1 对未确认视觉候选。该阶段的结果见 `docs/phases/PHASE3_REPORT.md`，历史 Phase 1/2 报告保留，恢复入口为 `HANDOFF.md`。原始阶段要求、报告与 Phase 0 证据统一见 [阶段资料索引](docs/phases/README.md)。

## Phase 3.1 稳定性维护

已修正 RSS 测试对内存驻留量和采样同步的假设，生产监控及阈值未修改。20 次受控 RSS 场景通过，回归结果见当期验证日志；本轮不需要 NAS，也没有读取照片。定位证据、历史首测的不确定性和轮询边界见 [维护报告](PHASE3_1_REPORT.md)。

本地复现：`.venv/bin/python tools/phase3_1_diagnostics.py --iterations 20`。它顺序执行，保存 RSS/PID/信号/退出证据，不启动照片分析。RSS 轮询仍不是 OS 硬性内存上限。

## 像素与视觉关系（Phase 3）

在工程目录安装可选图像依赖。Phase 1/2 原命令仍可用，Phase 3 使用项目环境：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --only-binary=:all: -r requirements-visual.txt
.venv/bin/python nas_photo_cli.py similarity analyze --manifest config/phase3_samples.json --max-read-bytes 134217728
.venv/bin/python nas_photo_cli.py similarity status
.venv/bin/python nas_photo_cli.py similarity status --job VISUAL_JOB_ID
.venv/bin/python nas_photo_cli.py similarity resume VISUAL_JOB_ID
.venv/bin/python nas_photo_cli.py similarity resume VISUAL_JOB_ID --retry-failed
.venv/bin/python nas_photo_cli.py similarity compare --job VISUAL_JOB_ID
.venv/bin/python nas_photo_cli.py similarity report --job VISUAL_JOB_ID --export
```

manifest 必须显式提供，且文件须已有元数据索引；不会自动发现或全库扫描。Phase 3 的 `config/phase3_samples.json` 固定了 28 个文件，其中原 15 个保留。已完成任务为 `3af529a4ed29418591650a7f7c72f8bf`。查询该任务并导出不读取原图。

L1 是指定解码器/规范化版本下完整 RGBA8 像素相同；L2 只给出视觉候选，不判断应该保留或删除哪张。报告同时保留原元数据、时间异常、来源路径、尺寸、格式、指纹、距离和验证状态。支持 JPEG/PNG/HEIC 主图；MOV、动画/多帧 JPEG/PNG、高位深/HDR、无法可靠转换的色彩模式或超资源图片跳过并记录原因。

调节阈值仅比较本地指纹，源内容读取为零。例如：

```sh
.venv/bin/python nas_photo_cli.py similarity compare --job VISUAL_JOB_ID --phash-distance 4 --dhash-distance 8
.venv/bin/python nas_photo_cli.py similarity compare --job VISUAL_JOB_ID --phash-distance 8 --dhash-distance 12
```

默认 pHash≤8、dHash≤12（各 64 bit），再检查长宽比、颜色和低信息量。候选边不具有传递性，不把相似链合并成相同组。距离不是概率。轻裁剪本地案例可识别，大裁剪未支持；真实候选尚未人工确认。

重复 analyze 会做 stat 验证后复用有效缓存，`--max-read-bytes 0` 可检查零内容读取。`--force` 强制重新分析，仍受显式范围与累计预算约束。缓存不能绝对发现保留相同文件状态的隐蔽内容替换；report/compare 不做实时文件监控。

默认单文件 64 MiB、16 MP、单边 16000、worker RSS 512 MiB、60 秒，单进程解码。任务默认读取预算 256 MiB；`visual_limits` 配置示例见 `config/example.json`。跨视觉任务累计上限 2 GiB，默认另留 512 MiB 给未精确计数的元数据工具；取消、失败和恢复不重置累计预算。`budget_exhausted` 保留待处理项，不会自动放宽读取限制。

首次连接旧数据库会兼容升级到 schema 3，并保留独立的 Phase 2/3 恢复备份。库选择、颜色/透明/方向规则和缓存边界见 `docs/PHASE3_LIBRARY_REVIEW.md`、`docs/ARCHITECTURE.md`。

验证：

```sh
.venv/bin/python -m unittest discover -s tests
.venv/bin/python tools/phase3_local_validation.py
```

第二条只生成本地合成案例证据，不读取 NAS。真实验收的读量、缓存和迁移证据在 `reports/phase3_validation.json`。复现此试点时使用原固定清单。

## 完全重复检测（Phase 2）

来源身份沿用 Phase 1。当前公寓局域网使用 `<LAN_NAS_IP>`，Tailscale 地址与其属于同一 NAS；只要共享挂载根及内部布局相同，无需更改来源 ID。

```sh
python3 nas_photo_cli.py duplicates candidates --manifest config/phase0_samples.json
python3 nas_photo_cli.py duplicates hash --manifest config/phase0_samples.json
python3 nas_photo_cli.py duplicates status
python3 nas_photo_cli.py duplicates status --job HASH_JOB_ID
python3 nas_photo_cli.py duplicates resume HASH_JOB_ID
python3 nas_photo_cli.py duplicates resume HASH_JOB_ID --retry-failed
python3 nas_photo_cli.py duplicates report --job HASH_JOB_ID --export
```

候选查询只读已有数据库。hash 必须给显式 manifest，默认仅处理该范围内大小相同的文件；独特大小只是暂时没有候选，不能证明永久唯一。跨来源文件保留各自身份，只有完整 SHA-256 证据才能建立重复组。

必要时对范围内的非候选执行验证，必须显式增加 `--include-non-candidates`。`--force-rehash` 跳过缓存，但仍只处理 manifest 中合格项；它不会自动扩大范围。示例：

```sh
python3 nas_photo_cli.py duplicates hash --manifest SMALL_EXPLICIT_MANIFEST --include-non-candidates --max-read-bytes 65536
python3 nas_photo_cli.py duplicates hash --manifest SMALL_EXPLICIT_MANIFEST --force-rehash
```

默认块大小 1 MiB，可用 `--chunk-bytes` 改为 1 字节至 4 MiB；默认任务内容读取预算 64 MiB，可用 `--max-read-bytes` 显式调整（软件上限 2 GiB）。配置也可设置 `hash_limits.max_read_bytes`、`hash_limits.chunk_bytes`。单文件大小、文件数、重试和运行时间沿用现有 limits。

**字节预算跨 resume 和重试累计，不自动重置。** `budget_exhausted` 表示有文件尚未处理，resume 不会放宽预算；确有需要时，另建同样显式范围、明确增加预算的任务。缓存命中不消耗内容读取预算，仍需有界 stat 验证。

Schema 1 会在首次写入前备份为 `data/index.pre-phase2.sqlite3` 并迁移至 2，也可显式执行 `duplicates migrate`。已有备份不会被覆盖，历史元数据、标签和任务保留。不要在数据库运行时直接用文件复制替代 SQLite backup。

报告位于 `reports/duplicates-<任务ID>.json/.jsonl/.csv`。未带 `--job` 的 report 查询当前数据库中的有效哈希，不读取照片；`--export` 生成文件。

报告区分成功哈希、缓存命中、失败、程序已观察读取量、未确定读取量和预算占用。超时/硬终止时计数可能只是下界，预算按预留上界保守扣除；没有中间哈希作为有效结果。程序读入字节不是精确 SMB/Tailscale 网络流量。

重复组中的 `potential_logical_bytes_reclaimable` 是按留一份计算的逻辑冗余；硬链接、Btrfs、快照等使物理空间无法据此确定，`actual_disk_space_saved` 保持 null。SHA-256 相同是极强的二进制证据，不是碰撞在数学上绝对不可能的证明。

缓存依据大小、mtime_ns、birthtime 原始值和实现版本；刻意保持大小/时间的内容替换可能漏检。需要时使用显式 force 验证。二进制相同也包含相同的内部 EXIF 字节；文件系统时间和外部索引证据可以不同。视觉相似、不同编码或 EXIF 不同的版本不属于本阶段结论。

## 运行条件

- Python 3.11+（当前实测 3.14 / macOS）。Python 核心无第三方包依赖。
- Git、Perl、`file`；视频需要 FFmpeg 的 `ffprobe`。
- ExifTool 固定在官方 13.59 / commit `2200871d9cef988051d2a99d67df3bda6cbb30a8`，独立使用其 CLI，不改动上游源码。许可保留在 `tools/exiftool/LICENSE`，README 亦保留上游许可说明。[官方仓库](https://github.com/exiftool/exiftool)

在工程目录运行：

```sh
python3 tools/install_exiftool.py
python3 nas_photo_cli.py --help
```

当前本机工具已准备好，安装脚本会核验已有版本，不会强制更新。无需启动 Docker、安装视觉模型或获取 NAS SSH 权限。

## 当前 NAS 的有限扫描

`config/local.json` 已配置五个来源及当前挂载根。`config/phase0_samples.json` 仅包含 Phase 0 的 15 个明确路径。先确保 Finder 已挂载共享；程序不会自行认证或重挂载。

```sh
python3 nas_photo_cli.py scan --manifest config/phase0_samples.json
python3 nas_photo_cli.py status
python3 nas_photo_cli.py status --job JOB_ID
python3 nas_photo_cli.py export JOB_ID
```

scan 会先输出持久任务 ID，然后处理。重跑未变化文件只检查 stat 并跳过元数据提取。数据位于 `data/index.sqlite3`，事件日志在 `logs/events.jsonl`，报告在 `reports/<JOB_ID>.json/.jsonl/.csv`。

**不要把这一受限 manifest 改成全库清单，也不要将 200 个上限理解为全库扫描授权。** 本轮无需增加样本来凑满额度。

## 取消及恢复

Ctrl+C 安全取消。需要继续时：

```sh
python3 nas_photo_cli.py resume JOB_ID
python3 nas_photo_cli.py resume JOB_ID --retry-failed
```

前者续跑待处理条目；后者额外为失败条目重新开放有限重试。机器重启或进程被强制结束后也使用同一命令，先恢复已有共享。已提交文件不会重建成新的身份。

任务状态可能为 `queued`、`running`、`completed`、`completed_with_errors`、`interrupted`、`cancelled`。CLI 返回 0 表示成功，2 表示任务仍有错误或中断，1 表示配置/命令错误；命令外层取消可能返回 130。

## 配置

新配置可参考 `config/example.json` 和 `config/manifest.example.json`。配置默认位于工程的 `config` 子目录；工程根由该位置的上一级确定。

- `device_id` 与 `source_id`：人为指定的稳定逻辑身份。LAN 与 Tailscale 指向同一设备，不能重建来源 ID。
- `mount_root`：可变挂载位置。只改入口，保留来源内部布局。
- `logical_root`：来源在共享内部的相对路径，不能用同名文件夹替代完整路径。
- `relative_path`：manifest 中相对于来源根的路径，拒绝绝对路径、路径逃逸和符号链接。
- `early_filesystem_before`：默认 `2000-01-01`，仅作为 UTC 文件系统日期筛查线。
- `conflict_tolerance_hours`：本机配置为 12 小时，只记录差异，不自动确定正确值。
- `future_tolerance_days`：默认 30 天。
- `file_timeout_seconds`：本机为 60 秒，包含远程 stat/文件头/元数据工具。
- `max_attempts`：默认 2 次；`run_budget_seconds` 默认 900 秒；单进程顺序处理。
- `max_files`：Phase 1 限制为 1..200，显式 manifest 必须满足上限。
- `max_file_bytes`：默认 512 MiB，防止受限测试误读非常大的文件。
- `paths`：数据库、日志、缓存和报告分别存放在本地工程目录下。网络卷输出被拒绝。

修改规则后可从现有证据重算标签，不读取照片：

```sh
python3 nas_photo_cli.py recalculate
```

任务会保存创建时的规则/限额快照；resume 使用该快照，以便同一任务保持一致。若要按新规则重新计算已保存记录，使用 recalculate 或新建显式扫描任务。

## 有限发现

提供受限发现功能，用于未来用户授权的小目录；本轮 NAS 实测没有调用它：

```sh
python3 nas_photo_cli.py discover --source SOURCE_ID --directory SMALL_SUBDIRECTORY --max-files 10 --max-entries 50 --max-dirs 2 --max-depth 1
```

输出 manifest、是否截断、访问错误及计数。硬限制目录深度最多 5、目录最多 50、条目最多 1000、文件最多 200。不会因未知文件格式而无限深入。不要将共享根作为无界递归目标。

## 测试与工程结构

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q src nas_photo_cli.py
```

自动测试使用本地临时数据，包含硬杀进程、路径失效/恢复、超时和来源映射模拟，不修改 NAS 文件。

| 目录/文件 | 用途 |
|---|---|
| `src/nas_photo/` | 配置、I/O worker、SQLite、时间规则、CLI |
| `tests/` | 可复现单元与故障测试 |
| `config/` | 来源、规则和显式 manifest |
| `data/` | 本地 SQLite、WAL 及写入锁 |
| `logs/` | 结构化事件 |
| `cache/` | 预留本地缓存；当前没有原图/缩略图缓存 |
| `reports/` | 验收证据与 JSON/JSONL/CSV 导出 |
| `tools/` | 固定版本 ExifTool 与获取脚本 |
| `docs/ARCHITECTURE.md` | 数据库结构、时间规则、恢复协议及已知限制 |

数据库 raw 数据是所选元数据标签的原始值，不是原文件副本或全部标签取证。默认 GPS 仅保存存在布尔值。内容质量、元数据完整度、时间可靠性与来源可信度分别记录，未生成综合质量分数。

工程不依赖 Progress App；它仅用于工作期间查看共享用量。**不得停止 `http://127.0.0.1:5180/` 服务、关闭共享页面或清理相关 Node/Codex 进程。**
