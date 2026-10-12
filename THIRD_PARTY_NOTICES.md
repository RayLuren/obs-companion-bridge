# 第三方来源与许可

本仓库只发布原创桥接源码、原创配置与脚本。下列依赖保持各自许可，不能将本项目 LICENSE 视为其重新授权。

| 依赖 | 用途与来源 | 许可 / 发布方式 |
| --- | --- | --- |
| OBS Studio 32.2.1 | 插件接口、构建头文件和本机导出符号，[官方源码](https://github.com/obsproject/obs-studio/tree/32.2.1) | GPL-2.0-or-later；头文件由准备脚本另行取得，仓库不提交 OBS DLL 或 import libraries |
| Python | 标准库、Tk、ctypes，[官方许可](https://docs.python.org/3/license.html) | 各组件许可；用户自行安装 |
| PyInstaller | 冻结构建，[官方许可](https://pyinstaller.org/en/stable/license.html) | GPL 与官方打包例外；仅构建依赖 |
| pywin32 | 当前冻结构建的 hidden imports，[官方说明](https://github.com/mhammond/pywin32#licenses) | 混合许可，以各文件为准；仅通过依赖声明获取，不在首发分发冻结包 |
| nginx | 可选本机转发器，[上游许可](https://nginx.org/LICENSE) | 上游许可不代表厂商修改版来源；本仓库不分发其二进制 |
| nginx-http-flv-module | HTTP-FLV 与 JSON RTMP 状态所需功能，[官方项目](https://github.com/winshining/nginx-http-flv-module) | BSD-2-Clause；不提交模块源码或厂商内置副本 |

伴侣程序、安装 `main.js`、媒体 SDK、addon 与内置 nginx 不属于本项目，不包含在 Git、Release 或 CI 产物中。适配层文档仅记载独立研究得到的协议与行为，不附安装源码。

公开 `reviewed_pipe_client.py` 基于本项目自写 Windows 管道客户端，已移除旧 HTTP 服务和诊断命令入口；没有装载或分发伴侣原生 SDK。

以后若提供冻结包，需根据实际包含的 Python、Tk、PyInstaller、pywin32 等文件补齐对应许可与 notices；首发只提供源码。
