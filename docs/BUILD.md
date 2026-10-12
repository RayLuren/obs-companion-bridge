# 构建与安装

首发是固定布局的 Windows 源码版本。下面的构建不会修改已安装的 OBS、伴侣或现有桥接。

## Python 面板

需要 Windows x64、Python 3.13（含 Tk）。运行源码只使用 Python 标准库；冻结打包使用单独的构建虚拟环境。

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe scripts\run_checks.py
.\.venv\Scripts\python.exe build_app.py
```

输出为 `dist\OBS伴侣桥接\OBS伴侣桥接.exe` 及旁边的 `_internal`。不能只复制 EXE。

## 原生 OBS 插件

需要 Visual Studio Build Tools 的 **使用 C++ 的桌面开发**、CMake 3.28+、Ninja。

准备脚本使用 OBS 官方固定版本的源码头文件，并从你指定的本机 OBS DLL 导出表生成 import libraries。下载和生成文件在 `.deps`，不会进入仓库。

```powershell
powershell -NoProfile -File scripts\prepare_obs_sdk.ps1 -ObsBin 'D:\obs-studio\bin\64bit'
scripts\build_plugin.cmd '.deps\obs-32.2.1'
```

输出为 `obs_plugin\build-full\obs-mate-menu.dll`。完整参数与开发者命令环境说明见 [插件构建说明](../obs_plugin/BUILDING.md)。只跑宿主测试不需要 OBS SDK：

```powershell
cmake -S obs_plugin -B obs_plugin\build-host-vs -A x64 -DBRIDGE_HOST_TEST_ONLY=ON
cmake --build obs_plugin\build-host-vs --config Release
ctest --test-dir obs_plugin\build-host-vs -C Release --output-on-failure
```

## 转发器与部署

转发器必须支持 `flv_live`、`gop_cache` 和 `rtmp_stat_format json`。本仓库不包含 nginx 或伴侣的程序文件。可以使用自己构建的兼容转发器，或从已安装伴侣中为本机准备一份独立副本；不得把该厂商副本提交到仓库。

首发安装目标固定为：

- 程序：`D:\OBS伴侣桥接\app\1.0.2`
- 本机转发器：`D:\OBS伴侣桥接\outputs\OBS伴侣本机桥接\rtmp_server`
- OBS 菜单插件：`D:\obs-studio\obs-plugins\64bit\obs-mate-menu.dll`

完成两部分构建后，关闭 OBS、伴侣及桥接后台，再执行：

```powershell
powershell -NoProfile -File scripts\install.ps1 -NginxExecutable 'C:\local-dependencies\nginx.exe'
```

把示例路径换成自己的兼容 nginx。安装脚本检查输入、运行中的软件和已有文件；默认拒绝覆盖，明确更新时使用 `-ReplaceExisting`，旧程序保存到独立备份目录。它不改 OBS 配置，不启动直播，不建立服务、计划任务或自动启动项。

OBS 打开后，在独立配置中设置自定义服务器 `rtmp://127.0.0.1:19360/obs`，本机流名称 `main`，编码 H.264/AAC，再按 README 日常步骤操作。原本的测试机器配置名不要求在其他机器上复用。

## 回退

停止桥接，正常退出伴侣并让后台释放连接，再关闭 OBS。移走新增的 `obs-mate-menu.dll` 后重开 OBS 即移除菜单。保留工具目录及日志以便排查；不用修改或删除伴侣版本目录。
