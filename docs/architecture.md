# 系统架构

## 总览

```
                        ┌────────────────────────────────────────────────────┐
                        │                    LinIoT 进程                     │
                        │                                                    │
 ESP32/树莓派/模拟器 ────┼──▶ MQTTService ──┐          ┌── WebSocket /ws ──▶ 仪表盘
 （MQTT 客户端）         │   (paho 线程)     │          │                    （浏览器）
                        │                   ▼          │
 任意 HTTP 客户端 ──────┼──▶ FastAPI /api/* ──▶ DeviceService ──▶ EventBus（进程内广播）
                        │   (线程池)            │    │
                        │                      ▼    ▼
                        │                 RuleEngine  事件: telemetry/device/
                        │              AnomalyDetector alert/command/scene_fired
                        │                   │         /alert_diagnosis
                        │                   ▼
                        │              SceneEngine ──▶ set_properties（控制命令）
                        │                   │
                        │              SQLite Store ◀── AI 助手（读快照/写命令/诊断回写）
                        │         devices/telemetry/rules/      │
                        │         alerts/commands/scenes        ▼
                        │                          OpenAI 兼容 /chat/completions
                        └────────────────────────────────────────────────────┘
```

## 模块职责

| 模块 | 文件 | 职责 |
|---|---|---|
| 存储 | `liniot/store.py` | SQLite 全部读写；单连接 + `RLock`，WAL 模式；遥测按设备环形保留 |
| 设备服务 | `liniot/service.py` | 用例编排：注册、遥测入库、状态维护、命令下发；HTTP/MQTT 两通道汇聚点 |
| 规则引擎 | `liniot/rules.py` | 遥测到达时同步判定阈值；按「(规则, 设备)」记录上升沿状态，避免重复告警 |
| 异常检测 | `liniot/anomaly.py` | EWMA 基线 + 3σ 判定，无需配置即可发现指标突变，同样边沿去重 |
| 场景引擎 | `liniot/scene.py` | 「当条件满足则执行动作」，冷却窗口防震荡；动作复用命令通道形成闭环 |
| 事件总线 | `liniot/bus.py` | 进程内发布/订阅；`call_soon_threadsafe` 把 MQTT 线程的事件安全投递到 asyncio |
| MQTT 接入 | `liniot/mqtt_service.py` | 订阅 `liniot/+/#` 上行主题；命令经 `liniot/{id}/command` 下发；断线自动重连 |
| HTTP API | `liniot/api.py` | REST + WebSocket；`/api/*` 语义与 MQTT 通道一致 |
| AI 助手 | `liniot/ai.py` | 对话查询/控制（快照注入 + 白名单校验），以及告警的异步 LLM 诊断回写 |
| 仪表盘 | `web/` | 原生 JS 单页，hash 路由；Canvas 图表零依赖 |
| 模拟器 | `liniot/simulator.py` | 虚拟传感器（随机游走）与智能灯（轮询命令并回报），无需硬件 |

## 关键设计

### 双通道一致语义

HTTP 与 MQTT 上报最终都调用 `DeviceService.ingest()` / `set_status()`，因此规则判定、
在线状态、实时推送的行为与接入方式无关。命令下发同时写库（HTTP 轮询拉取）并经 MQTT
广播（在线订阅），设备任选其一。

### 线程模型

- paho-mqtt 回调运行在独立网络线程；FastAPI 同步端点运行在 anyio 线程池。
- 两者都通过 `EventBus.publish()` → `loop.call_soon_threadsafe()` 把事件投递到
  asyncio 事件循环，再广播给 WebSocket 订阅者，避免跨线程直接操作连接。
- SQLite 共享单连接 + 线程锁，写入串行化；WAL 允许读写并发。单机台内设备规模
  （< 1000 台、点间隔秒级）完全够用，这是「轻量级」的取舍。

### 上升沿告警

规则在数据到达时同步判定。触发态以 `(rule_id, device_id)` 为键保存在内存：
条件持续为真只告警一次，恢复后再次越限才重新告警。全局规则（`device_id` 为空）
对每台设备独立做边沿判定，互不干扰（tests/test_rules.py 有专门回归用例）。
异常检测与场景引擎沿用同一语义：持续异常不重复告警，场景动作受冷却窗口约束。

### 智能处理管线

