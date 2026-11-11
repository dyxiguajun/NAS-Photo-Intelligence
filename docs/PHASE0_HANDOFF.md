# NAS Photo Intelligence — 交接入口

日期：2026-10-15，America/Los_Angeles。

## 当前状态

Phase 0 环境验证已完成。**下一轮等待用户明确进入 Phase 1；本轮没有实现照片引擎，也没有启动整夜/全量扫描。**

先读 `docs/phases/PHASE0_REPORT.md`，需要具体字段时查 `docs/phases/evidence/phase0_samples.json`，环境和路径查 `docs/phases/evidence/phase0_environment.json`。需求原文在 `docs/phases/PHASE0_REQUIREMENTS.md`。不要重新遍历整库或反复调查已验证内容；需要使用易变连接时只做最小健康检查。

## 已确认

- 工程：`<PROJECT_ROOT>`。检查开始时为空，现在只含调查产物。
- 运行在用户 Mac，macOS 15.8 / M1 / 8 GiB；可直接读本地与挂载 NAS。
- SMB：`/Volumes/<NAS_SHARE>`，服务器 `<LAN_NAS_IP>`，用户 AI。已通过实际目录和文件读取验证。挂载本身未强制只读，不要把“本轮只读操作”误写成“NAS 拒绝写入”。
- 三个来源：该挂载下的 `Document`、`OSU_OneDrive`、`Photos`。
- 历史根：`Document/CH_Photos/Photos`；相机目录**实际叫 DICM，不叫 DCIM**。
- `Document/CH_Photos/Photos/MobileBackup` 与 `Photos/MobileBackup` 是两个独立来源，不能按同名合并。
- OneDrive 样本从 `OSU_OneDrive/图片` 获取；学校文档未打开。T7 SSD 仅确认存在及顶层目录。
- 14 张照片 + 1 个 MOV，小预览/媒体信息检查成功；这不是完整图像/视频完整性验证，更不是全库质量结论。
- S01/S02 修改时间为 Unix 纪元附近，SMB 创建时间超出日期范围；S02 仍有 2024 年内部拍摄时间。
- S06 OneDrive 保留 2021 年内部拍摄时间，文件时间为 2024 年。
- S03 内部时间按显式偏移与文件时间差 16 小时，不能猜测修复。
- S07/S12 相机型号不同于备份文件夹名。S10/S11 经文件头、file、sips 核验为 JPEG；ImageIO 原始 UTI 却报 HEIC。保留多工具冲突，不要重述早期“jpg 实为 HEIC”的已纠正判断。
- S12/S15 为 Live Photo 配套候选，未验证内容标识。
- GPS 只保存存在布尔值，没有保存坐标；没有上传照片。

## 工具与未验证条件

- Python / SQLite / FFmpeg / ffprobe / sips / file 可用。PATH 未找到 ExifTool。
- 默认 Python 无 Pillow，既有 Codex 独立 Python 内有 Pillow 12.3.0；NAS 不应依赖这条 Mac 路径。
- 本轮 HEIC 元数据/小预览使用 macOS ImageIO；Linux HEIF 支持仍待验证。
- Mac Docker CLI 已有，但 daemon socket 不存在；没有启动 Docker Desktop。
- NAS SSH 端口可达，严格主机密钥验证失败，尚未认证或执行 NAS 命令。不要通过关闭验证来继续，也不要把该失败说成密码错误或 AI 账号被拒绝。
- NAS Docker 状态、管理身份、真实 Linux 路径、数据盘工作目录、当前系统盘余量均未知。历史硬件/空间数据来自用户文档，不能写成实时实测。

## 下一阶段方向

Mac 开发及有限样本验证，NAS 本机基础索引，必要时由 Mac 辅助图像计算。SQLite 标签/证据放运行主机的独立可写数据目录，避免 Mac 通过 SMB 操作 WAL 数据库。完整重复先于视觉近似，保留来源、版本、时间偏移和冲突；所有清理/修复先生成待审核候选。

批处理交给普通程序：有限并发、资源上限、检查点、错误日志和可取消操作。用户的低成本模型整夜计划不是本轮全量扫描授权，也不要求大模型逐张分析照片。

## 必须保留的执行边界

- 原始照片仅读取；不删除、移动、改名、写 EXIF、写时间或改权限。
- 未授权全量扫描、完整哈希库、感知哈希库、自动去重或恢复。
- 未安装大型依赖/镜像，未创建容器，未修改 NAS 用户、共享、SSH、网络或存储。
- 用量按 300 分钟窗口剩余判断；接近 5% 时保存交接并停止本任务。账户额度由多个 Work 共享，继续工作时重新读取实时值，不能依赖旧快照。
- **Progress App `http://127.0.0.1:5180/` 已启动，其他 Work 同时调用。任务完成或暂停时不得停止它、杀掉其 Node/Codex 连接进程或关闭共享页面。** 不要使用批量关闭服务、占用端口清理或通用进程清理。
- 未创建周期自动任务。本轮结束后没有额外后台照片扫描。
