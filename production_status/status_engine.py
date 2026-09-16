"""Pure health rules: current task risk, not a synthetic machine heartbeat.

Failure ratios use outstanding jobs plus completions in the last 24 hours.
Upload uses the current results of jobs ENTERED in the last 24 hours: the source
has no result timestamps, so this is deliberately not a 30-minute error rate.
"""
from dataclasses import asdict, dataclass, field

import pandas as pd

from .config import SEVERITY, Rules


@dataclass
class Snapshot:
    component: str
    system: str
    status: str = "unknown"
    reason: str = "暂无可判断的数据"
    data_complete: bool = True
    metrics: dict = field(default_factory=dict)
    details: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


def unknown(component, system, reason):
    return Snapshot(component, system, reason=reason, data_complete=False)


def text_column(df, name):
    return df.get(name, pd.Series("", index=df.index)).fillna("").astype(str).str.strip()


def time_column(df, name):
    # The shared fetcher returns local naive datetimes; normalize any offset strings.
    def parse(value):
        try:
            result = pd.Timestamp(value)
            if result.tzinfo is not None:
                result = result.tz_convert("Asia/Shanghai").tz_localize(None)
            return result
        except (ValueError, TypeError, OverflowError):
            return pd.NaT
    return pd.to_datetime(df.get(name, pd.Series(pd.NaT, index=df.index)).map(parse), errors="coerce")


def age_hours(now, dates):
    return ((pd.Timestamp(now) - dates).dt.total_seconds() / 3600).where(dates <= now)


def recent(dates, now):
    return dates.between(pd.Timestamp(now) - pd.Timedelta(hours=24), pd.Timestamp(now))


def raise_status(snapshot, status, reason):
    if SEVERITY[status] > SEVERITY[snapshot.status]:
        snapshot.status = status
    snapshot.reason = f"{snapshot.reason}；{reason}" if snapshot.reason else reason


def fail_rule(snapshot, rules, upload=False):
    failed = snapshot.metrics["failed_jobs"]
    total = snapshot.metrics["total_jobs"]
    rate = 100 * failed / total if total else 0.0
    snapshot.metrics["failure_rate"] = round(rate, 2)
    degraded_threshold = rules.upload_failure_degraded if upload else rules.failure_degraded
    if failed and (total < rules.min_rate_jobs or rate >= degraded_threshold):
        raise_status(snapshot, "degraded", f"{failed} 个失败任务，当前统计范围占比 {rate:.1f}%")
    elif failed:
        snapshot.reason = f"{failed} 个失败任务，占比 {rate:.1f}%，未达到性能下降阈值"
    if total < rules.min_rate_jobs:
        return
    if upload:
        if rate > rules.upload_failure_partial:
            raise_status(snapshot, "partial_outage", "上传失败占比超过部分异常阈值")
    elif rate > rules.failure_major:
        raise_status(snapshot, "major_outage", "失败占比超过严重异常阈值（规则推断）")
    elif rate >= rules.failure_partial:
        raise_status(snapshot, "partial_outage", "失败占比达到部分异常阈值")


def backlog_rule(snapshot, rules):
    overdue = snapshot.metrics["overdue_jobs"]
    active = snapshot.metrics["active_jobs"]
    if overdue:
        raise_status(snapshot, "degraded", f"{overdue} 个任务超过时限")
    if overdue >= rules.backlog_partial_count and overdue / max(active, 1) * 100 >= rules.backlog_partial_percent:
        raise_status(snapshot, "partial_outage", "超时任务数量和占比均达到部分异常阈值")


def detail_rows(df, selected, failed, overdue, ages, status_field):
    rows = []
    for index, row in df.loc[selected].iterrows():
        def val(name):
            value = row.get(name)
            return "" if value is None or pd.isna(value) else str(value)
        rows.append({
            "record_id": val("record_id"), "系统": val("系统"), "剧名": val("剧名"),
            "机器": val("生产机器"), "语言": val("语言"), "状态": val(status_field),
            "失败类型": val("失败类型"), "备注": val("备注"),
            "问题": " / ".join(label for label, flag in (("失败", failed.loc[index]), ("超时", overdue.loc[index])) if flag),
            "等待或运行小时": round(float(ages.loc[index]), 2) if pd.notna(ages.loc[index]) else None,
        })
    return sorted(rows, key=lambda row: (bool(row["问题"]), row["等待或运行小时"] or 0), reverse=True)


