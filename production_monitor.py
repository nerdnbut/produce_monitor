"""
生产监控面板

功能：
1. 从所有系统的剧识别表拉取数据
2. 展示生产进度大盘
3. 各平台质量对比
4. 实时监控
5. 支持按生产周期筛选（每周五为周期开始）
"""
import sys
import os
import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
from collections import defaultdict

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from youtube_data.lark import LarkBitableClient, Condition
from factory.table_manager import CORE_TABLES

# pyecharts 导入
from pyecharts.charts import Bar, Pie, Funnel, Line, Grid
from pyecharts import options as opts
from pyecharts.commons.utils import JsCode
from pyecharts.components import Table
import streamlit.components.v1 as components

# 系统配置
SYSTEMS = ["众益", "点众", "红果", "掌阅", "外部制作", "ReelShort"]

# 剧识别表需要查询的字段
RECOGNITION_FIELDS = [
    "剧id", "剧名", "生产状态", "整备状态", "生产机器",
    "开始生产时间", "识别角色结束时间", "入表时间", "剧时长(小时)",
    "音画同步检查", "识别字幕方式", "字幕高度",
    "角色识别报告", "角色分布比例", "剧字幕条数",
    "备注", "失败类型", "预计发布日期", "BGM处理情况", "生产周期",
    "BGM开始处理时间", "处理BGM结束时间", "生产总耗时", "NAS位置"
]

# 颜色主题
COLORS = {
    "primary": "#5470c6",
    "success": "#91cc75",
    "warning": "#fac858",
    "danger": "#ee6666",
    "info": "#73c0de",
    "purple": "#9a60b4",
    "cyan": "#3ba272",
    "orange": "#fc8452",
}

STATUS_COLORS = {
    "未开始": "#95a5a6",
    "合并视频": "#3498db",
    "识别字幕": "#9b59b6",
    "识别角色": "#e74c3c",
    "处理BGM": "#f39c12",
    "识别完成": "#27ae60",
    "完成": "#2ecc71",
    "失败": "#c0392b",
    "处理BGM失败": "#e74c3c",
}


def render_pyecharts(chart):
    """渲染 pyecharts 图表到 Streamlit"""
    components.html(chart.render_embed(), height=400, scrolling=False)


@st.cache_data(ttl=300)  # 缓存5分钟
def fetch_recognition_data(system_name: str) -> list:
    """
    从指定系统的剧识别表拉取所有数据
    """
    try:
        config = CORE_TABLES.get(system_name)
        if not config:
            return []

        app_token = config.get("app_token")
        tables = config.get("tables", {})
        recognition_table_id = tables.get("剧识别表")

        if not recognition_table_id:
            return []

        client = LarkBitableClient()

        records = client.search_all_records(
            app_token=app_token,
            table_id=recognition_table_id,
            field_names=RECOGNITION_FIELDS,
            filter_conditions=[]
        )

        for record in records:
            record["系统"] = system_name

        return records

    except Exception as e:
        st.error(f"拉取 [{system_name}] 数据失败: {e}")
        return []


@st.cache_data(ttl=300)
def fetch_all_systems_data() -> pd.DataFrame:
    """
    拉取所有系统的剧识别表数据
    """
    all_records = []

    for system_name in SYSTEMS:
        records = fetch_recognition_data(system_name)
        all_records.extend(records)

    if not all_records:
        return pd.DataFrame()

    rows = []
    for record in all_records:
        fields = record.get("fields", {})
        row = {
            "record_id": record.get("record_id"),
            "系统": record.get("系统"),
            "剧id": fields.get("剧id"),
            "剧名": _extract_text(fields.get("剧名")),
            "生产状态": _extract_text(fields.get("生产状态")),
            "整备状态": _extract_text(fields.get("整备状态")),
            "生产机器": _extract_text(fields.get("生产机器")),
            "开始生产时间": _parse_timestamp(fields.get("开始生产时间")),
            "识别角色结束时间": _parse_timestamp(fields.get("识别角色结束时间")),
            "入表时间": _parse_timestamp(fields.get("入表时间")),
            "剧时长(小时)": fields.get("剧时长(小时)"),
            "音画同步检查": _extract_text(fields.get("音画同步检查")),
            "识别字幕方式": _extract_text(fields.get("识别字幕方式")),
            "字幕高度": fields.get("字幕高度"),
            "角色识别报告": _extract_text(fields.get("角色识别报告")),
            "备注": _extract_text(fields.get("备注")),
            "失败类型": _extract_text(fields.get("失败类型")),
            "BGM处理情况": _extract_text(fields.get("BGM处理情况")),
            "预计发布日期": _parse_timestamp(fields.get("预计发布日期")),
            "生产周期": _extract_text(fields.get("生产周期")),
            "BGM开始处理时间": _parse_timestamp(fields.get("BGM开始处理时间")),
            "处理BGM结束时间": _parse_timestamp(fields.get("处理BGM结束时间")),
            "生产总耗时": fields.get("生产总耗时"),
            "NAS位置": _extract_text(fields.get("NAS位置")),
        }
        rows.append(row)

    df = pd.DataFrame(rows)
    return df


def _extract_text(field_value):
    """提取字段值中的文本"""
    if field_value is None:
        return None
    if isinstance(field_value, dict):
        return field_value.get("text") or field_value.get("value")
    if isinstance(field_value, list) and len(field_value) > 0:
        if isinstance(field_value[0], dict):
            return field_value[0].get("text") or field_value[0].get("value")
        return field_value[0]
    return str(field_value) if field_value else None


def _parse_timestamp(timestamp):
    """解析时间戳"""
    if timestamp is None:
        return None
    try:
        if isinstance(timestamp, (int, float)):
            if timestamp > 10000000000:
                timestamp = timestamp / 1000
            return datetime.fromtimestamp(timestamp)
        return timestamp
    except:
        return None


def create_kpi_card(title, value, delta=None, color="#5470c6"):
    """创建 KPI 卡片样式的 HTML"""
    delta_html = f'<div style="color: {"#91cc75" if delta and delta.startswith("+") else "#ee6666"}; font-size: 14px;">{delta}</div>' if delta else ""
    return f"""
    <div style="
        background: linear-gradient(135deg, {color}22, {color}11);
        border-left: 4px solid {color};
        border-radius: 8px;
        padding: 16px;
        text-align: center;
        box-shadow: 0 2px 8px rgba(0,0,0,0.1);
    ">
        <div style="color: #666; font-size: 14px; margin-bottom: 8px;">{title}</div>
        <div style="color: {color}; font-size: 28px; font-weight: bold;">{value}</div>
        {delta_html}
    </div>
    """


