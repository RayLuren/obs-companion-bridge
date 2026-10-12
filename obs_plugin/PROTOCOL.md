# OBS 菜单插件接口（1.0.0）

本插件针对当前 OBS 32.2.1 x64 编译。只添加“工具 → 启动伴侣桥接”菜单和本机受限控制管道，不修改 OBS 配置，不添加音视频输出，不控制录制。

## 启动入口

菜单通过 Windows ShellExecute 的 open 操作启动 `D:\OBS伴侣桥接\app\1.0.2\OBS伴侣桥接.exe`，不带参数。独立 EXE 负责已有后台单例、界面重开和管理员授权。OBS 不需要提权。

## 管道

`\\.\pipe\OBS_MATE_BRIDGE_S<WindowsSessionId>`

例如当前会话 1：`\\.\pipe\OBS_MATE_BRIDGE_S1`。SessionId 由 ProcessIdToSessionId 获取，不能硬编码为 1。ACL 仅当前 OBS 用户和 SYSTEM；使用受保护 DACL、PIPE_REJECT_REMOTE_CLIENTS，并核对客户端 SessionId 与用户 SID。客户端应使用允许 SECURITY_IMPERSONATION 的连接；默认 Windows CreateFile 的 impersonation 行为可用。

每个连接发一行 ASCII（最大 256 字节，包含换行），读取一行 UTF-8 JSON 后关闭连接。支持 `PING\n`、`STATUS\n`、`STOP_LOCAL\n`。不接受多行、额外命令、路径或自定义服务地址。

管道客户端应通过 GetNamedPipeServerProcessId 核对服务器进程是实际 OBS 安装中的 obs64.exe。请求处理只投递到原 OBS UI 线程，管道等待不阻塞 OBS 界面。

成功响应公共字段：

```json
{"ok":true,"protocol":1,"request":"STATUS","pid":1234,"session_id":1,"stream_active":false,"frontend_stream_active":false,"output_active":false,"output_reconnecting":false,"stream_connecting":false,"recording_active":false,"service_matches":true,"profile":"Codex SDK桥接测试","obs_version":"32.2.1","plugin_version":"1.0.0"}
```

`stream_active` 合并 frontend active、实际 output active、reconnecting 与前端的初次连接事件，防止连接或重连期间误报已停止。`service_matches` 要求当前服务 server/key 完全等于 `rtmp://127.0.0.1:19360/obs` 与 `main`；输出活跃时还要求实际绑定的服务同时匹配。

`STOP_LOCAL` 额外返回 `requested`、`stopped`。`requested:true` 仅表示已对这一个复核过的 streaming output 请求停止，`stopped:false` 时后台必须继续 STATUS 轮询。只有最终 `stream_active:false` 才可以关闭中转服务。实现调用 obs_output_stop 而不是 obs_frontend_streaming_stop，避免用户“停推时停止自动录制”的偏好间接影响录制。

初次连接尚未进入可停止的 active/capture 状态时，返回 `OBS_CONNECTING`，保留中转并稍后重试。若 OBS 编码器初始化失败且仍显示错误提示，需先处理 OBS 错误或正常退出 OBS；本插件不会把未确认的连接状态当成已停止。

错误响应例如：

```json
{"ok":false,"protocol":1,"request":"STOP_LOCAL","pid":1234,"session_id":1,"error":"SERVICE_MISMATCH"}
```

错误码还包括 `OBS_NOT_READY`（前端未完成初始化，稍后重试）、`UI_TIMEOUT`、`SHUTTING_DOWN`、`INVALID_REQUEST`、`CLIENT_DENIED`、`EVENT_FAILED`。

成功和错误响应都带 `pid` 和 `session_id`，均应与实际管道服务器身份一致。测试宿主使用带测试进程 PID 的独立管道，不占用真实 OBS 插件的管道。

## 安装与回退

完整构建输出为 `obs_plugin\build-full\obs-mate-menu.dll`，安装目标为 `D:\obs-studio\obs-plugins\64bit\obs-mate-menu.dll`。构建本身不安装或加载插件；构建与安装步骤见 [BUILDING.md](BUILDING.md) 和 [安装说明](../docs/BUILD.md)。

安装前正常退出 OBS；如目标已存在，先保留备份。安装后正常重开 OBS，检查工具菜单和管道 STATUS。回退时正常退出 OBS，仅删除本插件 DLL 或恢复它的备份，然后重开 OBS。没有 data/locale 目录，没有额外 OBS 配置改动。

源码为 `obs_mate_menu.cpp`，独立集成测试为 `bridge_plugin_test`。测试使用真实 Win32 管道/隐藏消息窗口和模拟 OBS 前端，不能代替安装后的真实 OBS 验证。原安装实机与公开副本的验证范围见 [验证说明](../docs/VALIDATION.md)；个人实机记录不在仓库中。
