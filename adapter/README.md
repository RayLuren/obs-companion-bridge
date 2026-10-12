# 已审查伴侣适配层

仅管理固定本机 HTTP-FLV 网络源，安装版本必须为
`D:\webcast_mate\13.1.1.502547155\直播伴侣.exe`。
导入模块不连接管道、初始化 SDK 或启动软件。GUI 与 nginx 不由本模块操作。

```python
from adapter.bridge_adapter import BridgeAdapter

bridge = BridgeAdapter(
    runtime_dir=r"D:\OBS伴侣桥接\app\runtime",
    legacy_manifest=r"D:\OBS伴侣桥接\outputs\OBS伴侣本机桥接\controller\bridge-manifest.json",
)
```

| API | 行为 |
| --- | --- |
| `discover()` | 只读发现唯一已审查主进程；不打开管道 |
| `connect()` | 建立并绑定 PID、启动时间、精确路径；重复调用复用连接 |
| `read_status()` | 通过当前有效画布只读核验共享素材表和记录对应源，返回脱敏状态 |
| `ensure_existing_source(enabled=True, audio=True)` | 显式接管旧 ID 或经严格重启核验的现存新 ID；允许创建的缺失场景仍须码流就绪 |
| `set_enabled(False)` | 只静音/隐藏已核验自建源；未连接时不发现、连接或新增；不移除素材 |
| `disconnect_safe()` | 伴侣仍活返回 `disconnected=False` 并保持管道；进程真退出才释放 |

`transport_factory(expected_pid=...)`、`discoverer()`、`stream_ready()` 可注入用于后台和测试。
前者返回具有 `identity/verify_identity/alive/call/close` 的受限 transport；发现函数返回 owner 字典列表；
码流函数返回布尔值。默认码流函数只访问固定本机 `/stat`，核查唯一活跃 H.264/AAC 发布及递增字节计数。
这些统计只用于允许创建，不能证明伴侣实际解码或手机播放。

## 生命周期和记录

- 已建立连接即使停止桥接也保留至该伴侣主进程正常退出。调用失败后同一 owner 不自动重连或重发；旧 owner 真退出才允许新 owner。
- 当前记录为 `runtime/bridge-manifest.json`，历史记录写到 `runtime/history/`。旧正式 manifest 仅作为输入，永不修改。
- 接管首先核验旧 ID、普通素材唯一登记、名称、类型、画布、固定本机 URL、完整创建字段，拒绝 `parentID/duplicate`，不按名猜测或删除。
- 旧 ID 只有 raw getter 为 `None` 且共享 `data/order`、`data2/order2` 和置顶表均无此 ID 才视为缺失。异常或字符串 `null` 不等于缺失。
- 只通过当前有效画布读取一次原始 `source` 和 `topSource`。未初始化的 SECOND 画布不必查询；当前画布 getter 的 `None`、缺字段或异常仍拒绝，不能据此认定空表。
- 三张登记表间任何同 ID 交叉登记、顺序重复或悬空 ID 均拒绝。固定 `secondSource=False` 桥接源必须唯一登记在 `source.data/order`，但原生 `viewIndex` 可以为 0 或 1，并须与 manifest 严格相符。
- 已有旧记录先完成归档和当前 owner 原子保存，之后才允许源写操作。不同 ID 同名素材只有下述严格重启恢复允许接管，其余拒绝。
- 启动、发现、连接和只读状态不修改源。已复用源每次核验当前设置，匹配时不重复 setter。

### 重启后的素材 ID 恢复

伴侣保存素材后，下一次主进程启动可能为同一素材重新分配 UUID。仅显式启动或停止源设置时允许恢复，
`read_status()` 始终只读，不接管或改写记录。恢复必须同时满足：

- 旧 owner 的 PID/启动时间身份与当前 owner 不同，旧记录阶段必须为 `attached`。
- 旧 ID getter 为字面 `None`，且 data、data2、top 的数据及顺序登记都不存在该 ID。
- 全局只有一个同名候选，唯一登记在 data/order；不能位于 data2 或 top。
- 候选完整创建字段、固定 URL、native viewIndex 和当前设置模式均与旧记录范围一致，拒绝 parentID/duplicate。
- 候选 `getSourceData` 的完整对象与登记对象逐字段相等，`getAudioData` 的数值设置可读取。

