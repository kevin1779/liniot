# HTTP API 与 WebSocket

Base URL：`http://<host>:8000/api`。交互式文档（Swagger UI）在 `/docs`。

## 健康与概览

### `GET /api/health`

```json
{ "status": "ok", "mqtt": false, "ai": false }
```

### `GET /api/overview`

```json
{ "devices": 4, "online": 4, "telemetry_points": 100, "alerts_24h": 4, "rules": 2 }
```

### `GET /api/overview/series?limit=120`

所有设备每个指标的近期序列（仪表盘单次拉全）：

```json
{
  "sensor-1": {
    "name": "温湿度传感器-1", "type": "sensor", "online": true,
    "metrics": { "temperature": [ { "metric": "temperature", "value": 24.1, "ts": 1793353000.1 } ] }
  }
}
```

## 设备

### `POST /api/devices`（幂等）

```bash
curl -X POST http://127.0.0.1:8000/api/devices \
  -H 'Content-Type: application/json' \
  -d '{"id":"sensor-1","name":"客厅温湿度传感器","type":"sensor","meta":{"room":"living"}}'
```

- `id` 省略时自动生成；同 `id` 重复注册返回 200（更新元数据），首次创建返回 201。
- `id` 规则：`^[a-z0-9][a-z0-9_-]{0,63}$`。
- `type` 自由文本，建议 `sensor` / `light` / `switch` / `generic`。
- `group`（可选，≤32 字符）写入 `meta["group"]`，供设备列表分组过滤；
  幂等重注册未指定分组时保留原分组，避免设备重启上报把分组弄丢。

### `GET /api/devices` · `GET /api/devices/{id}` · `DELETE /api/devices/{id}`

列表支持 `?group=xxx` 过滤（对应注册时的 `group` 字段）：

```bash
curl "http://127.0.0.1:8000/api/devices?group=一楼"
```

设备视图在基础字段上附加派生字段：

```json
{
  "id": "sensor-1", "name": "客厅温湿度传感器", "type": "sensor",
  "properties": {}, "status": "online", "last_seen": 1793353000.1,
  "meta": {"room": "living"},
  "online": true, "effective_status": "online"
}
```

`online` 按 `last_seen` 与 `LINIOT_ONLINE_TIMEOUT` 惰性计算。

### `POST /api/devices/{id}/telemetry`

单点（兼容字段）：

```bash
curl -X POST http://127.0.0.1:8000/api/devices/sensor-1/telemetry \
  -H 'Content-Type: application/json' -d '{"metric":"temperature","value":25.3}'
```

批量（推荐）：

```bash
curl -X POST http://127.0.0.1:8000/api/devices/sensor-1/telemetry \
  -H 'Content-Type: application/json' \
  -d '{"points":[{"metric":"temperature","value":25.3},{"metric":"humidity","value":61.0}]}'
```

响应 `{"accepted": 2}`。非法 metric（含空格等）与缺 value 的点会被过滤，
`ts` 省略时取服务器时间。

### `GET /api/devices/{id}/telemetry?metric=temperature&limit=200`

时间升序序列，供绘图；`limit` 上限 2000。

### `GET /api/devices/{id}/telemetry/export?metric=&limit=`

流式导出遥测 CSV（`StreamingResponse`，逐批查询，大结果集不全量驻留内存）：

```bash
curl -OJ "http://127.0.0.1:8000/api/devices/sensor-1/telemetry/export?metric=temperature&limit=100"
```

- 响应头 `Content-Disposition: attachment`，文件名 `liniot-{id}[-{metric}].csv`；
- 带 UTF-8 BOM，Excel 直接打开中文不乱码；
- 列：`device_id,metric,value,ts,time`。`ts` 为 Unix 时间戳（权威值），
  `time` 为服务器本地时间，便于直接阅读；
- `metric` 省略导出全部指标；`limit` 省略导出全部记录（受每设备 20000 条
  环形保留约束），给定时导出最近 N 条；记录按写入顺序（id 升序）产出；
- 设备不存在返回 404，`metric` 含非法字符返回 422。

### `GET /api/devices/{id}/telemetry/aggregate?metric=&window=5m`

按时间窗聚合遥测（服务端降采样，大时间跨度曲线用）：

```bash
curl "http://127.0.0.1:8000/api/devices/sensor-1/telemetry/aggregate?metric=temperature&window=5m"
```

```json
[ { "metric": "temperature", "ts": 1793352000.0, "count": 12,
    "avg": 24.31, "min": 23.9, "max": 24.8 } ]
```

- `window` 支持 `1m` / `5m` / `15m` / `1h`，其余值返回 422；
- 桶按 `floor(ts/window)*window` 对齐（UTC 秒），`ts` 为桶起点；
- `metric` 省略时返回该设备全部指标，各指标独立分桶；
- 分组扫描由 `(device_id, metric, ts)` 索引支撑，10 万行 < 100ms（本机基准）。

