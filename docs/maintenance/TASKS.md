# OpenCode 本地任务队列

状态：`queued` / `in_progress` / `blocked` / `done`
优先级：P0 阻断交付，P1 高，P2 中，P3 低

| ID | P | 状态 | 任务 | 验收条件 |
|---|---|---|---|---|
| OC-001 | P0 | in_progress | Git 基线：初始化、敏感审计、脱敏、忽略规则、首次本地提交 | `git status` 干净；无数据/媒体/凭据被跟踪；测试通过 |
| OC-002 | P0 | in_progress | GitHub Actions CI：依赖、密钥扫描、静态检查、格式检查、编译、全量测试 | PR 上自动运行；合成夹具即可执行；失败会阻止合并 |
| OC-003 | P1 | done | 仓库边界策略测试 | 受禁路径/扩展名被拒绝；`.gitignore` 关键项存在；单文件 ≤2 MiB |
| OC-004 | P1 | done | 库存幂等、计数与路径安全回归测试 | 重复批次不产生重复行；同尺寸不同 SHA 不误判；Unicode/空格/特殊字符往返一致 |
| OC-005 | P1 | done | 将 DSH evidence worker/runner 发布到受版本控制路径 | Codex 已发布 `tools/nas-evidence/`；`test_evidence_job.py` 在干净 checkout 可导入 |
| OC-006 | P1 | queued | 损坏媒体解析安全测试：PNG CRC、多流拼接、截断、伪造签名、异常块长度、解压资源耗尽 | 合成夹具；无真实照片；失败样本最小化；发现缺陷附复现 |
| OC-007 | P1 | queued | SQLite 写入失败与进程中断故障注入 | 事务回滚无半成品；重启恢复不漏文件；原始文件字节不变 |
| OC-008 | P1 | blocked | 审计 Codex 的共享状态/消息同步机制 | 原子写、消息去重、任务幂等、并发写不覆盖；待 Codex 发布后执行 |
| OC-009 | P2 | queued | Docker 实际可重建性验证 | 在 CI 或本地 Docker 中构建固定摘要镜像；失败可复现 |
| OC-010 | P2 | queued | 独立审计 `organize_transfer.py` / rclone 练习 | 不修改 Codex 文件；提交独立缺陷报告或 PR |
| OC-011 | P0 | blocked | 创建 GitHub 私有仓库并首次推送 | 用户确认仓库名/可见性；敏感扫描通过；推送后 CI 绿 |
| OC-012 | P2 | queued | 对 `src/nas_photo` 做数据丢失风险审计 | 按文件优先级输出问题清单；高优先级附最小复现 |

## 工作循环

同步状态 → 读取消息 → 选择一项 → 执行 → 验证 → 写 outbox → 更新本表与 `STATE.md` → 继续下一项。

## 不做的事

- 不直接修改 Codex/DSH 正在编辑的文件；不合并影响生产数据行为的 PR。
- 不上传原始照片、生产数据库、真实索引、NAS 凭据。
- 不在未确认时删除仓库、改可见性、改所有者或扩大权限。
