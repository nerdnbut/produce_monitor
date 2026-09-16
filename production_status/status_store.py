"""SQLite persistence; snapshots and incident transitions commit atomically."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

from .config import (INTERVAL_SECONDS, RULE_VERSION, SEVERITY, STALE_SECONDS,
                     bucket_time, database_path)


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class StatusStore:
    def __init__(self, path=None):
        self.path = database_path() if path is None else path

    @contextmanager
    def connection(self, write=False):
        from pathlib import Path
        path = Path(self.path)
        if write:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path), timeout=30)
        else:
            conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            if write:
                conn.commit()
        except Exception:
            if write:
                conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self):
        with self.connection(write=True) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS production_status_snapshot (
                    id INTEGER PRIMARY KEY, bucket TEXT NOT NULL,
                    observed_at TEXT NOT NULL, component TEXT NOT NULL,
                    system TEXT NOT NULL, status TEXT NOT NULL,
                    data_complete INTEGER NOT NULL, reason TEXT NOT NULL,
                    metrics_json TEXT NOT NULL, details_json TEXT NOT NULL,
                    rule_version TEXT NOT NULL, rules_json TEXT NOT NULL,
                    UNIQUE(bucket, component, system)
                );
                CREATE INDEX IF NOT EXISTS status_history_scope
                    ON production_status_snapshot(system, bucket, component);
                CREATE TABLE IF NOT EXISTS production_incident (
                    id INTEGER PRIMARY KEY, component TEXT NOT NULL,
                    system TEXT NOT NULL, started_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL, resolved_at TEXT,
                    severity TEXT NOT NULL, last_status TEXT NOT NULL,
                    title TEXT NOT NULL, description TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_open_incident
                    ON production_incident(component, system) WHERE resolved_at IS NULL;
                CREATE TABLE IF NOT EXISTS production_incident_event (
                    id INTEGER PRIMARY KEY, incident_id INTEGER NOT NULL,
                    observed_at TEXT NOT NULL, status TEXT NOT NULL,
                    description TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS incident_events_parent
                    ON production_incident_event(incident_id, observed_at);
                CREATE TABLE IF NOT EXISTS production_status_run (
                    bucket TEXT PRIMARY KEY, finished_at TEXT NOT NULL,
                    duration_seconds REAL NOT NULL, sample_count INTEGER NOT NULL,
                    error_count INTEGER NOT NULL
                );
            """)

    def has_bucket(self, bucket):
        with self.connection() as conn:
            return conn.execute("SELECT 1 FROM production_status_run WHERE bucket=?", (bucket.isoformat(),)).fetchone() is not None

    def save_snapshots(self, samples, observed_at, duration_seconds, rules):
        bucket = bucket_time(observed_at).isoformat()
        stamp = observed_at.isoformat(timespec="seconds")
        with self.connection(write=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            for sample in samples:
                cursor = conn.execute("""
                    INSERT OR IGNORE INTO production_status_snapshot
                    (bucket, observed_at, component, system, status, data_complete,
                     reason, metrics_json, details_json, rule_version, rules_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (bucket, stamp, sample.component, sample.system, sample.status,
                      int(sample.data_complete), sample.reason, encode(sample.metrics),
                      encode(sample.details), RULE_VERSION, encode(rules)))
                if cursor.rowcount:
                    self._update_incident(conn, sample, stamp)
            conn.execute("""
                INSERT OR IGNORE INTO production_status_run VALUES (?, ?, ?, ?, ?)
            """, (bucket, stamp, duration_seconds, len(samples),
                  sum(not sample.data_complete for sample in samples)))

    @staticmethod
    def _update_incident(conn, sample, stamp):
        current = conn.execute("""
            SELECT * FROM production_incident
            WHERE component=? AND system=? AND resolved_at IS NULL
        """, (sample.component, sample.system)).fetchone()
        status = sample.status
        # Missing observations never resolve an incident; partial data can still prove an issue.
        if status == "operational" and not sample.data_complete:
            status = "unknown"
        if not current:
            if SEVERITY[status] <= 0:
                return
            cursor = conn.execute("""
                INSERT INTO production_incident
                (component, system, started_at, last_seen_at, severity,
                 last_status, title, description) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (sample.component, sample.system, stamp, stamp, status, status,
                  f"{sample.component} / {sample.system} 生产健康异常", sample.reason))
            incident_id = cursor.lastrowid
        else:
            incident_id = current["id"]
            peak = max([current["severity"], status], key=lambda value: SEVERITY[value])
            conn.execute("""
                UPDATE production_incident SET last_seen_at=?, resolved_at=?,
                    severity=?, last_status=?, description=? WHERE id=?
            """, (stamp, stamp if status == "operational" else None, peak, status,
                  sample.reason, incident_id))
            gap = datetime.fromisoformat(stamp) - datetime.fromisoformat(current["last_seen_at"])
            if gap.total_seconds() > INTERVAL_SECONDS * 2:
                conn.execute("""
                    INSERT INTO production_incident_event
                    (incident_id, observed_at, status, description) VALUES (?, ?, ?, ?)
                """, (incident_id, stamp, "unknown", "中间有采集断档；断档期间是否恢复无法确定"))
            if status == current["last_status"]:
                return
        conn.execute("""
            INSERT INTO production_incident_event
            (incident_id, observed_at, status, description) VALUES (?, ?, ?, ?)
        """, (incident_id, stamp, status, sample.reason))

    def systems(self):
        with self.connection() as conn:
            return [row[0] for row in conn.execute("SELECT DISTINCT system FROM production_status_snapshot WHERE system <> 'all' ORDER BY system")]

    def latest(self, system, now):
        with self.connection() as conn:
            rows = conn.execute("""
                SELECT s.* FROM production_status_snapshot s
                JOIN (SELECT component, MAX(bucket) bucket FROM production_status_snapshot
                      WHERE system=? GROUP BY component) latest
                ON s.component=latest.component AND s.bucket=latest.bucket
                WHERE s.system=?
            """, (system, system)).fetchall()
        result = {}
        for row in rows:
            item = dict(row)
            item["metrics"] = json.loads(item.pop("metrics_json"))
            item["details"] = json.loads(item.pop("details_json"))
            if (now - datetime.fromisoformat(item["observed_at"])).total_seconds() > STALE_SECONDS:
                item["status"] = "unknown"
                item["data_complete"] = False
                item["reason"] = "最新快照已超过15分钟，当前状态未知。上次观测：" + item["reason"]
            result[item["component"]] = item
        return result

    def history(self, system, start, end):
        with self.connection() as conn:
            return [dict(row) for row in conn.execute("""
                SELECT bucket, component, status, data_complete
                FROM production_status_snapshot
                WHERE system=? AND bucket>=? AND bucket<? ORDER BY bucket
            """, (system, start.isoformat(), end.isoformat()))]

    def hour_details(self, system, component, start):
        with self.connection() as conn:
            rows = conn.execute("""
                SELECT * FROM production_status_snapshot
                WHERE system=? AND component=? AND bucket>=? AND bucket<? ORDER BY bucket
            """, (system, component, start.isoformat(), (start + timedelta(hours=1)).isoformat())).fetchall()
        return [dict(row) for row in rows]

    def incidents(self, system, start, end):
        with self.connection() as conn:
            rows = conn.execute("""
                SELECT * FROM production_incident WHERE system=? AND started_at<?
                    AND (resolved_at IS NULL OR resolved_at>=?)
                ORDER BY (resolved_at IS NULL) DESC, started_at DESC LIMIT 100
            """, (system, end.isoformat(), start.isoformat())).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["events"] = [dict(event) for event in conn.execute("""
                    SELECT observed_at, status, description FROM production_incident_event
                    WHERE incident_id=? ORDER BY observed_at, id
                """, (item["id"],))]
                result.append(item)
            return result

    def last_run(self):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM production_status_run ORDER BY bucket DESC LIMIT 1").fetchone()
            return dict(row) if row else None


def summarize_buckets(rows, start, end, now):
    """Use the expected five-minute grid; never forward-fill an absent observation."""
    end = min(end, bucket_time(now) + timedelta(seconds=INTERVAL_SECONDS))
    expected = max(0, int((end - start).total_seconds() // INTERVAL_SECONDS))
    selected = [row for row in rows if start.isoformat() <= row["bucket"] < end.isoformat()]
    valid = [row for row in selected if row["status"] != "unknown" and row["data_complete"]]
    available = sum(row["status"] in {"operational", "degraded"} for row in valid)
    healthy = sum(row["status"] == "operational" for row in valid)
    worst = max((row["status"] for row in selected), key=lambda value: SEVERITY[value], default="unknown")
    if len(valid) < expected and SEVERITY[worst] <= 0:
        worst = "unknown"
    return {
        "status": worst, "expected": expected, "observed": len(valid),
        "unknown": max(0, expected - len(valid)),
        "coverage": len(valid) / expected * 100 if expected else None,
        "availability": available / len(valid) * 100 if valid else None,
        "healthy": healthy / len(valid) * 100 if valid else None,
    }