### `POST /api/devices/{id}/status`

上报状态/合并属性（设备心跳或布尔属性回报）：

```bash
curl -X POST http://127.0.0.1:8000/api/devices/light-1/status \
  -H 'Content-Type: application/json' \
  -d '{"status":"online","properties":{"power":true}}'
```

`properties` 为增量合并；`status` 仅接受 `online` / `offline`。

### `POST /api/devices/{id}/properties`（下发命令）

```bash
curl -X POST http://127.0.0.1:8000/api/devices/light-1/properties \
  -H 'Content-Type: application/json' -d '{"properties":{"power":true}}'
```

命令进入队列；若启用 MQTT 同时向 `liniot/light-1/command` 广播。

### `GET /api/devices/{id}/commands`（设备拉取）

返回待执行命令并标记已下发（不重复投递）：

```json
[ { "id": 3, "payload": {"property": "power", "value": true}, "created_at": 1793353000.1 } ]
```

设备执行后应调用 `POST /status` 回报新属性，形成控制闭环。

## 规则与告警

### `POST /api/rules`

```bash
curl -X POST http://127.0.0.1:8000/api/rules \
  -H 'Content-Type: application/json' \
  -d '{"device_id":null,"metric":"temperature","op":">","threshold":30,"note":"高温"}'
```

`device_id` 为 `null` 表示对所有设备生效；`op` ∈ `> >= < <= ==`。

### `GET /api/rules` · `PATCH /api/rules/{id}` · `DELETE /api/rules/{id}`

PATCH 支持局部更新 `{"enabled": false}` / `{"threshold": 28}` / `{"note": "..."}`。

### `GET /api/alerts?limit=50`

```json
[ { "id": 2, "rule_id": 1, "device_id": "sensor-1",
    "message": "设备 sensor-1 的 temperature = 31.2 > 30（高温）",
    "level": "warning", "ts": 1793353000.1,
    "diagnosis": "传感器可能受热源干扰，建议检查安装位置并校准。" } ]
```

- `level` 为 `warning`（阈值规则）或 `anomaly`（EWMA 统计异常检测）；
- `diagnosis` 由 AI 助手异步生成（需配置 LLM Key），可能先为 `null` 稍后补全。

## 场景联动

### `POST /api/scenes`

```bash
curl -X POST http://127.0.0.1:8000/api/scenes \
  -H 'Content-Type: application/json' \
  -d '{"name":"高温自动开灯","metric":"temperature","op":">","threshold":28,
       "action_device_id":"light-1","action_property":"power",
       "action_value":true,"cooldown":60}'
```

- `source_device_id` 省略表示任意设备的该指标都触发；
- `action_value` 为标量（bool/int/float/str）；
- `cooldown`（秒）防止条件在阈值附近抖动导致命令来回震荡。

### `GET /api/scenes` · `PATCH /api/scenes/{id}` · `DELETE /api/scenes/{id}`

PATCH 支持 `{"enabled": false}` / `{"threshold": 26}` / `{"cooldown": 120}`。
触发时通过 WebSocket 推送 `scene_fired` 事件；动作经命令通道下发，
设备回报后闭环完成。

规则触发时同步 `POST` 同样的 JSON 到 `LINIOT_WEBHOOK_URL`（若配置）。

## AI 助手

### `POST /api/ai/chat`

```bash
curl -X POST http://127.0.0.1:8000/api/ai/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"打开所有的灯","history":[]}'
```

```json
{
  "reply": "好的，已下发命令：打开 智能灯-1、智能灯-2。",
  "actions": [ { "tool": "set_property", "device": "light-1",
                 "property": "power", "value": true } ],
  "results": [ { "ok": true, "detail": "已下发：light-1.power = true" } ],
  "mode": "local"
}
```

`mode` 为 `llm`（已配置大模型）或 `local`（离线规则回退）。详见 [AI 助手](ai-assistant.md)。

## WebSocket `GET /ws`

连接后先收一条 `hello`（当前设备与概览），之后实时收到事件流：

| type | 触发 | data |
|---|---|---|
| `hello` | 连接建立 | `{devices: [...], overview: {...}}` |
| `telemetry` | 任一设备上报数据 | `{device_id, points, source}` |
| `device` | 注册/状态变化 | 设备视图 |
| `device_deleted` | 删除设备 | `{id}` |
| `alert` | 规则触发 | 告警对象 |
| `command` | 命令下发 | 命令对象 |

浏览器示例：

```js
const ws = new WebSocket("ws://127.0.0.1:8000/ws");
ws.onmessage = (ev) => console.log(JSON.parse(ev.data));
```

## 错误约定

- `404`：设备/规则不存在（`{"detail": "设备不存在: xxx"}`）
- `422`：参数校验失败（FastAPI 标准格式）
- `200/201`：幂等注册分别表示更新/创建
