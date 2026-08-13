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
import json
import threading
import time
import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
from collections import defaultdict

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from youtube_data.lark import LarkBitableClient, Condition
from factory.table_manager import CORE_TABLES
from youtube_data.lark_message import LarkMessage

# pyecharts 导入
from pyecharts.charts import Bar, Pie, Funnel, Line, Grid
from pyecharts import options as opts
from pyecharts.commons.utils import JsCode
from pyecharts.components import Table
import streamlit.components.v1 as components

# 系统配置
SYSTEMS = ["众益", "点众", "红果", "掌阅", "外部制作", "ReelShort"]

# 生产日报自动发送配置：周一/周五 19 点发送一次
DAILY_REPORT_CHAT_ID = "oc_471f224b62b9acad8ffc4433cc687add"
AUTO_REPORT_WEEKDAYS = {0, 4}
AUTO_REPORT_HOUR = 19
AUTO_REPORT_CHECK_INTERVAL_SECONDS = 60
AUTO_REPORT_STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "auto_daily_report_state.json"
)
AUTO_REPORT_LOCK = threading.Lock()
LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai") if ZoneInfo else None

# 剧识别表需要查询的字段
RECOGNITION_FIELDS = [
    "剧id", "剧名", "生产状态", "整备状态", "生产机器",
    "开始生产时间", "识别角色结束时间", "入表时间", "剧时长(小时)",
    "音画同步检查", "识别字幕方式", "字幕高度",
    "角色识别报告", "角色分布比例", "剧字幕条数",
    "备注", "失败类型", "预计发布日期", "BGM处理情况", "生产周期",
    "BGM开始处理时间", "处理BGM结束时间", "生产总耗时", "NAS位置"
]

# 剧制作表-新需要查询的字段（用于配音情况统计）
PRODUCTION_FIELDS = [
    "剧名", "当前状态", "当前制作周期", "语言", "制作备注", "制作耗时小时"
]

