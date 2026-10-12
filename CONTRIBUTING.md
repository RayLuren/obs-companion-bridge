# 贡献说明

请先阅读 README 的兼容范围。提交修改保持范围清楚，说明改变的行为、验证方式及失败时如何保留现场。

在 Windows / Python 3.13 环境运行：

```powershell
python scripts\run_checks.py
cmake -S obs_plugin -B obs_plugin\build-host-vs -A x64 -DBRIDGE_HOST_TEST_ONLY=ON
cmake --build obs_plugin\build-host-vs --config Release
ctest --test-dir obs_plugin\build-host-vs -C Release --output-on-failure
```

不要让 CI 连接真实伴侣、管理员控制器或公开直播。新版本适配需要独立审查精确路径、生命周期、协议与素材数据，补充拒绝场景的测试；不通过删除身份校验来实现兼容。

不要提交个人日志、录播、运行 manifest、Cookie、令牌、推流凭据、厂商二进制或 SDK。已有测试仅使用明确的假数据。涉及权限、素材写操作与客户端退出的修改，应描述对其他客户端和素材的影响。
