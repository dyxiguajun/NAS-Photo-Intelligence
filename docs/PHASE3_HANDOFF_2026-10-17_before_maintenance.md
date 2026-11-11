# NAS Photo Intelligence Engine — Phase 3 交接

更新时间：2026-10-15。**Phase 3 基础开发和有限 NAS 验收已完成，停在该阶段；没有自动夜间任务或 Phase 4 工作。** 用户原要求：`docs/phases/PHASE3_REQUIREMENTS.md`。验收结论：`docs/phases/PHASE3_REPORT.md`，机器证据：`reports/phase3_validation.json`。

## 必须遵守

- 原始 NAS 照片只读；不能删除、移动、重命名、改 EXIF/时间/权限、覆盖重编码或上传第三方。
- Phase 3 累计最多 100 个独立文件，原 15 个计入；本轮已使用 **28 个**，其中新增 13 个。固定范围 `config/phase3_samples.json`。不要自行扩大范围或全库扫描。
- 当前只做 L0/L1/L2 关系，不决定保留哪张，不建立删除索引或执行整理。下一阶段等用户明确授权。
- **共享 Progress App `http://127.0.0.1:5180/` 必须继续运行**，不得关页面、停服务、清端口或终止其 Node/Codex/其他 Work。读取使用 `/api/codex/usage`，请求头 `X-Progress-Client: usage-v1`；查 300 分钟窗口实时值，接近剩余 5% 停扩展并交接。保存快照 `reports/phase3-usage-final.json` 不是实时值。

## 环境与来源

MacBook Air M1、8GB、Python 3.14。项目环境 `.venv/bin/python`；固定依赖 `requirements-visual.txt`，安装后约 182 MiB。ExifTool 仍为 13.59，ffprobe 沿用原配置，不提升核心 metadata extractor 版本。

此次 `<NAS_USER>@<LAN_NAS_IP>` 挂载 `/Volumes/<NAS_SHARE>` 成功；Tailscale 地址 `<TAILSCALE_NAS_IP>` 为同一 NAS。后续先按需验证挂载，不保证一直在线。身份是 `source_id + relative_path`，IP/挂载点不作为唯一身份。历史目录真实名称 DICM，两个 MobileBackup 分开，OneDrive 只访问确认的图片目录。不要重做 Phase 0。

## 当前数据和任务

- SQLite `data/index.sqlite3`，schema 3；28 文件、28 元数据观察、原完整 SHA 观察 1 条保留。
- 26 个当前有效视觉指纹：JPEG 18、PNG 6、HEIC 2。跳过 MOV 与多帧 JPEG 各 1。
- NAS L1 组 0；L2 候选 1（OneDrive file_id 17/19，pHash=8、dHash=0，未确认）。收紧阈值则无候选。不能把它当作同源证明。
- 主视觉任务 `3af529a4ed29418591650a7f7c72f8bf`，expanded 日志 `reports/phase3-nas-expanded.log`。
- 最终缓存任务 `c514329d2d9f4ad6b688b7f5574be6a5`：26 缓存、2 跳过，0 内容读取。
- 原缓存历史任务 `eddc44b0466040e0bfd96ecb781eaa79` 已在修复后恢复完成，0 字节；早期 budget_exhausted 日志保留作为过程记录，最新为 `reports/phase3-original-cache-completed.json`。
- 视觉累计实际/计费 51,462,535 字节，另预留 512 MiB 给未逐 read 计量的元数据工具；后者不能冒充精确测量或严格证明的 OS 上界。阶段上限 2 GiB，不重置已用视觉预算。
- 原 28 文件前后 stat/身份一致，程序 NAS 写操作为 0。

## 可执行入口

在工程根目录运行；查询和比较不读取原图：

```sh
.venv/bin/python nas_photo_cli.py similarity status --job 3af529a4ed29418591650a7f7c72f8bf
.venv/bin/python nas_photo_cli.py similarity report --job 3af529a4ed29418591650a7f7c72f8bf --export
.venv/bin/python nas_photo_cli.py similarity compare --job 3af529a4ed29418591650a7f7c72f8bf
.venv/bin/python -m unittest discover -s tests
```

如用户需要复核零读取缓存：

```sh
.venv/bin/python nas_photo_cli.py similarity analyze --manifest config/phase3_samples.json --max-read-bytes 0
```

analyze 必须显式 manifest 且已有元数据索引；resume 是 `similarity resume JOB_ID`，失败重试需 `--retry-failed`；`--force` 仍受范围和累计预算限制。旧 metadata/hash 使用各自原命令，不能用普通 resume 处理视觉任务。

## 代码与规则

- `imaging.py`：受控完整读取、RGBA8/ICC/EXIF 规范化、完整像素 SHA、成熟 ImageHash pHash/dHash、资源检查与解码证据。
- `similarity.py`：复用 jobs/job_items 的 durable 队列、原哈希代表复用、有效缓存、预算恢复、成对比较与导出。
- `visual_migration.py`、`migrations.py`：加法事务到 v3，恢复备份与回滚。
- `process.py/worker.py`：单文件隔离、自己的进程组超时/RSS 保护和父进程退出监护。
- `tests/test_similarity.py`：新测试；旧测试仅调整当前 schema 预期、测试连接关闭等兼容点。
- `tools/phase3_local_validation.py`：本地合成正例/裁剪边界，不读取 NAS。
- `tools/phase3_acceptance.py`：固定 28 文件的只读 stat 核对、本地比较、数据历史和报告验证；不发现/下载新照片。

详细颜色/透明/方向、缓存局限、非传递性及保护强度在 `docs/ARCHITECTURE.md`。库与许可依据在 `docs/PHASE3_LIBRARY_REVIEW.md`。

## 恢复资产和测试

`data/index.pre-phase2.sqlite3` 为 schema 1，`data/index.pre-phase3.sqlite3` 为升级前 schema 2，均不得覆盖。`data/index.phase3-complete.sqlite3` 为本轮最终本地快照。旧 Phase 0/1/2 报告、交接及验收日志保留；前一交接为 `docs/PHASE2_HANDOFF.md`。

最终 75 项测试通过（33.432 秒），日志 `reports/phase3-tests.log`；`pip check` 通过。数据库 integrity_check=ok，外键无错误，全部旧行保留。完成状态以最终报告/机器验收为准，早期测试失败日志仅为调试历史。

当前无需后台运行。后续接手先读本交接和 PHASE3_REPORT，查实时额度及挂载；不要因为换模型而重扫，也不要把模型作为逐图推理依赖。

## 2026-10-17 公寓外项目检查

已通过 Tailscale <TAILSCALE_NAS_IP> 恢复 AI 共享，挂载点保持 `/Volumes/<NAS_SHARE>`，配置无需修改。28 个既有样本 stat 均与索引一致；完整校验原有 53,032 字节样本与历史 SHA 一致。数据库及三份备份完整，现库所有表与 Phase 3 完成快照逐行一致；没有新任务、迁移或分析。

首次完整测试 74/75，一项 RSS 保护用例失败；单项复测、独立内存探测和完整复跑 75/75 均通过，首次原因未确诊，保留稳定性问题，不能用复跑覆盖首测。SMB 参数查询工具返回 68，但实际挂载和读取正常。Progress App 初始未运行，已恢复并保持运行。

详细检测与证据见 `PROJECT_INSPECTION_2026-10-17.md`、`reports/inspection-2026-10-17.json`。仍停在 Phase 3，不扩大真实样本或自动启动夜间/下一阶段任务。