核验通过后，先归档旧 owner/旧 ID 的记录，原子保存新 owner/新 ID，随后才配置该现存源。
该恢复过程不新增或删除素材；同一 owner 内换 ID、多候选、字段不完整、模式/画布改变都拒绝。
有旧 `attached` 记录且 owner 改变时，即使确认旧源不存在，零候选也拒绝并保留记录，不自动创建。
首次没有旧记录时，原本的严格缺失核验和本机 H.264/AAC 就绪后创建流程继续保留。
同一 owner 下确认源缺失，或旧记录已为 `removed` 且不存在同名候选时，也保留原已授权的创建流程；
这与旧 `attached` 记录跨重启后零候选的拒绝规则分别处理。停止 API 始终不创建。

## 经过本机源码验证的调用

证据文件：安装版本 `main.js`，SHA256
`a55f1cc2a635a538245a566c40675404cd008d911db2bc93c36ab634c37ec142`。
以下偏移为 Python Unicode 字符索引。

- `sourceStore.toggleVisible(sid, bool)`，偏移 2150829。通过 `CANVAS_CALL APPLY` 指定源画布。禁止顶层 `toggleVisible`，因为该包装可能改变其他 exclusive 素材。
- `sourceStore.setAudioData(sid, {setting:{volume,monitorType}}, true, true, false)`，偏移 2186596。原生 setter await 且严格返回 true 后，再用 `getAudioData(sid)`（869207）核验音量与模式。
- 当前有效画布 `CANVAS_CALL GET source` 与 `GET topSource`，设置模式确定 SourceStore 路径；单源 getter（2065196），画布枚举 0/1（2268328）。原始 `data/order/data2/order2` 逐表核验，避免 `getAllSource()`（2055503）的 map 合并覆盖隐藏同 ID 冲突。
- `toggleVisible` 未 await 原生 `Visible.Set` 且会吞异常；`visible` 保存值不能证明原生已隐藏。停止总返回 `local_stream_pause_required=True`，由后台暂停本机输入作为回退。

`getSourceData(sid)` 直接 JSON.stringify 该源记录（833268），不会从原生层补默认字段。
正常 onlineMedia 创建先刷新音频并写回 `payload.audio`（2109379），等待创建任务后将整个对象存入素材表（2128289）；
传入完整 audio.setting 且原生属性读取成功时，刷新返回原对象（2184801–2185960）。
这支持本桥接完整创建字段的严格核验；属性缺失时 JSON 可能省略 audio，应拒绝而非猜测默认。

共享 SourceStore 的证据链：主类构造唯一 `this.sourceStore` 并传给旧 Canvas（881348）；
新 LiveAtomManager 的 `baseEnv.sourceStore` 也直接来自同一对象（916394），
创建 LiveAtom 仅浅复制 baseEnv（688967、708400），LiveAtom 的 Stage 接收该 baseEnv（789660），
Stage 直接保存 `this.baseEnv=p`（2026963）。原始表初始化字段为 `data/order/data2/order2`（2054380）。
`getCanvas(SECOND)` 可能不存在（825300、973400），CANVAS_CALL 此时返回 undefined（884000）；
因此不能遍历两个 canvas 以推断两个独立 store，也不能将 undefined 当第二张空表。
普通源归入 data、secondSource 才归 data2（2065474 的 `setSourceProps`）；
`getSourceViewIndex`（2186354）依据实际源记录，`toggleVisible` 的 native track 从该 getter 获取。
scoped setter 使用已读取的当前有效 canvas 访问同一 store，源 native view 继续依据完整源记录。

## 结果边界

返回仅允许固定字段，不包含原 payload、网址、账户信息或控制令牌。
`audio_setting_verified` 表示音量/监听设置核验；
`video_data_verified`、`audio_data_verified`、`native_visible_verified` 不由设置 getter 冒充验证，当前均返回 false。
本机数据与云端画面声音需由后台的实际数据检测及根验证另行证明。

`reviewed_pipe_client.py` 基于本项目自主研究编写的 Windows 管道客户端，公开副本已移除历史 HTTP 服务和诊断 CLI，通过静态相对导入随 EXE 打包；
`windows_transport.py` 另外限制可调用方法和参数，不开放任意 RPC。
