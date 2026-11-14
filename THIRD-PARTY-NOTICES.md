# 第三方项目与来源

| 作者与项目 | 版本及用途 | 许可 |
| --- | --- | --- |
| [Powzix / ooz](https://github.com/powzix/ooz) 与 [oriath-net / gooz](https://github.com/oriath-net/gooz) | gooz 提交 `3eae6c61d925562077b7bbb053c05fdd5a1050d5` 的 C++ 解压源码。安装器通过独立程序读取已校验的本机游戏压缩块。独立程序入口在 `native-client/tools/release/decoder.cpp`。 | GPL-3.0-or-later，完整上游源码文件和 COPYING 在 `third-party/gooz`。 |
| [Python Software Foundation / CPython](https://www.python.org/downloads/release/python-31410/) | 3.14.10 Windows x64 嵌入版，随 Windows 安装 ZIP 提供。 | PSF 许可及随附声明见 `third-party/CPython-LICENSE.txt`。 |
| [Google 及贡献者 / Protocol Buffers](https://github.com/protocolbuffers/protobuf) | Python protobuf 7.36.2，解析本机资源和保存装备数据。 | BSD-3-Clause，见 `third-party/Protobuf-LICENSE.txt`。 |
| [skeeto / w64devkit](https://github.com/skeeto/w64devkit)、[GCC](https://gcc.gnu.org/) 与 MinGW-w64 贡献者 | w64devkit 2.11.0 / GCC 16.2.0，构建 Windows 本机组件。 | GCC Runtime Library Exception 和 MinGW-w64 许可见 `third-party`。 |

开发期间，使用 [FabianFG / CUE4Parse](https://github.com/FabianFG/CUE4Parse) 的 `e76778c10ed25860e11ec488750753224d067d87` 固定提交检查本机资源布局。Windows 安装 ZIP 使用上表的独立解压程序。原客户端内自带 [Tencent / UnLua](https://github.com/Tencent/UnLua)。

[ufodriverr / Dungeonborne on Bevy](https://github.com/ufodriverr/dungeonborne-bevy-public-v1) v0.8.0（GPL-3.0-only）用于本项目早期单独试验。本发行候选版运行原版 Unreal 客户端，历史 Bevy 引擎和资源转换不在本发行包内。

项目新增代码采用根目录的 MIT 许可，独立解压程序及其 C++ 入口沿用 GPL-3.0-or-later。随附组件各自保留原许可。
