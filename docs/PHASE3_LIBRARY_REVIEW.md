# Phase 3 图像库选择（2026-10-15）

选择 Pillow 12.3.0 + pillow-heif 1.8.1 + ImageHash 4.3.2。在本机 CPython 3.14 / macOS arm64 安装并运行成功；使用 PyPI 预编译 wheel，没有本机编译、PyTorch、视觉模型或 GPU 服务。NumPy 2.5.4、SciPy 1.18.1、PyWavelets 1.10.0 为 ImageHash 的依赖，固定版本见 `requirements-visual.txt`。安装后的项目 venv 约 182 MiB。

| 方案 | 结论与依据 |
|---|---|
| Pillow | 采用。跨平台 JPEG/PNG 解码、EXIF transpose、LANCZOS 和 LittleCMS 色彩转换均有成熟接口；原生像素分配需要额外进程与内存保护。[方向处理](https://pillow.readthedocs.io/en/stable/reference/ImageOps.html)、[色彩转换](https://pillow.readthedocs.io/en/stable/reference/ImageCms.html) |
| pillow-heif | 采用。PyPI 提供 CPython 3.14/macOS arm64 wheel，真实 2 张 HEIC 解码通过。显式单线程、关闭缩略图/深度/辅助图解码，保持 libheif 安全限制。[安装包](https://pypi.org/project/pillow-heif/)、[配置](https://pillow-heif.readthedocs.io/en/latest/options.html) |
| ImageHash | 采用现成 pHash 为主、dHash 为辅。pHash 用 SciPy DCT，dHash 提供第二个形状证据；均为 64 bit，不自行重写底层算法。aHash 更易受简单亮度分布影响，本轮不额外持久化。[算法与实现](https://github.com/JohannesBuchner/imagehash) |
| macOS ImageIO | 本轮不采用。Phase 0 已证明可访问 HEIC，但 Python 接入需要另一个原生 helper，色彩/方向策略也需单独版本化；目前跨平台 Pillow 链已满足实际样本。没有声称 ImageIO 不可行。 |
| SSIM/裁剪鲁棒哈希/大型模型 | 本轮未引入。长宽比、平均颜色和低信息量筛查已提供轻量辅助证据；复杂裁剪和语义检索留待明确授权。 |

Python 包许可证：Pillow 为 MIT-CMU；pillow-heif 为 BSD-3-Clause；ImageHash 为 BSD-2-Clause；NumPy/SciPy/PyWavelets 包含各自 BSD/MIT 及第三方许可。HEIF 二进制依赖有自己的许可证：libheif 为 LGPL，wheel 中本机查询到 libde265 解码器和 x265 编码器，x265 为 GPL；不能把 Python 包的 BSD 许可当作整个 wheel 的唯一许可。[Pillow LICENSE](https://github.com/python-pillow/Pillow/blob/main/LICENSE)、[pillow-heif 源码与许可文件](https://github.com/bigcat88/pillow_heif)、[libheif 许可和编解码器说明](https://github.com/strukturag/libheif)。当前仅本机安装运行，没有打包分发依赖。

实际解码栈与 codec 版本保存于每个任务 pipeline 及 `reports/phase3-migration-check.json`。Linux/NAS 有相同库的迁移路径，但本轮只验收这台 Mac，不能宣称所有平台的解码像素哈希天然相同。