def base_snapshot(component, system, df, active, completed, failed, overdue, ages, status_field):
    relevant = active | completed
    snapshot = Snapshot(component, system, status="operational", reason="")
    snapshot.metrics = {
        "total_jobs": int(relevant.sum()), "active_jobs": int(active.sum()),
        "completed_24h": int(completed.sum()), "failed_jobs": int((failed & relevant).sum()),
        "overdue_jobs": int((overdue & active).sum()),
        "scope": "全部未结束任务 + 最近24小时完成任务",
    }
    values = ages[active].dropna()
    snapshot.metrics.update({
        "avg_wait_hours": round(float(values.mean()), 2) if len(values) else None,
        "p95_wait_hours": round(float(values.quantile(.95)), 2) if len(values) else None,
        "max_wait_hours": round(float(values.max()), 2) if len(values) else None,
    })
    # Counts are complete; persisted drilldown is capped to keep five-minute snapshots bounded.
    snapshot.details = detail_rows(df, active, failed, overdue, ages, status_field)[:200]
    snapshot.metrics["details_total"] = int(active.sum())
    machines = text_column(df, "生产机器")
    snapshot.metrics["machines"] = [
        {"机器": machine, "进行中": int((active & machines.eq(machine)).sum()),
         "失败": int((failed & active & machines.eq(machine)).sum()),
         "超时": int((overdue & active & machines.eq(machine)).sum()),
         "24h完成": int((completed & machines.eq(machine)).sum())}
        for machine in sorted(machines[relevant & machines.ne("")].unique())
    ]
    if not relevant.any():
        snapshot.status = "unknown"
        snapshot.reason = "无进行中或近24小时完成任务；缺少独立心跳，无法确认生产能力"
    return snapshot


def finish(snapshot):
    if not snapshot.reason:
        snapshot.reason = "当前任务未触发异常规则"
    return snapshot


def calculate_recognition_status(df, system, now, rules=Rules()):
    if df.empty:
        return unknown("recognition", system, "数据读取成功，但暂无任务可供判断")
    state = text_column(df, "生产状态")
    end = time_column(df, "识别角色结束时间")
    early = state.isin(["未开始", "合并角色", "合并视频", "抹字幕", "抹字幕中", "识别字幕", "识别角色"])
    failed = state.eq("失败") | (state.isin(["未开始", "失败处理中"]) & text_column(df, "失败类型").ne(""))
    active = (early | failed | state.eq("失败处理中")) & end.isna()
    failed = failed & active
    # Already recognized / BGM jobs do not contaminate recognition failure or backlog.
    completed = recent(end, now)
    ages = age_hours(now, time_column(df, "入表时间"))
    overdue = active & (((early | failed) & (ages > rules.recognition_early_hours)) | (ages > rules.production_overdue_hours))
    result = base_snapshot("recognition", system, df, active, completed, failed, overdue, ages, "生产状态")
    fail_rule(result, rules)
    backlog_rule(result, rules)
    missing = int((active & ages.isna()).sum())
    if missing:
        result.data_complete = False
        raise_status(result, "degraded", f"{missing} 个任务缺少有效入表时间，超时判断不完整")
    result.metrics["scope"] += "；识别排队/运行超时从入表时间计算；机器数量非在线数量"
    return finish(result)


def calculate_bgm_status(df, system, now, rules=Rules()):
    if df.empty:
        return unknown("bgm", system, "数据读取成功，但暂无 BGM 任务")
    state = text_column(df, "生产状态")
    ready = time_column(df, "识别角色结束时间")
    start = time_column(df, "BGM开始处理时间")
    end = time_column(df, "处理BGM结束时间")
    failed = state.eq("处理BGM失败") | text_column(df, "BGM处理情况").str.contains("失败", regex=False)
    active = end.isna() & (state.isin(["识别完成", "处理BGM", "处理BGM失败"]) | failed)
    failed = failed & active
    completed = recent(end, now)
    # Waiting and processing are different clocks.
    waiting = active & start.isna()
    waits = age_hours(now, ready).where(waiting)
    running = age_hours(now, start).where(active & start.notna())
    ages = waits.combine_first(running)
    overdue = active & ((waits > rules.bgm_p95_degraded) | (running > rules.bgm_running_overdue))
    result = base_snapshot("bgm", system, df, active, completed, failed, overdue, ages, "生产状态")
    values = waits.dropna()
    result.metrics.update({
        "avg_wait_hours": round(float(values.mean()), 2) if len(values) else None,
        "p95_wait_hours": round(float(values.quantile(.95)), 2) if len(values) else None,
        "max_wait_hours": round(float(values.max()), 2) if len(values) else None,
        "waiting_jobs": int(waiting.sum()), "running_jobs": int((active & start.notna()).sum()),
    })
    fail_rule(result, rules)
    backlog_rule(result, rules)
    if len(values):
        avg = float(values.mean())
        if avg > rules.bgm_wait_major:
            raise_status(result, "major_outage", f"BGM 平均排队超过 {rules.bgm_wait_major:g} 小时阈值（规则推断）")
        elif avg >= rules.bgm_wait_partial:
            raise_status(result, "partial_outage", "BGM 平均排队达到部分异常阈值")
        elif avg >= rules.bgm_wait_degraded:
            raise_status(result, "degraded", "BGM 平均排队达到性能下降阈值")
        if values.quantile(.95) >= rules.bgm_p95_degraded or values.max() >= rules.bgm_max_degraded:
            raise_status(result, "degraded", "BGM P95 或最长排队超过阈值")
    if (active & ages.isna()).any():
        result.data_complete = False
        raise_status(result, "degraded", "部分 BGM 任务缺少有效时间，超时判断不完整")
    return finish(result)


