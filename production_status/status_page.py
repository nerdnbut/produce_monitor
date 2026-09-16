"""Standalone read-only Streamlit Production Status page."""
import html
import json
import sqlite3
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

# Also support `streamlit run produce_monitor/production_status/status_page.py`.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from produce_monitor.production_status.config import (
    COMPONENTS, SEVERITY, STATUS_COLORS, STATUS_LABELS, local_now,
)
from produce_monitor.production_status.status_store import StatusStore, summarize_buckets


def percent(value):
    return "—" if value is None else f"{value:.2f}%"


def color_bar(periods):
    bars = []
    for label, stats in periods:
        title = (f"{label} · {STATUS_LABELS[stats['status']]} · "
                 f"Availability {percent(stats['availability'])} · "
                 f"Healthy {percent(stats['healthy'])} · 覆盖率 {percent(stats['coverage'])} · "
                 f"未知 {stats['unknown']} 个5分钟采样桶")
        color = STATUS_COLORS[stats["status"]]
        # Hatching keeps missing coverage visible even when an observed issue is worse.
        background = (f"repeating-linear-gradient(135deg,{color},{color} 3px,#ffffff99 3px,#ffffff99 5px)"
                      if stats["unknown"] and stats["status"] != "unknown" else color)
        bars.append(f'<span class="ps-tick" tabindex="0" role="img" '
                    f'aria-label="{html.escape(title, quote=True)}" title="{html.escape(title, quote=True)}" '
                    f'style="background:{background}"></span>')
    return '<div class="ps-bars">' + "".join(bars) + '</div>'


def component_card(label, current, stats, periods):
    status = current.get("status", "unknown")
    text = STATUS_LABELS[status]
    reason = html.escape(current.get("reason", "尚无采集记录"))
    return (
        '<section class="ps-card"><div class="ps-heading">'
        f'<strong>{html.escape(label)}</strong><span style="color:{STATUS_COLORS[status]}">{text}</span></div>'
        f'{color_bar(periods)}<div class="ps-foot">'
        f'<span>Availability <b>{percent(stats["availability"])}</b></span>'
        f'<span>Healthy Time <b>{percent(stats["healthy"])}</b></span>'
        f'<span>监控覆盖率 <b>{percent(stats["coverage"])}</b></span></div>'
        f'<div class="ps-reason">{reason}</div></section>'
    )


def render_status_page():
    st.header("Production Status")
    st.caption("每5分钟采样 · 北京时间 · 独立于生产周期筛选 · Availability 为规则推断的生产健康可用率")
    st.markdown("""<style>
        .ps-card {border:1px solid #dfe4ea;border-radius:10px;padding:18px;margin:10px 0;color:inherit;}
        .ps-heading {display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;font-size:16px;}
        .ps-bars {display:flex;gap:2px;width:100%;height:26px;margin:16px 0 12px;}
        .ps-tick {flex:1 1 0;min-width:0;border-radius:2px;display:block;}
        .ps-tick:hover,.ps-tick:focus {outline:2px solid #6b7280;outline-offset:2px;}
        .ps-foot {display:flex;flex-wrap:wrap;gap:8px 22px;font-size:12px;color:#798391;}
        .ps-reason {font-size:12px;margin-top:9px;line-height:1.6;overflow-wrap:anywhere;}
        @media (max-width:600px) {.ps-card {padding:12px;} .ps-bars {gap:1px;} .ps-heading {font-size:14px;}}
    </style>""", unsafe_allow_html=True)
    store = StatusStore()
    if not Path(store.path).exists():
        st.info("尚未生成状态历史。启动独立采集器后，这里会自动显示观测结果。")
        st.code("python -m produce_monitor.production_status.collector", language="bash")
        st.caption(f"数据库：{store.path}")
        return
    try:
        _render_data(store)
    except (sqlite3.Error, OSError) as exc:
        st.error(f"状态数据库暂不可读（{type(exc).__name__}），当前生产状态未知。")


