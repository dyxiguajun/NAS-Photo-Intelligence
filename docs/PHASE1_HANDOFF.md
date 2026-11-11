# NAS Photo Intelligence — 当前交接入口

日期：2026-10-15，America/Los_Angeles。

## 当前状态

用户已解决远程挂载并明确恢复 Phase 1。**Phase 1 命令行 MVP 已实现和验收完成，当前没有后台扫描。等待用户决定下一阶段，不要自动进入 Phase 2或全量/整夜扫描。**

先读 `docs/phases/PHASE1_REPORT.md`，运行方法见 `README.md`，数据库/时间规则/恢复设计见 `docs/ARCHITECTURE.md`。需求保持 `docs/phases/PHASE1_REQUIREMENTS.md`。不要重复 Phase 0，也不要无意义重读大量照片。

## 实际连接及文件范围

- 工程：`<PROJECT_ROOT>`。
- 远程 SMB：`<NAS_USER>@<TAILSCALE_NAS_IP>`，实际挂载 `/Volumes/<NAS_SHARE>`，已实际读样本。
- LAN `<LAN_NAS_IP>` 与 Tailscale 地址为同一 NAS；来源 ID、设备 ID和内部相对路径不随访问地址变化。
- 当前五个来源在 `config/local.json`。真实历史目录叫 `DICM`，不能改成 `DCIM`。
- 两个不同位置的 MobileBackup 始终分开。OneDrive 仅使用图片目录，学校文档没有读取。
- `config/phase0_samples.json` 只包含原来的 15 个文件。本轮新增真实 NAS 样本为 0；不要为了凑满 200 个而遍历全库。
- 来源输入仅读取。当前 SMB 没有 OS 强制只读标志，不能把程序只读设计写成系统层写权限已撤销。

## 可运行成果与证据

- 直接运行 `python3 nas_photo_cli.py --help`，无需安装 Python 第三方包。
- ExifTool 13.59 已在 `tools/exiftool`，固定官方 commit `2200871d9cef988051d2a99d67df3bda6cbb30a8`，获取脚本只核验或首次下载，不强制更新。
- 本地 SQLite：`data/index.sqlite3`，15 个文件 / 15 份元数据观察，integrity_check=ok，外键错误 0。
- 初次任务 `1a7c19eec2f343ed9af389a1d846c1af`：15 成功、0 失败，47.92秒。
- 增量任务 `3304b3add2be4654a8e925f18bd2099e`：15 跳过、0 失败，2.54秒；无新增文件/观察。
- 31 项自动测试通过（最终日志 `reports/unit-tests.log`），包括硬杀恢复、安全取消、模拟路径失效恢复、挂载路径切换、有限重试和时间/写入边界。
- 全部 15 个样本与 Phase 0 的格式、尺寸、原始拍摄时间、大小、修改时间匹配。记录位于 `reports/phase1_validation.json`。
- 报告导出为 `reports/<任务ID>.json/.jsonl/.csv`，日志位于 `logs/events.jsonl`。

## 使用和恢复

```sh
python3 nas_photo_cli.py status
python3 nas_photo_cli.py scan --manifest config/phase0_samples.json
python3 nas_photo_cli.py resume JOB_ID
python3 nas_photo_cli.py resume JOB_ID --retry-failed
python3 nas_photo_cli.py export JOB_ID
python3 nas_photo_cli.py recalculate
```

这些是后续需要时的操作入口，不要求立即重跑已完成任务。resume 延续已提交队列；retry-failed 明确为失败项开放有限新预算。配置里的任务规则/限额在建任务时冻结，重算使用当前规则而无需读取照片。

## 必须保留的判断

- 2000 年以前的文件系统日期只做可配置筛查，不认定所有早期照片错误。
- 无效/超大创建时间保存原始字符串及错误标记；没有偏移的拍摄时间不补 UTC。
- 相差很大的日期可能只是复制/导出；只记证据，不自动修复或选定唯一正确日期。
- S10/S11 当前确认为 JPEG，历史 ImageIO 返回 HEIC 的分歧作为旧观察保留。ExifTool 新读出 -08:00 偏移，不覆盖 Phase 0 原记录。
- GPS 默认只持久记录是否存在，不能无故导出坐标。
- 同 stem 图片/MOV 只构成未验证配套候选，不能删除。
- 元数据完整度、时间可靠性、内容信息、来源信任分开，不恢复单一质量分数。

## 未完成/未测试/未授权

没有 Phase 2 哈希或去重、视觉相似、复杂可信度评分、文件版本/重命名识别、自动整理或照片写操作。时间规则不比较未知时区的绝对秒差；未做所有厂商元数据取证和完整图像/视频损坏检查。

网络中断使用本地路径模拟，未主动断开真实网络；机器重启、整夜扫描、NAS 本机部署、Linux/Windows 均未实测。NAS SSH 主机密钥和 Docker/数据盘部署条件仍待以后确认，不得关闭主机验证或强行使用管理员权限。

未来进入下一阶段前需用户指令。原始照片不得删除、移动、改名、写 EXIF/时间或改权限；不得未经授权扩大为全量扫描。

## 共享额度和 Progress

**必须保留 `http://127.0.0.1:5180/` 服务及共享页面，其他 Work 正在使用。禁止停止或清理相关端口、Node/Codex 进程。** 工程本身不依赖它，仅工作期间读取共享额度。

继续工作时读取真实 300 分钟窗口剩余值，不能使用旧快照。接近 5% 时不再扩展工作，保存代码、数据库、报告和交接后停止本任务，仍保留 Progress。

Phase 0 原文件未覆盖，原交接保存在 `docs/PHASE0_HANDOFF.md`；此前暂停的 Phase 1 状态存于 `docs/PHASE1_PAUSED_2026-10-15.md`，这些历史暂停信息已被本轮用户明确恢复指令取代。