def calculate_dubbing_status(df, system, now, rules=Rules()):
    if df.empty:
        return unknown("dubbing", system, "数据读取成功，但暂无配音任务")
    state = text_column(df, "当前状态")
    done = state.isin(["已完成", "待检查者确认"])
    failed = state.eq("失败")
    active = state.isin(["未开始", "制作中", "配音中", "失败"])
    request = time_column(df, "需求提交时间")
    prepared = time_column(df, "整备完成时间")
    ready = pd.concat([request, prepared], axis=1).max(axis=1).where(request.notna() & prepared.notna())
    ages = age_hours(now, ready)
    completed = done & recent(time_column(df, "制作完成时间"), now)
    overdue = active & (ages > rules.dubbing_overdue_hours)
    result = base_snapshot("dubbing", system, df, active, completed, failed, overdue, ages, "当前状态")
    result.metrics["waiting_upstream"] = int((active & prepared.isna()).sum())
    result.metrics["scope"] += "；超时从 max(需求提交时间, 整备完成时间) 计算，未整备记录为等待上游"
    fail_rule(result, rules)
    backlog_rule(result, rules)
    if (active & prepared.notna() & ages.isna()).any():
        result.data_complete = False
        raise_status(result, "degraded", "部分已整备任务缺少有效需求时间，超时判断不完整")
    if active.any() and not (active & ready.notna()).any() and not completed.any() and not failed.any():
        result.status = "unknown"
        result.reason = "任务等待上游整备，尚无可执行任务和完成记录可判断配音能力"
    return finish(result)


def calculate_upload_status(df, system, coordinator, now, rules=Rules()):
    if not coordinator.get("supported"):
        return unknown("upload", system, coordinator.get("error") or "协调服务状态不可用")
    online = coordinator["online_count"]
    if df.empty:
        result = Snapshot("upload", system, "operational" if online else "unknown",
                          "协调服务有在线执行端，当前无上传任务" if online else "无任务且无在线执行端，生产能力未知")
        result.metrics = {"online_clients": online, "scope": "共享上传执行端数量，并非本系统专属机器数"}
        return result
    manual = df.get("运营手动上传", pd.Series(False, index=df.index)).fillna(False).astype(bool)
    df = df.loc[~manual].copy()
    state = text_column(df, "上传状态")
    created = time_column(df, "入表时间")
    done = state.isin(["上传成功", "仅视频上传成功"])
    failed = state.eq("上传失败")
    active = ~done & ~state.isin(["取消", "已取消"])
    cohort = recent(created, now)
    ages = age_hours(now, created)
    overdue = active & (ages > rules.upload_overdue_hours)
    result = base_snapshot("upload", system, df, active, done & cohort, failed, overdue, ages, "上传状态")
    # Rate is for terminal results in the creation cohort, not historical current failures.
    result.metrics.update({"online_clients": online,
                           "total_jobs": int(((done | failed) & cohort).sum()),
                           "failed_jobs": int((failed & cohort).sum()),
                           "unresolved_failures": int(failed.sum()),
                           "scope": "失败率=近24小时入表且已有结果的自动上传任务；在线数为共享执行端数"})
    fail_rule(result, rules, upload=True)
    if (failed & ~cohort).any():
        raise_status(result, "degraded", f"仍有 {int((failed & ~cohort).sum())} 个历史上传失败任务未解决")
    backlog_rule(result, rules)
    if online == 0 and active.any():
        raise_status(result, "major_outage", "存在未完成上传任务，但协调服务报告在线执行端为0")
    elif online > 0 and result.status == "unknown":
        result.status, result.reason = "operational", "协调服务有在线执行端，当前无异常任务"
    if (active & ages.isna()).any():
        result.data_complete = False
        raise_status(result, "degraded", "部分上传任务缺少有效入表时间，超时判断不完整")
    return finish(result)


def aggregate(component, samples):
    if not samples:
        return unknown(component, "all", "未配置数据来源")
    worst = max(samples, key=lambda item: SEVERITY[item.status])
    missing = [item.system for item in samples if item.status == "unknown" or not item.data_complete]
    status = worst.status
    if missing and SEVERITY[status] <= 0:
        status = "unknown"
    reason = "；".join(f"{item.system}：{item.reason}" for item in samples if item.status != "operational")
    result = Snapshot(component, "all", status, reason or "所有业务系统均未触发异常规则", not missing)
    result.metrics = {"systems_total": len(samples), "systems_observed": len(samples) - len(missing),
                      "scope": "各业务系统按最严重状态汇总；任一系统未知时不计入完整观测 uptime"}
    for key in ["total_jobs", "active_jobs", "completed_24h", "failed_jobs", "overdue_jobs"]:
        result.metrics[key] = sum(item.metrics.get(key, 0) for item in samples)
    result.details = [{"系统": item.system, "状态": item.status, "原因": item.reason} for item in samples]
    return result
