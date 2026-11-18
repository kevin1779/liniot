"""PostgreSQL 存储后端：与 Store（SQLite）七域同接口的可插拔实现。

启用方式：设置 `LINIOT_DB_URL`（如 postgresql://liniot:liniot@127.0.0.1:5432/liniot）
并安装可选依赖 `pip install ".[postgres]"`（psycopg 3）。未配置时平台仍用 SQLite，
未安装 psycopg 不影响默认路径（本模块仅在选中 PG 后端时才被导入）。

与 SQLite 方言的差异点（其余 SQL 保持逐句对齐，行为一致性由测试保证）：
- 占位符 `%s`；自增主键 BIGSERIAL，插入用 `RETURNING id` 取代 lastrowid；
- 布尔条件求和改用 `COUNT(*) FILTER`；
- 聚合桶用 `FLOOR(ts / n)::bigint`（SQLite 的 CAST(ts / n AS INTEGER)）；
- 嵌套子查询需显式别名。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row

from .store import _row_to_device, hash_token

logger = logging.getLogger("liniot.pg")

PG_SCHEMA: list[str] = [
    """CREATE TABLE IF NOT EXISTS devices (
        id         TEXT PRIMARY KEY,
        name       TEXT NOT NULL,
        type       TEXT NOT NULL DEFAULT 'generic',
        meta       TEXT NOT NULL DEFAULT '{}',
        properties TEXT NOT NULL DEFAULT '{}',
        status     TEXT NOT NULL DEFAULT 'offline',
        last_seen  DOUBLE PRECISION NOT NULL DEFAULT 0,
        created_at DOUBLE PRECISION NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS telemetry (
        id        BIGSERIAL PRIMARY KEY,
        device_id TEXT NOT NULL,
        metric    TEXT NOT NULL,
        value     DOUBLE PRECISION NOT NULL,
        -- TimescaleDB 的分块列必须是时间类型，故用 TIMESTAMPTZ 存 epoch；
        -- 边界处与 float epoch 互转，对外 API 语义与 SQLite 完全一致
        ts        TIMESTAMPTZ NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_tel_dev_metric_ts ON telemetry(device_id, metric, ts)",
    """CREATE TABLE IF NOT EXISTS rules (
        id         BIGSERIAL PRIMARY KEY,
        device_id  TEXT,
        metric     TEXT NOT NULL,
        op         TEXT NOT NULL,
        threshold  DOUBLE PRECISION NOT NULL,
        enabled    SMALLINT NOT NULL DEFAULT 1,
        note       TEXT NOT NULL DEFAULT '',
        created_at DOUBLE PRECISION NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS alerts (
        id        BIGSERIAL PRIMARY KEY,
        rule_id   BIGINT,
        device_id TEXT,
        message   TEXT NOT NULL,
        level     TEXT NOT NULL DEFAULT 'warning',
        ts        DOUBLE PRECISION NOT NULL,
        diagnosis TEXT,
        acknowledged    SMALLINT NOT NULL DEFAULT 0,
        acknowledged_at DOUBLE PRECISION
    )""",
    "CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts)",
    """CREATE TABLE IF NOT EXISTS scenes (
        id               BIGSERIAL PRIMARY KEY,
        name             TEXT NOT NULL,
        enabled          SMALLINT NOT NULL DEFAULT 1,
        source_device_id TEXT,
        metric           TEXT NOT NULL,
        op               TEXT NOT NULL,
        threshold        DOUBLE PRECISION NOT NULL,
        action_device_id TEXT NOT NULL,
        action_property  TEXT NOT NULL,
        action_value     TEXT NOT NULL,
        cooldown         INTEGER NOT NULL DEFAULT 60,
        last_fired       DOUBLE PRECISION NOT NULL DEFAULT 0,
        created_at       DOUBLE PRECISION NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS commands (
        id           BIGSERIAL PRIMARY KEY,
        device_id    TEXT NOT NULL,
        payload      TEXT NOT NULL,
        created_at   DOUBLE PRECISION NOT NULL,
        delivered_at DOUBLE PRECISION,
        property_key TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS api_tokens (
        id           BIGSERIAL PRIMARY KEY,
        name         TEXT NOT NULL,
        token_hash   TEXT NOT NULL UNIQUE,
        role         TEXT NOT NULL DEFAULT 'viewer',
        created_at   DOUBLE PRECISION NOT NULL,
        last_used_at DOUBLE PRECISION,
        revoked_at   DOUBLE PRECISION
    )""",
    """CREATE TABLE IF NOT EXISTS shadows (
        device_id   TEXT PRIMARY KEY,
        desired     TEXT NOT NULL DEFAULT '{}',
        reported    TEXT NOT NULL DEFAULT '{}',
        version     INTEGER NOT NULL DEFAULT 0,
        desired_ts  DOUBLE PRECISION,
        reported_ts DOUBLE PRECISION
    )""",
]

_EMPTY_SHADOW = {"desired": {}, "reported": {}, "version": 0,
                 "desired_ts": None, "reported_ts": None}


class PostgresStore:
    """与 Store 同接口的 PostgreSQL 实现：单连接 + 线程锁，所有访问串行化。"""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.lock = threading.RLock()
        self._inserts_since_prune = 0
        self.timescaledb = False
        with self.lock:
            # autocommit：语句即提交，避免只读查询把连接留在
            # idle-in-transaction 状态长期持有表锁（truncate/迁移会被卡住）
            self.conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
            for statement in PG_SCHEMA:
                self.conn.execute(statement)
            self._try_enable_timescaledb()

    def _try_enable_timescaledb(self) -> None:
        """探测 timescaledb 扩展并把 telemetry 转为 hypertable（best-effort）。

        扩展缺失、权限不足或已有数据迁移失败时记一条告警并退回普通表——
        不影响任何功能。不自动启用压缩策略：压缩 chunk 会与环形保留的
        DELETE 冲突，是否压缩交由用户按 TimescaleDB 文档自行决定。
        """
        try:
            row = self.conn.execute(
                "SELECT extname FROM pg_extension WHERE extname='timescaledb'"
            ).fetchone()
            if row is None:
                return  # 未安装扩展：普通表即可
            self.conn.execute(
                "SELECT create_hypertable('telemetry', 'ts',"
                " if_not_exists => TRUE, migrate_data => TRUE,"
                " chunk_time_interval => INTERVAL '1 day')"
            )
            self.timescaledb = True
            logger.info("TimescaleDB 已启用：telemetry 转为 hypertable（按天分块）")
        except Exception as exc:
            logger.warning("TimescaleDB hypertable 启用失败，退回普通表: %s", exc)

    def _one(self, sql: str, params: tuple | list = ()) -> Optional[dict[str, Any]]:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    # ------------------------------------------------------------------ 设备

    def upsert_device(self, device_id: str, name: str, type_: str,
                      meta: dict[str, str]) -> tuple[dict[str, Any], bool]:
        """注册设备；id 已存在时更新名称/类型/元数据并返回 (设备, created=False)。"""
        now = time.time()
        with self.lock:
            row = self._one("SELECT id FROM devices WHERE id=%s", (device_id,))
            if row is None:
                self.conn.execute(
                    "INSERT INTO devices(id,name,type,meta,properties,status,last_seen,created_at)"
                    " VALUES(%s,%s,%s,%s,'{}','offline',0,%s)",
                    (device_id, name, type_, json.dumps(meta, ensure_ascii=False), now),
                )
                self.conn.commit()
                created = True
            else:
                self.conn.execute(
                    "UPDATE devices SET name=%s, type=%s, meta=%s WHERE id=%s",
                    (name, type_, json.dumps(meta, ensure_ascii=False), device_id),
                )
                self.conn.commit()
                created = False
            return self.get_device(device_id), created  # type: ignore[return-value]

    def get_device(self, device_id: str) -> Optional[dict[str, Any]]:
        row = self._one("SELECT * FROM devices WHERE id=%s", (device_id,))
        return _row_to_device(row) if row else None

    def list_devices(self) -> list[dict[str, Any]]:
        return [_row_to_device(r)
                for r in self._all("SELECT * FROM devices ORDER BY created_at")]

    def delete_device(self, device_id: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM devices WHERE id=%s", (device_id,))
            for table in ("telemetry", "rules", "commands", "shadows"):
                self.conn.execute(f"DELETE FROM {table} WHERE device_id=%s", (device_id,))
            self.conn.commit()

    def set_status(self, device_id: str, status: str,
                   properties_patch: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
        with self.lock:
            row = self._one("SELECT properties FROM devices WHERE id=%s", (device_id,))
            if row is None:
                return None
            props = json.loads(row["properties"])
            if properties_patch:
                props.update(properties_patch)
            self.conn.execute(
                "UPDATE devices SET status=%s, properties=%s, last_seen=%s WHERE id=%s",
                (status, json.dumps(props, ensure_ascii=False), time.time(), device_id),
            )
            self.conn.commit()
        return self.get_device(device_id)

    def touch(self, device_id: str) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE devices SET last_seen=%s, status='online' WHERE id=%s",
                (time.time(), device_id),
            )
            self.conn.commit()

    # ------------------------------------------------------------------ 遥测

    def insert_telemetry(self, device_id: str, points: list[dict[str, Any]]) -> int:
        now = time.time()
        # ts=0 是合法时间戳，须用显式判空而非 or 兜底；epoch float → UTC datetime
        rows = [(device_id, p["metric"], float(p["value"]),
                 datetime.fromtimestamp(float(p["ts"]) if p.get("ts") is not None
                                        else now, tz=timezone.utc))
                for p in points]
        with self.lock:
            with self.conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO telemetry(device_id,metric,value,ts) VALUES(%s,%s,%s,%s)",
                    rows,
                )
            self._inserts_since_prune += len(rows)
            if self._inserts_since_prune >= 200:
                self._inserts_since_prune = 0
                self._prune()
        return len(rows)

    def _prune(self) -> None:
        self.conn.execute(
            "DELETE FROM telemetry WHERE id IN ("
            "  SELECT id FROM ("
            "    SELECT id, ROW_NUMBER() OVER (PARTITION BY device_id ORDER BY id DESC) AS rn"
            "    FROM telemetry) AS t WHERE rn > 20000)"
        )
        self.conn.commit()

    def prune(self, retention: int) -> None:
        """按设备保留最近 retention 条记录（供测试与运维显式调用）。"""
        with self.lock:
            self.conn.execute(
                "DELETE FROM telemetry WHERE id IN ("
                "  SELECT id FROM ("
                "    SELECT id, ROW_NUMBER() OVER (PARTITION BY device_id ORDER BY id DESC) AS rn"
                "    FROM telemetry) AS t WHERE rn > %s)",
                (retention,),
            )
            self.conn.commit()

    def query_telemetry(self, device_id: str, metric: Optional[str] = None,
                        limit: int = 200) -> list[dict[str, Any]]:
        """按时间升序返回遥测序列，供图表绘制。"""
        sql = "SELECT metric,value,ts FROM telemetry WHERE device_id=%s"
        params: list[Any] = [device_id]
        if metric:
            sql += " AND metric=%s"
            params.append(metric)
        sql += " ORDER BY ts DESC, id DESC LIMIT %s"
        params.append(limit)
        rows = self._all(sql, params)
        return [{"metric": r["metric"], "value": r["value"],
                 "ts": r["ts"].timestamp()}  # TIMESTAMPTZ → float epoch
                for r in reversed(rows)]

    def iter_telemetry(self, device_id: str, metric: Optional[str] = None,
                       limit: Optional[int] = None,
                       batch: int = 1000) -> Iterator[dict[str, Any]]:
        """流式迭代遥测：keyset 分页逐批查询，供 CSV 导出等大结果集场景。"""
        last_id = 0
        if limit is not None and limit > 0:
            sql = "SELECT id FROM telemetry WHERE device_id=%s"
            params: list[Any] = [device_id]
            if metric:
                sql += " AND metric=%s"
                params.append(metric)
            sql += " ORDER BY id DESC LIMIT 1 OFFSET %s"
            params.append(limit - 1)
            row = self._one(sql, params)
            if row is not None:
                last_id = row["id"] - 1  # 其后恰为"最近 limit 条"
        while True:
            sql = "SELECT id,metric,value,ts FROM telemetry WHERE device_id=%s AND id>%s"
            params = [device_id, last_id]
            if metric:
                sql += " AND metric=%s"
                params.append(metric)
            sql += " ORDER BY id LIMIT %s"
            params.append(batch)
            rows = self._all(sql, params)
            if not rows:
                return
            for r in rows:
                yield {"metric": r["metric"], "value": r["value"],
                       "ts": r["ts"].timestamp()}
            last_id = rows[-1]["id"]

    def aggregate_telemetry(self, device_id: str, metric: Optional[str] = None,
                            window: int = 300) -> list[dict[str, Any]]:
        """按时间桶聚合遥测：返回每 (metric, 桶) 的 count/avg/min/max。

        桶按 floor(epoch 秒 / window) * window 对齐（UTC 秒），与 SQLite
        后端的 CAST 行为一致，输出 ts 为整数 epoch。
        """
        sql = ("SELECT metric, FLOOR(EXTRACT(epoch FROM ts) / %s)::bigint * %s AS bucket,"
               " COUNT(*) AS count, AVG(value) AS avg,"
               " MIN(value) AS min, MAX(value) AS max"
               " FROM telemetry WHERE device_id=%s")
        params: list[Any] = [window, window, device_id]
        if metric:
            sql += " AND metric=%s"
            params.append(metric)
        sql += " GROUP BY metric, bucket ORDER BY metric, bucket"
        rows = self._all(sql, params)
        return [{"metric": r["metric"], "ts": r["bucket"], "count": r["count"],
                 "avg": r["avg"], "min": r["min"], "max": r["max"]} for r in rows]

    def latest_metrics(self) -> dict[tuple[str, str], dict[str, float]]:
        """每个 (device, metric) 的最新值，供 AI 快照与概览使用。"""
        rows = self._all(
            "SELECT device_id, metric, value, ts FROM telemetry"
            " WHERE id IN (SELECT MAX(id) FROM telemetry GROUP BY device_id, metric)"
        )
        return {(r["device_id"], r["metric"]): {"value": r["value"],
                                                "ts": r["ts"].timestamp()}
                for r in rows}

    def metrics_of(self, device_id: str) -> list[str]:
        rows = self._all(
            "SELECT DISTINCT metric FROM telemetry WHERE device_id=%s ORDER BY metric",
            (device_id,),
        )
        return [r["metric"] for r in rows]

    def count_telemetry(self) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM telemetry")
        return row["n"] if row else 0

    # ------------------------------------------------------------------ 规则

    def create_rule(self, device_id: Optional[str], metric: str, op: str,
                    threshold: float, note: str = "", enabled: bool = True) -> dict[str, Any]:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO rules(device_id,metric,op,threshold,enabled,note,created_at)"
                " VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (device_id, metric, op, threshold, int(enabled), note, time.time()),
            )
            rule_id = cur.fetchone()["id"]
            self.conn.commit()
            return self.get_rule(rule_id)  # type: ignore[return-value]

    def get_rule(self, rule_id: int) -> Optional[dict[str, Any]]:
        row = self._one("SELECT * FROM rules WHERE id=%s", (rule_id,))
        return dict(row) if row else None

    def list_rules(self, only_enabled: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM rules" + (" WHERE enabled=1" if only_enabled else "") + " ORDER BY id"
        return [dict(r) for r in self._all(sql)]

    def update_rule(self, rule_id: int, **changes: Any) -> Optional[dict[str, Any]]:
        allowed = {"enabled", "note", "threshold"}
        sets, params = [], []
        for key, value in changes.items():
            if key in allowed and value is not None:
                sets.append(f"{key}=%s")
                params.append(value if not isinstance(value, bool) else int(value))
        if not sets:
            return self.get_rule(rule_id)
        with self.lock:
            self.conn.execute(f"UPDATE rules SET {', '.join(sets)} WHERE id=%s", (*params, rule_id))
            self.conn.commit()
        return self.get_rule(rule_id)

    def delete_rule(self, rule_id: int) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM rules WHERE id=%s", (rule_id,))
            self.conn.commit()

    # ------------------------------------------------------------------ 告警

    def insert_alert(self, rule_id: Optional[int], device_id: Optional[str],
                     message: str, level: str = "warning") -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO alerts(rule_id,device_id,message,level,ts)"
                " VALUES(%s,%s,%s,%s,%s) RETURNING id",
                (rule_id, device_id, message, level, time.time()),
            )
            alert_id = cur.fetchone()["id"]
            self.conn.commit()
        return int(alert_id)

    def list_alerts(self, limit: int = 50, ack: Optional[str] = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM alerts"
        params: list[Any] = []
        if ack == "open":
            sql += " WHERE acknowledged=0"
        elif ack in ("acked", "done"):
            sql += " WHERE acknowledged=1"
        sql += " ORDER BY id DESC LIMIT %s"
        params.append(limit)
        return [dict(r) for r in self._all(sql, params)]

    def acknowledge_alert(self, alert_id: int) -> Optional[dict[str, Any]]:
        """标记告警已处理，返回更新后的告警；不存在返回 None。"""
        with self.lock:
            if self._one("SELECT id FROM alerts WHERE id=%s", (alert_id,)) is None:
                return None
            self.conn.execute(
                "UPDATE alerts SET acknowledged=1, acknowledged_at=%s WHERE id=%s",
                (time.time(), alert_id))
            self.conn.commit()
            row = self._one("SELECT * FROM alerts WHERE id=%s", (alert_id,))
        return dict(row) if row else None

    def set_alert_diagnosis(self, alert_id: int, diagnosis: str) -> None:
        with self.lock:
            self.conn.execute("UPDATE alerts SET diagnosis=%s WHERE id=%s",
                              (diagnosis, alert_id))
            self.conn.commit()

    def count_alerts_since(self, since: float) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM alerts WHERE ts>=%s", (since,))
        return row["n"] if row else 0

    def count_open_alerts(self) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM alerts WHERE acknowledged=0")
        return row["n"] if row else 0

    # ------------------------------------------------------------------ 场景

    def create_scene(self, name: str, source_device_id: Optional[str], metric: str,
                     op: str, threshold: float, action_device_id: str,
                     action_property: str, action_value: str,
                     cooldown: int = 60) -> dict[str, Any]:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO scenes(name,enabled,source_device_id,metric,op,threshold,"
                "action_device_id,action_property,action_value,cooldown,created_at)"
                " VALUES(%s,1,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (name, source_device_id, metric, op, threshold,
                 action_device_id, action_property, action_value, cooldown, time.time()),
            )
            scene_id = cur.fetchone()["id"]
            self.conn.commit()
            return self.get_scene(scene_id)  # type: ignore[return-value]

    def get_scene(self, scene_id: int) -> Optional[dict[str, Any]]:
        row = self._one("SELECT * FROM scenes WHERE id=%s", (scene_id,))
        return dict(row) if row else None

    def list_scenes(self, only_enabled: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM scenes" + (" WHERE enabled=1" if only_enabled else "") + " ORDER BY id"
        return [dict(r) for r in self._all(sql)]

    def update_scene(self, scene_id: int, **changes: Any) -> Optional[dict[str, Any]]:
        allowed = {"enabled", "name", "threshold", "cooldown"}
        sets, params = [], []
        for key, value in changes.items():
            if key in allowed and value is not None:
                sets.append(f"{key}=%s")
                params.append(value if not isinstance(value, bool) else int(value))
        if not sets:
            return self.get_scene(scene_id)
        with self.lock:
            self.conn.execute(f"UPDATE scenes SET {', '.join(sets)} WHERE id=%s",
                              (*params, scene_id))
            self.conn.commit()
        return self.get_scene(scene_id)

    def delete_scene(self, scene_id: int) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM scenes WHERE id=%s", (scene_id,))
            self.conn.commit()

    def touch_scene(self, scene_id: int, ts: float) -> None:
        with self.lock:
            self.conn.execute("UPDATE scenes SET last_fired=%s WHERE id=%s", (ts, scene_id))
            self.conn.commit()

    # ------------------------------------------------------------------ 命令

    def queue_command(self, device_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO commands(device_id,payload,created_at,property_key)"
                " VALUES(%s,%s,%s,%s) RETURNING id",
                (device_id, json.dumps(payload, ensure_ascii=False), now,
                 payload.get("property")),
            )
            command_id = cur.fetchone()["id"]
            self.conn.commit()
            return {"id": command_id, "device_id": device_id,
                    "payload": payload, "created_at": now, "delivered_at": None}

    def pending_commands(self, device_id: str) -> list[dict[str, Any]]:
        """取走待下发命令并标记为已下发（HTTP 轮询通道）。"""
        now = time.time()
        with self.lock:
            rows = self._all(
                "SELECT * FROM commands WHERE device_id=%s AND delivered_at IS NULL ORDER BY id",
                (device_id,),
            )
            if rows:
                self.conn.execute(
                    "UPDATE commands SET delivered_at=%s WHERE id = ANY(%s)",
                    (now, [r["id"] for r in rows]),
                )
                self.conn.commit()
        return [{"id": r["id"], "payload": json.loads(r["payload"]),
                 "created_at": r["created_at"]} for r in rows]

    # ------------------------------------------------------------------ 设备影子

    def get_shadow(self, device_id: str) -> dict[str, Any]:
        """读影子；设备尚无影子时返回全空结构（version 0）。"""
        row = self._one("SELECT * FROM shadows WHERE device_id=%s", (device_id,))
        if row is None:
            return {"device_id": device_id, **_EMPTY_SHADOW}
        return {"device_id": row["device_id"],
                "desired": json.loads(row["desired"]),
                "reported": json.loads(row["reported"]),
                "version": row["version"],
                "desired_ts": row["desired_ts"],
                "reported_ts": row["reported_ts"]}

    def _merge_shadow(self, device_id: str, field: str,
                      patch: dict[str, Any]) -> dict[str, Any]:
        """合并 desired/reported 一侧并升版本（delta 由读取方按需计算）。"""
        if field not in ("desired", "reported"):
            raise ValueError(field)
        with self.lock:
            shadow = self.get_shadow(device_id)
            merged = {**shadow[field], **patch}
            version = shadow["version"] + 1
            now = time.time()
            desired = merged if field == "desired" else shadow["desired"]
            reported = shadow["reported"] if field == "desired" else merged
            desired_ts = now if field == "desired" else shadow["desired_ts"]
            reported_ts = shadow["reported_ts"] if field == "desired" else now
            self.conn.execute(
                "INSERT INTO shadows(device_id,desired,reported,version,desired_ts,reported_ts)"
                " VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(device_id) DO UPDATE SET"
                " desired=EXCLUDED.desired, reported=EXCLUDED.reported,"
                " version=EXCLUDED.version, desired_ts=EXCLUDED.desired_ts,"
                " reported_ts=EXCLUDED.reported_ts",
                (device_id, json.dumps(desired, ensure_ascii=False),
                 json.dumps(reported, ensure_ascii=False), version,
                 desired_ts, reported_ts),
            )
            self.conn.commit()
        return self.get_shadow(device_id)

    def set_desired(self, device_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """控制端写期望状态；空 patch 直接返回现值。"""
        if not patch:
            return self.get_shadow(device_id)
        return self._merge_shadow(device_id, "desired", patch)

    def set_reported(self, device_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """设备侧写上报状态；空 patch 直接返回现值。"""
        if not patch:
            return self.get_shadow(device_id)
        return self._merge_shadow(device_id, "reported", patch)

    def requeue_shadow_commands(self, device_id: str, delta: dict[str, Any],
                                grace: float = 10.0) -> list[dict[str, Any]]:
        """影子补发：delta 中「无在途命令、且最近一次投递已超过 grace 秒」的键重新入队。"""
        if not delta:
            return []
        now = time.time()
        requeued: list[dict[str, Any]] = []
        with self.lock:
            for key, value in delta.items():
                row = self._one(
                    "SELECT COUNT(*) FILTER (WHERE delivered_at IS NULL) AS pending,"
                    " MAX(delivered_at) AS last_delivered"
                    " FROM commands WHERE device_id=%s AND property_key=%s",
                    (device_id, key),
                )
                if row["pending"]:
                    continue  # 在途命令还没被拉走，等设备自行拉取即可
                if row["last_delivered"] is not None and now - row["last_delivered"] < grace:
                    continue  # 刚投递过，先给设备留执行 + 回报的时间
                payload = {"property": key, "value": value, "reason": "shadow_retry"}
                cur = self.conn.execute(
                    "INSERT INTO commands(device_id,payload,created_at,property_key)"
                    " VALUES(%s,%s,%s,%s) RETURNING id",
                    (device_id, json.dumps(payload, ensure_ascii=False), now, key),
                )
                requeued.append({"id": cur.fetchone()["id"], "device_id": device_id,
                                 "payload": payload, "created_at": now,
                                 "delivered_at": None})
            if requeued:
                self.conn.commit()
        return requeued

    # ------------------------------------------------------------------ API 令牌

    def create_token(self, name: str, role: str, token_hash: str) -> dict[str, Any]:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO api_tokens(name,token_hash,role,created_at)"
                " VALUES(%s,%s,%s,%s) RETURNING id",
                (name, token_hash, role, time.time()),
            )
            token_id = cur.fetchone()["id"]
            self.conn.commit()
            return self.get_token(token_id)  # type: ignore[return-value]

    def get_token(self, token_id: int) -> Optional[dict[str, Any]]:
        row = self._one("SELECT * FROM api_tokens WHERE id=%s", (token_id,))
        return dict(row) if row else None

    def list_tokens(self) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT * FROM api_tokens ORDER BY (revoked_at IS NOT NULL), id DESC")
        return [dict(r) for r in rows]

    def revoke_token(self, token_id: int) -> Optional[dict[str, Any]]:
        """吊销令牌（保留记录用于审计）；已吊销的重复吊销幂等返回原记录。"""
        with self.lock:
            row = self._one("SELECT * FROM api_tokens WHERE id=%s", (token_id,))
            if row is None:
                return None
            if row["revoked_at"] is None:
                self.conn.execute(
                    "UPDATE api_tokens SET revoked_at=%s WHERE id=%s",
                    (time.time(), token_id))
                self.conn.commit()
        return self.get_token(token_id)

    def find_token(self, token_hash: str, touch_after: float = 60.0) -> Optional[dict[str, Any]]:
        """按哈希查找有效令牌（已吊销视为无效），并节流刷新 last_used_at。"""
        with self.lock:
            row = self._one(
                "SELECT * FROM api_tokens WHERE token_hash=%s AND revoked_at IS NULL",
                (token_hash,),
            )
            if row is None:
                return None
            now = time.time()
            if row["last_used_at"] is None or row["last_used_at"] < now - touch_after:
                self.conn.execute(
                    "UPDATE api_tokens SET last_used_at=%s WHERE id=%s", (now, row["id"]))
                self.conn.commit()
        return dict(row)

    def update_token(self, token_id: int, role: str) -> Optional[dict[str, Any]]:
        """修改令牌角色；不存在返回 None。"""
        with self.lock:
            row = self._one("SELECT id FROM api_tokens WHERE id=%s", (token_id,))
            if row is None:
                return None
            self.conn.execute("UPDATE api_tokens SET role=%s WHERE id=%s", (role, token_id))
        return self.get_token(token_id)

    def count_active_admin_tokens(self, exclude_id: Optional[int] = None) -> int:
        """未吊销的 admin 令牌数（排除 exclude_id），供防自锁守卫使用。"""
        sql = ("SELECT COUNT(*) AS n FROM api_tokens"
               " WHERE role='admin' AND revoked_at IS NULL")
        params: list[Any] = []
        if exclude_id is not None:
            sql += " AND id != %s"
            params.append(exclude_id)
        row = self._one(sql, params)
        return row["n"] if row else 0

    def count_tokens(self) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM api_tokens")
        return row["n"] if row else 0