每批遥测在 `DeviceService.ingest` 中依次经过：入库 → 规则判定 → 异常检测 →
场景联动，全部同步完成（微秒级）；LLM 诊断是唯一的异步环节——`main.py` 的
`diagnosis_worker` 协程监听告警事件，调 LLM 生成原因分析后回写
`alerts.diagnosis` 并广播 `alert_diagnosis` 事件，失败静默降级，不阻塞数据面。

### 数据保留

每设备保留最近 `LINIOT_RETENTION`（默认 20000）条遥测。写入路径每 200 条批量
执行一次窗口函数清理，避免表无限增长；索引 `(device_id, metric, ts)` 保证曲线
查询为 O(log n + limit)。

### AI 安全边界

AI 只能调用 `set_property` 一个动作，且属性名必须已存在于设备 `properties` 中，
值必须是标量；LLM 输出按 JSON 解析，非法动作被逐条拒绝并保留在响应的 `results`
里可审计。所有下发命令写入 `commands` 表，设备确认后属性才变更。

## 访问控制与多用户

访问控制位于 `build_router` 的 `require_token` 依赖中，作用于除
`GET /api/health`（探活）外的全部 `/api/*` 路由，语义分两层：

- **401**：令牌缺失或错误。主体由 `resolve_principal` 解析——依次匹配
  共享令牌（`LINIOT_AUTH_TOKEN`，视为 admin）、多用户令牌
  （`LINIOT_API_TOKENS`，名称:令牌:角色，由 `config.parse_api_tokens`
  解析，非法条目跳过）与数据库动态令牌（`api_tokens` 表，只存 SHA-256
  哈希，由仪表盘「访问令牌」页签发/吊销，查找命中即节流刷新
  `last_used_at`）；
- **403**：令牌有效但角色无权。角色矩阵：admin 全部；device 读取 +
  上报遥测/状态 + 拉取命令 + 设备自注册（`DEVICE_WRITE_RE` 限定写路径）；
  viewer 只读。

未配置任何令牌（环境变量与数据库均无）时 `resolve_principal` 返回匿名
admin——开箱即用行为与历史版本完全一致；签发首个动态令牌后匿名访问
自动关闭。令牌管理端点（`/api/tokens` CRUD）挂在独立的 admin 专属路由组
（`require_admin`），viewer/device 角色访问直接 403。WebSocket 通过
`?token=` 查询参数做同一校验（浏览器 WS 无法自定义请求头），拒绝码 4401。

设计取舍：令牌管理界面已交付（签发/吊销/角色绑定，令牌即"用户"）；
端点级细粒度权限编辑界面仍记录在 ROADMAP，按需推进。

## 设备影子与离线命令

影子落在 `shadows` 表（`device_id` 主键）：`desired`（控制端期望）与
`reported`（设备回报）两侧独立合并，每次变更 `version` +1。写入路径：

- `POST /properties`（`DeviceService.set_properties`）：命令照旧入队并经
  MQTT 广播，同时合并进 `desired`——设备离线时控制意图持久可见；
- `POST /status` 与 MQTT status（`DeviceService.set_status`）：属性照旧
  合并进 `devices.properties`，同时写 `reported`；心跳的空属性不算变化。

`GET /api/devices/{id}/shadow` 返回 desired/reported/delta/version，delta
即两侧不一致的键——设备重连后单请求即可知道该做什么，逐键执行并回报后
delta 自然清空。

**自动补发**：设备拉取命令（`poll_commands`）时对 delta 中每个键检查
`commands` 表（按 `property_key` 列查询）：无在途命令、且最近一次投递超过
10 秒宽限窗口（覆盖拉取→执行→回报耗时）→ 重新入队，payload 带
`reason=shadow_retry`。这把 v0.x「命令只投递一次、拉取后掉线即丢失」的
缺口补成至少一次送达，最终以 reported 追平 desired 为准。

## 扩展点

- **新协议接入**：实现一个把报文转成 `DeviceService` 调用的服务类即可（参考
  `MQTTService.handle_telemetry_payload`）。
- **新规则类型**：在 `rules.py` 增加判定函数与持久化字段；引擎与 API 结构已分离。
- **替换存储**：`Store` 的接口即契约（devices/telemetry/rules/alerts/commands/
  shadows/api_tokens 七域）。PostgreSQL 实现已随 `liniot/pg_store.py` 提供：
  设置 `LINIOT_DB_URL` 即启用（需 `pip install ".[postgres]"`，psycopg 3，
  可选依赖），行为一致性由带 postgres service 容器的 CI 对齐测试保证；
  SQLite 仍为默认。
