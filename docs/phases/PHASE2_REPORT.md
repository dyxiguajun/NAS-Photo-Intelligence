# NAS Photo Intelligence Engine — Phase 2 验收报告

日期：2026-10-15，America/Los_Angeles。

## 结论

Phase 2 已完成：在现有工程和同一 SQLite 数据库上增加大小候选筛选、分块 SHA-256、缓存、持久化哈希任务、动态重复组、I/O 统计及 JSON/JSONL/CSV 导出。没有重新设计 Phase 1，也没有开发 GUI、视觉相似或自动整理。

真实 NAS 样本仍是原来的 15 个文件，新增真实样本为 0。15 个文件大小均不相同，因此默认候选为 0、默认内容读取为 0。对原样本 S04 单独验证完整哈希和缓存，第一次读取 53,032 字节，第二次命中缓存读取 0 字节。真实重复组为 0，未扩大范围寻找正例。

## 当前环境

用户明确说明已返回公寓局域网，优先于交接文本中的 Tailscale 背景。本轮确认 LAN `<LAN_NAS_IP>:445` 可达，通过 Finder 复用已有 AI 身份挂载 `<NAS_USER> 共享给我`，真实路径为 `/Volumes/<NAS_SHARE>`。未创建用户、修改权限或网络配置。

来源 ID、设备 ID及来源内部路径保持不变；LAN 和 Tailscale 被视为同一 NAS 的两个入口。没有访问 TimeMachine 内部内容。

检查时 Progress 服务未运行，已用其现有构建恢复 `127.0.0.1:5180` 服务；没有修改其源码、关闭页面或清理其他进程。结束时继续保留服务。

## 实现内容

- 继续复用 Python 标准库、原 Config、单写入锁、隔离 worker、jobs/job_items、错误表和日志；SHA-256 使用 hashlib，不安装新框架或模型。
- 候选只按显式 manifest 中的索引大小分组，支持跨来源，不依赖名称、扩展名、日期或相机信息。
- 单个大小项为 not_candidate，不能解释为永久唯一，后续索引增长可重新规划。
- 全文件按可配置块顺序读取，默认 1 MiB。没有额外部分指纹阶段，避免增加小样本随机 I/O。
- 完整哈希与前后文件状态、实现版本、完成时间、读入量及错误一起持久保存。中间或失败结果没有有效 digest。
- 路径与 fd 的大小、mtime、设备/inode 及其他可用状态检查；结束补查 ctime。前后状态一致仍不能绝对证明期间从未变化。
- 缓存复用必须经过 live stat，并匹配大小、mtime_ns、birthtime 原值及实现版本。显式 force-rehash 可以绕过缓存，但不能扩大 manifest。
- 当前指针 valid/stale/unavailable/verifying 与观察历史分离。有效重复组由视图动态生成，重新验证变化文件后旧成员退出；失败不丢失原元数据或哈希历史。
- 原有 Live Photo 候选关系保留；同名图片和 MOV 不因此成为重复文件。
- 文件数、单文件大小、单文件整体截止时间、每次运行时间、有限重试、整个任务读取预算共同限制工作。
- 字节预算跨重试/恢复累计。硬终止无法精确结算时，计数只作下界，预留大小继续作为上界扣除；未完成哈希不发布。

## 数据库迁移和兼容性

原库 `data/index.sqlite3` 从 schema 1 迁移到 2。事先保存可恢复副本 `data/index.pre-phase2.sqlite3`，没有反复覆盖备份。

先在本地数据库副本验证迁移及幂等，再处理正式数据库。正式迁移前后的旧 sources、files、jobs、job_items、observations、labels、time_evidence、candidate_relations 逐行核验保持一致（新增列按旧字段前缀比较）。新增表与视图记录在 `schema_migrations`。

结果：15 条文件记录、15 份元数据观察及原标签/配套候选保留；SQLite integrity_check=ok，外键错误 0。Phase 1 的原命令保留；本轮重新执行状态查询与旧任务导出，兼容导出写在 `reports/phase2-compat/`，不覆盖原 Phase 1 导出。原 31 项测试在本轮全套测试中重新通过。

## 真实 NAS 测试

| 指标 | 实际结果 |
|---|---|
| 已索引真实文件 | 15 |
| 新增真实文件 | 0 |
| 大小候选组 / 候选成员 | 0 / 0 |
| 默认计划需要完整哈希 | 0 |
| 为验证 SMB 哈希链路单独选择的原样本 | S04，OneDrive 图片目录中原有 JPEG，53,032 字节 |
| 完整哈希成功 / 失败 | 1 / 0 |
| 后续缓存命中 | 1 |
| 当前有效完整哈希观察 | 1（缓存复用未新增观察） |
| 真实完全重复组 / 成员 / 冗余文件 | 0 / 0 / 0 |
| 潜在逻辑冗余 | 0 字节 |
| 哈希测试内容实际观察读入量 | 53,032 字节 |
| 缓存避免的内容读取 | 53,032 字节 |
| 部分指纹读入量 | 0（未实现该阶段） |
| 原始照片写入操作 | 0 |
| 数据库大小 | 266240 字节（此轮记录时） |

每个任务独立计数：

| 任务 ID | 完成状态计数 | 内容读取字节 | 耗时（秒） |
|---|---|---:|---:|
| 22d0b41a663c4bd09c17f248d4091117 | {'not_candidate': 15} | 0 | 0.0003 |
| ebfc15ca3f544006959fd5db750b05ec | {'done': 1} | 53032 | 0.5647 |
| 24dda05c02484d839d158c046a49f69a | {'skipped': 1} | 0 | 0.1305 |