def calculate_avg_times(df: pd.DataFrame) -> dict:
    """
    计算已完成记录的平均耗时
    - 识别角色耗时：识别角色结束时间 - 开始生产时间
    - BGM处理耗时：BGM处理结束时间 - BGM开始处理时间（必须两个值都有）
    - 生产总耗时：识别角色结束时间 - 开始生产时间
    """
    # 已完成的记录
    completed_df = df[df["生产状态"] == "完成"].copy()

    if completed_df.empty:
        return {
            "avg_recognition_time": None,  # 识别角色耗时
            "avg_bgm_time": None,  # BGM处理耗时
            "avg_total_time": None,  # 生产总耗时
        }

    # 计算识别角色耗时（识别角色结束时间 - 开始生产时间）
    completed_df["识别角色耗时"] = completed_df.apply(
        lambda row: (row["识别角色结束时间"] - row["开始生产时间"]).total_seconds() / 3600
        if pd.notna(row["识别角色结束时间"]) and pd.notna(row["开始生产时间"])
        else None,
        axis=1
    )

    # 计算BGM处理耗时（必须两个值都有）
    completed_df["BGM耗时"] = completed_df.apply(
        lambda row: (row["处理BGM结束时间"] - row["BGM开始处理时间"]).total_seconds() / 3600
        if pd.notna(row["处理BGM结束时间"]) and pd.notna(row["BGM开始处理时间"])
        else None,
        axis=1
    )

    # 计算生产总耗时（识别角色结束时间 - 开始生产时间）
    completed_df["生产总耗时"] = completed_df["识别角色耗时"]  # 使用同样的计算方式

    return {
        "avg_recognition_time": completed_df["识别角色耗时"].dropna().mean(),
        "avg_bgm_time": completed_df["BGM耗时"].dropna().mean(),
        "avg_total_time": completed_df["生产总耗时"].dropna().mean(),
    }


def calculate_avg_production_time(df: pd.DataFrame) -> dict:
    """
    计算符合条件的记录的生产总耗时平均值
    筛选条件：
    - 生产状态 = 完成
    - 开始生产时间、识别角色结束时间、BGM开始处理时间、处理BGM结束时间 四个字段都不为空
    """
    # 已完成的记录
    completed_df = df[df["生产状态"] == "完成"].copy()

    if completed_df.empty:
        return {
            "avg_production_time": None,
            "valid_count": 0,
        }

    # 筛选四个时间字段都不为空的记录
    valid_df = completed_df[
        completed_df["开始生产时间"].notna() &
        completed_df["识别角色结束时间"].notna() &
        completed_df["BGM开始处理时间"].notna() &
        completed_df["处理BGM结束时间"].notna()
    ].copy()

    if valid_df.empty:
        return {
            "avg_production_time": None,
            "valid_count": 0,
        }

    # 获取生产总耗时字段值（从飞书表中的字段）
    production_times = valid_df["生产总耗时"].dropna()

    if production_times.empty:
        return {
            "avg_production_time": None,
            "valid_count": len(valid_df),
        }

    # 计算平均值
    avg_time = production_times.mean()

    return {
        "avg_production_time": avg_time,
        "valid_count": len(valid_df),
    }


def calculate_cycle_production_time(df: pd.DataFrame) -> pd.DataFrame:
    """
    计算每个生产周期的平均生产总耗时
    筛选条件同上
    """
    # 已完成的记录
    completed_df = df[df["生产状态"] == "完成"].copy()

    if completed_df.empty:
        return pd.DataFrame()

    # 筛选四个时间字段都不为空的记录
    valid_df = completed_df[
        completed_df["开始生产时间"].notna() &
        completed_df["识别角色结束时间"].notna() &
        completed_df["BGM开始处理时间"].notna() &
        completed_df["处理BGM结束时间"].notna() &
        completed_df["生产周期"].notna()
    ].copy()

    if valid_df.empty:
        return pd.DataFrame()

    # 按生产周期分组计算平均值
    cycle_stats = valid_df.groupby("生产周期").agg(
        平均生产总耗时=("生产总耗时", "mean"),
        记录数=("生产总耗时", "count")
    ).reset_index()

    # 过滤掉没有生产总耗时数据的周期
    cycle_stats = cycle_stats[cycle_stats["平均生产总耗时"].notna()]

    # 按周期排序
    cycle_stats = cycle_stats.sort_values("生产周期")

    return cycle_stats


def estimate_completion_time(row: pd.Series, avg_times: dict) -> str:
    """
    根据生产状态预估完成时长
    """
    status = row["生产状态"]

    if status == "未开始":
        # 未开始：预估完成时间 = 生产总耗时的平均值
        if avg_times["avg_total_time"]:
            return f"{avg_times['avg_total_time']:.1f}h"
        return "暂无数据"

    elif status in ["合并视频", "识别字幕", "识别角色"]:
        # 识别中：预估 = 识别角色耗时的平均值
        if avg_times["avg_recognition_time"]:
            return f"{avg_times['avg_recognition_time']:.1f}h"
        return "暂无数据"

    elif status in ["处理BGM", "识别完成"]:
        # 处理BGM或识别完成：预估 = BGM处理耗时
        if avg_times["avg_bgm_time"]:
            return f"{avg_times['avg_bgm_time']:.1f}h"
        return "暂无数据"

    elif status in ["处理BGM失败", "失败", "失败处理中"]:
        return "等待处理"

    return "未知状态"


