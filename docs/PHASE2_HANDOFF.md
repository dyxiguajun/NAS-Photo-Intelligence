# NAS Photo Intelligence — 当前交接入口

日期：2026-10-15，America/Los_Angeles。

## 状态

**Phase 2 完全重复检测已实现和验收完成。当前没有后台照片任务；等待用户决定下一阶段，不自动进入 Phase 3。**

先读 `docs/phases/PHASE2_REPORT.md`，使用方法见 README，数据模型和限制见 `docs/ARCHITECTURE.md`，本阶段原要求在 `docs/phases/PHASE2_REQUIREMENTS.md`。历史报告及 JSON 保留，不重复 Phase 0/Phase 1 调查。

## 环境与授权

用户已回到公寓局域网。本轮挂载 `<NAS_USER>@<LAN_NAS_IP>` 到 `/Volumes/<NAS_SHARE>`，旧 Tailscale 地址 `<TAILSCALE_NAS_IP>` 属同一 NAS。来源身份仍是 source_id + relative_path，与 IP 和挂载名称无关。网络入口改变时只调整 mount_root，保持来源布局。

真实测试仍限制 `config/phase0_samples.json` 的原 15 个文件。Phase 1 软件 200 文件上限不代表新增 NAS 哈希授权。历史目录叫 DICM；两个 MobileBackup 分别作为不同来源，OneDrive 仅照片目录。

原始照片仅读取，不能删除、移动、改名、写 EXIF/时间、改权限或复制全库。没有 NAS Docker/SSH 部署，也没有公开网络端口。

## 当前成果与实测

- 同一 `data/index.sqlite3`，schema 2；迁移前备份 `data/index.pre-phase2.sqlite3`（schema 1，可恢复）。正式迁移旧行逐一保持，先在副本测试再迁移。
- 15 文件、15 原元数据观察仍保留，SQLite integrity_check=ok，外键错误 0，Live Photo 配套候选保留。
- 大小候选：0 组 / 0 成员。默认 15 项哈希任务全部 not_candidate，内容读取 0。
- 原 S04 非候选单文件验证：成功完整 SHA-256，读取 53,032 字节。第二次命中缓存，读取 0，未新增哈希观察。
- NAS 当前有效哈希观察 1，重复组 0；不能将无候选说成已证明全库或这些文件永久唯一。
- 本轮 54 项测试通过，包括 Phase 1 回归、跨来源正例、同大小不同内容、变化与旧组退出、超时、部分失败、取消/SIGKILL 恢复、预算、迁移、空文件/硬链接。
- 本地模拟正例导出 `reports/phase2-local-positive.*`：4 个合成文件，3 成员跨来源重复组，逻辑冗余 62 字节；不是 NAS 真实照片。
- 实测、哈希值、前后状态和字节量见 `reports/phase2_validation.json`；测试日志 `reports/phase2-tests.log`；迁移证据 `reports/phase2-migration-check.json`。

三个 NAS 哈希任务：

- 候选任务 `22d0b41a663c4bd09c17f248d4091117`。
- 单文件完整哈希 `ebfc15ca3f544006959fd5db750b05ec`。
- 单文件缓存复用 `24dda05c02484d839d158c046a49f69a`。

## 命令

```sh
python3 nas_photo_cli.py duplicates candidates --manifest config/phase0_samples.json
python3 nas_photo_cli.py duplicates hash --manifest config/phase0_samples.json
python3 nas_photo_cli.py duplicates status
python3 nas_photo_cli.py duplicates resume HASH_JOB_ID
python3 nas_photo_cli.py duplicates resume HASH_JOB_ID --retry-failed
python3 nas_photo_cli.py duplicates report --job HASH_JOB_ID --export
```

无需立即重跑已完成任务。hash 必须显式 manifest，默认仅同大小候选；include-non-candidates 是明确扩展到该 manifest 的非候选，不是扩大 NAS 样本授权。force-rehash 绕过缓存，但保留同样范围和资源上限。

旧 scan/status/resume/export/recalculate 保留。哈希任务使用 duplicates resume，不能让元数据 resume 误处理。Phase 1 兼容导出测试写到 `reports/phase2-compat/`，原导出未覆盖。

## 参数与可靠性

- 块默认 1 MiB，支持 1 字节..4 MiB；整个哈希任务读取预算默认 64 MiB，CLI max-read-bytes 0..2 GiB；默认单文件限 512 MiB。
- 读取预算跨重试/resume 不重置。先预留 size，正常按实际读入结算；硬终止不明部分继续按上界扣除，报告给出下界和不精确标志。
- budget_exhausted 会保留待处理条目；resume 不增加预算，需要时另建明确预算且同样授权范围的新任务。
- 单文件整体截止、有限重试、每次 run/resume 运行时间限制沿用现有配置。硬杀的未提交尾部运行时间不当作精确性能统计。
- 原文件从头重算，不恢复 SHA 内部状态；部分哈希绝不标记 valid。
- stat 缓存不能识别所有相同大小/时间的替换；测试明确覆盖其局限及 force 的复核能力。
- 动态重复视图仅使用当前 valid 指针及完整观察。索引状态变化、哈希验证中或不可读时排除旧有效关系，保留历史证据。
- 纯数据库报告是截至最近观察的状态，不会主动重新读源文件或监控文件系统。

## 判断边界

SHA-256 相同是极强的二进制相同证据，不是理论碰撞不可能的证明。二进制相同包含相同内部 EXIF；外部文件时间或提取记录可能不同。不同编码、分辨率、EXIF 的视觉同图留待 Phase 3。

逻辑冗余不能当物理释放；hardlink/Btrfs/快照/压缩未实测，actual_disk_space_saved 为空。图片/MOV 配套关系不是重复关系。

未做全库、整夜、视觉相似、照片写入、元数据修复、NAS 本机部署、真实断网/重启或大规模性能测试。受控故障全部使用本地模拟文件，不能宣称真实 NAS 中断已验收。

## 共享 Progress 与额度

**不得停止 `http://127.0.0.1:5180/`，不得关闭共享页面或清理相关 Node/Codex/其他 Work 进程。** 检查时服务未运行，本轮已恢复其已有构建，结束保持运行。工程本身不依赖 Progress。

继续工作时读取真实共享 300 分钟剩余，不能依赖旧快照。接近 5% 时保存代码、数据库、测试记录和交接后停止本任务，仍保留 Progress。

Phase 1 原交接另存 `docs/PHASE1_HANDOFF.md`。Phase 0/1 原报告与测试日志保留；旧“等待 Phase 2”状态已被用户本轮明确授权取代。
