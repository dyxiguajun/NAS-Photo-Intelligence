# Phase 1～3 架构和数据库

## 仓库地图与三 Agent 分工

| 组件 | 位置 | 职责 |
|---|---|---|
| `nas-photo` CLI | `src/nas_photo/cli.py` | 元数据扫描、精确重复、视觉证据、整理模拟 |
| SQLite 证据库 | `src/nas_photo/store.py`、`migrations.py` | WAL、事务、队列、观察历史与 schema 迁移 |
| 元数据/哈希/视觉 | `engine.py`、`duplicates.py`、`similarity.py`、`imaging.py` | 有界读取、隔离 worker、恢复与报告 |
| Docker Czkawka | `tools/czkawka_docker_batch.py` | fnOS 上固定镜像、只读扫描、案例级检查点 |
| NAS 证据 worker | `tools/nas-evidence/` | NAS 侧可恢复 SHA-256/ExifTool 观察 |
| 三 Agent 通信 | `tools/agent_sync.py` | 本地追加式消息、原子发布、去重与回执 |
| CI/质量 | `.github/workflows/ci.yml`、`tests/`、`tools/maintenance/` | 测试、静态检查、密钥扫描 |

三 Agent 分工：

- **Codex**：一级负责人；架构、主工程代码、权威数据库、Docker 集成、最终集成与验收。
- **DSH**：二级负责人；视觉匹配、HEIC/PNG/JPEG 媒体分析、损坏恢复、参考库与算法验证。
- **OpenCode**：三级负责人；GitHub、CI、独立审计、故障注入、SQLite/Docker 可靠性、质量保障。

GitHub 只包含可审查的源码、测试、文档和脱敏示例。运行时协调、完整报告、生产索引和媒体保留在本地；外部审查入口见 `docs/AI_REVIEW.md`。

## 输入与输出

来源配置定义 `device_id`、稳定 `source_id`、`logical_root`；网络地址是背景信息，`mount_root` 是可变访问入口。文件身份是数据库唯一约束 `(source_id, relative_path)`，不包含 IP、挂载名称、inode 或绝对路径。

同一个 `source_id` 不允许静默改指另一设备/逻辑来源；同一个设备和逻辑根也不能重复定义两个来源 ID。迁移 LAN/Tailscale 时只修改 `devices.<id>.mount_root`，保持共享内部布局和来源 ID。目录内部重命名识别尚未实现。

写入仅发生在本地工程输出目录。配置拒绝输出越过工程根、与输入重叠，以及位于已知网络挂载。源路径的相对部分拒绝绝对路径、`..`、反斜杠和空字符；工作进程拒绝符号链接及非普通文件。此程序不是操作系统沙盒，NAS 共享也未被强制只读；未来容器部署仍需只读绑定。

## 执行流程

1. CLI 验证显式 manifest，最多 200 项。NAS 验收仅使用原来的 15 项。
2. 文件身份和任务队列先提交到 SQLite。
3. 单个持久任务条目先变为 `running` 并提交，再启动一个独立 I/O 进程。只有一个活动文件工作进程。
4. 工作进程进行路径检查、stat、64 字节文件头读取、file / ExifTool 提取。视频另用 ffprobe；不解码整图、不复制原图。
5. 若大小、mtime 纳秒值、birthtime 原始值与已索引结果相同，且提取器版本相同，只做 stat 后跳过元数据工具。标签仍可按当前任务规则重新计算。
6. 读取前后的文件身份、大小、mtime 再检查。一旦发生变化，结果作为错误处理，不能覆盖成功观察。
7. 文件最新信息、观察历史、时间证据、标签与任务条目状态在同一个事务中提交。每文件一个事务；无需等待整批完成。
8. 日志只写事件、文件 ID、状态和错误；导出生成摘要 JSON、逐文件 JSONL、CSV。

发现功能是独立的有界操作：目录深度、目录数、条目数、文件数和总超时都有限制；结果输出 manifest 和诊断。发现操作本身不扫描元数据，也不是全库清单。中断发现可以重做这一小范围；已提交索引任务的断点恢复由数据库队列负责。

## 超时、恢复及并发