def render_overview_tab(df: pd.DataFrame):
    """渲染概览标签页（生产进度大盘 + 失败分析）"""
    st.header("📊 生产进度大盘")

    if df.empty:
        st.warning("暂无数据")
        return

    # ===== 第一行：四大模块 KPI 卡片 =====
    col1, col2, col3, col4 = st.columns(4)

    total_count = len(df)
    # 已完成：生产状态=完成
    completed_count = len(df[df["生产状态"] == "完成"])

    # 未完成细分：识别完成、处理BGM、未开始
    not_started_count = len(df[df["生产状态"] == "未开始"])
    recognition_done_count = len(df[df["生产状态"] == "识别完成"])
    processing_bgm_count = len(df[df["生产状态"] == "处理BGM"])
    not_completed_count = not_started_count + recognition_done_count + processing_bgm_count

    # 失败细分：识别失败、BGM失败、其他
    recognize_fail_count = len(df[df["生产状态"] == "失败"])
    bgm_fail_count = len(df[df["生产状态"] == "处理BGM失败"])
    # 其他失败状态
    fail_statuses = ["失败", "处理BGM失败", "失败处理中"]
    other_fail_count = len(df[df["生产状态"].isin(fail_statuses)]) - recognize_fail_count - bgm_fail_count
    total_fail_count = recognize_fail_count + bgm_fail_count + max(0, other_fail_count)

    with col1:
        st.markdown(create_kpi_card("总剧数", total_count, color="#5470c6"), unsafe_allow_html=True)
    with col2:
        st.markdown(create_kpi_card("已完成", completed_count, color="#91cc75"), unsafe_allow_html=True)
    with col3:
        st.markdown(create_kpi_card("未完成", not_completed_count, color="#f39c12"), unsafe_allow_html=True)
    with col4:
        st.markdown(create_kpi_card("失败", total_fail_count, color="#ee6666"), unsafe_allow_html=True)

    st.markdown("---")

    # ===== 未完成细分 =====
    st.subheader("📋 未完成明细")
    col_left, col_center, col_right = st.columns(3)
    with col_left:
        st.markdown(create_kpi_card("识别完成", recognition_done_count, color="#27ae60"), unsafe_allow_html=True)
    with col_center:
        st.markdown(create_kpi_card("处理BGM", processing_bgm_count, color="#f39c12"), unsafe_allow_html=True)
    with col_right:
        st.markdown(create_kpi_card("未开始", not_started_count, color="#95a5a6"), unsafe_allow_html=True)

    st.markdown("---")

    # ===== 失败细分 =====
    st.subheader("❌ 失败明细")
    col_left, col_center, col_right = st.columns(3)
    with col_left:
        st.markdown(create_kpi_card("识别失败", recognize_fail_count, color="#c0392b"), unsafe_allow_html=True)
    with col_center:
        st.markdown(create_kpi_card("BGM失败", bgm_fail_count, color="#e74c3c"), unsafe_allow_html=True)
    with col_right:
        st.markdown(create_kpi_card("其他失败", max(0, other_fail_count), color="#9a60b4"), unsafe_allow_html=True)

    # 如果有其他失败状态，显示警告
    if other_fail_count > 0:
        other_statuses = df[df["生产状态"].isin(["失败", "处理BGM失败"])] == False
        unique_other_statuses = df[~df["生产状态"].isin(["完成", "识别完成", "合并视频", "识别字幕", "识别角色", "处理BGM", "未开始", "失败", "处理BGM失败", "失败处理中"])]["生产状态"].unique().tolist()
        if unique_other_statuses:
            st.caption(f"⚠️ 其他失败状态: {unique_other_statuses}")

    st.markdown("---")

    # ===== 生产总耗时指标 =====
    st.subheader("⏱️ 生产总耗时分析")

    # 计算平均生产总耗时
    prod_time_stats = calculate_avg_production_time(df)
    cycle_time_stats = calculate_cycle_production_time(df)

    col_left, col_right = st.columns(2)

    with col_left:
        # 显示平均生产总耗时卡片
        if prod_time_stats["avg_production_time"] is not None:
            avg_time_str = f"{prod_time_stats['avg_production_time']:.1f}h"
            st.markdown(
                create_kpi_card(
                    "平均生产总耗时",
                    avg_time_str,
                    color="#9a60b4"
                ),
                unsafe_allow_html=True
            )
            st.caption(f"📊 基于 {prod_time_stats['valid_count']} 条完整记录统计")
            st.caption("📋 筛选条件：生产状态=完成，四个时间字段均不为空")
        else:
            st.markdown(
                create_kpi_card("平均生产总耗时", "暂无数据", color="#9a60b4"),
                unsafe_allow_html=True
            )
            st.caption("📋 需要有完成的记录且四个时间字段均不为空")

    with col_right:
        # 显示各周期生产总耗时折线图
        if not cycle_time_stats.empty:
            cycles = cycle_time_stats["生产周期"].tolist()
            avg_times = cycle_time_stats["平均生产总耗时"].tolist()
            counts = cycle_time_stats["记录数"].tolist()

            line = (
                Line(init_opts=opts.InitOpts(width="100%", height="300px", theme="light"))
                .add_xaxis(cycles)
                .add_yaxis(
                    series_name="平均生产总耗时",
                    y_axis=[round(t, 1) for t in avg_times],
                    symbol="circle",
                    symbol_size=8,
                    linestyle_opts=opts.LineStyleOpts(width=2, color="#9a60b4"),
                    itemstyle_opts=opts.ItemStyleOpts(color="#9a60b4"),
                    label_opts=opts.LabelOpts(is_show=True, position="top", formatter="{c}h"),
                )
                .set_global_opts(
                    title_opts=opts.TitleOpts(title="各生产周期生产总耗时变化"),
                    xaxis_opts=opts.AxisOpts(axislabel_opts=opts.LabelOpts(rotate=30)),
                    yaxis_opts=opts.AxisOpts(name="小时"),
                    tooltip_opts=opts.TooltipOpts(
                        trigger="axis",
                        formatter=JsCode("""
                            function(params) {
                                var cycle = params[0].axisValue;
                                var time = params[0].value;
                                var idx = params[0].dataIndex;
                                var count = """ + str(counts) + """[idx];
                                return '周期: ' + cycle + '<br/>平均耗时: ' + time + 'h<br/>记录数: ' + count;
                            }
                        """)
                    ),
                    legend_opts=opts.LegendOpts(pos_top="top"),
                )
            )
            render_pyecharts(line)
        else:
            st.info("暂无各周期生产总耗时数据")

    st.markdown("---")

    # ===== 第二行：生产漏斗 =====
    st.subheader("📈 生产漏斗")

    status_order = ["未开始", "合并视频", "识别字幕", "识别角色", "处理BGM", "识别完成", "完成", "失败", "处理BGM失败"]
    status_counts = df["生产状态"].value_counts().reindex(status_order, fill_value=0)

    data_pair = [(status, int(count)) for status, count in status_counts.items() if count > 0]

    funnel = (
        Funnel(init_opts=opts.InitOpts(width="100%", height="400px", theme="light"))
        .add(
            series_name="剧目数",
            data_pair=data_pair,
            gap=2,
            label_opts=opts.LabelOpts(position="inside", formatter="{b}: {c}"),
            itemstyle_opts=opts.ItemStyleOpts(
                color=JsCode("""
                function(params) {
                    var colors = ['#95a5a6', '#3498db', '#9b59b6', '#e74c3c', '#f39c12', '#27ae60', '#2ecc71', '#c0392b', '#e74c3c'];
                    return colors[params.dataIndex % colors.length];
                }
                """)
            ),
        )
        .set_global_opts(
            title_opts=opts.TitleOpts(title=""),
            legend_opts=opts.LegendOpts(pos_top="bottom"),
        )
    )
    render_pyecharts(funnel)

    st.markdown("---")

    # ===== 第三行：各系统剧目数量（堆叠条形图） =====
    st.subheader("🏢 各系统剧目数量")

    # 按系统分组计算总数和已完成数
    system_stats = df.groupby("系统").agg(
        总剧目数=("剧id", "count"),
        已完成=("生产状态", lambda x: (x == "完成").sum())
    ).reset_index()

    systems = system_stats["系统"].tolist()
    total_counts = system_stats["总剧目数"].tolist()
    completed_counts = system_stats["已完成"].tolist()

    bar = (
        Bar(init_opts=opts.InitOpts(width="100%", height="400px", theme="light"))
        .add_xaxis(systems)
        .add_yaxis(
            series_name="总剧目数",
            y_axis=total_counts,
            z=1,
            bar_width="40%",
            label_opts=opts.LabelOpts(is_show=True, position="top"),
            itemstyle_opts=opts.ItemStyleOpts(color="#d3dce6"),
        )
        .add_yaxis(
            series_name="已完成",
            y_axis=completed_counts,
            z=2,
            bar_width="40%",
            label_opts=opts.LabelOpts(is_show=True, position="top"),
            itemstyle_opts=opts.ItemStyleOpts(color="#91cc75"),
        )
        .set_global_opts(
            title_opts=opts.TitleOpts(title=""),
            xaxis_opts=opts.AxisOpts(axislabel_opts=opts.LabelOpts(rotate=0)),
            yaxis_opts=opts.AxisOpts(name="剧目数"),
            tooltip_opts=opts.TooltipOpts(trigger="axis", axis_pointer_type="shadow"),
            legend_opts=opts.LegendOpts(pos_top="top"),
        )
    )
    render_pyecharts(bar)


