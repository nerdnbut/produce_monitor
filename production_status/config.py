"""Status configuration. Times are persisted as Beijing wall-clock ISO strings."""
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

COMPONENTS = {
    "recognition": "角色识别 / Recognition",
    "bgm": "BGM Processing",
    "dubbing": "配音 / Dubbing Production",
    "upload": "自动上传 / Auto Upload",
}
STATUS_LABELS = {
    "operational": "正常 · Operational",
    "degraded": "性能下降 · Degraded",
    "partial_outage": "部分异常 · Partial Outage",
    "major_outage": "严重异常 · Major Outage",
    "unknown": "未知 · Unknown",
}
STATUS_COLORS = {
    "operational": "#20bf9a", "degraded": "#f6c344",
    "partial_outage": "#f38b4a", "major_outage": "#e45b64",
    "unknown": "#c5cbd3",
}
SEVERITY = {"unknown": -1, "operational": 0, "degraded": 1,
            "partial_outage": 2, "major_outage": 3}
INTERVAL_SECONDS = 300
STALE_SECONDS = 900
RULE_VERSION = "production-health-v1"


def local_now():
    return datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)


def bucket_time(value):
    return value.replace(minute=value.minute // 5 * 5, second=0, microsecond=0)


def database_path():
    return Path(os.environ.get(
        "PRODUCTION_STATUS_DB", str(Path(__file__).parent / "data" / "status.sqlite3")
    )).expanduser().resolve()


@dataclass(frozen=True)
class Rules:
    min_rate_jobs: int = 20
    failure_degraded: float = 2.0
    failure_partial: float = 5.0
    failure_major: float = 20.0
    recognition_early_hours: float = 24.0
    production_overdue_hours: float = 48.0
    dubbing_overdue_hours: float = 48.0
    backlog_partial_count: int = 5
    backlog_partial_percent: float = 20.0
    bgm_wait_degraded: float = 4.0
    bgm_wait_partial: float = 8.0
    bgm_wait_major: float = 16.0
    bgm_p95_degraded: float = 8.0
    bgm_max_degraded: float = 24.0
    bgm_running_overdue: float = 24.0
    upload_overdue_hours: float = 24.0
    upload_failure_degraded: float = 5.0
    upload_failure_partial: float = 20.0


def load_rules():
    filename = os.environ.get("PRODUCTION_STATUS_RULES")
    overrides = json.loads(Path(filename).read_text(encoding="utf-8")) if filename else {}
    values = asdict(Rules())
    if set(overrides) - set(values):
        raise ValueError("Status 规则配置包含未知字段")
    for key, value in overrides.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < 1e9:
            raise ValueError(f"Status 规则 {key} 必须为有限正数")
    values.update(overrides)
    if not values["failure_degraded"] < values["failure_partial"] < values["failure_major"] <= 100:
        raise ValueError("失败率阈值必须按 degraded < partial < major <= 100 排列")
    if not values["bgm_wait_degraded"] < values["bgm_wait_partial"] < values["bgm_wait_major"]:
        raise ValueError("BGM 等待阈值必须递增")
    if not values["upload_failure_degraded"] < values["upload_failure_partial"] <= 100:
        raise ValueError("上传失败率阈值必须递增且不超过100")
    if values["backlog_partial_percent"] > 100:
        raise ValueError("积压占比不能超过100")
    return Rules(**values)
