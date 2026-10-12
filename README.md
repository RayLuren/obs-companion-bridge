# OBS 伴侣桥接

[![Windows checks](https://github.com/RayLuren/obs-companion-bridge/actions/workflows/windows.yml/badge.svg)](https://github.com/RayLuren/obs-companion-bridge/actions/workflows/windows.yml)
[![License: GPL-2.0-or-later](https://img.shields.io/badge/license-GPL--2.0--or--later-blue)](LICENSE)

在 OBS 的 **工具 → 启动伴侣桥接** 中打开控制面板，将 OBS 的画面和声音送到抖音直播伴侣的本机网络素材。OBS 负责已有场景、滤镜与混音，伴侣负责正常的直播上传。

**Windows 源码研究版，当前版本 1.0.2。** 首发提供原创源码、配置、构建脚本及测试，不提供伴侣文件、媒体 SDK 或打包好的转发器。兼容范围和安装布局仍有限制，请先看下表。

English: A Windows local OBS-to-Douyin Live Companion bridge with an OBS Tools launcher, an elevation-separated controller, and one managed local media source. This is a source-only, version-specific experimental integration.

## 工作方式

```mermaid
flowchart LR
    A[OBS 场景和混音] -->|本机 RTMP：H.264 / AAC| B[回环转发器]
    B -->|本机 HTTP-FLV| C[伴侣网络素材]
    C --> D[伴侣正常直播上传]
    E[OBS 工具菜单] --> F[启动 / 停止 / 状态面板]
    F --> B
    F --> C
```

- 首次从 OBS 菜单启动后台时，使用标准 Windows 管理员授权；已有后台只恢复面板。
- 面板 X 只收起窗口，保留当前状态。重复菜单不会另开一个后台或自动重启已停止的输出。
- OBS、伴侣及桥接后台可按任意顺序打开，面板等待未就绪的组件。
- 停止桥接会停止 OBS 的**本机**推流、静音管理的素材并关闭转发器，保留 OBS 录制。
- 只管理一个经严格身份核验的网络素材；保留素材记录，并处理伴侣重启后的 UUID 变化。
- 本机接口按用户、Windows 会话、进程路径限制访问，命令固定为 `status/show/start/stop/quit`。
- 日志自动轮换。停止后素材可能保留最后一帧，正式下播仍需在伴侣关播。

这里不提取平台推流码，不改变云端认证，也不修改伴侣程序文件。

## 已验证范围

| 项目 | 当前范围 |
| --- | --- |
| 系统 | Windows x64，开发与实测使用 Windows 11 |
| OBS | 32.2.1，`D:\obs-studio` |
| 伴侣 | 已审查安装目录 `D:\webcast_mate\13.1.1.502547155` |
| 桥接工具 | `D:\OBS伴侣桥接`，避开伴侣版本更新目录 |
| 编码 | 本机 RTMP，H.264 视频与 AAC 音频 |
| 公开首发 | 源码和可复现的构建步骤；不包含厂商二进制 |

目前精确路径是进程身份校验的一部分。**换安装位置或升级伴侣不属于已验证范围**，不能只删除校验来强行兼容。伴侣新目录需要重新检查其协议及素材行为。

## 获取与构建

```powershell
git clone https://github.com/RayLuren/obs-companion-bridge.git
cd obs-companion-bridge
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe scripts\run_checks.py
.\.venv\Scripts\python.exe build_app.py
```

原生菜单插件还需 Visual Studio C++ Build Tools、CMake 和 Ninja。完整步骤见 [构建与安装](docs/BUILD.md)。转发器必须支持 HTTP-FLV、GOP cache 和 JSON 状态统计；普通 nginx 不一定满足要求。安装脚本只使用你指定的本机转发器，不下载或分发伴侣自带文件。

## 日常操作

1. 打开 OBS 和伴侣。在 OBS 选择已配置的本机输出配置。
2. 点 **工具 → 启动伴侣桥接**，首次启动后台时允许管理员运行。面板处于停止状态时点 **启动桥接**。
3. 等待 **等待 OBS 输出**，再点 OBS 的 **开始直播**。这一步只向本机输出；先检查伴侣预览。
4. 正式开播时，再在伴侣点击 **开始直播**。关闭面板不会改变桥接状态。

首次使用需把 OBS 自定义服务器设为 `rtmp://127.0.0.1:19360/obs`，本机流名称为 `main`。这是本地配置，不是平台推流凭据。请使用独立 OBS 配置，保留原来的平台推流设置。

停止时点 **停止桥接**，等状态显示已停止，通常需要约半分钟。伴侣的正式关播独立操作。

## 测试与排查

95 项 Python 自动检查覆盖状态机、面板生命周期、IPC 限制、素材身份与跨重启恢复；原生宿主测试覆盖真实 Windows 管道和受限停止输出。自动测试使用模拟 OBS/伴侣，不会连接真实 SDK。

本机另验证过菜单启动、预览、伴侣重启恢复、停止再启动、面板收起与菜单恢复、停止输出保留录制。公开副本去除了历史客户端 HTTP/诊断 CLI。实机日志和个人运行记录未上传，详细证据范围见 [验证说明](docs/VALIDATION.md)。本次整理未重新验证公开直播的云端音画和长时间流畅度。

日志位置：`D:\OBS伴侣桥接\logs\bridge.log` 与 `desktop-ui.log`；管理记录在 `app\runtime`。报告问题前请去掉账号、令牌、推流凭据和私人文件名。

## 项目结构

| 目录 | 内容 |
| --- | --- |
| `app/` | 普通权限启动器、管理员后台、面板、托盘与受限 IPC |
| `adapter/` | 已审查版本的管道客户端和素材管理 |
| `obs_plugin/` | OBS 菜单及受限本机输出控制插件 |
| `relay/` | 原创转发器配置与生命周期脚本，不含 `nginx.exe` |
| `scripts/` | 构建、安装与离线测试入口 |
| `tests/` | 不连接真实软件的检查 |
| `docs/` | 构建、架构和验证范围 |

## 许可证与贡献

原创项目代码按 **GPL-2.0-or-later** 发布，完整文本见 [LICENSE](LICENSE)。第三方依赖及来源见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。本项目为独立研究项目，与 OBS 或抖音官方没有隶属关系。

提交问题与修改前请阅读 [贡献说明](CONTRIBUTING.md)；敏感问题见 [SECURITY.md](SECURITY.md)。
