<h1 align="center">NAS Photo Intelligence</h1>

<p align="center">给 NAS 照片建立只读索引，找出重复文件和待复核的相似照片。</p>

<p align="center">
  <a href="docs/ARCHITECTURE.md">架构</a> ·
  <a href="docs/phases/README.md">阶段报告</a> ·
  <a href="docs/AI_REVIEW.md">外部审查入口</a>
</p>

同一张照片可能在不同目录里出现，也可能经过重编码、改尺寸或裁剪。这个工具按显式清单读取照片，在本机写入 SQLite 和报告，原始媒体只读。任务可中断并从已保存的进度继续。

## 报告里有哪些证据

| 工作 | 输出 |
| --- | --- |
| **元数据索引** | 记录来源、文件属性、时间证据和异常标签。 |
| **字节重复** | 用完整 SHA-256 建立重复组。 |
| **像素相同** | 按指定解码和规范化规则比较像素。 |
| **视觉相似** | 用感知哈希找候选，供人工逐对复核。 |

真实 NAS 上的视觉验证使用了 28 个明确列出的文件。26 张生成指纹，2 个跳过，找到 1 对待人工确认的候选。样本范围和结果见 [Phase 3 报告](docs/phases/PHASE3_REPORT.md)。

## 从公开仓库开始

在仓库根目录用 Python 3.11 或更新版本查看命令入口。

```sh
python3 nas_photo_cli.py --help
python3 nas_photo_cli.py scan --help
python3 nas_photo_cli.py duplicates --help
python3 nas_photo_cli.py similarity --help
```

[配置模板](config/example.json)和[清单样例](config/manifest.example.json)说明了输入格式。扫描和哈希以提供的文件清单和读取预算为准。元数据提取使用 ExifTool，视频探测使用 FFmpeg 的 `ffprobe`。图像依赖和合成数据测试的准备步骤见[外部审查入口](docs/AI_REVIEW.md#6-如何运行测试)。

[交接记录](HANDOFF.md)收录各阶段背景，[维护状态](docs/maintenance/STATE.md)记录最近一次公开核对。生产照片、原始数据库和真实清单保存在本地工作区。