- supervisor 使用独立进程组隔离每个文件，截止时间涵盖路径/stat 和工具调用；超时及取消仅终止自己创建的进程组。
- 工作进程观察父进程退出，尽力结束所属工具进程组。内核中无法立即中断的 I/O 不保证马上退出，但主任务能保存此前已提交进度。
- 工具输出使用临时文件，读取大小上限为 2 MiB；工作进程响应上限为 4 MiB。临时空间跟随操作系统临时目录，不保存照片副本。
- 自动重试默认最多 2 次。失败条目持久保存；`resume --retry-failed` 明确开始新的有限重试预算，累计次数继续保留。
- Ctrl+C / SIGTERM 转为可恢复取消。强制退出后再次 resume 将遗留的 `running` 条目重置为待处理。已提交 `done/skipped` 条目不重做。
- 文件锁保证同一数据库只有一个索引/重算写入者。读取状态和导出使用只读连接，导出在一致的读取事务中完成。
- 运行总时间默认最多 900 秒，达到限制时任务为 `interrupted`，可 resume。重启机器后命令依赖用户恢复挂载，不会自动重新挂载或开始扫描。

## 基础数据表（Phase 1 保留，当前 schema version 3）

| 表 | 用途 |
|---|---|
| `sources` | 逻辑身份、描述、最近访问根；唯一设备/逻辑根 |
| `files` | 稳定文件 ID、相对路径、增量指纹、最新信息和状态 |
| `jobs` | 持久任务、规则及限额快照、状态、累计耗时 |
| `job_items` | 逐文件任务状态、当前预算次数、累计次数及上次错误 |
| `observations` | 每次成功元数据提取的原始字段、解析结果与分析快照；保留历史 |
| `time_evidence` | 最新原始时间字符串、解析值、UTC 值、时区状态、解析错误 |
| `labels` | 标签、来源、具体理由和计算时间；引擎重算只替换 `origin=engine` |
| `errors` | 每次失败的时间、任务、文件、尝试次数、错误码和原因 |
| `candidate_relations` | 同一来源、同路径 stem 的图片/视频配套候选；始终 `unverified` |