def render_system_comparison_tab(df: pd.DataFrame):
    """渲染系统对比标签页"""
    st.header("🏢 各平台质量对比")

    if df.empty:
        st.warning("暂无数据")
        return

    # ===== 各系统状态分布堆叠柱状图 =====
    col_left, col_right = st.columns(2)

    with col_left:
        st.subheader("📊 各系统状态分布")

        # 准备数据
        status_pivot = pd.crosstab(df["系统"], df["生产状态"])
        systems = status_pivot.index.tolist()

        bar = Bar(init_opts=opts.InitOpts(width="100%", height="450px", theme="light"))
        bar.add_xaxis(systems)

        status_order = ["未开始", "合并视频", "识别字幕", "识别角色", "处理BGM", "识别完成", "完成", "失败", "处理BGM失败"]
        for status in status_order:
            if status in status_pivot.columns:
                bar.add_yaxis(
                    series_name=status,
                    y_axis=status_pivot[status].tolist(),
                    stack="总量",
                    label_opts=opts.LabelOpts(is_show=False),
                    itemstyle_opts=opts.ItemStyleOpts(color=STATUS_COLORS.get(status, "#999")),
                )

        bar.set_global_opts(
            title_opts=opts.TitleOpts(title=""),
            xaxis_opts=opts.AxisOpts(axislabel_opts=opts.LabelOpts(rotate=0)),
            yaxis_opts=opts.AxisOpts(name="剧目数"),
            tooltip_opts=opts.TooltipOpts(trigger="axis", axis_pointer_type="shadow"),
            legend_opts=opts.LegendOpts(pos_top="top"),
        )
        render_pyecharts(bar)

    with col_right:
        st.subheader("📈 各系统完成率")

        completion_rate = df.groupby("系统").apply(
            lambda x: (x["生产状态"].isin(["识别完成", "完成"]).sum() / len(x) * 100) if len(x) > 0 else 0
        ).round(1)

        systems = completion_rate.index.tolist()
        rates = completion_rate.values.tolist()

        bar = (
            Bar(init_opts=opts.InitOpts(width="100%", height="450px", theme="light"))
            .add_xaxis(systems)
            .add_yaxis(
                series_name="完成率",
                y_axis=rates,
                label_opts=opts.LabelOpts(position="top", formatter="{c}%"),
                itemstyle_opts=opts.ItemStyleOpts(
                    color=JsCode("""
                    function(params) {
                        var value = params.value;
                        if (value >= 80) return '#91cc75';
                        if (value >= 50) return '#fac858';
                        return '#ee6666';
                    }
                    """)
                ),
            )
            .set_global_opts(
                title_opts=opts.TitleOpts(title=""),
                xaxis_opts=opts.AxisOpts(axislabel_opts=opts.LabelOpts(rotate=0)),
                yaxis_opts=opts.AxisOpts(name="完成率 (%)", max_=100),
                tooltip_opts=opts.TooltipOpts(trigger="axis", axis_pointer_type="shadow"),
            )
        )
        render_pyecharts(bar)


