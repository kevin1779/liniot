# Roadmap 路线图

> 认领方式：在对应 Issue 下评论「认领」，或直接提交 PR（关联 Issue 编号）。
> 标注 **good first issue** 的任务范围清晰、适合首次贡献；标注 **help wanted**
> 的任务有一定复杂度，维护者会参与方案讨论。

## v0.1（已发布）

- [x] MQTT / HTTP 双通道设备接入
- [x] SQLite 时序存储与环形保留
- [x] 阈值规则引擎（上升沿去重）+ Webhook
- [x] 实时仪表盘（WebSocket + Canvas 图表）
- [x] AI 助手（LLM + 本地回退，动作白名单审计）
- [x] 设备模拟器、ESP32 固件示例、Docker Compose 一键部署

## v0.2「智能化」（已发布）

- [x] **统计异常检测**：EWMA + 3σ，无需配置规则即可发现指标突变
- [x] **告警智能诊断**：规则/异常告警自动生成 LLM 原因分析与处置建议
- [x] **场景联动**：`当 [条件] 则 [动作]` 自动化，含冷却窗口防震荡

## v0.3（规划中，按优先级排序）

- [x] **时序聚合查询**：按 1m/5m/1h 窗口降采样（avg/min/max），大时间跨度曲线不再全量拉点。
- [ ] *good first issue* **暗色主题**：把 `web/style.css` 的设计变量抽成
      `:root` / `[data-theme="dark"]` 双主题 + 仪表盘右上角切换按钮。
- [ ] *good first issue* **CSV 导出**：`GET /api/devices/{id}/telemetry/export`
      流式返回 CSV；前端设备详情页加导出按钮。
- [x] *good first issue* **设备分组/标签**：利用现有 `meta` 字段实现分组过滤与
      概览页按组聚合视图（按组聚合视图为可选项，暂未做）。
- [x] *help wanted* **Modbus TCP 适配器**：可配置轮询 Modbus 从站并映射为设备指标，
      参照 `MQTTService` 的「转成 DeviceService 调用」模式（见 docs/modbus.md）。
- [ ] *help wanted* **多用户与 API Token**：设备 Token 鉴权 + 管理端简单会话，
      保持「默认开箱即用、可选开启鉴权」。

## 远期想法

- TimescaleDB / PostgreSQL 存储后端（`Store` 接口契约已隔离，可插拔实现）
- 设备影子与离线命令缓存（断网续传）
- ESP32 OTA 升级通道
- 插件机制：第三方接入适配器以独立包形式安装
- Grafana 数据源插件

## 版本节奏

小步快跑：每个小版本 2–4 周，跟随 [CHANGELOG](CHANGELOG.md) 发布，
破坏性变更提前一个版本在 Issue 公示。
