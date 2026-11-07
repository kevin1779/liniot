# Changelog

## 未发布

- 遥测 CSV 导出：新增 `GET /api/devices/{id}/telemetry/export` 流式导出接口
  （keyset 分批查询，带 UTF-8 BOM，Excel 直接打开不乱码），设备详情页
  遥测卡片新增导出按钮，链接跟随当前指标；测试增至 60 个
- 时序聚合查询：新增 `GET /api/devices/{id}/telemetry/aggregate`，支持
  1m/5m/15m/1h 窗口返回 count/avg/min/max（10 万行 < 100ms）；设备详情页
  新增聚合视图切换按钮；修复 `ts=0` 被当作缺省时间戳的边界问题；测试增至 62 个
- 告警诊断限流：同一设备默认 300 秒内只诊断一次（`LINIOT_AI_DIAGNOSIS_COOLDOWN`
  可调），避免传感器抖动时 LLM 调用失控（社区建议 #10）；测试增至 64 个
- 设备分组/标签：注册支持 `group` 字段（写入 meta，幂等重注册时保留），
  `GET /api/devices?group=` 过滤，仪表盘设备列表分组下拉与分组列、
  详情页分组徽标（good first issue #3）；测试增至 71 个
- 概览页按分组过滤：分组下拉联动设备在线统计与曲线卡片（补齐 #3 可选项）
- 告警确认闭环：告警中心支持标记已处理（`POST /api/alerts/{id}/ack`），
  列表支持 open/acked 过滤，WebSocket `alert_ack` 多端同步；测试增至 79 个
- 访问令牌鉴权（社区建议 #9 的安全闭环）：设置 `LINIOT_AUTH_TOKEN` 后所有
  API 与 WebSocket 需携带令牌（仪表盘弹出验证框），health 保持开放；
  默认不启用、行为不变；README 安全提示同步更新；测试增至 80 个
- 告警多渠道推送：`LINIOT_ALERT_WEBHOOKS` 支持钉钉/飞书/企业微信群机器人
  （按 URL 自动识别报文），规则与异常告警均推送；测试增至 77 个
- Modbus TCP 接入适配器：`LINIOT_MODBUS_MAP` 配置寄存器映射（四类寄存器 +
  可选系数），后台线程轮询转遥测；pymodbus 为可选依赖，未安装不影响平台
  （社区建议 #5）；测试增至 70 个

## 0.2.0 (2026-10-31)

智能化版本：感知 → 异常发现 → AI 诊断 → 自动处置的完整闭环。

- 统计异常检测：EWMA 基线 + 3σ 判定，无需配置规则即可发现指标突变，
  边沿触发去重（`LINIOT_ANOMALY` 级别告警）
- 场景联动：`当 [设备指标满足条件] 则 [对另一设备执行动作]`，
  冷却窗口防止命令震荡，REST 全套 CRUD + 仪表盘场景页
- 告警智能诊断：配置 LLM Key 后，告警自动异步生成原因分析与处置建议，
  附加在告警详情中（`LINIOT_AI_DIAGNOSIS=off` 可关闭），失败静默降级
- 存储迁移：alerts 表新增 diagnosis 列（旧库自动升级），新增 scenes 表
- 测试增至 57 个；前端静态资源版本刷新

## 0.1.0 (2026-10-30)

首个公开版本。

- 设备接入：HTTP 与 MQTT 双通道，语义一致；遗嘱消息下线检测
- 存储：SQLite（WAL），遥测按设备环形保留，批量窗口清理
- 规则引擎：阈值规则，按「规则 × 设备」上升沿去重，全局规则，Webhook 通知
- 实时仪表盘：原生 JS 零构建，WebSocket 推送 + 轮询降级，Canvas 实时图表，
  属性控制，规则管理，告警中心
- AI 助手：OpenAI 兼容 API（可配 DeepSeek/Ollama 等），无 Key 本地意图回退，
  动作白名单校验与命令审计
- 工具链：设备模拟器、`liniot run/simulate` CLI、40 个 pytest 用例、
  GitHub Actions CI、Docker Compose（含 mosquitto）、ESP32 固件示例（HTTP/MQTT）