# 自动上传统计直接读取各系统「上传表」。任务创建时间作为每日入表口径，
# 上传状态和备注用于识别自动上传结果、运营手动上传及失败原因。
UPLOAD_STAT_FIELDS = [
    "任务创建时间", "剧id", "频道id", "剧名", "语言", "上传状态", "备注", "剧链接"
]
UPLOAD_STAT_SYSTEMS = [
    name for name, config in CORE_TABLES.items()
    if config.get("tables", {}).get("上传表")
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


@st.cache_data(ttl=300)  # 缓存5分钟
def fetch_production_data(system_name: str) -> list:
    """
    从指定系统的剧制作表-新拉取所有数据（用于配音情况统计）
    """
    try:
        config = CORE_TABLES.get(system_name)
        if not config:
            return []

        app_token = config.get("app_token")
        tables = config.get("tables", {})
        production_table_id = tables.get("剧制作表-新")

        if not production_table_id:
            return []

        client = LarkBitableClient()

        records = client.search_all_records(
            app_token=app_token,
            table_id=production_table_id,
            field_names=PRODUCTION_FIELDS,
            filter_conditions=[]
        )

        for record in records:
            record["系统"] = system_name

        return records

    except Exception as e:
        import traceback
        print(f"拉取 [{system_name}] 剧制作表数据失败: {e}")
        print(traceback.format_exc())
        return []


@st.cache_data(ttl=300)
def fetch_upload_statistics_data() -> pd.DataFrame:
    """读取全部已配置上传表，并转换为自动上传统计明细。"""
    client = LarkBitableClient()
    rows = []

    for system_name in UPLOAD_STAT_SYSTEMS:
        config = CORE_TABLES.get(system_name, {})
        table_id = config.get("tables", {}).get("上传表")
        try:
            records = client.search_all_records(
                app_token=config.get("app_token"),
                table_id=table_id,
                field_names=UPLOAD_STAT_FIELDS,
                filter_conditions=[],
            )
        except Exception as exc:
            print(f"拉取 [{system_name}] 上传表失败: {exc}")
            continue

        for record in records:
            fields = record.get("fields", {})
            created_at = _parse_timestamp(fields.get("任务创建时间"))
            if pd.isna(created_at):
                continue
            if not isinstance(created_at, datetime):
                created_at = pd.to_datetime(created_at, errors="coerce")
                if pd.isna(created_at):
                    continue
                created_at = created_at.to_pydatetime()
            status = (_extract_text(fields.get("上传状态")) or "").strip()
            remark = (_extract_text(fields.get("备注")) or "").strip()
            is_manual = "运营手动上传" in remark
            is_success = status in {"上传成功", "仅视频上传成功"}
            is_failed = status == "上传失败"
            rows.append({
                "record_id": record.get("record_id"),
                "系统": system_name,
                "入表日期": created_at.date(),
                "入表时间": created_at,
                "剧名": _extract_text(fields.get("剧名")),
                "频道id": _extract_text(fields.get("频道id")),
                "上传状态": status,
                "备注": remark,
                "运营手动上传": is_manual,
                "实际自动上传": (is_success or is_failed) and not is_manual,
                "上传成功": is_success and not is_manual,
                "上传失败": is_failed and not is_manual,
            })

    return pd.DataFrame(rows)


def _classify_upload_failure_reason(remark: str) -> str:
    """把容易变化的错误详情归并为可读、稳定的失败原因。"""
    text = str(remark or "").strip()
    lowered = text.lower()
    rules = [
        (("quota", "额度", "配额"), "频道/应用额度不足"),
        (("token", "oauth", "授权", "凭证", "认证"), "Token或授权异常"),
        (("subtitle", "caption", "字幕"), "字幕上传失败"),
        (("network", "timeout", "timed out", "连接", "网络", "ssl"), "网络或超时"),
        (("resource", "资源", "文件不存在", "找不到文件", "no such file"), "资源文件异常"),
        (("youtube", "上传返回空", "upload"), "YouTube上传异常"),
    ]
    for keywords, category in rules:
        if any(keyword in lowered for keyword in keywords):
            return category
    return text[:60] if text else "未填写失败原因"


def _build_upload_daily_summary(df: pd.DataFrame) -> pd.DataFrame:
    """按入表日期、系统生成任务 cohort 统计。"""
    if df.empty:
        return pd.DataFrame()
    summary = (
        df.groupby(["入表日期", "系统"], as_index=False)
        .agg(
            入表任务数=("record_id", "count"),
            实际自动上传数=("实际自动上传", "sum"),
            运营手动上传数=("运营手动上传", "sum"),
            上传成功数=("上传成功", "sum"),
            上传失败数=("上传失败", "sum"),
        )
    )
    summary["实际使用率"] = summary["实际自动上传数"].div(
        summary["入表任务数"].replace(0, pd.NA)
    ).fillna(0).mul(100)
    summary["实际成功率"] = summary["上传成功数"].div(
        summary["实际自动上传数"].replace(0, pd.NA)
    ).fillna(0).mul(100)
    summary["实际失败率"] = summary["上传失败数"].div(
        summary["实际自动上传数"].replace(0, pd.NA)
    ).fillna(0).mul(100)
    return summary.sort_values(["入表日期", "系统"], ascending=[False, True])


def _recognition_records_to_dataframe(all_records: list) -> pd.DataFrame:
    """
    将剧识别表记录转换为面板使用的 DataFrame
    """
    if not all_records:
        return pd.DataFrame()

    rows = []
    for record in all_records:
        fields = record.get("fields", {})
        row = {
            "record_id": record.get("record_id"),
            "系统": record.get("系统"),
            "来源表": "剧识别表",
            "来源表ID": CORE_TABLES.get(record.get("系统"), {}).get("tables", {}).get("剧识别表", ""),
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

    return pd.DataFrame(rows)


def fetch_all_systems_data_for_auto_report() -> pd.DataFrame:
    """
    后台自动日报专用的数据拉取，不依赖 Streamlit 页面上下文
    """
    all_records = []

    for system_name in SYSTEMS:
        try:
            config = CORE_TABLES.get(system_name)
            if not config:
                continue

            app_token = config.get("app_token")
            tables = config.get("tables", {})
            recognition_table_id = tables.get("剧识别表")

            if not recognition_table_id:
                continue

            client = LarkBitableClient()
            records = client.search_all_records(
                app_token=app_token,
                table_id=recognition_table_id,
                field_names=RECOGNITION_FIELDS,
                filter_conditions=[]
            )

            for record in records:
                record["系统"] = system_name

            all_records.extend(records)

        except Exception as e:
            print(f"自动日报拉取 [{system_name}] 数据失败: {e}")

    return _recognition_records_to_dataframe(all_records)


@st.cache_data(ttl=300)
def fetch_all_systems_data() -> pd.DataFrame:
    """
    拉取所有系统的剧识别表数据
    """
    all_records = []

    for system_name in SYSTEMS:
        records = fetch_recognition_data(system_name)
        all_records.extend(records)

    return _recognition_records_to_dataframe(all_records)


@st.cache_data(ttl=300)
def fetch_all_systems_production_data() -> pd.DataFrame:
    """
    拉取所有系统的剧制作表-新数据（用于配音情况统计）
    只保留最近四个生产周期的记录，减少数据量
    """
    all_records = []

    for system_name in SYSTEMS:
        records = fetch_production_data(system_name)
        all_records.extend(records)

    if not all_records:
        return pd.DataFrame()

    rows = []
    for record in all_records:
        fields = record.get("fields", {})
        row = {
            "record_id": record.get("record_id"),
            "系统": record.get("系统"),
            "剧名": _extract_text(fields.get("剧名")),
            "当前状态": _extract_text(fields.get("当前状态")),
            "当前制作周期": _extract_text(fields.get("当前制作周期")),
            "语言": _extract_text(fields.get("语言")),
            "制作备注": _extract_text(fields.get("制作备注")),
            "制作耗时小时": fields.get("制作耗时小时"),
        }
        rows.append(row)

    df = pd.DataFrame(rows)

    # 筛选最近四个生产周期的记录
    if not df.empty and "当前制作周期" in df.columns:
        # 获取所有非空的周期
        all_cycles = df["当前制作周期"].dropna().unique().tolist()
        # 过滤掉空字符串
        all_cycles = [c for c in all_cycles if c]
        # 按周期降序排序（周期格式为YYYYMMDD，字符串排序即可）
        all_cycles_sorted = sorted(all_cycles, reverse=True)
        # 取最近四个周期
        recent_cycles = all_cycles_sorted[:4]

        if recent_cycles:
            # 筛选只保留最近四个周期的数据
            df = df[df["当前制作周期"].isin(recent_cycles)]

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


def get_local_now() -> datetime:
    """获取北京时间；不支持 zoneinfo 时退回系统本地时间"""
    if LOCAL_TIMEZONE:
        return datetime.now(LOCAL_TIMEZONE).replace(tzinfo=None)
    return datetime.now()


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


def calculate_avg_recognition_time(df: pd.DataFrame) -> dict:
    """
    计算识别角色耗时统计
    筛选条件：
    - 开始生产时间、识别角色结束时间字段都不为空

    注意：不限制生产状态，只要有这两个时间字段就可以计算

    返回:
        dict: {
            "avg_recognition_time": 总体平均识别角色耗时,
            "valid_count": 总体有效记录数,
            "avg_time_no_error": 识别时无报错的平均耗时,
            "count_no_error": 识别时无报错的记录数,
            "avg_time_with_error": 识别时有报错的平均耗时,
            "count_with_error": 识别时有报错的记录数,
            "error_type_distribution": 失败类型占比字典 {失败类型: 数量},
        }
    """
    # 筛选两个字段都不为空的记录
    valid_df = df[
        df["开始生产时间"].notna() &
        df["识别角色结束时间"].notna()
    ].copy()

    if valid_df.empty:
        return {
            "avg_recognition_time": None,
            "valid_count": 0,
            "avg_time_no_error": None,
            "count_no_error": 0,
            "avg_time_with_error": None,
            "count_with_error": 0,
            "error_type_distribution": {},
        }

    # 计算识别角色耗时（识别角色结束时间 - 开始生产时间），单位：小时
    valid_df["识别角色耗时"] = valid_df.apply(
        lambda row: (row["识别角色结束时间"] - row["开始生产时间"]).total_seconds() / 3600,
        axis=1
    )

    # 过滤掉异常值（负数或超大值）
    valid_df = valid_df[(valid_df["识别角色耗时"] >= 0) & (valid_df["识别角色耗时"] < 1000)]

    if valid_df.empty:
        return {
            "avg_recognition_time": None,
            "valid_count": 0,
            "avg_time_no_error": None,
            "count_no_error": 0,
            "avg_time_with_error": None,
            "count_with_error": 0,
            "error_type_distribution": {},
        }

    # 计算总体平均值
    avg_time = valid_df["识别角色耗时"].mean()
    valid_count = len(valid_df)

    # 区分识别时无报错和有报错
    # 无报错：失败类型为空或NaN
    no_error_df = valid_df[
        valid_df["失败类型"].isna() | (valid_df["失败类型"] == "")
    ].copy()
    # 有报错：失败类型不为空
    with_error_df = valid_df[
        valid_df["失败类型"].notna() & (valid_df["失败类型"] != "")
    ].copy()

    # 计算无报错的平均耗时
    avg_time_no_error = no_error_df["识别角色耗时"].mean() if not no_error_df.empty else None
    count_no_error = len(no_error_df)

    # 计算有报错的平均耗时
    avg_time_with_error = with_error_df["识别角色耗时"].mean() if not with_error_df.empty else None
    count_with_error = len(with_error_df)

    # 统计失败类型分布（只统计有报错的记录）
    error_type_distribution = {}
    if not with_error_df.empty:
        error_type_counts = with_error_df["失败类型"].value_counts()
        error_type_distribution = error_type_counts.to_dict()

    return {
        "avg_recognition_time": avg_time,
        "valid_count": valid_count,
        "avg_time_no_error": avg_time_no_error,
        "count_no_error": count_no_error,
        "avg_time_with_error": avg_time_with_error,
        "count_with_error": count_with_error,
        "error_type_distribution": error_type_distribution,
    }


def calculate_avg_bgm_time(df: pd.DataFrame) -> dict:
    """计算BGM处理平均耗时，仅统计开始、结束时间均不为空的记录。"""
    valid_df = df[
        df["BGM开始处理时间"].notna() &
        df["处理BGM结束时间"].notna()
    ].copy()

    if valid_df.empty:
        return {"avg_bgm_time": None, "valid_count": 0}

    valid_df["BGM开始处理时间"] = pd.to_datetime(
        valid_df["BGM开始处理时间"], errors="coerce"
    )
    valid_df["处理BGM结束时间"] = pd.to_datetime(
        valid_df["处理BGM结束时间"], errors="coerce"
    )
    valid_df = valid_df[
        valid_df["BGM开始处理时间"].notna() &
        valid_df["处理BGM结束时间"].notna()
    ].copy()

    if valid_df.empty:
        return {"avg_bgm_time": None, "valid_count": 0}

    valid_df["BGM处理耗时"] = (
        valid_df["处理BGM结束时间"] - valid_df["BGM开始处理时间"]
    ).dt.total_seconds() / 3600

    return {
        "avg_bgm_time": valid_df["BGM处理耗时"].mean(),
        "valid_count": len(valid_df),
    }


def get_recognition_time_details(df: pd.DataFrame) -> pd.DataFrame:
    """返回与识别耗时指标口径一致的逐条明细。"""
    valid_df = df[
        df["开始生产时间"].notna() &
        df["识别角色结束时间"].notna()
    ].copy()

    if valid_df.empty:
        return valid_df

    valid_df["识别角色耗时(小时)"] = valid_df.apply(
        lambda row: (row["识别角色结束时间"] - row["开始生产时间"]).total_seconds() / 3600,
        axis=1
    )
    valid_df = valid_df[
        (valid_df["识别角色耗时(小时)"] >= 0) &
        (valid_df["识别角色耗时(小时)"] < 1000)
    ].copy()
    valid_df["是否报错"] = valid_df["失败类型"].notna() & (valid_df["失败类型"] != "")
    valid_df["识别角色耗时(小时)"] = valid_df["识别角色耗时(小时)"].round(1)
    return valid_df


def calculate_cycle_recognition_time(df: pd.DataFrame) -> pd.DataFrame:
    """
    计算每个生产周期的平均识别角色耗时
    筛选条件同上
    """
    # 筛选两个字段都不为空且有生产周期的记录
    valid_df = df[
        df["开始生产时间"].notna() &
        df["识别角色结束时间"].notna() &
        df["生产周期"].notna()
    ].copy()

    if valid_df.empty:
        return pd.DataFrame()

    # 计算识别角色耗时
    valid_df["识别角色耗时"] = valid_df.apply(
        lambda row: (row["识别角色结束时间"] - row["开始生产时间"]).total_seconds() / 3600,
        axis=1
    )

    # 过滤掉异常值
    valid_df = valid_df[(valid_df["识别角色耗时"] >= 0) & (valid_df["识别角色耗时"] < 1000)]

    if valid_df.empty:
        return pd.DataFrame()

    # 过滤掉超过当前周期的未来周期
    current_cycle = get_current_cycle()
    valid_df = valid_df[valid_df["生产周期"] <= current_cycle]

    if valid_df.empty:
        return pd.DataFrame()

    # 按生产周期分组计算平均值
    cycle_stats = valid_df.groupby("生产周期").agg(
        平均识别角色耗时=("识别角色耗时", "mean"),
        记录数=("识别角色耗时", "count")
    ).reset_index()

    # 过滤掉没有有效数据的周期
    cycle_stats = cycle_stats[cycle_stats["平均识别角色耗时"].notna()]

    # 按周期排序
    cycle_stats = cycle_stats.sort_values("生产周期")

    return cycle_stats


def calculate_cycle_bgm_time(df: pd.DataFrame) -> pd.DataFrame:
    """按生产周期计算BGM处理平均耗时，口径与总体BGM指标一致。"""
    valid_df = df[
        df["BGM开始处理时间"].notna() &
        df["处理BGM结束时间"].notna() &
        df["生产周期"].notna()
    ].copy()

    if valid_df.empty:
        return pd.DataFrame()

    valid_df["BGM开始处理时间"] = pd.to_datetime(
        valid_df["BGM开始处理时间"], errors="coerce"
    )
    valid_df["处理BGM结束时间"] = pd.to_datetime(
        valid_df["处理BGM结束时间"], errors="coerce"
    )
    valid_df["生产周期"] = valid_df["生产周期"].astype(str).str.strip()
    valid_df = valid_df[
        valid_df["BGM开始处理时间"].notna() &
        valid_df["处理BGM结束时间"].notna() &
        valid_df["生产周期"].ne("") &
        valid_df["生产周期"].le(get_current_cycle())
    ].copy()

    if valid_df.empty:
        return pd.DataFrame()

    valid_df["BGM处理耗时"] = (
        valid_df["处理BGM结束时间"] - valid_df["BGM开始处理时间"]
    ).dt.total_seconds() / 3600

    cycle_stats = valid_df.groupby("生产周期").agg(
        BGM处理平均耗时=("BGM处理耗时", "mean"),
        记录数=("BGM处理耗时", "count")
    ).reset_index()
    return cycle_stats.sort_values("生产周期")


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

    # 过滤掉超过当前周期的未来周期
    # 当前周期 = 本周五（无论今天是周几）
    current_cycle = get_current_cycle()
    valid_df = valid_df[valid_df["生产周期"] <= current_cycle]

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


def calculate_cycle_weekday_production_time(df: pd.DataFrame) -> pd.DataFrame:
    """
    计算每个生产周期中不同入表时间窗口的平均生产总耗时
    - 周一线：上个周五至本周一入表的记录
    - 周五线：上个周五至本周五入表的记录
    """
    if df.empty:
        return pd.DataFrame()

    required_cols = [
        "生产状态", "生产周期", "入表时间", "生产总耗时",
        "开始生产时间", "识别角色结束时间", "BGM开始处理时间", "处理BGM结束时间"
    ]
    if any(col not in df.columns for col in required_cols):
        return pd.DataFrame()

    current_cycle = get_current_cycle()
    all_cycles = df["生产周期"].dropna().astype(str).str.strip()
    all_cycles = sorted([cycle for cycle in all_cycles.unique().tolist() if cycle and cycle <= current_cycle])

    if not all_cycles:
        return pd.DataFrame()

    completed_df = df[df["生产状态"] == "完成"].copy()
    valid_df = completed_df[
        completed_df["生产周期"].notna() &
        completed_df["入表时间"].notna() &
        completed_df["生产总耗时"].notna() &
        completed_df["开始生产时间"].notna() &
        completed_df["识别角色结束时间"].notna() &
        completed_df["BGM开始处理时间"].notna() &
        completed_df["处理BGM结束时间"].notna()
    ].copy()

    if valid_df.empty:
        return pd.DataFrame()

    valid_df["生产周期"] = valid_df["生产周期"].astype(str).str.strip()
    valid_df = valid_df[valid_df["生产周期"].isin(all_cycles)]
    valid_df["入表时间"] = pd.to_datetime(valid_df["入表时间"], errors="coerce")
    valid_df["生产总耗时"] = pd.to_numeric(valid_df["生产总耗时"], errors="coerce")
    valid_df = valid_df[
        valid_df["入表时间"].notna() &
        valid_df["生产总耗时"].notna() &
        (valid_df["生产总耗时"] >= 0) &
        (valid_df["生产总耗时"] < 1000)
    ].copy()

    if valid_df.empty:
        return pd.DataFrame()

    valid_df["入表日期"] = valid_df["入表时间"].dt.date

    rows = []
    for cycle in all_cycles:
        try:
            cycle_friday = datetime.strptime(cycle, "%Y%m%d").date()
        except ValueError:
            continue

        last_friday = cycle_friday - timedelta(days=7)
        cycle_monday = last_friday + timedelta(days=3)
        cycle_df = valid_df[valid_df["生产周期"] == cycle]
        monday_df = cycle_df[
            (cycle_df["入表日期"] >= last_friday) &
            (cycle_df["入表日期"] <= cycle_monday)
        ]
        friday_df = cycle_df[
            (cycle_df["入表日期"] >= last_friday) &
            (cycle_df["入表日期"] <= cycle_friday)
        ]

        row = {
            "生产周期": cycle,
            "周一平均生产总耗时": monday_df["生产总耗时"].mean() if not monday_df.empty else None,
            "周五平均生产总耗时": friday_df["生产总耗时"].mean() if not friday_df.empty else None,
            "周一记录数": len(monday_df),
            "周五记录数": len(friday_df),
            "周一统计范围": f"{last_friday.strftime('%m-%d')}~{cycle_monday.strftime('%m-%d')}",
            "周五统计范围": f"{last_friday.strftime('%m-%d')}~{cycle_friday.strftime('%m-%d')}",
        }
        rows.append(row)

    result = pd.DataFrame(rows)
    if result.empty:
        return pd.DataFrame()

    result = result[
        result["周一平均生产总耗时"].notna() |
        result["周五平均生产总耗时"].notna()
    ]

    return result


def calculate_cycle_completion_rate(df: pd.DataFrame) -> pd.DataFrame:
    """
    计算每个生产周期的完成率
    完成率 = 生产状态为完成的记录数 / 周期总记录数
    """
    if df.empty or "生产周期" not in df.columns or "生产状态" not in df.columns:
        return pd.DataFrame()

    valid_df = df[df["生产周期"].notna()].copy()
    if valid_df.empty:
        return pd.DataFrame()

    current_cycle = get_current_cycle()
    valid_df["生产周期"] = valid_df["生产周期"].astype(str).str.strip()
    valid_df = valid_df[(valid_df["生产周期"] != "") & (valid_df["生产周期"] <= current_cycle)]

    if valid_df.empty:
        return pd.DataFrame()

    cycle_stats = valid_df.groupby("生产周期").agg(
        总数=("生产状态", "size"),
        已完成=("生产状态", lambda x: (x == "完成").sum())
    ).reset_index()
    cycle_stats["完成率"] = cycle_stats.apply(
        lambda row: row["已完成"] / row["总数"] * 100 if row["总数"] else 0,
        axis=1
    )
    cycle_stats = cycle_stats.sort_values("生产周期")

    return cycle_stats


def calculate_dubbing_production_time(production_df: pd.DataFrame) -> dict:
    """
    计算剧制作表-新的制作耗时统计
    筛选条件：制作耗时小时不为空且不为0

    返回:
        dict: {
            "avg_time": 平均制作耗时,
            "valid_count": 有效记录数,
            "cycle_stats": 各周期统计DataFrame,
            "system_cycle_stats": 各系统各周期统计DataFrame
        }
    """
    if production_df.empty:
        return {
            "avg_time": None,
            "valid_count": 0,
            "cycle_stats": pd.DataFrame(),
            "system_cycle_stats": pd.DataFrame()
        }

    # 筛选有效记录：制作耗时小时不为空且不为0
    valid_df = production_df[
        production_df["制作耗时小时"].notna() &
        (production_df["制作耗时小时"] != 0)
    ].copy()

    if valid_df.empty:
        return {
            "avg_time": None,
            "valid_count": 0,
            "cycle_stats": pd.DataFrame(),
            "system_cycle_stats": pd.DataFrame()
        }

    # 计算总体平均值
    avg_time = valid_df["制作耗时小时"].mean()
    valid_count = len(valid_df)

    # 按周期统计
    cycle_stats = pd.DataFrame()
    system_cycle_stats = pd.DataFrame()

    if "当前制作周期" in valid_df.columns:
        cycle_valid_df = valid_df[valid_df["当前制作周期"].notna()]

        if not cycle_valid_df.empty:
            # 各周期平均耗时
            cycle_stats = cycle_valid_df.groupby("当前制作周期").agg(
                平均制作耗时=("制作耗时小时", "mean"),
                记录数=("制作耗时小时", "count")
            ).reset_index()
            cycle_stats = cycle_stats.sort_values("当前制作周期")

            # 各系统各周期平均耗时
            system_cycle_stats = cycle_valid_df.groupby(["系统", "当前制作周期"]).agg(
                平均制作耗时=("制作耗时小时", "mean"),
                记录数=("制作耗时小时", "count")
            ).reset_index()
            system_cycle_stats = system_cycle_stats.sort_values(["系统", "当前制作周期"])

    return {
        "avg_time": avg_time,
        "valid_count": valid_count,
        "cycle_stats": cycle_stats,
        "system_cycle_stats": system_cycle_stats
    }


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

    elif status in ["合并视频", "识别字幕", "识别角色", "下载资源失败"]:
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


def build_daily_report_message(df: pd.DataFrame, current_cycle: str) -> str:
    """
    构建生产日报消息正文
    """
    # 排除外部制作系统
    report_systems = [s for s in SYSTEMS if s != "外部制作"]

    # 筛选当前周期的数据
    current_cycle_df = df[df["生产周期"] == current_cycle]

    message_lines = [f"📋【生产日报】周期 {current_cycle}", ""]

    total_all = 0
    completed_all = 0
    processing_all = 0
    failed_all = 0
    handling_all = 0
    abnormal_all = 0
    waiting_all = 0
    other_all = 0

    failed_details = []
    handling_details = []

    for system in report_systems:
        system_df = current_cycle_df[current_cycle_df["系统"] == system]
        if system_df.empty:
            continue

        sys_total = len(system_df)
        sys_completed = len(system_df[system_df["生产状态"] == "完成"])
        sys_processing = len(system_df[system_df["生产状态"].isin([
            "合并视频", "识别字幕", "识别角色", "处理BGM", "识别完成"
        ])])
        sys_failed = len(system_df[system_df["生产状态"].isin(["失败", "处理BGM失败"])])
        sys_handling = len(system_df[system_df["生产状态"] == "失败处理中"])
        not_started = system_df[system_df["生产状态"] == "未开始"]
        abnormal_mask = (
            (not_started["整备状态"].isna() | (not_started["整备状态"] == "")) |
            (not_started["NAS位置"].isna() | (not_started["NAS位置"] == ""))
        )
        waiting_mask = (~abnormal_mask) & (not_started["整备状态"] == "文件结构已对齐")
        sys_abnormal = len(not_started[abnormal_mask])
        sys_waiting = len(not_started[waiting_mask])
        sys_other = sys_total - (
            sys_completed + sys_processing + sys_failed + sys_handling + sys_abnormal + sys_waiting
        )

        total_all += sys_total
        completed_all += sys_completed
        processing_all += sys_processing
        failed_all += sys_failed
        handling_all += sys_handling
        abnormal_all += sys_abnormal
        waiting_all += sys_waiting
        other_all += sys_other

        for _, row in system_df[system_df["生产状态"].isin(["失败", "处理BGM失败"])].iterrows():
            failed_details.append(f"{system}-{row.get('剧名', '未知')}")
        for _, row in system_df[system_df["生产状态"] == "失败处理中"].iterrows():
            handling_details.append(f"{system}-{row.get('剧名', '未知')}")

        message_lines.append(f"🏢 {system}：")
        message_lines.append(f"  总数 {sys_total} | 完成 {sys_completed} | 生产中 {sys_processing} | 失败 {sys_failed} | 失败处理中 {sys_handling} | 缺少资源 {sys_abnormal} | 等待识别 {sys_waiting} | 其他未完成 {sys_other}")

    message_lines.insert(1, f"📊 汇总：总数 {total_all} | 完成 {completed_all} | 生产中 {processing_all} | 失败 {failed_all} | 失败处理中 {handling_all} | 缺少资源 {abnormal_all} | 等待识别 {waiting_all} | 其他未完成 {other_all}")
    message_lines.insert(2, "")

    if failed_details:
        message_lines.append("")
        message_lines.append(f"❌ 失败待处理记录（{len(failed_details)}部）：")
        for detail in failed_details[:20]:
            message_lines.append(f"  - {detail}")
        if len(failed_details) > 20:
            message_lines.append(f"  ... 共 {len(failed_details)} 部")

    if handling_details:
        message_lines.append("")
        message_lines.append(f"🔄 失败处理中（{len(handling_details)}部）：")
        for detail in handling_details[:20]:
            message_lines.append(f"  - {detail}")
        if len(handling_details) > 20:
            message_lines.append(f"  ... 共 {len(handling_details)} 部")

    return "\n".join(message_lines)


def send_custom_report_to_group(content: str):
    """
    发送自定义内容到飞书群组
    """
    try:
        lark_message_client = LarkMessage()

        lark_message_client.send_message(DAILY_REPORT_CHAT_ID, content)

        return True, "日报已发送成功"

    except Exception as e:
        return False, f"发送失败: {str(e)}"


def send_daily_report_to_group(df: pd.DataFrame, current_cycle: str):
    """
    发送生产日报到飞书群组

    统计内容：
    - 各系统当前生产周期的生产情况
    - 总数、已整备完成、失败、失败处理中、整备异常、等待进入识别
    - 不统计外部制作系统
    """
    try:
        lark_message_client = LarkMessage()

        # 发送消息
        message = build_daily_report_message(df, current_cycle)
        lark_message_client.send_message(DAILY_REPORT_CHAT_ID, message)

        return True, "日报已发送成功"

    except Exception as e:
        return False, f"发送失败: {str(e)}"


def _load_auto_report_state() -> dict:
    """读取自动日报发送状态"""
    if not os.path.exists(AUTO_REPORT_STATE_FILE):
        return {}

    try:
        with open(AUTO_REPORT_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"读取自动日报状态失败: {e}")
        return {}


def _save_auto_report_state(state: dict):
    """保存自动日报发送状态"""
    try:
        tmp_file = f"{AUTO_REPORT_STATE_FILE}.tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp_file, AUTO_REPORT_STATE_FILE)
    except Exception as e:
        print(f"保存自动日报状态失败: {e}")


def _is_recent_auto_report_sending(state_item: dict, now: datetime) -> bool:
    """判断是否已有一个近期开启的发送任务，避免并发重复发送"""
    if state_item.get("status") != "sending":
        return False

    updated_at = state_item.get("updated_at")
    if not updated_at:
        return False

    try:
        updated_time = datetime.strptime(updated_at, "%Y-%m-%d %H:%M:%S")
        return (now - updated_time).total_seconds() < 15 * 60
    except Exception:
        return False


def _mark_auto_report_state(send_key: str, status: str, message: str, current_cycle: str = None):
    """记录自动日报发送结果"""
    with AUTO_REPORT_LOCK:
        state = _load_auto_report_state()
        state[send_key] = {
            "status": status,
            "message": message,
            "current_cycle": current_cycle,
            "updated_at": get_local_now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        _save_auto_report_state(state)


def try_send_auto_daily_report(now: datetime = None):
    """
    到达周一/周五 19 点时自动发送生产日报
    """
    now = now or get_local_now()

    if now.weekday() not in AUTO_REPORT_WEEKDAYS or now.hour != AUTO_REPORT_HOUR:
        return False, "未到自动发送时间"

    send_key = now.strftime("%Y-%m-%d")

    with AUTO_REPORT_LOCK:
        state = _load_auto_report_state()
        state_item = state.get(send_key, {})
        if state_item.get("status") == "success":
            return False, "今日自动日报已发送"
        if _is_recent_auto_report_sending(state_item, now):
            return False, "自动日报正在发送中"

        state[send_key] = {
            "status": "sending",
            "message": "自动日报发送中",
            "current_cycle": None,
            "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _save_auto_report_state(state)

    try:
        df = fetch_all_systems_data_for_auto_report()
        if df.empty:
            msg = "自动日报发送失败: 未获取到生产数据"
            _mark_auto_report_state(send_key, "failed", msg)
            print(msg)
            return True, msg

        current_cycle = get_current_cycle()
        success, msg = send_daily_report_to_group(df, current_cycle)
        status = "success" if success else "failed"
        _mark_auto_report_state(send_key, status, msg, current_cycle)
        print(f"自动日报发送结果: {msg}")
        return True, msg

    except Exception as e:
        msg = f"自动日报发送异常: {e}"
        _mark_auto_report_state(send_key, "failed", msg)
        print(msg)
        return True, msg


def _auto_daily_report_scheduler_loop():
    """自动日报后台调度循环"""
    print("自动生产日报定时器已启动：周一/周五 19:00 发送")
    while True:
        try:
            try_send_auto_daily_report()
        except Exception as e:
            print(f"自动日报定时器异常: {e}")

        time.sleep(AUTO_REPORT_CHECK_INTERVAL_SECONDS)


@st.cache_resource(show_spinner=False)
def start_auto_daily_report_scheduler():
    """启动自动日报后台线程，Streamlit 进程内只启动一次"""
    thread = threading.Thread(
        target=_auto_daily_report_scheduler_loop,
        name="auto-daily-report-scheduler",
        daemon=True,
    )
    thread.start()
    return thread


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

    # 未完成统一按“非完成”计算，确保与“总剧数 - 已完成”完全一致。
    # 之前这里只累加了三种状态，会漏掉合并视频、识别字幕、识别角色、失败等记录。
    not_completed_count = total_count - completed_count

    # 未完成细分
    not_started_count = len(df[df["生产状态"] == "未开始"])
    recognition_done_count = len(df[df["生产状态"] == "识别完成"])
    processing_bgm_count = len(df[df["生产状态"] == "处理BGM"])
    in_progress_count = len(df[df["生产状态"].isin(["合并视频", "识别字幕", "识别角色"])])
    regular_incomplete_statuses = [
        "未开始", "识别完成", "处理BGM", "合并视频", "识别字幕", "识别角色"
    ]
    other_incomplete_df = df[
        (df["生产状态"] != "完成") &
        (~df["生产状态"].isin(regular_incomplete_statuses))
    ].copy()
    other_incomplete_count = len(other_incomplete_df)

    # 失败分为两个互斥口径：
    # 1. 当前失败：当前生产状态就是“失败”
    # 2. 历史失败：当前已进入“失败处理中/未开始”，但仍保留失败类型
    failure_type_present = df["失败类型"].fillna("").astype(str).str.strip().ne("")
    current_fail_mask = df["生产状态"] == "失败"
    historical_fail_handling_mask = (df["生产状态"] == "失败处理中") & failure_type_present
    historical_fail_not_started_mask = (df["生产状态"] == "未开始") & failure_type_present

    current_fail_count = int(current_fail_mask.sum())
    historical_fail_handling_count = int(historical_fail_handling_mask.sum())
    historical_fail_not_started_count = int(historical_fail_not_started_mask.sum())
    historical_fail_count = historical_fail_handling_count + historical_fail_not_started_count
    total_fail_count = current_fail_count + historical_fail_count

    with col1:
        st.markdown(create_kpi_card("总剧数", total_count, color="#5470c6"), unsafe_allow_html=True)
    with col2:
        st.markdown(create_kpi_card("已完成", completed_count, color="#91cc75"), unsafe_allow_html=True)
    with col3:
        st.markdown(create_kpi_card("未完成", not_completed_count, color="#f39c12"), unsafe_allow_html=True)
    with col4:
        st.markdown(create_kpi_card("失败合计", total_fail_count, color="#ee6666"), unsafe_allow_html=True)

    st.markdown("---")

    # ===== 未完成细分 =====
    st.subheader("📋 未完成明细")
    col_left, col_center, col_right, col_progress, col_other = st.columns(5)
    with col_left:
        st.markdown(create_kpi_card("识别完成", recognition_done_count, color="#27ae60"), unsafe_allow_html=True)
    with col_center:
        st.markdown(create_kpi_card("处理BGM", processing_bgm_count, color="#f39c12"), unsafe_allow_html=True)
    with col_right:
        st.markdown(create_kpi_card("未开始", not_started_count, color="#95a5a6"), unsafe_allow_html=True)
    with col_progress:
        st.markdown(create_kpi_card("生产中", in_progress_count, color="#3498db"), unsafe_allow_html=True)
    with col_other:
        st.markdown(create_kpi_card("其他未完成", other_incomplete_count, color="#e67e22"), unsafe_allow_html=True)

    if not other_incomplete_df.empty:
        other_status_counts = (
            other_incomplete_df["生产状态"]
            .fillna("空状态")
            .astype(str)
            .value_counts()
            .to_dict()
        )
        other_status_summary = " | ".join(
            f"{status}: {count} 条" for status, count in other_status_counts.items()
        )
        st.caption(f"其他未完成状态分布：{other_status_summary}")

        incomplete_detail_columns = [
            "系统", "剧名", "剧id", "生产周期", "生产状态", "失败类型",
            "整备状态", "生产机器", "备注", "来源表", "来源表ID", "record_id"
        ]
        incomplete_detail_columns = [
            column for column in incomplete_detail_columns if column in other_incomplete_df.columns
        ]
        with st.expander(f"🔎 查看其他未完成的 {other_incomplete_count} 条记录"):
            st.dataframe(
                other_incomplete_df[incomplete_detail_columns],
                use_container_width=True,
                hide_index=True
            )

    st.markdown("---")

    # ===== 失败细分 =====
    st.subheader("❌ 失败明细")
    col_left, col_center, col_right = st.columns(3)
    with col_left:
        st.markdown(create_kpi_card("当前失败", current_fail_count, color="#c0392b"), unsafe_allow_html=True)
        st.caption("生产状态 = 失败")
    with col_center:
        st.markdown(create_kpi_card("历史失败", historical_fail_count, color="#e67e22"), unsafe_allow_html=True)
        st.caption("状态为失败处理中/未开始，且失败类型不为空")
    with col_right:
        st.markdown(
            create_kpi_card(
                "历史失败状态分布",
                f"处理中 {historical_fail_handling_count} / 未开始 {historical_fail_not_started_count}",
                color="#3498db"
            ),
            unsafe_allow_html=True
        )

    # 如果有未知状态，显示警告
    known_statuses = ["完成", "识别完成", "合并视频", "识别字幕", "识别角色", "处理BGM", "未开始", "失败", "处理BGM失败", "失败处理中"]
    unknown_status_df = df[~df["生产状态"].isin(known_statuses)].copy()
    if not unknown_status_df.empty:
        unknown_status_counts = (
            unknown_status_df["生产状态"]
            .fillna("空状态")
            .astype(str)
            .value_counts()
            .to_dict()
        )
        unknown_status_summary = " | ".join(
            f"{status}: {count} 条" for status, count in unknown_status_counts.items()
        )
        st.warning(f"⚠️ 其他生产状态：{unknown_status_summary}")

        unknown_detail_columns = [
            "系统", "剧名", "剧id", "生产周期", "生产状态", "失败类型",
            "整备状态", "备注", "来源表", "来源表ID", "record_id"
        ]
        unknown_detail_columns = [
            column for column in unknown_detail_columns if column in unknown_status_df.columns
        ]
        with st.expander(f"🔎 查看其他生产状态的 {len(unknown_status_df)} 条记录"):
            st.dataframe(
                unknown_status_df[unknown_detail_columns],
                use_container_width=True,
                hide_index=True
            )

    st.markdown("---")

    # ===== 识别角色耗时指标 =====
    st.subheader("⏱️ 识别角色与BGM处理耗时分析")

    # 计算平均识别角色耗时
    recognition_time_stats = calculate_avg_recognition_time(df)
    bgm_time_stats = calculate_avg_bgm_time(df)
    cycle_recognition_stats = calculate_cycle_recognition_time(df)
    cycle_bgm_stats = calculate_cycle_bgm_time(df)

    # 第一行：识别角色与BGM处理平均耗时
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        # 显示总体平均识别角色耗时卡片
        if recognition_time_stats["avg_recognition_time"] is not None:
            avg_time_str = f"{recognition_time_stats['avg_recognition_time']:.1f}h"
            st.markdown(
                create_kpi_card(
                    "平均识别角色耗时",
                    avg_time_str,
                    color="#73c0de"
                ),
                unsafe_allow_html=True
            )
            st.caption(f"📊 基于 {recognition_time_stats['valid_count']} 条有效记录")
        else:
            st.markdown(
                create_kpi_card("平均识别角色耗时", "暂无数据", color="#73c0de"),
                unsafe_allow_html=True
            )

    with col2:
        # 显示识别时无报错的平均耗时
        if recognition_time_stats["avg_time_no_error"] is not None:
            avg_time_str = f"{recognition_time_stats['avg_time_no_error']:.1f}h"
            st.markdown(
                create_kpi_card(
                    "识别时无报错",
                    avg_time_str,
                    color="#91cc75"
                ),
                unsafe_allow_html=True
            )
            st.caption(f"📊 {recognition_time_stats['count_no_error']} 条记录")
        else:
            st.markdown(
                create_kpi_card("识别时无报错", "暂无数据", color="#91cc75"),
                unsafe_allow_html=True
            )

    with col3:
        # 显示识别时有报错的平均耗时
        if recognition_time_stats["avg_time_with_error"] is not None:
            avg_time_str = f"{recognition_time_stats['avg_time_with_error']:.1f}h"
            st.markdown(
                create_kpi_card(
                    "识别时有报错",
                    avg_time_str,
                    color="#ee6666"
                ),
                unsafe_allow_html=True
            )
            st.caption(f"📊 {recognition_time_stats['count_with_error']} 条记录")
        else:
            st.markdown(
                create_kpi_card("识别时有报错", "暂无数据", color="#ee6666"),
                unsafe_allow_html=True
            )

    with col4:
        if bgm_time_stats["avg_bgm_time"] is not None:
            avg_time_str = f"{bgm_time_stats['avg_bgm_time']:.1f}h"
            st.markdown(
                create_kpi_card("BGM处理平均耗时", avg_time_str, color="#9a60b4"),
                unsafe_allow_html=True
            )
            st.caption(f"📊 基于 {bgm_time_stats['valid_count']} 条有效记录")
        else:
            st.markdown(
                create_kpi_card("BGM处理平均耗时", "暂无数据", color="#9a60b4"),
                unsafe_allow_html=True
            )

    # 指标追溯入口：明细严格使用与上方识别耗时相同的时间及异常值筛选口径
    recognition_details = get_recognition_time_details(df)
    error_details = (
        recognition_details[recognition_details["是否报错"]].copy()
        if not recognition_details.empty else recognition_details.copy()
    )
    if not error_details.empty:
        error_details["来源"] = error_details["系统"].fillna("") + " / " + error_details["来源表"].fillna("剧识别表")
        detail_columns = [
            "来源", "来源表ID", "record_id", "剧id", "剧名", "生产周期", "生产状态",
            "失败类型", "开始生产时间", "识别角色结束时间", "识别角色耗时(小时)", "备注"
        ]
        detail_columns = [column for column in detail_columns if column in error_details.columns]
        error_details_display = error_details[detail_columns].sort_values(
            "识别角色耗时(小时)", ascending=False
        )

        with st.expander(f"🔎 查看识别时有报错的 {len(error_details_display)} 条记录及来源表"):
            st.caption("来源格式：系统 / 表名；表 ID 和 record_id 可用于回查飞书原始记录。")
            st.dataframe(error_details_display, use_container_width=True, hide_index=True)
            st.download_button(
                label="📥 导出识别报错明细（CSV）",
                data=error_details_display.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"识别报错明细_{get_local_now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv",
                key="download_recognition_error_details",
            )

    st.caption(
        "📋 识别耗时：开始生产时间、识别角色结束时间均不为空 | "
        "BGM耗时：BGM开始处理时间、处理BGM结束时间均不为空 | "
        "无报错：失败类型为空 | 有报错：失败类型不为空"
    )

    # 第二行：失败类型占比饼图（仅显示有报错的记录）
    error_type_distribution = recognition_time_stats.get("error_type_distribution", {})
    if error_type_distribution:
        st.markdown("**📊 失败类型分布（仅统计识别时有报错的记录）**")

        # 准备饼图数据
        data_pair = [(error_type, count) for error_type, count in error_type_distribution.items()]
        # 按数量降序排序
        data_pair.sort(key=lambda x: x[1], reverse=True)

        # 计算总数用于显示百分比
        total_errors = sum(error_type_distribution.values())

        pie = (
            Pie(init_opts=opts.InitOpts(width="100%", height="400px", theme="light"))
            .add(
                series_name="失败类型",
                data_pair=data_pair,
                radius=["30%", "60%"],
                label_opts=opts.LabelOpts(
                    formatter=JsCode("""
                        function(params) {
                            var percent = (params.value / """ + str(total_errors) + """ * 100).toFixed(1);
                            return params.name + ': ' + params.value + ' (' + percent + '%)';
                        }
                    """)
                ),
                itemstyle_opts=opts.ItemStyleOpts(
                    border_width=2,
                    border_color="#fff"
                ),
            )
            .set_global_opts(
                title_opts=opts.TitleOpts(title=""),
                legend_opts=opts.LegendOpts(pos_left="left", pos_top="middle", type_="scroll"),
                tooltip_opts=opts.TooltipOpts(
                    trigger="item",
                    formatter="{b}: {c} ({d}%)"
                ),
            )
        )
        render_pyecharts(pie)
    else:
        if recognition_time_stats["count_with_error"] == 0:
            st.success("✅ 所有识别记录均无报错")

    # 第三行：各周期识别角色耗时折线图
    if not cycle_recognition_stats.empty:
        cycles = cycle_recognition_stats["生产周期"].tolist()
        avg_times = cycle_recognition_stats["平均识别角色耗时"].tolist()
        counts = cycle_recognition_stats["记录数"].tolist()

        line = (
            Line(init_opts=opts.InitOpts(width="100%", height="300px", theme="light"))
            .add_xaxis(cycles)
            .add_yaxis(
                series_name="平均识别角色耗时",
                y_axis=[round(t, 1) for t in avg_times],
                symbol="circle",
                symbol_size=8,
                linestyle_opts=opts.LineStyleOpts(width=2, color="#73c0de"),
                itemstyle_opts=opts.ItemStyleOpts(color="#73c0de"),
                label_opts=opts.LabelOpts(is_show=True, position="top", formatter="{c}h"),
            )
            .set_global_opts(
                title_opts=opts.TitleOpts(title="各生产周期识别角色耗时变化"),
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
        st.info("暂无各周期识别角色耗时数据")

    # 第四行：各生产周期BGM处理平均耗时折线图
    if not cycle_bgm_stats.empty:
        bgm_cycles = cycle_bgm_stats["生产周期"].tolist()
        bgm_avg_times = cycle_bgm_stats["BGM处理平均耗时"].tolist()
        bgm_counts = cycle_bgm_stats["记录数"].tolist()

        bgm_line = (
            Line(init_opts=opts.InitOpts(width="100%", height="300px", theme="light"))
            .add_xaxis(bgm_cycles)
            .add_yaxis(
                series_name="BGM处理平均耗时",
                y_axis=[round(value, 1) for value in bgm_avg_times],
                symbol="circle",
                symbol_size=8,
                linestyle_opts=opts.LineStyleOpts(width=2, color="#9a60b4"),
                itemstyle_opts=opts.ItemStyleOpts(color="#9a60b4"),
                label_opts=opts.LabelOpts(is_show=True, position="top", formatter="{c}h"),
            )
            .set_global_opts(
                title_opts=opts.TitleOpts(title="各生产周期BGM处理平均耗时变化"),
                xaxis_opts=opts.AxisOpts(axislabel_opts=opts.LabelOpts(rotate=30)),
                yaxis_opts=opts.AxisOpts(name="小时"),
                tooltip_opts=opts.TooltipOpts(
                    trigger="axis",
                    formatter=JsCode("""
                        function(params) {
                            var cycle = params[0].axisValue;
                            var time = params[0].value;
                            var idx = params[0].dataIndex;
                            var count = """ + str(bgm_counts) + """[idx];
                            return '周期: ' + cycle + '<br/>BGM平均耗时: ' + time + 'h<br/>记录数: ' + count;
                        }
                    """)
                ),
                legend_opts=opts.LegendOpts(pos_top="top"),
            )
        )
        render_pyecharts(bgm_line)
    else:
        st.info("暂无各周期BGM处理耗时数据")

    st.markdown("---")

    # ===== 生产总耗时指标 =====
    st.subheader("⏱️ 生产总耗时分析")

    # 计算平均生产总耗时
    prod_time_stats = calculate_avg_production_time(df)
    cycle_time_stats = calculate_cycle_production_time(df)
    weekday_cycle_time_stats = calculate_cycle_weekday_production_time(df)
    cycle_completion_stats = calculate_cycle_completion_rate(df)

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

    # ===== 周一/周五生产总耗时对比 =====
    st.subheader("📈 周一/周五生产总耗时对比")
    st.caption("📋 按入表时间窗口统计：周一线=上个周五至本周一，周五线=上个周五至本周五；筛选条件与平均生产总耗时一致")

    if not weekday_cycle_time_stats.empty:
        cycles = weekday_cycle_time_stats["生产周期"].tolist()
        monday_times = [
            round(t, 1) if pd.notna(t) else None
            for t in weekday_cycle_time_stats["周一平均生产总耗时"].tolist()
        ]
        friday_times = [
            round(t, 1) if pd.notna(t) else None
            for t in weekday_cycle_time_stats["周五平均生产总耗时"].tolist()
        ]
        monday_counts = weekday_cycle_time_stats["周一记录数"].tolist()
        friday_counts = weekday_cycle_time_stats["周五记录数"].tolist()
        monday_ranges = weekday_cycle_time_stats["周一统计范围"].tolist()
        friday_ranges = weekday_cycle_time_stats["周五统计范围"].tolist()

        line = (
            Line(init_opts=opts.InitOpts(width="100%", height="380px", theme="light"))
            .add_xaxis(cycles)
            .add_yaxis(
                series_name="周一窗口平均生产总耗时",
                y_axis=monday_times,
                symbol="circle",
                symbol_size=8,
                linestyle_opts=opts.LineStyleOpts(width=2, color="#5470c6"),
                itemstyle_opts=opts.ItemStyleOpts(color="#5470c6"),
                label_opts=opts.LabelOpts(is_show=False),
            )
            .add_yaxis(
                series_name="周五窗口平均生产总耗时",
                y_axis=friday_times,
                symbol="diamond",
                symbol_size=8,
                linestyle_opts=opts.LineStyleOpts(width=2, color="#fc8452"),
                itemstyle_opts=opts.ItemStyleOpts(color="#fc8452"),
                label_opts=opts.LabelOpts(is_show=False),
            )
            .set_global_opts(
                title_opts=opts.TitleOpts(title=""),
                xaxis_opts=opts.AxisOpts(axislabel_opts=opts.LabelOpts(rotate=30)),
                yaxis_opts=opts.AxisOpts(name="小时"),
                tooltip_opts=opts.TooltipOpts(
                    trigger="axis",
                    formatter=JsCode("""
                        function(params) {
                            var mondayCounts = """ + str(monday_counts) + """;
                            var fridayCounts = """ + str(friday_counts) + """;
                            var mondayRanges = """ + str(monday_ranges) + """;
                            var fridayRanges = """ + str(friday_ranges) + """;
                            var idx = params[0].dataIndex;
                            var html = '周期: ' + params[0].axisValue;
                            params.forEach(function(item) {
                                var count = item.seriesName.indexOf('周一') >= 0 ? mondayCounts[idx] : fridayCounts[idx];
                                var range = item.seriesName.indexOf('周一') >= 0 ? mondayRanges[idx] : fridayRanges[idx];
                                var value = (item.value === null || item.value === undefined) ? '暂无' : item.value + 'h';
                                html += '<br/>' + item.marker + item.seriesName + ': ' + value + '（' + count + '条，' + range + '）';
                            });
                            return html;
                        }
                    """)
                ),
                legend_opts=opts.LegendOpts(pos_top="top"),
            )
        )
        render_pyecharts(line)
    else:
        st.info("暂无周一/周五生产总耗时数据")

    st.markdown("---")

    # ===== 各生产周期完成率 =====
    st.subheader("📊 各生产周期完成率")

    if not cycle_completion_stats.empty:
        cycles = cycle_completion_stats["生产周期"].tolist()
        rates = [round(rate, 1) for rate in cycle_completion_stats["完成率"].tolist()]
        total_counts = cycle_completion_stats["总数"].tolist()
        completed_counts = cycle_completion_stats["已完成"].tolist()

        bar = (
            Bar(init_opts=opts.InitOpts(width="100%", height="380px", theme="light"))
            .add_xaxis(cycles)
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
                xaxis_opts=opts.AxisOpts(axislabel_opts=opts.LabelOpts(rotate=30)),
                yaxis_opts=opts.AxisOpts(name="完成率 (%)", min_=0, max_=100),
                tooltip_opts=opts.TooltipOpts(
                    trigger="axis",
                    axis_pointer_type="shadow",
                    formatter=JsCode("""
                        function(params) {
                            var totalCounts = """ + str(total_counts) + """;
                            var completedCounts = """ + str(completed_counts) + """;
                            var idx = params[0].dataIndex;
                            return '周期: ' + params[0].axisValue +
                                '<br/>完成率: ' + params[0].value + '%' +
                                '<br/>已完成: ' + completedCounts[idx] +
                                '<br/>总数: ' + totalCounts[idx];
                        }
                    """)
                ),
                legend_opts=opts.LegendOpts(pos_top="top"),
            )
        )
        render_pyecharts(bar)
    else:
        st.info("暂无各生产周期完成率数据")

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
    now = get_local_now()
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

    today = get_local_now().date()

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


def render_production_schedule_tab(df: pd.DataFrame):
    """合并展示全部系统剧识别任务，并按预计发布日期生成生产排期。"""
    st.header("📅 生产任务排期")
    st.caption("汇总所有系统剧识别表中生产状态不等于“完成”的任务，按预计发布日期从早到晚排列；未填写预计发布日期的任务排在最后。")

    if df.empty:
        st.warning("暂无可排期的生产任务。")
        return

    schedule_df = df[
        df["生产状态"].fillna("").astype(str).str.strip().ne("完成")
    ].copy()
    if schedule_df.empty:
        st.success("当前没有未完成的生产任务。")
        return
    schedule_df["预计发布日期"] = pd.to_datetime(
        schedule_df["预计发布日期"], errors="coerce"
    )
    schedule_df["入表时间"] = pd.to_datetime(
        schedule_df["入表时间"], errors="coerce"
    )

    # 与实时监控使用相同的状态耗时口径，并转换为具体的预计产出时间。
    avg_times = calculate_avg_times(df)
    estimate_base_time = get_local_now()

    def estimate_output_at(row: pd.Series) -> str:
        status = str(row.get("生产状态") or "").strip()
        estimate_hours = None
        if status == "未开始":
            estimate_hours = avg_times.get("avg_total_time")
        elif status in ["合并视频", "识别字幕", "识别角色", "下载资源失败"]:
            estimate_hours = avg_times.get("avg_recognition_time")
        elif status in ["处理BGM", "识别完成"]:
            estimate_hours = avg_times.get("avg_bgm_time")

        if estimate_hours is None or pd.isna(estimate_hours):
            return "等待处理" if status else "暂无数据"
        return (estimate_base_time + timedelta(hours=float(estimate_hours))).strftime(
            "%Y-%m-%d %H:%M"
        )

    schedule_df["预计产出时间"] = schedule_df.apply(estimate_output_at, axis=1)

    drama_keyword = st.text_input(
        "搜索剧名",
        placeholder="输入剧名关键字",
        key="production_schedule_drama_keyword",
    ).strip()
    if drama_keyword:
        schedule_df = schedule_df[
            schedule_df["剧名"].fillna("").astype(str).str.contains(
                drama_keyword, case=False, regex=False
            )
        ]

    if schedule_df.empty:
        st.info("当前筛选条件下没有生产任务。")
        return

    today = get_local_now().date()
    release_dates = schedule_df["预计发布日期"].dt.date
    overdue_count = int(release_dates.lt(today).sum())
    today_count = int(release_dates.eq(today).sum())
    upcoming_count = int(release_dates.gt(today).sum())
    unscheduled_count = int(schedule_df["预计发布日期"].isna().sum())

    metric_cols = st.columns(5)
    metric_values = [
        ("排期任务总数", len(schedule_df), "#5470c6"),
        ("逾期未完成", overdue_count, "#ee6666"),
        ("今日发布", today_count, "#fac858"),
        ("后续待处理", upcoming_count, "#73c0de"),
        ("未填写发布日期", unscheduled_count, "#95a5a6"),
    ]
    for col, (title, value, color) in zip(metric_cols, metric_values):
        with col:
            st.markdown(create_kpi_card(title, value, color=color), unsafe_allow_html=True)

    schedule_df = schedule_df.sort_values(
        ["预计发布日期", "入表时间", "系统"],
        ascending=[True, True, True],
        na_position="last",
    ).reset_index(drop=True)
    schedule_df.insert(0, "处理顺序", schedule_df.index + 1)
    schedule_df["预计发布日期"] = schedule_df["预计发布日期"].dt.strftime("%Y-%m-%d").fillna("未排期")
    schedule_df["入表时间"] = schedule_df["入表时间"].dt.strftime("%Y-%m-%d %H:%M").fillna("")

    display_columns = [
        "处理顺序", "预计发布日期", "预计产出时间", "系统", "剧名", "生产状态",
        "整备状态", "入表时间", "失败类型",
    ]
    display_columns = [column for column in display_columns if column in schedule_df.columns]
    st.markdown("---")
    st.subheader("📋 全系统任务处理顺序")
    st.dataframe(
        schedule_df[display_columns],
        use_container_width=True,
        hide_index=True,
        height=680,
    )


def render_upload_statistics_tab(upload_df: pd.DataFrame):
    """渲染自动上传每日大盘。"""
    st.header("🚀 自动上传统计")
    st.caption(
        "统计口径：按上传表任务的创建日期归属；实际自动上传=上传成功/仅视频上传成功/上传失败，"
        "且排除备注为“运营手动上传”的任务。实际使用率=实际自动上传数÷入表任务数，"
        "实际成功率=上传成功数÷实际自动上传数。"
    )

    if upload_df.empty:
        st.warning("暂无上传表数据，请检查上传表配置或任务创建时间字段。")
        return

    min_date = upload_df["入表日期"].min()
    max_date = upload_df["入表日期"].max()
    default_start = max(min_date, max_date - timedelta(days=29))
    filter_col1, filter_col2 = st.columns([2, 3])
    with filter_col1:
        date_range = st.date_input(
            "统计日期",
            value=(default_start, max_date),
            min_value=min_date,
            max_value=max_date,
            key="upload_statistics_date_range",
        )
    with filter_col2:
        selected_systems = st.multiselect(
            "系统",
            options=sorted(upload_df["系统"].dropna().unique().tolist()),
            default=sorted(upload_df["系统"].dropna().unique().tolist()),
            key="upload_statistics_systems",
        )

    filtered = upload_df.copy()
    if isinstance(date_range, (tuple, list)) and len(date_range) == 2:
        filtered = filtered[
            filtered["入表日期"].between(date_range[0], date_range[1])
        ]
    if selected_systems:
        filtered = filtered[filtered["系统"].isin(selected_systems)]
    else:
        filtered = filtered.iloc[0:0]

    if filtered.empty:
        st.info("当前筛选范围内暂无上传任务。")
        return

    total_input = len(filtered)
    total_actual = int(filtered["实际自动上传"].sum())
    total_manual = int(filtered["运营手动上传"].sum())
    total_success = int(filtered["上传成功"].sum())
    total_failed = int(filtered["上传失败"].sum())
    usage_rate = total_actual / total_input * 100 if total_input else 0
    success_rate = total_success / total_actual * 100 if total_actual else 0

    metric_cols = st.columns(7)
    metrics = [
        ("入表任务", total_input, "#5470c6"),
        ("实际自动上传", total_actual, "#73c0de"),
        ("实际使用率", f"{usage_rate:.1f}%", "#9a60b4"),
        ("运营手动上传", total_manual, "#fac858"),
        ("上传成功", total_success, "#91cc75"),
        ("实际成功率", f"{success_rate:.1f}%", "#3ba272"),
        ("上传失败", total_failed, "#ee6666"),
    ]
    for col, (title, value, color) in zip(metric_cols, metrics):
        with col:
            st.markdown(create_kpi_card(title, value, color=color), unsafe_allow_html=True)
    st.caption(f"实际成功率分子/分母：{total_success}/{total_actual}")

    daily_system = _build_upload_daily_summary(filtered)
    daily_total = (
        filtered.groupby("入表日期", as_index=False)
        .agg(
            入表任务数=("record_id", "count"),
            实际自动上传数=("实际自动上传", "sum"),
            运营手动上传数=("运营手动上传", "sum"),
            上传成功数=("上传成功", "sum"),
            上传失败数=("上传失败", "sum"),
        )
        .sort_values("入表日期")
    )

    st.markdown("---")
    st.subheader("📈 每日上传趋势")
    trend = Line(init_opts=opts.InitOpts(height="420px"))
    trend.add_xaxis([str(value) for value in daily_total["入表日期"]])
    for column, label in [
        ("入表任务数", "入表任务"),
        ("实际自动上传数", "实际自动上传"),
        ("运营手动上传数", "运营手动上传"),
        ("上传成功数", "上传成功"),
        ("上传失败数", "上传失败"),
    ]:
        trend.add_yaxis(label, daily_total[column].astype(int).tolist(), is_smooth=True)
    trend.set_global_opts(
        tooltip_opts=opts.TooltipOpts(trigger="axis"),
        legend_opts=opts.LegendOpts(pos_top="2%"),
        xaxis_opts=opts.AxisOpts(axislabel_opts=opts.LabelOpts(rotate=35)),
        yaxis_opts=opts.AxisOpts(name="任务数", min_=0),
    )
    render_pyecharts(trend)

    st.markdown("---")
    st.subheader("📉 每日自动上传成功率 / 失败率")
    st.caption(
        "横轴按任务创建日期统计；实际成功率=上传成功数÷实际自动上传数，"
        "实际失败率=上传失败数÷实际自动上传数。当天没有实际上传时不绘制比率点。"
    )
    rate_scope_options = ["所有系统（合并）"] + sorted(
        filtered["系统"].dropna().unique().tolist()
    )
    rate_scope = st.selectbox(
        "成功率/失败率统计范围",
        options=rate_scope_options,
        key="upload_rate_scope",
    )
    rate_source = (
        filtered
        if rate_scope == "所有系统（合并）"
        else filtered[filtered["系统"] == rate_scope]
    )
    daily_rates = (
        rate_source.groupby("入表日期", as_index=False)
        .agg(
            实际自动上传数=("实际自动上传", "sum"),
            上传成功数=("上传成功", "sum"),
            上传失败数=("上传失败", "sum"),
        )
        .sort_values("入表日期")
    )
    valid_upload_counts = daily_rates["实际自动上传数"].replace(0, pd.NA)
    daily_rates["实际成功率"] = daily_rates["上传成功数"].div(valid_upload_counts).mul(100)
    daily_rates["实际失败率"] = daily_rates["上传失败数"].div(valid_upload_counts).mul(100)

    rate_line = Line(init_opts=opts.InitOpts(height="420px"))
    rate_line.add_xaxis([str(value) for value in daily_rates["入表日期"]])
    rate_line.add_yaxis(
        "实际成功率",
        [round(value, 1) if pd.notna(value) else None for value in daily_rates["实际成功率"]],
        is_smooth=False,
        symbol="circle",
        symbol_size=6,
        linestyle_opts=opts.LineStyleOpts(width=2, color="#3ba272"),
        itemstyle_opts=opts.ItemStyleOpts(color="#3ba272"),
        label_opts=opts.LabelOpts(is_show=False),
    )
    rate_line.add_yaxis(
        "实际失败率",
        [round(value, 1) if pd.notna(value) else None for value in daily_rates["实际失败率"]],
        is_smooth=False,
        symbol="circle",
        symbol_size=6,
        linestyle_opts=opts.LineStyleOpts(width=2, color="#ee6666"),
        itemstyle_opts=opts.ItemStyleOpts(color="#ee6666"),
        label_opts=opts.LabelOpts(is_show=False),
    )
    rate_line.set_global_opts(
        tooltip_opts=opts.TooltipOpts(trigger="axis"),
        legend_opts=opts.LegendOpts(pos_top="2%"),
        xaxis_opts=opts.AxisOpts(
            type_="category",
            boundary_gap=False,
            axislabel_opts=opts.LabelOpts(rotate=30, font_size=11),
            splitline_opts=opts.SplitLineOpts(is_show=False),
        ),
        yaxis_opts=opts.AxisOpts(
            min_=0,
            max_=100,
            interval=20,
            axislabel_opts=opts.LabelOpts(formatter="{value}%", font_size=11),
            splitline_opts=opts.SplitLineOpts(
                is_show=True,
                linestyle_opts=opts.LineStyleOpts(color="#e8edf3", width=1),
            ),
        ),
    )
    render_pyecharts(rate_line)

    st.markdown("---")
    st.subheader("🏢 每日各系统明细")
    display_summary = daily_system.copy()
    display_summary["入表日期"] = display_summary["入表日期"].astype(str)
    display_summary["实际使用率"] = display_summary["实际使用率"].map(lambda value: f"{value:.1f}%")
    display_summary["实际成功率"] = display_summary["实际成功率"].map(lambda value: f"{value:.1f}%")
    display_summary["实际失败率"] = display_summary["实际失败率"].map(lambda value: f"{value:.1f}%")
    st.dataframe(display_summary, use_container_width=True, hide_index=True)

    st.markdown("---")
    st.subheader("❌ 失败原因占比")
    failed_df = filtered[filtered["上传失败"]].copy()
    if failed_df.empty:
        st.success("当前筛选范围内没有自动上传失败任务。")
    else:
        failed_df["失败原因"] = failed_df["备注"].map(_classify_upload_failure_reason)
        reason_stats = failed_df["失败原因"].value_counts().rename_axis("失败原因").reset_index(name="失败数")
        reason_stats["占比"] = reason_stats["失败数"].div(reason_stats["失败数"].sum()).mul(100)
        chart_col, table_col = st.columns([3, 2])
        with chart_col:
            reason_pie = Pie(init_opts=opts.InitOpts(height="400px"))
            reason_pie.add(
                "失败原因",
                [list(item) for item in reason_stats[["失败原因", "失败数"]].itertuples(index=False, name=None)],
                radius=["38%", "68%"],
            )
            reason_pie.set_global_opts(legend_opts=opts.LegendOpts(type_="scroll", orient="vertical", pos_left="2%"))
            reason_pie.set_series_opts(label_opts=opts.LabelOpts(formatter="{b}: {d}%"))
            render_pyecharts(reason_pie)
        with table_col:
            reason_display = reason_stats.copy()
            reason_display["占比"] = reason_display["占比"].map(lambda value: f"{value:.1f}%")
            st.dataframe(reason_display, use_container_width=True, hide_index=True)
        with st.expander(f"查看 {len(failed_df)} 条上传失败明细"):
            st.dataframe(
                failed_df[["入表日期", "系统", "剧名", "频道id", "失败原因", "备注"]]
                .sort_values(["入表日期", "系统"], ascending=[False, True]),
                use_container_width=True,
                hide_index=True,
            )


def get_current_cycle() -> str:
    """获取当前生产周期（每周五为周期开始）

    当前周期 = 本周五（无论今天是周几）
    - 今天是周一到周四：当前周期 = 本周五
    - 今天是周五到周日：当前周期 = 今天（周五）
    """
    today = get_local_now()

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


def get_recent_cycles(count: int = 4) -> list:
    """获取最近 count 个生产周期（含当前周期）"""
    current_date = datetime.strptime(get_current_cycle(), "%Y%m%d")
    return [
        (current_date - timedelta(days=7 * index)).strftime("%Y%m%d")
        for index in range(count)
    ]


def format_recent_cycle_option(cycle: str) -> str:
    """格式化最近周期快捷选项"""
    if not cycle:
        return "不使用快捷选择"

    recent_cycles = get_recent_cycles()
    if cycle in recent_cycles:
        index = recent_cycles.index(cycle)
        if index == 0:
            return f"{cycle}（当前周期）"
        return f"{cycle}（上{index}周期）"

    return cycle


def is_monday_10am() -> bool:
    """判断是否是周一早上10点左右"""
    now = get_local_now()
    return now.weekday() == 0 and 9 <= now.hour <= 11


def render_daily_report_tab(df: pd.DataFrame):
    """渲染每日生产日报标签页"""
    st.header("📋 每日生产日报")

    if df.empty:
        st.warning("暂无数据")
        return

    now = get_local_now()
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

    # ===== 发送日报按钮 =====
    st.subheader("📤 发送日报")

    # 使用 session_state 保存状态
    if "show_preview" not in st.session_state:
        st.session_state.show_preview = False
    if "preview_content" not in st.session_state:
        st.session_state.preview_content = ""

    # 预览按钮
    if st.button("👁️ 预览日报内容", key="preview_daily_report_btn"):
        st.session_state.show_preview = True
        st.session_state.preview_content = build_daily_report_message(df, current_cycle)

    # 显示预览内容
    if st.session_state.show_preview and st.session_state.preview_content:
        st.markdown("---")
        st.subheader("📋 预览内容（可编辑）")
        st.caption("💡 可以直接在下方文本框中修改内容，修改后点击确认发送")
        edited_content = st.text_area("日报内容", st.session_state.preview_content, height=400, key="preview_text_area")

        col_confirm, col_cancel = st.columns(2)
        with col_confirm:
            if st.button("✅ 确认发送", key="confirm_send_btn"):
                with st.spinner("正在发送日报..."):
                    # 使用编辑后的内容发送
                    success, msg = send_custom_report_to_group(edited_content)
                    if success:
                        st.success(msg)
                        st.session_state.show_preview = False
                        st.session_state.preview_content = ""
                    else:
                        st.error(msg)
        with col_cancel:
            if st.button("❌ 取消", key="cancel_send_btn"):
                st.session_state.show_preview = False
                st.session_state.preview_content = ""
                st.rerun()

    st.markdown("---")

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

    # ===== 失败类型分析 =====
    st.subheader("📈 失败类型分析")

    # 获取所有周期
    all_cycles = df["生产周期"].dropna().unique().tolist()
    all_cycles = sorted([c for c in all_cycles if c])

    if all_cycles:
        # 统计每个周期的失败类型
        cycle_fail_stats = []

        for cycle in all_cycles:
            cycle_df = df[df["生产周期"] == cycle]
            # 筛选失败记录
            cycle_failed = cycle_df[cycle_df["生产状态"].isin(["失败", "处理BGM失败"])]

            if cycle_failed.empty:
                continue

            # 统计失败类型
            fail_type_counts = cycle_failed["失败类型"].value_counts()

            total_fail = len(cycle_failed)
            top_fail_type = fail_type_counts.index[0] if len(fail_type_counts) > 0 else "未知"
            top_fail_count = fail_type_counts.iloc[0] if len(fail_type_counts) > 0 else 0
            top_fail_rate = (top_fail_count / total_fail * 100) if total_fail > 0 else 0

            cycle_fail_stats.append({
                "周期": cycle,
                "失败总数": total_fail,
                "主要失败类型": f"{top_fail_type} ({top_fail_count}部, {top_fail_rate:.1f}%)",
                "失败类型分布": fail_type_counts.to_dict()
            })

        if cycle_fail_stats:
            # 显示表格
            fail_stats_df = pd.DataFrame(cycle_fail_stats)
            display_df = fail_stats_df[["周期", "失败总数", "主要失败类型"]].copy()
            st.dataframe(display_df, use_container_width=True, hide_index=True)

            # 绘制失败类型趋势图
            st.markdown("**各周期失败类型分布**")

            # 获取所有失败类型
            all_fail_types = set()
            for stat in cycle_fail_stats:
                all_fail_types.update(stat["失败类型分布"].keys())
            all_fail_types = sorted([t for t in all_fail_types if t])

            # 构建每个失败类型的趋势数据
            cycles_list = [s["周期"] for s in cycle_fail_stats]

            # 使用堆叠柱状图
            bar = Bar(init_opts=opts.InitOpts(width="100%", height="400px", theme="light"))
            bar.add_xaxis(cycles_list)

            # 预定义颜色列表
            colors = ["#ee6666", "#fac858", "#73c0de", "#9a60b4", "#fc8452", "#3ba272", "#5470c6", "#91cc75"]

            # 为每种失败类型添加数据
            for idx, fail_type in enumerate(all_fail_types):
                values = []
                for stat in cycle_fail_stats:
                    values.append(stat["失败类型分布"].get(fail_type, 0))

                color = colors[idx % len(colors)]
                bar.add_yaxis(
                    series_name=fail_type,
                    y_axis=values,
                    stack="总量",
                    label_opts=opts.LabelOpts(is_show=False),
                    itemstyle_opts=opts.ItemStyleOpts(color=color),
                )

            bar.set_global_opts(
                title_opts=opts.TitleOpts(title=""),
                xaxis_opts=opts.AxisOpts(axislabel_opts=opts.LabelOpts(rotate=30)),
                yaxis_opts=opts.AxisOpts(name="失败数"),
                tooltip_opts=opts.TooltipOpts(trigger="axis", axis_pointer_type="shadow"),
                legend_opts=opts.LegendOpts(pos_top="top", type_="scroll"),
            )
            render_pyecharts(bar)

        else:
            st.info("各周期无失败记录")
    else:
        st.info("暂无周期数据")

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

    # 1. 失败记录（未处理的失败）
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

    # 2. 失败处理中记录（正在处理的失败）
    handling_df = df[df["生产状态"] == "失败处理中"].copy()
    if not handling_df.empty:
        for _, row in handling_df.iterrows():
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
                "问题类型": "失败处理中",
                "失败类型": row.get("失败类型") or "-",
                "异常原因": "正在处理",
                "备注": row.get("备注", ""),
                "入表时间": entry_time,
                "遗留时长": pending_str,
                "遗留小时": pending_hours if pd.notna(entry_time) else 999999,
            })

    # 3. 整备异常记录（未开始且整备状态或NAS位置为空）
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

                # 统计各类问题数量
                fail_count = len(cycle_issues[cycle_issues["问题类型"] == "失败"])
                handling_count = len(cycle_issues[cycle_issues["问题类型"] == "失败处理中"])
                abnormal_count = len(cycle_issues[cycle_issues["问题类型"] == "整备异常"])

                cycle_info = f"📅 周期 {cycle_label}"
                if fail_count > 0:
                    cycle_info += f" | 失败 {fail_count}条"
                if handling_count > 0:
                    cycle_info += f" | 失败处理中 {handling_count}条"
                if abnormal_count > 0:
                    cycle_info += f" | 异常 {abnormal_count}条"

                st.markdown(f"**{cycle_info}**")

                # 显示该周期的问题记录
                display_df = cycle_issues[["剧名", "问题类型", "失败类型", "异常原因", "遗留时长", "备注", "遗留小时"]].copy()
                display_df = display_df.sort_values("遗留小时", ascending=False)
                display_df = display_df.drop(columns=["遗留小时"])

                # 高亮显示：失败=红色，失败处理中=蓝色，整备异常=黄色
                def highlight_row(row):
                    problem_type = row["问题类型"]
                    if problem_type == "失败":
                        return ["background-color: #ffebee"] * len(row)  # 红色
                    elif problem_type == "失败处理中":
                        return ["background-color: #e3f2fd"] * len(row)  # 蓝色
                    else:
                        return ["background-color: #fff8e1"] * len(row)  # 黄色

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

        # 失败记录（未处理）
        failed_count = len(system_df[system_df["生产状态"].isin(["失败", "处理BGM失败"])])
        # 失败处理中记录
        handling_count = len(system_df[system_df["生产状态"] == "失败处理中"])
        # 整备异常记录
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
            "失败": failed_count,
            "失败处理中": handling_count,
            "整备异常": abnormal_count,
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
            subset=['失败', '失败处理中', '整备异常']
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


def render_dubbing_tab(production_df: pd.DataFrame):
    """渲染配音情况统计标签页"""
    st.header("🎤 配音情况统计")

    # 提示用户数据范围
    st.info("📌 为提升加载速度，配音数据仅显示最近四个生产周期的记录")

    if production_df.empty:
        st.warning("暂无数据")
        return

    # ===== 周期筛选 =====
    st.subheader("📅 周期筛选")

    # 获取所有周期
    all_cycles = production_df["当前制作周期"].dropna().unique().tolist()
    all_cycles = sorted([c for c in all_cycles if c], reverse=True)  # 按周期降序

    # 周期选择器
    selected_cycle = st.selectbox(
        "选择制作周期",
        options=["全部周期"] + all_cycles,
        key="dubbing_cycle_filter"
    )

    # 根据选择筛选数据
    if selected_cycle == "全部周期":
        df_filtered = production_df
    else:
        df_filtered = production_df[production_df["当前制作周期"] == selected_cycle]

    # 配音完成定义：当前状态 = 已完成 或 待检查者确认
    completed_statuses = ["已完成", "待检查者确认"]
    # 待配音定义：当前状态 = 失败 或 未开始
    pending_statuses = ["失败", "未开始"]
    # 用于存储未完成周期
    incomplete_cycles = []

    st.markdown("---")

    # ===== 总体概览 =====
    st.subheader("📊 总体概览")

    total_count = len(df_filtered)
    dubbing_completed = len(df_filtered[df_filtered["当前状态"].isin(completed_statuses)])
    dubbing_pending = len(df_filtered[df_filtered["当前状态"].isin(pending_statuses)])
    dubbing_other = total_count - dubbing_completed - dubbing_pending  # 其他状态
    completion_rate = (dubbing_completed / total_count * 100) if total_count > 0 else 0

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.markdown(create_kpi_card("总记录数", total_count, color="#5470c6"), unsafe_allow_html=True)
    with col2:
        st.markdown(create_kpi_card("配音完成", dubbing_completed, color="#91cc75"), unsafe_allow_html=True)
    with col3:
        st.markdown(create_kpi_card("待配音", dubbing_pending, color="#fac858"), unsafe_allow_html=True)
    with col4:
        st.markdown(create_kpi_card("完成率", f"{completion_rate:.1f}%", color="#73c0de"), unsafe_allow_html=True)

    # 显示各状态分布
    if dubbing_other > 0:
        st.caption(f"📋 其他状态记录: {dubbing_other}条（非配音完成也非待配音）")

    st.markdown("---")

    # ===== 制作耗时统计 =====
    st.subheader("⏱️ 制作耗时统计")

    # 计算制作耗时
    dubbing_time_stats = calculate_dubbing_production_time(df_filtered)

    col_left, col_right = st.columns(2)

    with col_left:
        # 显示平均制作耗时卡片
        if dubbing_time_stats["avg_time"] is not None:
            avg_time_str = f"{dubbing_time_stats['avg_time']:.1f}h"
            st.markdown(
                create_kpi_card(
                    "平均制作耗时",
                    avg_time_str,
                    color="#9a60b4"
                ),
                unsafe_allow_html=True
            )
            st.caption(f"📊 基于 {dubbing_time_stats['valid_count']} 条有效记录统计")
            st.caption("📋 筛选条件：制作耗时小时不为空且不为0")
        else:
            st.markdown(
                create_kpi_card("平均制作耗时", "暂无数据", color="#9a60b4"),
                unsafe_allow_html=True
            )
            st.caption("📋 需要有制作耗时小时字段的有效数据")

    with col_right:
        # 显示各周期制作耗时折线图
        if not dubbing_time_stats["cycle_stats"].empty:
            cycle_stats_df = dubbing_time_stats["cycle_stats"]
            cycles = cycle_stats_df["当前制作周期"].tolist()
            avg_times = cycle_stats_df["平均制作耗时"].tolist()
            counts = cycle_stats_df["记录数"].tolist()

            line = (
                Line(init_opts=opts.InitOpts(width="100%", height="300px", theme="light"))
                .add_xaxis(cycles)
                .add_yaxis(
                    series_name="平均制作耗时",
                    y_axis=[round(t, 1) for t in avg_times],
                    symbol="circle",
                    symbol_size=8,
                    linestyle_opts=opts.LineStyleOpts(width=2, color="#9a60b4"),
                    itemstyle_opts=opts.ItemStyleOpts(color="#9a60b4"),
                    label_opts=opts.LabelOpts(is_show=True, position="top", formatter="{c}h"),
                )
                .set_global_opts(
                    title_opts=opts.TitleOpts(title="各周期平均制作耗时"),
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
            st.info("暂无各周期制作耗时数据")

    st.markdown("---")

    # ===== 各系统各周期制作耗时 =====
    if not dubbing_time_stats["system_cycle_stats"].empty:
        st.subheader("📊 各系统各周期制作耗时")

        system_cycle_df = dubbing_time_stats["system_cycle_stats"]

        # 数据透视表：行为周期，列为系统
        pivot_df = system_cycle_df.pivot_table(
            index="当前制作周期",
            columns="系统",
            values="平均制作耗时",
            aggfunc="mean"
        ).reset_index()

        # 显示表格
        display_df = pivot_df.round(1)
        st.dataframe(display_df, use_container_width=True, hide_index=True)

        # 绘制各系统折线图
        systems_in_data = [col for col in pivot_df.columns if col != "当前制作周期"]

        if systems_in_data:
            line = Line(init_opts=opts.InitOpts(width="100%", height="400px", theme="light"))
            line.add_xaxis(pivot_df["当前制作周期"].tolist())

            # 定义颜色
            system_colors = {
                "众益": "#5470c6", "点众": "#91cc75", "红果": "#ee6666",
                "掌阅": "#73c0de", "外部制作": "#9a60b4", "ReelShort": "#fc8452"
            }

            for sys_name in systems_in_data:
                color = system_colors.get(sys_name, "#999")
                values = pivot_df[sys_name].tolist()
                # 替换NaN为None
                values = [round(v, 1) if pd.notna(v) else None for v in values]

                line.add_yaxis(
                    series_name=sys_name,
                    y_axis=values,
                    symbol="circle",
                    symbol_size=6,
                    linestyle_opts=opts.LineStyleOpts(width=2, color=color),
                    itemstyle_opts=opts.ItemStyleOpts(color=color),
                    label_opts=opts.LabelOpts(is_show=False),
                )

            line.set_global_opts(
                title_opts=opts.TitleOpts(title=""),
                xaxis_opts=opts.AxisOpts(axislabel_opts=opts.LabelOpts(rotate=30)),
                yaxis_opts=opts.AxisOpts(name="小时"),
                tooltip_opts=opts.TooltipOpts(trigger="axis"),
                legend_opts=opts.LegendOpts(pos_top="top", type_="scroll"),
            )
            render_pyecharts(line)

    st.markdown("---")

    # ===== 各系统配音情况 =====
    st.subheader("🏢 各系统配音情况")

    # 按系统统计
    system_stats = []
    for system in SYSTEMS:
        system_df = df_filtered[df_filtered["系统"] == system]
        if system_df.empty:
            continue

        sys_total = len(system_df)
        sys_completed = len(system_df[system_df["当前状态"].isin(completed_statuses)])
        sys_pending = len(system_df[system_df["当前状态"].isin(pending_statuses)])
        sys_rate = (sys_completed / sys_total * 100) if sys_total > 0 else 0

        system_stats.append({
            "系统": system,
            "总数": sys_total,
            "配音完成": sys_completed,
            "待配音": sys_pending,
            "完成率": f"{sys_rate:.1f}%"
        })

    if system_stats:
        system_stats_df = pd.DataFrame(system_stats)

        # 显示表格
        st.dataframe(system_stats_df, use_container_width=True, hide_index=True)

        # 绘制堆叠柱状图
        systems = [s["系统"] for s in system_stats]
        completed_counts = [s["配音完成"] for s in system_stats]
        pending_counts = [s["待配音"] for s in system_stats]

        bar = (
            Bar(init_opts=opts.InitOpts(width="100%", height="400px", theme="light"))
            .add_xaxis(systems)
            .add_yaxis(
                series_name="配音完成",
                y_axis=completed_counts,
                stack="总量",
                label_opts=opts.LabelOpts(is_show=True, position="inside"),
                itemstyle_opts=opts.ItemStyleOpts(color="#91cc75"),
            )
            .add_yaxis(
                series_name="待配音",
                y_axis=pending_counts,
                stack="总量",
                label_opts=opts.LabelOpts(is_show=True, position="inside"),
                itemstyle_opts=opts.ItemStyleOpts(color="#fac858"),
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

    st.markdown("---")

    # ===== 按周期统计配音情况 =====
    if selected_cycle == "全部周期" and all_cycles:
        st.subheader("📈 各周期配音完成趋势")

        # 按周期统计
        cycle_stats = []

        for cycle in sorted(all_cycles):
            cycle_df = df_filtered[df_filtered["当前制作周期"] == cycle]
            if cycle_df.empty:
                continue

            cycle_total = len(cycle_df)
            cycle_completed = len(cycle_df[cycle_df["当前状态"].isin(completed_statuses)])
            cycle_pending = len(cycle_df[cycle_df["当前状态"].isin(pending_statuses)])
            cycle_rate = (cycle_completed / cycle_total * 100) if cycle_total > 0 else 0

            cycle_stats.append({
                "周期": cycle,
                "总数": cycle_total,
                "配音完成": cycle_completed,
                "待配音": cycle_pending,
                "完成率": round(cycle_rate, 1)
            })

            # 记录未完成的周期
            if cycle_completed < cycle_total:
                incomplete_cycles.append(cycle)

        if cycle_stats:
            cycle_stats_df = pd.DataFrame(cycle_stats)

            # 折线图
            cycles = cycle_stats_df["周期"].tolist()
            totals = cycle_stats_df["总数"].tolist()
            completed = cycle_stats_df["配音完成"].tolist()
            rates = cycle_stats_df["完成率"].tolist()

            line = (
                Line(init_opts=opts.InitOpts(width="100%", height="400px", theme="light"))
                .add_xaxis(cycles)
                .add_yaxis(
                    series_name="总数",
                    y_axis=totals,
                    symbol="circle",
                    symbol_size=8,
                    linestyle_opts=opts.LineStyleOpts(width=2, color="#5470c6"),
                    itemstyle_opts=opts.ItemStyleOpts(color="#5470c6"),
                    label_opts=opts.LabelOpts(is_show=True, position="top"),
                )
                .add_yaxis(
                    series_name="配音完成",
                    y_axis=completed,
                    symbol="diamond",
                    symbol_size=8,
                    linestyle_opts=opts.LineStyleOpts(width=2, color="#91cc75"),
                    itemstyle_opts=opts.ItemStyleOpts(color="#91cc75"),
                    label_opts=opts.LabelOpts(is_show=True, position="top"),
                )
                .set_global_opts(
                    title_opts=opts.TitleOpts(title=""),
                    xaxis_opts=opts.AxisOpts(axislabel_opts=opts.LabelOpts(rotate=30)),
                    yaxis_opts=opts.AxisOpts(name="数量"),
                    tooltip_opts=opts.TooltipOpts(trigger="axis"),
                    legend_opts=opts.LegendOpts(pos_top="top"),
                )
            )
            render_pyecharts(line)

            # 显示未完成周期的统计表
            incomplete_stats = cycle_stats_df[cycle_stats_df["配音完成"] < cycle_stats_df["总数"]]
            if not incomplete_stats.empty:
                st.markdown("---")
                st.subheader("⚠️ 未完成周期统计")
                st.caption("以下周期的配音完成数不等于总数，需要关注")

                # 高亮显示未完成周期
                def highlight_incomplete(row):
                    return ['background-color: #fff3cd' if row['配音完成'] < row['总数'] else '' for _ in row]

                styled_stats = incomplete_stats.style.apply(highlight_incomplete, axis=1)
                st.dataframe(styled_stats, use_container_width=True, hide_index=True)

    st.markdown("---")

    # ===== 历史周期未完成记录明细 =====
    if selected_cycle == "全部周期" and incomplete_cycles:
        st.subheader("🔴 历史周期未完成配音记录")
        st.caption("以下记录位于历史周期但配音未完成，需要特别关注")

        # 获取所有历史周期（排除当前周期）中未完成的记录
        current_cycle = get_current_cycle()

        # 筛选历史周期且未完成配音的记录
        history_incomplete_df = df_filtered[
            (df_filtered["当前制作周期"].isin(incomplete_cycles)) &
            (~df_filtered["当前状态"].isin(completed_statuses))
        ].copy()

        if history_incomplete_df.empty:
            st.success("✅ 历史周期无未完成记录")
        else:
            # 按周期分组展示
            for cycle in sorted(incomplete_cycles):
                cycle_incomplete = history_incomplete_df[history_incomplete_df["当前制作周期"] == cycle]
                if cycle_incomplete.empty:
                    continue

                # 标记是否为历史周期
                is_history = cycle < current_cycle
                cycle_label = f"📅 周期 {cycle}"
                if is_history:
                    cycle_label += " ⚠️ 历史周期"

                st.markdown(f"### {cycle_label} ({len(cycle_incomplete)}条)")

                # 按系统分组
                for system in SYSTEMS:
                    system_records = cycle_incomplete[cycle_incomplete["系统"] == system]
                    if system_records.empty:
                        continue

                    st.markdown(f"**{system}** ({len(system_records)}条)")

                    display_cols = ["剧名", "当前状态", "语言", "制作备注"]
                    available_cols = [col for col in display_cols if col in system_records.columns]
                    st.dataframe(
                        system_records[available_cols],
                        use_container_width=True,
                        hide_index=True
                    )

                st.markdown("")

            # 导出功能
            csv = history_incomplete_df.to_csv(index=False).encode('utf-8-sig')
            st.download_button(
                label="📥 导出历史周期未完成记录(CSV)",
                data=csv,
                file_name=f"历史周期未完成配音_{get_local_now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv"
            )

    st.markdown("---")

    # ===== 待配音明细 =====
    st.subheader("📋 待配音明细")

    pending_df = df_filtered[df_filtered["当前状态"].isin(pending_statuses)].copy()

    if pending_df.empty:
        st.success("✅ 当前没有待配音记录")
    else:
        # 按系统分组展示
        for system in SYSTEMS:
            system_pending = pending_df[pending_df["系统"] == system]
            if system_pending.empty:
                continue

            st.markdown(f"### 🏢 {system} ({len(system_pending)}条)")

            display_cols = ["剧名", "当前状态", "当前制作周期", "语言", "制作备注"]
            available_cols = [col for col in display_cols if col in system_pending.columns]
            st.dataframe(
                system_pending[available_cols].sort_values("当前制作周期"),
                use_container_width=True,
                hide_index=True
            )
            st.markdown("")

        # 导出功能
        csv = pending_df.to_csv(index=False).encode('utf-8-sig')
        st.download_button(
            label="📥 导出待配音明细(CSV)",
            data=csv,
            file_name=f"待配音明细_{get_local_now().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv"
        )


def main():
    """主函数"""
    st.set_page_config(
        page_title="生产监控面板",
        page_icon="🎬",
        layout="wide"
    )

    start_auto_daily_report_scheduler()

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

    # 生产周期快捷选择 + 手动输入
    st.sidebar.markdown("### 生产周期")
    recent_cycle_options = get_recent_cycles()
    quick_cycle = st.sidebar.selectbox(
        "快捷选择最近周期",
        options=[""] + recent_cycle_options,
        format_func=format_recent_cycle_option,
        key="quick_cycle_select",
        help="选择后优先按该周期筛选"
    )
    manual_cycle = st.sidebar.text_input(
        "输入生产周期",
        value="",
        placeholder="例如: 20260501",
        key="cycle_input",
        help="不选择快捷周期时生效，留空显示全部数据，格式: YYYYMMDD"
    )
    selected_cycle = quick_cycle or manual_cycle.strip()

    # 刷新按钮
    if st.sidebar.button("🔄 刷新数据", key="refresh_data_btn"):
        st.cache_data.clear()
        st.rerun()

    st.sidebar.markdown("---")
    st.sidebar.markdown("### 📝 说明")
    st.sidebar.markdown("- 留空显示全部数据")
    st.sidebar.markdown("- 快捷选择包含最近四个生产周期")
    st.sidebar.markdown("- 选择快捷周期后优先使用下拉值")
    st.sidebar.markdown("- 格式: YYYYMMDD")
    st.sidebar.markdown("- 如: 20260501")
    st.sidebar.markdown("- 自动日报：周一/周五 19:00 发送")

    # ===== 主内容区域 =====
    st.title("🎬 生产监控面板")
    st.markdown(f"**最后更新时间**: {get_local_now().strftime('%Y-%m-%d %H:%M:%S')}")

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

    # # 拉取剧制作表数据（用于配音情况统计）
    # with st.spinner("正在拉取配音数据..."):
    #     production_df = fetch_all_systems_production_data()

    # 标签页：增加自动上传统计大盘
    tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8 = st.tabs([
        "📊 概览", "🏢 系统对比", "⏱️ 实时监控", "📦 今日入库", "📋 生产日报", "🎤 配音情况", "🚀 自动上传统计", "📅 生产任务排期"
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

    # with tab6:
    #     render_dubbing_tab(production_df)

    with tab7:
        with st.spinner("正在拉取各系统上传表数据..."):
            upload_df = fetch_upload_statistics_data()
        render_upload_statistics_tab(upload_df)

    with tab8:
        render_production_schedule_tab(df)


if __name__ == "__main__":
    main()