def render_realtime_monitor_tab(df: pd.DataFrame):
    """渲染实时监控标签页"""
    st.header("⏱️ 实时监控")

    if df.empty:
        st.warning("暂无数据")
        return

    # 计算平均耗时用于预估
    avg_times = calculate_avg_times(df)

    # 显示平均耗时参考
    avg_rec = f"{avg_times['avg_recognition_time']:.1f}h" if avg_times['avg_recognition_time'] else "暂无"
    avg_bgm = f"{avg_times['avg_bgm_time']:.1f}h" if avg_times['avg_bgm_time'] else "暂无"
    avg_total = f"{avg_times['avg_total_time']:.1f}h" if avg_times['avg_total_time'] else "暂无"
    st.caption(f"📌 平均耗时参考 - 识别角色: {avg_rec} | BGM处理: {avg_bgm} | 生产总耗时: {avg_total}")

    # 筛选需要显示的记录：未开始、识别中、处理BGM中、识别完成
    active_statuses = ["未开始", "合并视频", "识别字幕", "识别角色", "处理BGM", "识别完成"]
    active_df = df[df["生产状态"].isin(active_statuses)].copy()

    if active_df.empty:
        st.info("当前没有正在处理的剧目")
        return

    # 剧名搜索
    search_name = st.text_input("🔍 搜索剧名", placeholder="输入剧名关键字", key="realtime_search")

    if search_name:
        active_df = active_df[active_df["剧名"].str.contains(search_name, na=False, case=False)]

    # 系统筛选
    all_systems = ["全部"] + sorted(df["系统"].unique().tolist())
    selected_system = st.selectbox("选择系统", options=all_systems, key="realtime_system")

    if selected_system != "全部":
        active_df = active_df[active_df["系统"] == selected_system]

    if active_df.empty:
        st.info("没有符合条件的记录")
        return

    # 计算已处理时长和预估完成时长
    now = datetime.now()
    active_df = active_df.copy()

    active_df["已处理时长"] = active_df["开始生产时间"].apply(
        lambda x: (now - x).total_seconds() / 3600 if pd.notna(x) else 0
    )

    active_df["已处理时长"] = active_df["已处理时长"].apply(
        lambda x: f"{x:.1f}h" if x > 0 else "-"
    )

    active_df["预估完成时长"] = active_df.apply(
        lambda row: estimate_completion_time(row, avg_times), axis=1
    )

    # 按状态分组显示
    st.markdown("---")

    # 未开始记录
    not_started_df = active_df[active_df["生产状态"] == "未开始"]
    if not not_started_df.empty:
        st.subheader(f"📋 未开始 ({len(not_started_df)})")
        display_cols = ["系统", "剧名", "生产状态", "生产机器", "已处理时长", "预估完成时长"]
        available_cols = [col for col in display_cols if col in not_started_df.columns]
        st.dataframe(
            not_started_df[available_cols].sort_values("系统"),
            use_container_width=True,
            hide_index=True
        )
        st.markdown("---")

    # 识别中记录（合并视频、识别字幕、识别角色）
    recognizing_df = active_df[active_df["生产状态"].isin(["合并视频", "识别字幕", "识别角色"])]
    if not recognizing_df.empty:
        st.subheader(f"🔄 识别中 ({len(recognizing_df)})")
        display_cols = ["系统", "剧名", "生产状态", "生产机器", "已处理时长", "预估完成时长"]
        available_cols = [col for col in display_cols if col in recognizing_df.columns]
        st.dataframe(
            recognizing_df[available_cols].sort_values("系统"),
            use_container_width=True,
            hide_index=True
        )
        st.markdown("---")

    # 处理BGM中记录
    processing_bgm_df = active_df[active_df["生产状态"] == "处理BGM"]
    if not processing_bgm_df.empty:
        st.subheader(f"🎵 处理BGM中 ({len(processing_bgm_df)})")
        display_cols = ["系统", "剧名", "生产状态", "生产机器", "已处理时长", "预估完成时长"]
        available_cols = [col for col in display_cols if col in processing_bgm_df.columns]
        st.dataframe(
            processing_bgm_df[available_cols].sort_values("系统"),
            use_container_width=True,
            hide_index=True
        )
        st.markdown("---")

    # 识别完成记录（待处理BGM）
    recognition_done_df = active_df[active_df["生产状态"] == "识别完成"]
    if not recognition_done_df.empty:
        st.subheader(f"✅ 识别完成 ({len(recognition_done_df)})")
        display_cols = ["系统", "剧名", "生产状态", "生产机器", "已处理时长", "预估完成时长"]
        available_cols = [col for col in display_cols if col in recognition_done_df.columns]
        st.dataframe(
            recognition_done_df[available_cols].sort_values("系统"),
            use_container_width=True,
            hide_index=True
        )


