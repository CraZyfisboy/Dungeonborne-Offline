# Dungeonborne Offline

用于 Steam 版《Dungeonborne》1.0.0.97 的 Windows 单机 MOD。使用原版营地、地图、怪物和战斗。角色、装备、仓库和金币写入本机存档。

拿到 Windows 安装 ZIP 后，解压到可写目录，双击「安装单机 MOD.cmd」。安装完成后通过「启动单机.cmd」进入游戏。建议把解压目录放在游戏所在的同一 NTFS 分区。详细步骤、备份和排查见 [安装说明](安装说明.txt)。

- 两张原版地图与三档难度，支持撤离、红门二层和紫门首领路线。
- 原版大厅支持角色、传家宝、商人、炼金、仓库和本地虚拟交易行。
- 仓库免费扩页，付金币升级地牢背包。药水和绷带每堆最多 999。
- 控制台提供金币调整、1～10 倍掉落和按怪物、宝箱与难度设定掉落。

玩家应保留自己通过 Steam 安装的原版游戏。安装时读取已校验的本机资源，并在 MOD 目录生成补丁与商品数据。安装 ZIP 带有 Windows 运行环境，支持直接转发。SQLite 存档请随 `native-client/local-save` 一起备份。

## 从源码运行

在 Windows x64 上准备 Python 3.14 与 `protobuf==7.36.2`，使用 MinGW-w64 的 `g++` 和 `gcc` 构建两个本机组件。源码包包含解压组件及其许可。

~~~powershell
.\native-client\tools\release\build-components.ps1
python -m pip install -r requirements.txt
python -B -X utf8 .\native-client\tools\release\install.py
python -B -X utf8 .\native-client\tools\launch_offline.py
~~~

原版游戏及资源归 Mithril Interactive 与各自权利人所有。这是玩家制作项目。第三方组件和许可见 [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)。