SQLite 采用 WAL、外键和 `synchronous=FULL`。数据库保存在 Mac 本地。文件原始 epoch 值保存为字符串，避免将异常 birthtime 写进 SQLite INTEGER 或让 datetime 溢出。[SQLite WAL 官方文档](https://www.sqlite.org/wal.html)

## 元数据与时间证据

ExifTool 只接收代码内固定的读取参数，不提供用户任意参数透传。禁用自定义 ExifTool 配置，不使用写入赋值、递归或 `-all`。保存选定标签的原始值，保留组名，不声称已经取证全部 EXIF/XMP/MakerNotes。[官方 ExifTool 项目](https://github.com/exiftool/exiftool)

读取字段包含原始/数字化/修改时间、时区偏移、相机、镜头、方向、尺寸、格式、部分内容标识。GPS 默认只持久保存是否存在；精确坐标从工具原始输出中过滤，不写入索引、事件日志或导出。工具短暂输出可能包含所请求 GPS 字段，临时文件在进程处理后关闭；这不构成把原始照片上传外部服务。

`metadata_json` 保存当前过滤后的提取结果；`observations` 保留成功观察历史。新观察补全字段，不改写 Phase 0 原报告。S10/S11 的历史 ImageIO 分歧作为带来源的 `prior_evidence` 保存，不能误认为当前 ExifTool 也返回 HEIC。

没有默认的“最佳时间”或合并质量分数。内容信息、元数据完整度、时间可靠性、来源可信程度分别存储；可靠性和来源信任为 `unassessed`。

标签含义：

- `early_filesystem_timestamp`：UTC 文件系统日历日期早于配置线，仅筛查。
- `invalid_timestamp`：非空时间无效、溢出、非有限数或无法解析。
- `missing_capture_time`：没有可解析的原始拍摄/视频容器时间候选；不是推断真实时间。
- `timezone_uncertain`：拍摄时间候选缺少明确偏移，不补 UTC。
- `timestamp_conflict`：有明确偏移的可比较字段差异超阈值；复制/导出也可能造成差异，不判断哪一项错误。
- `future_timestamp`：超过当前时间加配置容忍天数；无时区值仅按日历日期筛查。
- `metadata_incomplete`：缺少原始时间候选，或图片缺少相机型号；不代表内容质量低。
- `format_identification_conflict`：文件头、file、ExifTool 或带出处的历史工具证据有分歧；扩展名仅作为提示。

文件名时间和目录年月保存为辅助证据。部分日期保留年月精度，不补造日期或时区。没有相机年代数据库、复杂可信度评分、删除标签生成或自动修复。

## 已知边界

- 元数据和哈希缓存的 stat 指纹不是内容哈希；同大小且保留时间的内容替换可能漏检，Phase 2 提供显式 force 验证。
- 不是全量库存，未索引文件不能判断为已删除，不自动清理旧记录。
- 小样本相同 stem 配套是候选，尚未核对 Live Photo 内容标识。
- 同一来源候选查找当前适用于受限样本，正式大库前需要进一步索引优化。
- 当前运行与实测平台为 macOS/POSIX；没有完成 NAS/Linux 或 Windows 验收。
- 网络故障恢复以本地路径失效模拟验证；未人为断开真实 Tailscale/SMB。


## Phase 2：迁移、哈希证据和动态重复组

`migrations.py` 仅在 schema 1 时执行到 2 的一次性加法迁移。先用 SQLite `Connection.backup` 保存可恢复数据库，再在一个事务中增加列、表、视图和触发器。版本 0 的新空库直接建立基础结构并升级；版本 2 不再次执行 DDL；未知版本拒绝写入。[Python SQLite backup 文档](https://docs.python.org/3.14/library/sqlite3.html#sqlite3.Connection.backup)

原 jobs/job_items 复用；新增 kind 区分 metadata/hash，原任务默认为 metadata。哈希任务保存参数快照、原范围、逐文件队列、读取量和预算，不重新提取 EXIF。Phase 1 的 resume 拒绝误处理 hash 任务，哈希使用 `duplicates resume`。

| 新结构 | 用途 |
|---|---|
| `schema_migrations` | 迁移版本、应用时间和说明 |
| `hash_observations` | 完整 SHA-256 或失败观察、实现版本、前后状态、读入量、完成时间和错误；失败没有 digest |
| `hash_current` | 每个逻辑文件的当前哈希指针及 valid/stale/unavailable/verifying 状态 |
| `active_exact_hashes` | 仅当前指针 valid 且对应观察 valid 的证据 |
| `exact_duplicate_groups` | 按 algorithm、完整 digest、size 动态分组，至少两个成员 |
| `exact_duplicate_members` | 当前有效组的成员，保留独立 file_id/source_id/path |

哈希表有约束：valid 必须有 64 字符 digest、读入量等于文件大小、完成时间且无错误。没有保存 hashlib 的中间对象或部分摘要。SHA-256 按可配置块流式 update，使用标准库，不引入哈希框架。[Python hashlib 文档](https://docs.python.org/3.14/library/hashlib.html)

默认大小候选只在显式 manifest 的既有索引中分组，不按文件名、扩展名或日期筛选。大小独特项记录 not_candidate；后续新索引出现时可重新规划，不产生“永久唯一”标签。本阶段不实现部分内容指纹，减少额外随机读取与维护负担。

### 一次哈希尝试

1. 提交 running 条目，使用隔离 worker 做安全路径/stat 探测。
2. 最新成功观察与 live stat 指纹、实现版本相符时复用缓存，内容读取 0 字节。缓存只减少内容 I/O，不跳过路径可读性检查。
3. 需要计算时先将当前指针置 verifying，移出有效重复关系；检查文件大小、整个任务剩余字节预算及时间。
4. 在 SQLite 事务中预留完整文件大小的读取预算，并保存仅属于本任务的本地计数文件路径，然后开始读取。
5. 通过 O_RDONLY/O_NOFOLLOW 打开，核对 fd 与路径的设备、inode、大小和 mtime。每块 os.read 返回后记录累计字节，再更新 SHA-256。
6. 读取预先验证的完整逻辑长度；提前 EOF 报错。结尾再次核对路径/fd、大小、mtime、birthtime 指纹与 ctime。没有通过全部检查就不发布 digest。
7. 成功后在一个事务中保存有效观察、更新指针和任务条目；失败记录错误且不丢失旧哈希历史或元数据历史。

前后 stat 一致不能绝对证明期间没有变化；缓存也不能捕获所有同大小/时间替换。实际整理前可再增加字节确认，此阶段没有删除接口。

文件状态变化或 Phase 1 索引 fingerprint/status 变化时，旧指针变 stale；暂不可读变 unavailable。旧观察保留，但动态视图排除非 valid 指针，所以成员重新验证后不会永远留在旧组。缓存重激活须重新 stat 匹配，force 明确忽略缓存。

哈希状态与元数据采集状态分开。哈希可以发现更新后的文件状态，但不自动改写旧 EXIF 证据；报告的 `metadata_matches_hash_state` 表明元数据观察是否对应当前哈希指纹。

### 读取量、硬终止和预算

任务参数与字节预算跨重试/resume 保留。每尝试开始前全额预留 size；正常完成/可捕获失败时按实际读入量结算，释放未用预留。每文件截止时间涵盖 stat 探测和完整读取；每次 run/resume 另有运行时间上限，正常退出的耗时累计保存。被硬杀的一小段运行时间无法完全计入已提交计时，不把它当精确墙钟性能。

读取过程中使用 24 字节本地计数器保存观察下界。超时、取消、硬终止后没有正常结果时，恢复读取该计数器并保存失败观察，整笔预留仍作为保守上界扣除。进程在 read 返回与计数落盘之间被杀，或整机断电时，尾部读入量可能不明；报告显式标记 read_bytes_are_exact=false 和 unconfirmed_reserved_read_bytes，不虚报精确值。部分读取从文件起点重算，不恢复中间 SHA 状态。

预算不足时任务是 budget_exhausted，未处理条目仍保留。resume 不重置预算；需要新增明确预算时，由使用者建立同样受限范围的新任务。单文件失败自动重试次数有限，显式 retry-failed 开启新的次数预算但不清零已用读取预算。

报告分别记录 scope 的索引逻辑大小、实际观察读取、成功完整哈希读取、失败/中断读取下界、缓存减少的内容读取和未确定预留。未做部分指纹，其读入量为 0。所有统计都是应用层文件字节，不等于物理网络流量、磁盘读量或 Btrfs 真实占用。

### 报告和限制

报告关联各成员来源、相对路径、hash_id、元数据、异常标签和检查时间。同一 SHA-256 的内部文件字节（包括内嵌 EXIF）相同；不同的外部证据不意味着内部 EXIF 不同。Live Photo 配套候选仍保留，不能把同名图片/MOV 当完全重复。

潜在逻辑冗余按每组保留一份计算；硬链接仍保留多个逻辑文件身份。没有验证物理块、快照或压缩，不能报告实际释放空间。

有效状态仅描述最近观察，后台没有文件监视；纯数据库 report 不重新读源文件。当前报告以受限任务/小样本为目标，大规模全库报告的流式聚合和物理块关系仍需后续评估。真实 NAS 哈希只验证原有单个样本，本地模拟覆盖正例、内容变化、部分失败、超时和强制终止。Linux/NAS 部署、整机重启及大文件/全库性能尚未实测。

## Phase 3：图像内容证据（当前 schema 3）

保持 NAS 只读提供原始文件、Mac 单文件隔离处理、本地 SQLite 持久队列。没有 NAS 部署、全库扫描、GUI 或模型服务。真实授权以 `docs/phases/PHASE3_REQUIREMENTS.md` 为准：最多 100 个独立 NAS 文件，原 15 个计入；本轮实际 28 个。Phase 1 的软件上限 200 不能扩大此授权。

### 分层与持久结构

- L0：直接沿用 Phase 2 的 `hash_observations/hash_current` 和 SHA-256 动态等价组。
- L1：完整规范化 RGBA8 像素 SHA-256，与文件 SHA-256 分开；按 pipeline、宽高和像素指纹建立严格等价组。
- L2：ImageHash pHash + dHash，保存成对候选边；不使用并查集合并相似链，不生成删除建议或保留优先级。
- Related asset：既有 Live Photo 同 stem 候选单独保留，未确认内容标识。

| 新表/视图 | 用途 |
|---|---|
| `visual_observations` | 有效/失败/不支持/资源跳过历史，完整像素指纹、结果 JSON、pipeline key、前状态、读取量、代表复用出处 |
| `visual_current` | 每文件当前指针和 valid/stale/unavailable/verifying/unsupported/limited 状态 |
| `active_visual_fingerprints` | 仅有效的当前指纹；二进制代表复用还要求对应 Phase 2 哈希仍有效 |
| `visual_comparisons` | 一次本地比较的参数、视觉观察范围、时间和筛选计数 |
| `visual_relations` | 比较批次中的 L1/L2 成对证据，绑定两端观察 ID；保留历史 |
| `active_visual_relations` | 两端观察仍为当前有效指纹的边；旧边自动退出有效结果 |

v2→v3 为加法事务迁移；先验证副本，再保存 `index.pre-phase3.sqlite3`，不覆盖 `index.pre-phase2.sqlite3`。重复迁移不重复建表，失败回滚。v1 库依次升级，未知版本拒绝。所有数据库连接在初始化失败时关闭，备份连接亦显式关闭。

### L1 规范化 `rgba8-icc-srgb-exif-v1`

1. 安全路径/stat 后，用只读 fd 对完整压缩文件做一次有界读取，保存在 RAM 中的 BytesIO；解码器之后的随机 seek/read 只访问 RAM，不反复访问 NAS。不落盘原图。
2. Pillow 根据真实内容仅接受 JPEG/PNG/HEIF；不靠扩展名决定真实格式。MOV 在队列规划时排除。动画/多帧 JPEG、PNG 整体跳过，不把首帧冒充整文件。HEIF 只使用容器主图，不比较辅助资源；该区别写入规则。
3. Pillow `ImageOps.exif_transpose` 应用方向；pillow-heif 在打开时处理容器/EXIF 方向，记录原方向及实际调用路径，避免再次人为旋转。
4. 8 bit RGB/RGBA/L/LA/P 转 RGBA8。ICC 用 LittleCMS 相对色度转换至 sRGB，Alpha 单独保留；无 ICC 的 RGB/灰度/调色板假设 sRGB。无 ICC 的 CMYK、无法可靠转换的色彩模式、HDR/高位深拒绝。HEIF RGB8 先经 libheif 转换，其版本作为 pipeline 一部分。
5. L1 保留每个 RGBA 值，包括透明区域隐藏 RGB。逐 64 行对**完整**像素做 SHA-256，输入含像素格式和宽高前缀，绝不以缩略图计算 L1。
6. 记录 pipeline 完整描述及其 canonical JSON SHA-256 key，包含 Python 包、JPEG/zlib/LittleCMS/libheif/HEVC codec 版本、像素格式、色彩、透明与方向规则。不同 pipeline 不合并 L1/L2。

这里的相同仅表示在该解码和规范化规则下像素相同；不宣称原始传感器数据、所有软件显示结果或未来任意解码器均相同。非法 ICC 属错误，没有无声回退为 sRGB。

### L2 指纹与候选

L1 图像缩至最长边 256，LANCZOS；透明部分合成白底。ImageHash 为 0/90/180/270° 各生成 64 bit pHash（hash_size=8、highfreq_factor=4）和 dHash。原始哈希、算法包版本、预处理、原方向宽高、RGB 平均值、灰度标准差随观察保存。旋转只参与 L2；L1 不把实际旋转后的不同像素强制相等。

默认同时满足：pHash 汉明距离≤8，dHash≤12，相应旋转后的长宽比相对差≤0.08，平均 RGB 最大通道差≤35；灰度标准差任一<5 时不建立 L2 边。阈值可通过 compare 参数修改，比较保存指纹，不读取图片。不把距离换算成概率。已满足 L1 的一对只记录更强的 L1 关系，不重复输出为 L2。

低信息量与颜色筛查可减少纯色误判，但不能保证截图、天空、连拍或相似构图不会误报。候选始终 unverified，需要以后人工确认。轻裁剪只做探索性测试，大裁剪、滤镜、任意角度旋转和语义同源不保证识别。

小范围最多 100 个既有文件直接两两比较，26 个有效指纹实际比较 325 对。`scoped_visuals` 提供候选检索边界，未来可改分桶/BK-tree；不将全库平方比较写成长期唯一方案。

### 缓存、失效与 L0 复用

每次 analyze 都使用隔离 stat 探测。大小/mtime/birthtime 指纹、已知设备/inode 身份和 pipeline 一致才可复用。Phase 1 更新文件指纹/状态或 Phase 2 更新哈希会使 visual_current 失效；已知新的完整 SHA 若与旧视觉读取 SHA 不符，也不能复用。force 跳过缓存且不突破预算。修改后旧观察仍保留，旧关系退出当前视图。

对当前有效 SHA 相同、且代表已在同一视觉任务中实际验证过的成员，可以关联代表指纹，记录 reused_visual_id 及本文件 binary_hash_id。强制分析不进行代表复用。只沿用已有 Phase 2 完整哈希，不把不完整读取或文件名相同当作二进制证据。

确定不支持的容器结果可以在相同 stat/身份/pipeline 条件下复用“不支持”状态，避免反复读取；它永远不成为有效视觉指纹。“未分析”不等于“不相似”。文件状态仍不是内容身份的绝对证明：未被发现的同大小/时间/身份替换可能逃过缓存；compare/report 不做文件系统监控，仅反映记录时的状态。

### 隔离、内存和预算

默认同时只有一个解码 worker。压缩输入≤64 MiB，像素≤16,000,000，单边≤16,000，保守工作集估计与 512 MiB RSS 上限。像素门限在完整 load 前检查；Pillow 解压缩炸弹告警升级错误，libheif 安全限制保持开启。HEIF/BLAS 线程数设置为 1。父进程每约 0.2 秒只检查自己创建的 worker PID 的 RSS，超限只终止该独立进程组。估计及采样是保护措施，不是操作系统硬内存沙盒，也不能保证完全捕捉瞬时峰值。

每文件默认 60 秒，覆盖 stat 与解码；每 run/resume 默认 900 秒，有限重试 2 次。父进程退出监护与既有隔离机制继续使用，不使用线程超时替代进程隔离。单图错误不撤销已提交文件。

复用 jobs/job_items，kind=visual。读取前持久预留完整 size，并用独立的 `hash-visual-` 24 字节本地计数器记录已读下界。正常返回按实际读入量结算；硬终止/超时保留整笔预留，恢复不重置预算，未知尾部显式标记不精确。读取前后 fd/path 身份、大小/时间及 ctime 检查失败不发布指纹。Ctrl+C/SIGTERM 保存状态；resume 只处理未完成项。

每任务默认 256 MiB，CLI 可进一步缩小；本阶段所有 visual 任务累计计费上限 2 GiB，并默认为既有元数据工具预留 512 MiB。该预留不是元数据精确读入量。此次新增 13 文件总逻辑大小 17,037,215 字节，ExifTool/file 沿用固定只读调用但未逐 read 计数，所以报告分开给出视觉读取实测值和外部读取预留，不伪称精确总读取或网络流量。不得通过新建任务绕开累计视觉预算。


## Phase 3.1 维护结论

生产 monitor/worker、Schema 3、视觉算法和阈值均保持不变。旧测试的全零对象分配未保证可观测 RSS 超限；测试层改为明确 ready/首次采样/分配握手、随机填充并持续保留访问内存，并检查实际 RSS、目标 PGID 和回收结果。`tests/worker_probe.py` 的追踪钩子转发真实 ps/信号调用，不伪造值，不进入生产数据库。

`tools/phase3_1_diagnostics.py` 可在没有 NAS 的环境中顺序复现 RSS 与生命周期场景。记录的 sampled_max_RSS_KiB 仅是父进程采样最大值；allocation 标记中的 worker_highwater_at_allocation 是 worker 在该时点的 OS 高水位（当前 Darwin 为字节），不能当成完整生命周期的绝对峰值。

直接子进程的回收与父进程被强杀后的 OS 接管分开验证；后者确认 worker 消失或不可运行，不把 zombie 状态误称为已由死亡父进程回收。资源 limited 项保留未分析状态，解除实际问题后需新显式任务；硬杀遗留 pending/running 条目使用原任务 resume。

详细报告：`../PHASE3_1_REPORT.md`，历史首测无法逐时刻还原的不确定性仍保留。轮询不是硬内存沙盒，瞬时峰值盲区未因单元测试通过而消失。