def render_today_input_tab(df: pd.DataFrame):
    """渲染今日入库标签页"""
    st.header("📦 今日入库生产进度")

    if df.empty:
        st.warning("暂无数据")
        return

    today = datetime.now().date()

    # ===== 系统筛选器 =====
    st.subheader("🔍 系统筛选")
    all_systems = ["全部系统"] + sorted(df["系统"].unique().tolist())
    selected_systems = st.multiselect(
        "选择要查看的系统",
        options=all_systems,
        default=["全部系统"],
        key="today_input_system_filter",
        help="选择多个系统查看各自的入表和产出趋势"
    )

    # 根据筛选条件处理数据
    if "全部系统" in selected_systems:
        df_filtered = df
    else:
        df_filtered = df[df["系统"].isin(selected_systems)]

    # ===== 今日入库记录 =====
    st.subheader("📅 今日入库记录")

    today_df = df_filtered[df_filtered["入表时间"].apply(lambda x: x.date() if pd.notna(x) else None) == today]

    if today_df.empty:
        st.success("今日暂无入库记录")
    else:
        col1, col2, col3 = st.columns(3)
        with col1:
            st.markdown(create_kpi_card("今日入库", len(today_df), color="#5470c6"), unsafe_allow_html=True)
        with col2:
            today_completed = len(today_df[today_df["生产状态"] == "完成"])
            st.markdown(create_kpi_card("今日已完成", today_completed, color="#91cc75"), unsafe_allow_html=True)
        with col3:
            today_in_progress = len(today_df[today_df["生产状态"].isin(["合并视频", "识别字幕", "识别角色", "处理BGM"])])
            st.markdown(create_kpi_card("今日处理中", today_in_progress, color="#fac858"), unsafe_allow_html=True)

        st.markdown("---")
        st.subheader("📋 今日入库详情")

        display_cols = ["系统", "剧名", "生产状态", "整备状态", "生产机器", "入表时间", "生产周期"]
        available_cols = [col for col in display_cols if col in today_df.columns]
        st.dataframe(
            today_df[available_cols].sort_values("入表时间", ascending=False),
            use_container_width=True,
            hide_index=True
        )

    st.markdown("---")

    # ===== 每日入表与产出趋势折线面积图 =====
    st.subheader("📈 每日入表与产出趋势")

    # 判断是单一系统还是多系统/全部
    is_single_system = len(selected_systems) == 1 and "全部系统" not in selected_systems
    is_all_systems = "全部系统" in selected_systems

    if is_all_systems:
        # 全部系统模式：显示各系统分别的入表和产出，以及总和

        # 计算每个系统每日入表量
        df_with_date = df_filtered[df_filtered["入表时间"].notna()].copy()
        df_with_date["入表日期"] = df_with_date["入表时间"].apply(lambda x: x.date())
        daily_input_by_system = df_with_date.groupby(["入表日期", "系统"]).size().reset_index(name="入表量")

        # 计算每个系统每日产出量
        df_completed = df_filtered[df_filtered["生产状态"] == "完成"].copy()
        df_completed = df_completed[df_completed["识别角色结束时间"].notna()]
        df_completed["完成日期"] = df_completed["识别角色结束时间"].apply(lambda x: x.date())
        daily_output_by_system = df_completed.groupby(["完成日期", "系统"]).size().reset_index(name="产出量")

        # 获取所有日期和系统
        all_dates = pd.concat([
            daily_input_by_system[["入表日期"]].rename(columns={"入表日期": "日期"}),
            daily_output_by_system[["完成日期"]].rename(columns={"完成日期": "日期"})
        ]).drop_duplicates().sort_values("日期")

        systems_list = sorted(df_filtered["系统"].unique().tolist())

        if len(all_dates) == 0:
            st.info("暂无趋势数据")
        else:
            # 系统选择器
            trend_systems = st.multiselect(
                "选择趋势图中显示的系统",
                options=systems_list,
                default=systems_list[:3] if len(systems_list) > 3 else systems_list,
                key="trend_system_filter"
            )

            # 创建折线图
            line = (
                Line(init_opts=opts.InitOpts(width="100%", height="400px", theme="light"))
            )

            dates = [d.strftime("%m-%d") for d in all_dates["日期"]]
            line.add_xaxis(dates)

            # 定义颜色
            system_colors = {
                "众益": "#5470c6", "点众": "#91cc75", "极剧": "#fac858",
                "红果": "#ee6666", "掌阅": "#73c0de", "外部制作": "#9a60b4",
                "ReelShort": "#fc8452"
            }

            # 为每个选中的系统添加入表和产出线
            for sys_name in trend_systems:
                color = system_colors.get(sys_name, "#999")

                # 入表量
                sys_input = daily_input_by_system[daily_input_by_system["系统"] == sys_name]
                input_dict = dict(zip(sys_input["入表日期"], sys_input["入表量"]))
                input_values = [input_dict.get(d, 0) for d in all_dates["日期"]]

                line.add_yaxis(
                    series_name=f"{sys_name}-入表",
                    y_axis=input_values,
                    symbol="circle",
                    symbol_size=6,
                    linestyle_opts=opts.LineStyleOpts(width=2, color=color),
                    itemstyle_opts=opts.ItemStyleOpts(color=color),
                    areastyle_opts=opts.AreaStyleOpts(opacity=0.1, color=color),
                    label_opts=opts.LabelOpts(is_show=False),
                )

                # 产出量
                sys_output = daily_output_by_system[daily_output_by_system["系统"] == sys_name]
                output_dict = dict(zip(sys_output["完成日期"], sys_output["产出量"]))
                output_values = [output_dict.get(d, 0) for d in all_dates["日期"]]

                line.add_yaxis(
                    series_name=f"{sys_name}-产出",
                    y_axis=output_values,
                    symbol="diamond",
                    symbol_size=6,
                    linestyle_opts=opts.LineStyleOpts(width=2, color=color, type_="dashed"),
                    itemstyle_opts=opts.ItemStyleOpts(color=color),
                    label_opts=opts.LabelOpts(is_show=False),
                )

            line.set_global_opts(
                title_opts=opts.TitleOpts(title=""),
                xaxis_opts=opts.AxisOpts(axislabel_opts=opts.LabelOpts(rotate=30)),
                yaxis_opts=opts.AxisOpts(name="数量", min_=0),
                tooltip_opts=opts.TooltipOpts(trigger="axis"),
                legend_opts=opts.LegendOpts(pos_top="top", type_="scroll"),
            )
            render_pyecharts(line)

    else:
        # 单一系统模式：只显示该系统的入表和产出对比

        df_with_date = df_filtered[df_filtered["入表时间"].notna()].copy()
        df_with_date["入表日期"] = df_with_date["入表时间"].apply(lambda x: x.date())
        daily_input = df_with_date.groupby("入表日期").size().reset_index(name="入表量")

        df_completed = df_filtered[df_filtered["生产状态"] == "完成"].copy()
        df_completed = df_completed[df_completed["识别角色结束时间"].notna()]
        df_completed["完成日期"] = df_completed["识别角色结束时间"].apply(lambda x: x.date())
        daily_output = df_completed.groupby("完成日期").size().reset_index(name="产出量")

        # 合并为趋势数据
        all_dates = pd.concat([
            daily_input[["入表日期"]].rename(columns={"入表日期": "日期"}),
            daily_output[["完成日期"]].rename(columns={"完成日期": "日期"})
        ]).drop_duplicates().sort_values("日期")

        trend = all_dates.merge(daily_input.rename(columns={"入表日期": "日期"}), on="日期", how="left")
        trend = trend.merge(daily_output.rename(columns={"完成日期": "日期"}), on="日期", how="left")
        trend = trend.fillna(0).sort_values("日期")

        if len(trend) == 0:
            st.info("暂无趋势数据")
        else:
            dates = [d.strftime("%m-%d") for d in trend["日期"]]
            input_counts = [int(c) for c in trend["入表量"]]
            output_counts = [int(c) for c in trend["产出量"]]

            line = (
                Line(init_opts=opts.InitOpts(width="100%", height="400px", theme="light"))
                .add_xaxis(dates)
                .add_yaxis(
                    series_name="入表量",
                    y_axis=input_counts,
                    symbol="circle",
                    symbol_size=8,
                    linestyle_opts=opts.LineStyleOpts(width=2, color="#5470c6"),
                    itemstyle_opts=opts.ItemStyleOpts(color="#5470c6"),
                    areastyle_opts=opts.AreaStyleOpts(opacity=0.2, color="#5470c6"),
                    label_opts=opts.LabelOpts(is_show=True, position="top", formatter="{c}"),
                )
                .add_yaxis(
                    series_name="产出量",
                    y_axis=output_counts,
                    symbol="diamond",
                    symbol_size=8,
                    linestyle_opts=opts.LineStyleOpts(width=2, color="#91cc75", type_="dashed"),
                    itemstyle_opts=opts.ItemStyleOpts(color="#91cc75"),
                    label_opts=opts.LabelOpts(is_show=True, position="top", formatter="{c}"),
                )
                .set_global_opts(
                    title_opts=opts.TitleOpts(title=""),
                    xaxis_opts=opts.AxisOpts(axislabel_opts=opts.LabelOpts(rotate=30)),
                    yaxis_opts=opts.AxisOpts(name="数量", min_=0),
                    tooltip_opts=opts.TooltipOpts(trigger="axis"),
                    legend_opts=opts.LegendOpts(pos_top="top"),
                )
            )
            render_pyecharts(line)


def get_current_cycle() -> str:
    """获取当前生产周期（每周五为周期开始）

    当前周期 = 本周五（无论今天是周几）
    - 今天是周一到周四：当前周期 = 本周五
    - 今天是周五到周日：当前周期 = 今天（周五）
    """
    today = datetime.now()

    # 计算本周五（weekday=4 代表周五）
    days_until_friday = (4 - today.weekday()) % 7
    if days_until_friday == 0:
        # 今天是周五
        friday = today
    else:
        # 本周五还没到或已过，计算本周五
        friday = today + timedelta(days=days_until_friday)

    return friday.strftime("%Y%m%d")


def get_last_cycle() -> str:
    """获取上一个生产周期"""
    current = get_current_cycle()
    # 上周五 = 本周五 - 7天
    current_date = datetime.strptime(current, "%Y%m%d")
    last_friday = current_date - timedelta(days=7)
    return last_friday.strftime("%Y%m%d")


def is_monday_10am() -> bool:
    """判断是否是周一早上10点左右"""
    now = datetime.now()
    return now.weekday() == 0 and 9 <= now.hour <= 11


