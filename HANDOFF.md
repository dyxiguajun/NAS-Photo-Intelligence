# NAS Photo Intelligence Engine — Phase 3.1 交接

更新时间：2026-10-17。**Phase 3.1 维护完成，停在该阶段；Phase 4 未授权。** 当前报告为 `PHASE3_1_REPORT.md`，机器验收为 `reports/phase3_1_validation.json`，本轮原要求为 `docs/phases/PHASE3_1_REQUIREMENTS.md`。

## 本轮结论

旧 RSS 整合用例第 7 次重复复现原正常返回：200 MiB 全零对象分配后，RSS 阶段 23 次实测采样最大仅 21.14 MiB，未超过 128 MiB 阈值。已确认测试没有保证实际 RSS 超限；未观察到持续超限而不终止的生产故障。历史首测没有 RSS 时间线，无法逐细节还原，也未最终区分底层页驻留机制。

修订测试使用明确握手、随机填充且持续访问的内存，真实 supervisor/ps/信号路径不变。20 个受控 RSS 场景全部触发 memory_limit，目标全部回收且信号仅指向自身组；正常、崩溃、SIGTERM→SIGKILL、真实 RSS 队列恢复和父进程强杀恢复通过。完整回归 **78 项通过，42.721 秒**。

生产 Python 源码及阈值完全未改，Schema 仍为 3，正式数据库所有行与维护前一致、完整性/外键正常；三份历史备份字节未变。本轮 NAS 内容读取、stat 探测、新样本、写入和重连均为 0。不要为复现维护任务连接 NAS。

## 当前文件与复现入口

- `tests/test_similarity.py`：修订 RSS 断言、父死亡状态检查，新增正常/崩溃/真实 RSS 恢复覆盖。
- `tests/worker_probe.py`：仅测试层的真实进程与内存 fixture，记录握手、RSS、PID/PGID、信号和回收。
- `tools/phase3_1_diagnostics.py`：本地顺序诊断工具，不读 NAS 或正式数据库。
- `reports/phase3_1-before/`：维护前源码副本。
- `reports/phase3_1-legacy-rss.json`：原用例失败时间线；复现脚本读取旧副本。
- `reports/phase3_1-rss-diagnostic.json`：20 轮实测数据。
- `reports/phase3_1-tests.log`：完整回归日志。

```sh
.venv/bin/python tools/phase3_1_diagnostics.py --iterations 20
.venv/bin/python -m unittest discover -s tests
```

已有证据足够时无需重复用完 20 轮。测试使用本地临时图像/数据库，不能把模拟结果当作新 NAS 验收。单项资源 limited 不是普通 pending：移除资源问题后用新显式任务；强杀遗留项使用原任务 resume，已提交观察不重做。

## 保留边界

RSS 轮询不是 OS 硬上限，采样最大值不是生命周期绝对峰值，仍可能漏过瞬时超限。主进程死后的 worker 最终回收由 OS 接管；测试确认其消失或非可运行，不能冒称由死亡父进程回收。

不要重做 Phase 0～3，不修改视觉相似规则，不开始 Phase 4，不新增 GUI/部署/全库扫描，不执行清理或修复照片。

## 现有数据与网络背景

只读数据库再次确认 28 文件、28 元数据观察、26 有效视觉指纹及原有 SHA/任务/关系保留，正式库 `data/index.sqlite3`，schema 3。已有主视觉任务 `3af529a4ed29418591650a7f7c72f8bf`。

公寓外 Tailscale NAS 地址 <TAILSCALE_NAS_IP>，局域网地址 <LAN_NAS_IP> 是同一设备，AI 共享通常挂载 `/Volumes/<NAS_SHARE>`。这是前次检查背景，本轮没有重新检查或重挂载。网络离线不妨碍 Phase 3.1 本地复现。来源身份始终 source_id + relative_path；DICM 保留真实拼写，两个 MobileBackup 分开。

原始 NAS 照片不得删除、移动、改名、改 EXIF/时间/权限、覆盖或外传。Phase 3 固定范围 `config/phase3_samples.json` 仍为原 28 项；不得自动扩大或开始下一阶段。

## 服务与额度

共享 Progress App `http://127.0.0.1:5180/` 保持运行，本轮确认主页 HTTP 200、用量接口有效。不能停服务、关页面、清端口或通用 kill/pkill。读取真实 300 分钟额度，接近剩余 5% 保存成果停止；报告中的百分比只是记录时快照。

## 历史与恢复资产

历史开发文档在 `docs/phases/`，前次检查为 `PROJECT_INSPECTION_2026-10-17.md`。维护前完整交接保存在 `docs/PHASE3_HANDOFF_2026-10-17_before_maintenance.md`，旧交接及报告没有覆盖。

`data/index.pre-phase2.sqlite3`、`data/index.pre-phase3.sqlite3`、`data/index.phase3-complete.sqlite3` 均保留原字节；无需新迁移、重建索引或覆盖备份。当前没有后台照片任务。本轮工作已结束，等待用户下一阶段明确授权。