单样本任务显式使用 include-non-candidates，读取预算 65,536 字节；这是验证真实 SMB 文件读取与缓存链路所需的有限正向测试，仍在原来的 15 项内。没有对其余非候选进行内容哈希，也没有重新提取 EXIF。

上述读入量来自程序的文件 read 返回数据，不是精确的 SMB/Tailscale 网络流量。统计包含进程启动和状态探测；53KB 小样本不足以估算整库吞吐、NAS 磁盘性能或决定将计算迁移到 NAS。

## 本地正例及自动测试

本轮最终 **54 项通过，0 失败、0 跳过，23.332 秒**。日志为 `reports/phase2-tests.log`，没有用旧阶段结果冒充新测试。

覆盖原 Phase 1 的 31 项回归，以及 Phase 2 的：

- 不同名称/路径、跨来源但字节相同；保留独立文件 ID并建立重复组。
- 同名不同内容、同大小不同内容，不产生错误重复组。
- 新文件使原 not_candidate 成为候选；未变化缓存为 0 内容读取，force 重新读取。
- 文件大小或 mtime 变化后重算；组成员变化后旧关系退出；元数据索引状态变动触发失效。
- 中途修改、部分读异常、真实隔离 worker 读取超时，都不发布不完整哈希。
- 主进程 SIGKILL 后保留已完成文件，仅重做未完成项；取消后恢复；未知尾部 I/O 标记为不精确。
- 字节预算跨恢复不重置；零字节新预算仍可使用有效缓存。
- 同一来源换挂载根继续复用身份；路径失效后有限重试、恢复后缓存重激活。
- 符号链接拒绝、安全路径保护、Live Photo 配套候选与二进制重复分离。
- 空文件、单字节、硬链接逻辑成员；仅报告逻辑冗余，不承诺物理释放。
- 同大小且保留 mtime 的替换可能继续命中缓存；force 验证发现差异。这项测试明确证明并保留缓存限制。
- schema 1 -> 2、恢复备份、幂等及旧记录保留；重复组报告关联原元数据。

另保存一个可查看的**纯本地模拟**报告：4 个合成字节文件，其中 3 个跨来源副本形成 1 组，另一文件大小相同但内容不同，不进入该组。程序读取 124 字节；潜在逻辑冗余 62 字节，实际物理释放为 null。JSON/JSONL/CSV 在 `reports/phase2-local-positive.*`，这些不是 NAS 照片或真实库重复结果。

Phase 1 图片索引通常不接纳无法识别格式的空文件；Phase 2 哈希模块本身支持已有索引中的空/极小文件，测试通过直接建立受控临时索引来覆盖这一边界。

## 交付及恢复

统一入口仍为 `python3 nas_photo_cli.py`。新增 `duplicates candidates/hash/status/resume/report/migrate`，说明见 README。

- 实现：`src/nas_photo/duplicates.py`、`hashing.py`、`migrations.py`，复用现有 worker 和持久队列。
- 数据：`data/index.sqlite3`；迁移恢复副本：`data/index.pre-phase2.sqlite3`。
- 机器验收：`reports/phase2_validation.json`，包含实际 SHA-256、文件身份、前后状态与 I/O 统计。
- 迁移证据：`reports/phase2-migration-check.json`。
- NAS 执行记录：`reports/phase2-nas-candidates.log`、`phase2-nas-single.log`、`phase2-nas-cache.log`。
- 每个哈希任务的重复组导出：`reports/duplicates-<任务ID>.json/.jsonl/.csv`。
- 本地正例：`reports/phase2-local-positive.json/.jsonl/.csv`。
- 工程架构：`docs/ARCHITECTURE.md`；当前恢复入口：`HANDOFF.md`。
- Phase 0 / Phase 1 原报告、原验收 JSON、原测试日志未覆盖；Phase 1 原交接保存在 `docs/PHASE1_HANDOFF.md`。

哈希中断后使用 `duplicates resume JOB_ID`；失败项需要时加 retry-failed。已完成条目不重做，当前文件从头重新计算。budget_exhausted 不会自动放宽额度；确需增加预算时另建同样显式、经授权范围的任务。

## 限制和未执行事项

SHA-256 一致是极强二进制内容证据，非数学上绝不碰撞。若二进制完全一致，内嵌 EXIF 字节也一致；外部文件系统时间或旧提取报告不同，不表示内部 EXIF 不同。视觉内容相同、格式/分辨率/内嵌元数据不同的版本交给以后阶段。

缓存与前后 stat 检查不能捕捉所有期间变化。没有文件系统实时监视，报告有效性截至最近记录的观察；纯数据库查询不会再次确认源文件当前状态。强制中断计数可能只是下界，不能将其包装成精确网络流量。

没有检查 Btrfs 的实时物理块、快照、压缩或去重配置。potential_logical_bytes_reclaimable 只是按留一份计算的逻辑冗余，actual_disk_space_saved 保持 null；本轮没有删除行为。

故障和正例测试仅本地模拟；未切断真实 NAS 网络、未对真实照片注入修改、未重启机器。未做大文件/全库性能、NAS/Linux/Windows 部署、部分指纹或任意字节偏移续算。

Phase 2 完成后停止，不自动进入 Phase 3、不启动全库或整夜任务，不修改 NAS 用户、权限、SSH、存储和网络。

## 额度及共享服务

使用 Progress 本地真实接口监控共享 300 分钟额度，最新快照在 `reports/phase2_usage.json`。本轮没有接近剩余 5% 暂停线；无需耗尽额度。Progress 服务及共享页面保持可用，不停止或清理其 Node/Codex 进程。