def render_daily_report_tab(df: pd.DataFrame):
    """渲染每日生产日报标签页"""
    st.header("📋 每日生产日报")

    if df.empty:
        st.warning("暂无数据")
        return

    now = datetime.now()
    today = now.date()

    # ===== 周期信息 =====
    current_cycle = get_current_cycle()
    last_cycle = get_last_cycle()

    st.markdown(f"""
    <div style="background-color: #e8f4fd; padding: 15px; border-radius: 10px; border-left: 4px solid #5470c6; margin-bottom: 20px;">
        <h4 style="margin: 0; color: #333;">📅 周期信息</h4>
        <p style="margin: 5px 0; color: #666;">当前周期: <b>{current_cycle}</b> | 上周期: <b>{last_cycle}</b></p>
        <p style="margin: 5px 0; color: #666;">报告生成时间: <b>{now.strftime('%Y-%m-%d %H:%M:%S')}</b></p>
    </div>
    """, unsafe_allow_html=True)

    # ===== 第一部分：上周期生产情况（简化版） =====
    st.subheader("📆 上周期生产情况")

    last_cycle_df = df[df["生产周期"] == last_cycle]

    if not last_cycle_df.empty:
        total_count = len(last_cycle_df)
        completed_count = len(last_cycle_df[last_cycle_df["生产状态"] == "完成"])
        completion_rate = (completed_count / total_count * 100) if total_count > 0 else 0

        col1, col2, col3 = st.columns(3)
        with col1:
            st.markdown(create_kpi_card("上周期总数", total_count, color="#5470c6"), unsafe_allow_html=True)
        with col2:
            st.markdown(create_kpi_card("已完成", completed_count, color="#91cc75"), unsafe_allow_html=True)
        with col3:
            st.markdown(create_kpi_card("完成率", f"{completion_rate:.1f}%", color="#73c0de"), unsafe_allow_html=True)
    else:
        st.info(f"上周期 ({last_cycle}) 暂无数据")

    st.markdown("---")

    # ===== 第二部分：本周期生产情况 =====
    st.subheader("📊 本周期生产情况")

    current_cycle_df = df[df["生产周期"] == current_cycle]

    if not current_cycle_df.empty:
        # 今日新增
        today_new_count = len(current_cycle_df[
            current_cycle_df["入表时间"].apply(lambda x: x.date() if pd.notna(x) else None) == today
        ])

        # 剩余待处理（生产状态 != 完成）
        pending_count = len(current_cycle_df[
            current_cycle_df["生产状态"] != "完成"
        ])

        # 今日完成
        today_completed_count = len(current_cycle_df[
            (current_cycle_df["生产状态"] == "完成") &
            (current_cycle_df["识别角色结束时间"].apply(lambda x: x.date() if pd.notna(x) else None) == today)
        ])

        col1, col2, col3 = st.columns(3)
        with col1:
            st.markdown(create_kpi_card("今日新增", today_new_count, color="#5470c6"), unsafe_allow_html=True)
        with col2:
            st.markdown(create_kpi_card("剩余待处理", pending_count, color="#fac858"), unsafe_allow_html=True)
        with col3:
            st.markdown(create_kpi_card("今日完成", today_completed_count, color="#91cc75"), unsafe_allow_html=True)
    else:
        st.info(f"当前周期 ({current_cycle}) 暂无数据")

    st.markdown("---")

    # ===== 第三部分：失败记录与异常记录（合并展示） =====
    st.subheader("⚠️ 失败记录与异常记录")
    st.caption("📋 按系统 > 生产周期（越早越靠前）展示失败和整备异常记录")

    # 收集所有需要关注的记录
    issue_records = []

    # 格式化遗留时长的函数
    def format_pending_hours(hours):
        """格式化遗留时长：小于24小时显示小时，大于24小时转换成天"""
        if hours is None or pd.isna(hours):
            return "-"
        if hours < 24:
            return f"{hours:.1f}h"
        else:
            days = hours / 24
            return f"{days:.1f}天"

    # 1. 失败记录
    failed_df = df[df["生产状态"].isin(["失败", "处理BGM失败"])].copy()
    if not failed_df.empty:
        for _, row in failed_df.iterrows():
            # 计算遗留时长
            entry_time = row.get("入表时间")
            if pd.notna(entry_time):
                pending_hours = (now - entry_time).total_seconds() / 3600
                pending_str = format_pending_hours(pending_hours)
            else:
                pending_str = "-"
                pending_hours = 999999

            issue_records.append({
                "系统": row.get("系统"),
                "生产周期": row.get("生产周期", ""),
                "剧名": row.get("剧名"),
                "问题类型": "失败",
                "失败类型": row.get("失败类型") or "-",
                "异常原因": "",
                "备注": row.get("备注", ""),
                "入表时间": entry_time,
                "遗留时长": pending_str,
                "遗留小时": pending_hours if pd.notna(entry_time) else 999999,
            })

    # 2. 整备异常记录（未开始且整备状态或NAS位置为空）
    not_started_df = df[df["生产状态"] == "未开始"].copy()
    if not not_started_df.empty:
        abnormal_df = not_started_df[
            (not_started_df["整备状态"].isna() | (not_started_df["整备状态"] == "")) |
            (not_started_df["NAS位置"].isna() | (not_started_df["NAS位置"] == ""))
        ].copy()

        for _, row in abnormal_df.iterrows():
            # 标记异常原因
            issues = []
            if pd.isna(row.get("整备状态")) or row.get("整备状态") == "":
                issues.append("整备状态为空")
            if pd.isna(row.get("NAS位置")) or row.get("NAS位置") == "":
                issues.append("NAS位置为空")

            # 计算遗留时长
            entry_time = row.get("入表时间")
            if pd.notna(entry_time):
                pending_hours = (now - entry_time).total_seconds() / 3600
                pending_str = format_pending_hours(pending_hours)
            else:
                pending_str = "-"
                pending_hours = 999999

            issue_records.append({
                "系统": row.get("系统"),
                "生产周期": row.get("生产周期", ""),
                "剧名": row.get("剧名"),
                "问题类型": "整备异常",
                "失败类型": "",
                "异常原因": " | ".join(issues),
                "备注": row.get("备注", ""),
                "入表时间": entry_time,
                "遗留时长": pending_str,
                "遗留小时": pending_hours,
            })

    if issue_records:
        issue_df = pd.DataFrame(issue_records)

        # 按系统分组展示
        for system in SYSTEMS:
            system_issues = issue_df[issue_df["系统"] == system]
            if system_issues.empty:
                continue

            st.markdown(f"### 🏢 {system} ({len(system_issues)}条)")

            # 按生产周期分组，周期越早越靠前
            cycles = system_issues["生产周期"].unique().tolist()
            # 排序：空值放最后，其他按周期字符串升序（越早越小）
            cycles_sorted = sorted([c for c in cycles if c], reverse=False)
            if "" in cycles or None in cycles:
                cycles_sorted.append("")

            for cycle in cycles_sorted:
                if cycle == "":
                    cycle_label = "未知周期"
                else:
                    cycle_label = cycle

                cycle_issues = system_issues[system_issues["生产周期"] == cycle]
                if cycle_issues.empty:
                    continue

                # 统计失败和异常数量
                fail_count = len(cycle_issues[cycle_issues["问题类型"] == "失败"])
                abnormal_count = len(cycle_issues[cycle_issues["问题类型"] == "整备异常"])

                cycle_info = f"📅 周期 {cycle_label}"
                if fail_count > 0:
                    cycle_info += f" | 失败 {fail_count}条"
                if abnormal_count > 0:
                    cycle_info += f" | 异常 {abnormal_count}条"

                st.markdown(f"**{cycle_info}**")

                # 显示该周期的问题记录
                display_df = cycle_issues[["剧名", "问题类型", "失败类型", "异常原因", "遗留时长", "备注", "遗留小时"]].copy()
                display_df = display_df.sort_values("遗留小时", ascending=False)
                display_df = display_df.drop(columns=["遗留小时"])

                # 高亮显示
                def highlight_row(row):
                    if row["问题类型"] == "失败":
                        return ["background-color: #ffebee"] * len(row)
                    else:
                        return ["background-color: #fff8e1"] * len(row)

                styled_df = display_df.style.apply(highlight_row, axis=1)
                st.dataframe(styled_df, use_container_width=True, hide_index=True)

            st.markdown("")
    else:
        st.success("✅ 当前没有失败或异常记录")

    st.markdown("---")

    # ===== 第四部分：各系统汇总表 =====
    st.subheader("📊 各系统生产日报汇总")

    summary_data = []

    for system in SYSTEMS:
        system_df = df[df["系统"] == system]

        if system_df.empty:
            continue

        # 今日新增
        today_new = len(system_df[
            system_df["入表时间"].apply(lambda x: x.date() if pd.notna(x) else None) == today
        ])

        # 总待处理（生产状态 != 完成）
        pending = len(system_df[
            system_df["生产状态"] != "完成"
        ])

        # 今日完成
        today_completed = len(system_df[
            (system_df["生产状态"] == "完成") &
            (system_df["识别角色结束时间"].apply(lambda x: x.date() if pd.notna(x) else None) == today)
        ])

        # 失败+异常总数
        failed_count = len(system_df[system_df["生产状态"].isin(["失败", "处理BGM失败"])])
        not_started = system_df[system_df["生产状态"] == "未开始"]
        abnormal_count = len(not_started[
            (not_started["整备状态"].isna() | (not_started["整备状态"] == "")) |
            (not_started["NAS位置"].isna() | (not_started["NAS位置"] == ""))
        ])

        summary_data.append({
            "系统": system,
            "今日新增": today_new,
            "剩余待处理": pending,
            "今日完成": today_completed,
            "失败+异常": failed_count + abnormal_count,
        })

    if summary_data:
        summary_df = pd.DataFrame(summary_data)

        # 高亮显示有问题的记录
        def highlight_issues(val):
            if val > 0:
                return 'background-color: #fff3cd'
            return ''

        styled_df = summary_df.style.applymap(
            highlight_issues,
            subset=['失败+异常']
        )

        st.dataframe(styled_df, use_container_width=True, hide_index=True)

        # 导出功能
        csv = summary_df.to_csv(index=False).encode('utf-8-sig')
        st.download_button(
            label="📥 导出日报汇总(CSV)",
            data=csv,
            file_name=f"生产日报_{now.strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv"
        )