def _render_data(store):
    now = local_now()
    controls = st.columns([2, 1, 1])
    with controls[0]:
        system = st.selectbox("业务系统", ["all"] + store.systems(),
                              format_func=lambda value: "全部系统" if value == "all" else value,
                              key="ps_system")
    with controls[1]:
        days = st.selectbox("历史范围", [90, 30, 7], format_func=lambda value: f"最近 {value} 天", key="ps_days")
    with controls[2]:
        if st.button("刷新状态", key="ps_refresh"):
            st.rerun()
    today = datetime.combine(now.date(), time.min)
    start = today - timedelta(days=days - 1)
    end = today + timedelta(days=1)
    latest = store.latest(system, now)
    history = store.history(system, start, end)
    run = store.last_run()
    if run:
        st.caption(f"最近采集完成：{run['finished_at'].replace('T', ' ')} · 耗时 {run['duration_seconds']:.1f} 秒")
        if (now - datetime.fromisoformat(run["finished_at"])).total_seconds() > 900:
            st.warning("采集器已超过15分钟没有更新。历史保留，当前状态已标为未知。")
    else:
        st.info("数据库已准备好，等待第一次采集。")
    current_states = [latest.get(component, {}).get("status", "unknown") for component in COMPONENTS]
    abnormal = sum(SEVERITY[state] > 0 for state in current_states)
    missing = sum(state == "unknown" or not latest.get(component, {}).get("data_complete", False)
                  for component, state in zip(COMPONENTS, current_states))
    if abnormal:
        st.warning(f"{abnormal} 个生产组件存在异常" + (f" · {missing} 个组件观测不完整" if missing else ""))
    elif missing:
        st.info(f"{missing} 个组件状态未知或观测不完整")
    else:
        st.success("All Systems Operational · 所有生产组件正常")
    st.caption("绿色 正常 · 黄色 性能下降 · 橙色 部分异常 · 红色 严重异常 · 灰色 未知；斜纹表示当天同时存在观测缺口。")
    by_component = {component: [row for row in history if row["component"] == component] for component in COMPONENTS}
    for component, label in COMPONENTS.items():
        rows = by_component[component]
        # Bucket once by day rather than rescanning 90 days for each daily stripe.
        day_rows = {}
        for row in rows:
            day_rows.setdefault(row["bucket"][:10], []).append(row)
        periods = []
        for offset in range(days):
            date = start + timedelta(days=offset)
            stats = summarize_buckets(day_rows.get(date.date().isoformat(), []), date, date + timedelta(days=1), now)
            periods.append((date.strftime("%Y-%m-%d"), stats))
        totals = summarize_buckets(rows, start, end, now)
        current = latest.get(component, {})
        st.markdown(component_card(label, current, totals, periods), unsafe_allow_html=True)
        with st.expander(f"查看 {label} 当前详情"):
            _render_current_details(current)
            if system == "all":
                st.caption("在顶部选择具体业务系统，可查看对应任务和机器。未知来源会影响整体覆盖率。")
    st.caption(f"{start:%Y-%m-%d} → 今天 · 每格一天；悬停查看每日状态。历史从实际采集开始，缺失记录保持未知。")

    st.subheader("每日 / 每小时详情")
    first, second = st.columns(2)
    with first:
        selected_component = st.selectbox("生产组件", list(COMPONENTS), format_func=COMPONENTS.get, key="ps_component")
    with second:
        saved_day = st.session_state.get("ps_date")
        if saved_day is not None:
            st.session_state["ps_date"] = min(now.date(), max(start.date(), saved_day))
        selected_day = st.date_input("日期", now.date(), min_value=start.date(), max_value=now.date(), key="ps_date")
    day_start = datetime.combine(selected_day, time.min)
    day_history = [row for row in by_component[selected_component] if row["bucket"][:10] == selected_day.isoformat()]
    hours = [(f"{hour:02d}:00", summarize_buckets(day_history, day_start + timedelta(hours=hour),
              day_start + timedelta(hours=hour + 1), now)) for hour in range(24)]
    st.markdown(color_bar(hours), unsafe_allow_html=True)
    st.caption("00:00 → 23:00 · 每格一小时，未到的小时显示为未知且不计入覆盖率分母。")
    max_hour = now.hour if selected_day == now.date() else 23
    hour = st.selectbox("查看小时", list(range(max_hour + 1)), format_func=lambda value: f"{value:02d}:00–{value:02d}:59", key="ps_hour")
    hour_start = day_start + timedelta(hours=hour)
    details = store.hour_details(system, selected_component, hour_start)
    if not details:
        st.info("这一小时尚无观测记录，无法判断生产是否正常。")
    else:
        display = []
        for row in details:
            metrics = json.loads(row["metrics_json"])
            display.append({"采样时间": row["observed_at"].replace("T", " "),
                            "状态": STATUS_LABELS[row["status"]], "数据完整": bool(row["data_complete"]),
                            "未完成": metrics.get("active_jobs"), "失败": metrics.get("failed_jobs"),
                            "超时": metrics.get("overdue_jobs"), "原因": row["reason"]})
        st.dataframe(pd.DataFrame(display), hide_index=True, use_container_width=True)
        chosen = st.selectbox("查看该次快照的任务 / 机器详情", range(len(details)),
                              format_func=lambda index: details[index]["observed_at"].replace("T", " "), key="ps_snapshot")
        row = dict(details[chosen])
        row["metrics"] = json.loads(row["metrics_json"])
        row["details"] = json.loads(row["details_json"])
        _render_current_details(row)
        st.download_button("下载本小时快照 CSV", pd.DataFrame(display).to_csv(index=False).encode("utf-8-sig"),
                           f"status_{selected_component}_{selected_day}_{hour:02d}.csv", "text/csv", key="ps_download")

    st.subheader("Recent Incidents · 异常历史")
    incidents = store.incidents(system, start, end)
    if not incidents:
        st.caption("所选范围尚未记录异常事件；没有记录不代表未观测时段正常。")
    for incident in incidents:
        last_seen = datetime.fromisoformat(incident["last_seen_at"])
        lost_observation = incident["last_status"] == "unknown" or (now - last_seen).total_seconds() > 900
        status = "已恢复" if incident["resolved_at"] else ("观测中断" if lost_observation else "未恢复")
        label = COMPONENTS.get(incident["component"], incident["component"])
        with st.expander(f"{incident['started_at'].replace('T', ' ')} · {label} · {status}"):
            st.write(incident["description"])
            st.caption(f"最高严重度：{STATUS_LABELS[incident['severity']]} · 恢复观测时间：{incident['resolved_at'] or '尚未观测到恢复'}")
            observed_end = datetime.fromisoformat(incident["resolved_at"]) if incident["resolved_at"] else last_seen
            span_hours = (observed_end - datetime.fromisoformat(incident["started_at"])).total_seconds() / 3600
            st.caption(f"首次至最近/恢复观测跨度：{span_hours:.2f} 小时；若有采集断档，不代表期间持续故障。")
            st.dataframe(pd.DataFrame([{"观测时间": event["observed_at"].replace("T", " "),
                                       "状态": STATUS_LABELS[event["status"]], "说明": event["description"]}
                                      for event in incident["events"]]), hide_index=True, use_container_width=True)
    with st.expander("统计口径与接入范围"):
        st.markdown(
            "Availability =（正常 + 性能下降）÷ 有效采样桶；Healthy Time = 正常 ÷ 有效采样桶。"
            "未知、数据不完整及采集缺口不进入这两个指标的分母，另用监控覆盖率展示。"
            "每桶代表一次5分钟采样，并非精确故障分钟数；日/小时颜色取该时段已观测的最严重状态。\n\n"
            "本版按识别、BGM、配音、自动上传四个组件采样。翻译/语音合成/剪辑等细分步骤尚无独立事件源；"
            "机器明细是任务归属统计，不是机器心跳。没有任务且没有心跳证据时标记未知。"
            "严重异常是生产规则判断，不代表已确认机器或供应商宕机。\n\n"
            "上传失败率按近24小时入表且已有结果的自动任务计算；排除运营手动上传。"
            "上传执行端数量来自共享协调服务。历史恢复时间是首次观测到恢复的时间。"
            "本页不发送飞书消息；现有识别警报通知保持原来的独立机制。"
        )


