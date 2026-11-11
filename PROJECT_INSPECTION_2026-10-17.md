# NAS Photo Intelligence Engine 项目检测报告

检测日期：2026-10-17，America/Los_Angeles。完成时间：2026-10-17T13:18:03.980129-07:00。

**结论：公寓外的 Tailscale 远程访问已恢复，现有 Phase 3 项目与数据可用。完整测试复跑 75/75 通过；首次出现过 1 项内存保护测试失败，原因尚未确认，列为测试稳定性问题。**

本次是检查和恢复既有连接，没有启动新照片分析、夜间任务或 Phase 4。项目代码、依赖、配置及数据库没有改动；只新增检查报告和本地日志，并更新交接提示。

## 网络与 NAS

| 检查项 | 当前结果 |
|---|---|
| 当前场景 | 公寓之外，使用既有 Tailscale 网络 |
| Mac Tailscale | Running，版本 1.102.3，健康告警为空 |
| Mac 虚拟地址 | <TAILSCALE_CLIENT_IP> |
| NAS 设备 | <NAS_NAME>，<TAILSCALE_NAS_IP>，在线 |
| 路径探测 | 直连；最后 3 次为 22 / 23 / 21 ms，未显示经 DERP 中继 |
| SMB TCP 445 | 可连接 |
| 共享卷 | 已实际挂载到 `/Volumes/<NAS_SHARE>` |
| 登录身份 | 沿用 AI；未修改账户、共享权限或 NAS 配置 |
| 工程挂载根 | 与现有配置一致，无需改配置或重建来源身份 |

检查开始时没有 NAS 挂载，Finder 仍保存公寓内网 `<LAN_NAS_IP>` 的连接。已取消旧地址的等待，并改用 `smb://<NAS_USER>@<TAILSCALE_NAS_IP>/<NAS_USER>%20%E5%85%B1%E4%BA%AB%E7%BB%99%E6%88%91`。最终通过系统挂载记录、Finder 共享窗口及实际读取三方面验证连接成功。

既有五个逻辑来源仍按 source_id + relative_path 识别；没有新增来源、修正 DICM 拼写或把两个 MobileBackup 混在一起。

## 有限照片读取验证

- 仅检查 `config/phase3_samples.json` 内原有 **28 个独立文件**，28/28 的隔离 stat 探测成功。
- 28/28 的文件大小与时间等 stat 指纹与索引一致，覆盖既有五个来源。
- 另完整读取原有 OneDrive 样本 file_id 4：`<onedrive-01>.jpg`，实读 **53,032 字节**，完整 SHA-256 与历史有效观察一致。
- 校验值：`<sha256-of-private-sample>`。
- 没有递归扫描、发现新样本、重新提取整批元数据、重新解码全部照片或执行删除/改写。

53,032 字节是本轮显式文件内容读取，不是 SMB/Tailscale 网络流量。其余 27 文件本次只验证 stat 和路径可访问性，不能据此声称已重新核对全部文件字节或完成全库检查。

## 项目、数据库及历史结果

| 检查项 | 结果 |
|---|---:|
| SQLite schema | 3 |
| integrity_check | ok |
| foreign_key_check | 无错误 |
| 文件 / 元数据观察 | 28 / 28 |
| 完整 SHA-256 历史观察 | 1 |
| 当前有效视觉指纹 | 26 |
| 当前不同的 L2 候选对 | 1，仍未人工确认 |
| 运行中的任务 | 0 |
| 图像处理 pipeline | 与 Phase 3 记录一致 |
| Python 依赖检查 | No broken requirements found |

当前数据库的 16 张表与 `data/index.phase3-complete.sqlite3` **逐行一致**。没有迁移、增加任务或写入新分析观察。已有 3 条 active 关系行来自三次比较记录，逻辑上是同一对 file_id 17/19，不能误报为三对候选。

三份恢复备份均通过完整性检查：

| 备份 | schema | 完整性 |
|---|---:|---|
| index.pre-phase2.sqlite3 | 1 | ok |
| index.pre-phase3.sqlite3 | 2 | ok |
| index.phase3-complete.sqlite3 | 3 | ok |

## 自动测试与异常复核

| 执行 | 结果 | 耗时 |
|---|---|---:|
| 首次完整测试 | 74 通过，1 失败 | 51.412 秒 |
| 失败用例单独复测 | 1/1 通过 | 0.379 秒 |
| 完整复跑 | 75/75 通过 | 46.369 秒 |

首次失败是 `test_real_timeout_and_rss_guard_only_own_worker`：测试期望工作进程被 RSS 保护终止，但得到正常返回。之后单项与完整复跑均通过。额外在独立本地工作进程中分别分配 200 MiB 的全零和随机数据，128 MiB RSS 阈值均触发 `memory_limit`，仅终止本测试自己的进程。

**这说明保护机制在后续探测中能触发，但不能解释或抹去首测失败。** 当前证据不足以确定首次失败来自测试注入、调度、内存驻留变化或其他原因。保留为偶发测试稳定性问题；长期无人值守运行前应进一步稳定该测试并确认原因。本次按检查范围没有修改实现或放宽断言。

另有附加诊断限制：系统 `smbutil statshares` 对此共享返回 68／share name doesn't exist，未能取得 SMB 协商参数。挂载和实际文件读取均已另行通过，因此不把该工具失败判为共享不可用；也不据此宣称已验证 SMB 协议版本、签名或加密参数。

## Progress App 与额度

检查开始时 `http://127.0.0.1:5180/` 未响应。已按既有启动方式恢复本机服务，主页 HTTP 200，真实用量 API 返回成功，**保持运行**。没有关闭其页面、清理端口或终止其他 Work。

检查记录时 300 分钟窗口剩余约 82%（仅当时快照）。后续仍须查实时值，接近剩余 5% 时停止扩展并交接。

## 证据与恢复入口

- 机器报告：`reports/inspection-2026-10-17.json`。
- NAS 文件状态与代表文件校验：`reports/inspection-2026-10-17-nas.json`。
- 首次测试：`reports/inspection-2026-10-17-tests.log`。
- 单项复测：`reports/inspection-2026-10-17-rss-retest.log`。
- 完整复跑：`reports/inspection-2026-10-17-tests-rerun.log`。
- 独立 RSS 探测：`reports/inspection-2026-10-17-rss-diagnostic.json`。

后续继续使用 `HANDOFF.md` 和 `PHASE3_REQUIREMENTS.md`；当前开发阶段未变化。既有已完成任务和报告可以本地查询，用户明确授权下一步前不扩大扫描或进入下一阶段。