def main():
    """主函数"""
    st.set_page_config(
        page_title="生产监控面板",
        page_icon="🎬",
        layout="wide"
    )

    # 自定义 CSS
    st.markdown("""
    <style>
        .stTabs [data-baseweb="tab-list"] {
            gap: 8px;
        }
        .stTabs [data-baseweb="tab"] {
            height: 50px;
            padding-left: 20px;
            padding-right: 20px;
            background-color: #f0f2f6;
            border-radius: 8px 8px 0 0;
        }
        .stTabs [aria-selected="true"] {
            background-color: #5470c6;
            color: white;
        }
        .stMetric {
            background-color: #f8f9fa;
            padding: 10px;
            border-radius: 8px;
        }
        .cycle-info {
            background-color: #e8f4fd;
            padding: 10px;
            border-radius: 8px;
            border-left: 4px solid #5470c6;
            margin-bottom: 10px;
        }
    </style>
    """, unsafe_allow_html=True)

    # ===== 侧边栏：周期筛选 =====
    st.sidebar.title("📅 筛选条件")

    # 生产周期输入框
    st.sidebar.markdown("### 生产周期")
    selected_cycle = st.sidebar.text_input(
        "输入生产周期",
        value="",
        placeholder="例如: 20260501",
        key="cycle_input",
        help="留空显示全部数据，格式: YYYYMMDD"
    )

    # 刷新按钮
    if st.sidebar.button("🔄 刷新数据", key="refresh_data_btn"):
        st.cache_data.clear()
        st.rerun()

    st.sidebar.markdown("---")
    st.sidebar.markdown("### 📝 说明")
    st.sidebar.markdown("- 留空显示全部数据")
    st.sidebar.markdown("- 格式: YYYYMMDD")
    st.sidebar.markdown("- 如: 20260501")

    # ===== 主内容区域 =====
    st.title("🎬 生产监控面板")
    st.markdown(f"**最后更新时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # 显示当前筛选周期
    if selected_cycle:
        st.markdown(f"""
        <div class="cycle-info">
            📊 当前筛选周期: <b>{selected_cycle}</b>
        </div>
        """, unsafe_allow_html=True)

    with st.spinner("正在拉取数据..."):
        df = fetch_all_systems_data()

    if df.empty:
        st.error("无法获取数据，请检查网络连接和配置")
        return

    # 根据周期筛选数据（按"生产周期"字段筛选）
    if selected_cycle:
        df_filtered = df[df["生产周期"] == selected_cycle]
        st.info(f"📍 周期 {selected_cycle} 共 {len(df_filtered)} 条记录")
    else:
        df_filtered = df

    # 标签页：增加生产日报标签页
    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "📊 概览", "🏢 系统对比", "⏱️ 实时监控", "📦 今日入库", "📋 生产日报"
    ])

    with tab1:
        render_overview_tab(df_filtered)

    with tab2:
        render_system_comparison_tab(df_filtered)

    with tab3:
        render_realtime_monitor_tab(df_filtered)

    with tab4:
        render_today_input_tab(df_filtered)

    with tab5:
        render_daily_report_tab(df_filtered)


if __name__ == "__main__":
    main()