def _render_current_details(current):
    metrics = current.get("metrics", {})
    labels = {"total_jobs": "统计任务数", "active_jobs": "未完成任务", "failed_jobs": "失败任务",
              "overdue_jobs": "超时任务", "completed_24h": "近24h完成 / 上传为创建批次成功数",
              "failure_rate": "失败占比(%)", "avg_wait_hours": "平均等待/运行(h)",
              "p95_wait_hours": "P95等待/运行(h)", "max_wait_hours": "最长等待/运行(h)",
              "online_clients": "共享上传在线执行端", "waiting_upstream": "等待上游整备"}
    values = [{"指标": label, "值": str(metrics[key])} for key, label in labels.items() if key in metrics and metrics[key] is not None]
    if values:
        st.dataframe(pd.DataFrame(values), hide_index=True, use_container_width=True)
    if metrics.get("scope"):
        st.caption(metrics["scope"])
    if metrics.get("machines"):
        st.caption("机器维度任务统计（未接入心跳，不能据此判定在线/离线）")
        st.dataframe(pd.DataFrame(metrics["machines"]), hide_index=True, use_container_width=True)
    if current.get("details"):
        st.dataframe(pd.DataFrame(current["details"]), hide_index=True, use_container_width=True)
        if metrics.get("details_total", 0) > 200:
            st.caption(f"本快照共有 {metrics['details_total']} 个未结束任务，明细保存超时/失败优先的前200条；指标使用完整数据。")


if __name__ == "__main__":
    st.set_page_config(page_title="Production Status", page_icon="🟢", layout="wide")
    # Refresh only the local status page, not any production sources.
    if hasattr(st, "fragment"):
        st.fragment(run_every="60s")(render_status_page)()
    else:
        render_status_page()
