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

## v0.3（已发布，2026-11-08）

- [x] **时序聚合查询**：按 1m/5m/1h 窗口降采样（avg/min/max），大时间跨度曲线不再全量拉点。
- [x] *good first issue* **CSV 导出**：流式返回 CSV + 详情页导出按钮
      （由社区贡献者通过 PR #8 交付）。
- [x] *good first issue* **设备分组/标签**：利用现有 `meta` 字段实现分组过滤与
      概览页按组聚合视图。
- [x] *help wanted* **Modbus TCP 适配器**：可配置轮询 Modbus 从站并映射为设备指标，
      参照 `MQTTService` 的「转成 DeviceService 调用」模式（见 docs/modbus.md）。
- [x] **告警确认闭环**：告警中心支持标记已处理，列表 open/acked 过滤，多端同步。
- [x] *help wanted* **多用户与 API Token**：管理端访问令牌（`LINIOT_AUTH_TOKEN`，
      默认开箱即用、可选开启）与仪表盘验证框已交付；MQTT 通道沿用 Broker 鉴权；
      多用户细分账号（每人独立令牌/权限）移至 v0.4。

## v0.4（已发布，2026-11-15）

- [x] *good first issue* **暗色主题**：双主题变量 + 切换按钮 + Canvas 主题化。
      实现吸收了 PR #7 的提案与评审结论（原地重绘、--surface 表面色、网格线主题化）。
- [x] **多用户细分账号**：`LINIOT_API_TOKENS` 为每人签发独立令牌并绑定
      admin/device/viewer 角色矩阵（401/403 语义分离）；更细粒度的端点级
      权限管理界面留待后续。

## v0.5（已发布，2026-11-17）

- [x] **令牌管理界面**：仪表盘可视化签发/吊销独立令牌（落库只存哈希、签发时
      一次性展示、记录最近使用时间），突破 `LINIOT_API_TOKENS` 环境变量
      静态配置的局限。
- [x] **设备影子与离线命令**：desired/reported 状态分离，设备离线时命令缓存、
      上线后自动补发（由维护者交付，v0.5 第二项）。
- [x] **PostgreSQL 存储后端**：`liniot/pg_store.py` 与 SQLite 同接口七域实现，
      `LINIOT_DB_URL` 启用（psycopg 3 可选依赖），CI 带 PG 容器跑对齐测试。

## v0.6（已发布，2026-11-18）

主题：可观测性与权限完善——把 LinIoT 接进主流监控/权限工具链（预计 12 月上旬发布）。

- [x] *good first issue* **令牌角色可视化编辑**：令牌列表直接修改角色
      （`PATCH /api/tokens/{id}`），页面内展示 admin/device/viewer 权限矩阵
      说明，补完 v0.4 留下的「端点级权限管理界面」口子。
- [x] **Prometheus 指标端点**：`GET /metrics` 输出设备总数/在线数/遥测
      保留点数/规则数/开放与 24h 告警计数（文本格式 gauge），配合 Grafana
      即刻接入主流监控栈（无需单独的数据源插件）。
- [x] **TimescaleDB 支持**：PG 后端启动时自动探测 timescaledb 扩展并
      将 telemetry 转为按天分块的 hypertable（分区与降采样加速），扩展
      缺失或迁移失败时静默退回普通表；不自动启用压缩（与环形保留的
      DELETE 冲突，交由用户按需开启）；compose 提供 `--profile timescale`
      变体与文档。

## 远期想法

- TimescaleDB / PostgreSQL 存储后端（`Store` 接口契约已隔离，可插拔实现）
- 设备影子与离线命令缓存（断网续传）
- ESP32 OTA 升级通道
- 插件机制：第三方接入适配器以独立包形式安装
- Grafana 数据源插件

## 版本节奏

小步快跑：每个小版本 2–4 周，跟随 [CHANGELOG](CHANGELOG.md) 发布，
破坏性变更提前一个版本在 Issue 公示。
