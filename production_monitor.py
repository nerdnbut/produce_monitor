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
import requests
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
from pyecharts.charts import Bar, Pie, Line, Grid
from pyecharts import options as opts
from pyecharts.commons.utils import JsCode
from pyecharts.components import Table
import streamlit.components.v1 as components

# 系统配置
SYSTEMS = ["点众", "红果", "外部制作", "众益"]
# 识别生产电脑清单。产量图表始终展示这些电脑，没有记录时按 0 统计。
RECOGNITION_MACHINE_IDS = [
    "990", "991", "992", "993", "994", "995", "996", "998",
    "1001", "1002", "1003", "1004", "1005", "1006", "1007", "1008",
    "1009", "1011", "1012", "1013", "1014",
]
PANEL_RECENT_CYCLE_COUNT = 5
DASHBOARD_CACHE_TTL_SECONDS = 30 * 60
AUTO_UPLOAD_COORDINATOR_URL = os.environ.get(
    "AUTO_UPLOAD_COORDINATOR_URL",
    "http://127.0.0.1:8899",
).rstrip("/")

# 生产日报自动发送配置：周一/周五 19 点发送一次
DAILY_REPORT_CHAT_ID = "oc_471f224b62b9acad8ffc4433cc687add"
AUTO_REPORT_WEEKDAYS = {0, 4}
AUTO_REPORT_HOUR = 19
AUTO_REPORT_CHECK_INTERVAL_SECONDS = 60
AUTO_REPORT_STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "auto_daily_report_state.json"
)
# 生产任务告警发送给小柯，参考 factory/auto_produce.py 的 send_to_xiaoke。
RECOGNITION_ALERT_CHAT_ID = "dbe571bc"
RECOGNITION_ALERT_RECEIVE_ID_TYPE = "user_id"
RECOGNITION_ALERT_STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "recognition_failure_alert_state.json"
)
RECOGNITION_FAILURE_WAIT_HOURS = 24
PRODUCTION_EARLY_STAGE_ALERT_HOURS = 24
PRODUCTION_NOT_COMPLETE_ALERT_HOURS = 48
RECOGNITION_ALERT_COOLDOWN_HOURS = 6
PRODUCTION_EARLY_STAGES = ["未开始", "合并视频", "识别字幕", "识别角色"]
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
    "剧名", "语言", "当前状态", "当前制作周期", "申请日期",
    "需求提交时间", "整备完成时间", "制作完成时间", "制作总耗时"
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

# 生产数据分析以数据中的最新生产周期为起点，向前统计 10 个周期。
PRODUCTION_ANALYSIS_CYCLE_COUNT = 10

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


def render_pyecharts(chart, height=400):
    """渲染 pyecharts 图表到 Streamlit"""
    components.html(chart.render_embed(), height=height, scrolling=False)


def _build_cycle_filter_conditions(field_name: str, recent_cycles: tuple) -> list:
    """构建多个生产周期的 OR 查询条件，避免从飞书全量拉取后再筛选。"""
    return [
        Condition.builder()
        .field_name(field_name)
        .operator("is")
        .value([cycle])
        .build()
        for cycle in recent_cycles
    ]


@st.cache_data(ttl=DASHBOARD_CACHE_TTL_SECONDS, show_spinner=False)
def fetch_recognition_data(system_name: str, recent_cycles: tuple) -> list:
    """
    从指定系统的剧识别表拉取最近若干生产周期的数据。
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
            filter_conditions=_build_cycle_filter_conditions(
                "生产周期", recent_cycles
            ),
            filter_conjunction="or",
        )

        for record in records:
            record["系统"] = system_name

        return records

    except Exception as e:
        st.error(f"拉取 [{system_name}] 数据失败: {e}")
        return []


@st.cache_data(ttl=DASHBOARD_CACHE_TTL_SECONDS, show_spinner=False)
def fetch_production_data(system_name: str, recent_cycles: tuple) -> list:
    """
    从指定系统的剧制作表-新拉取最近若干制作周期的数据。
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
        production_fields = [
            field_name for field_name in PRODUCTION_FIELDS
            if not (system_name == "外部制作" and field_name == "申请日期")
        ]

        records = client.search_all_records(
            app_token=app_token,
            table_id=production_table_id,
            field_names=production_fields,
            filter_conditions=_build_cycle_filter_conditions(
                "当前制作周期", recent_cycles
            ),
            filter_conjunction="or",
        )

        for record in records:
            record["系统"] = system_name

        return records

    except Exception as e:
        import traceback
        print(f"拉取 [{system_name}] 剧制作表数据失败: {e}")
        print(traceback.format_exc())
        return []


@st.cache_data(ttl=DASHBOARD_CACHE_TTL_SECONDS, show_spinner=False)
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


@st.cache_data(ttl=10, show_spinner=False)
def fetch_auto_upload_client_status() -> dict:
    """读取上传协调服务记录的在线客户端状态。"""
    status_url = f"{AUTO_UPLOAD_COORDINATOR_URL}/api/status"
    try:
        response = requests.get(status_url, timeout=3)
        response.raise_for_status()
        payload = response.json()
        if "online_client_count" not in payload:
            return {
                "supported": False,
                "online_count": None,
                "clients": [],
                "error": "协调服务尚未提供在线客户端状态，请重启协调服务。",
            }

        clients = payload.get("online_clients") or []
        return {
            "supported": True,
            "online_count": int(payload.get("online_client_count", len(clients)) or 0),
            "clients": clients,
            "timeout_seconds": payload.get("client_online_timeout_seconds"),
            "error": "",
        }
    except Exception as exc:
        return {
            "supported": False,
            "online_count": None,
            "clients": [],
            "error": f"无法连接上传协调服务：{exc}",
        }


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
    recent_cycles = tuple(get_recent_cycles(PANEL_RECENT_CYCLE_COUNT))

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
                filter_conditions=_build_cycle_filter_conditions(
                    "生产周期", recent_cycles
                ),
                filter_conjunction="or",
            )

            for record in records:
                record["系统"] = system_name

            all_records.extend(records)

        except Exception as e:
            print(f"自动日报拉取 [{system_name}] 数据失败: {e}")

    return _recognition_records_to_dataframe(all_records)


@st.cache_data(ttl=DASHBOARD_CACHE_TTL_SECONDS, show_spinner=False)
def fetch_all_systems_data() -> pd.DataFrame:
    """
    拉取所有系统最近 5 个周期的剧识别表数据。
    """
    all_records = []
    recent_cycles = tuple(get_recent_cycles(PANEL_RECENT_CYCLE_COUNT))

    for system_name in SYSTEMS:
        records = fetch_recognition_data(system_name, recent_cycles)
        all_records.extend(records)

    return _recognition_records_to_dataframe(all_records)


@st.cache_data(ttl=DASHBOARD_CACHE_TTL_SECONDS, show_spinner=False)
def fetch_all_systems_production_data() -> pd.DataFrame:
    """
    拉取所有系统最近 5 个制作周期的剧制作表-新数据。
    """
    all_records = []
    recent_cycles = tuple(get_recent_cycles(PANEL_RECENT_CYCLE_COUNT))

    for system_name in SYSTEMS:
        records = fetch_production_data(system_name, recent_cycles)
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
            "语言": _extract_text(fields.get("语言")),
            "当前状态": _extract_text(fields.get("当前状态")),
            "当前制作周期": _extract_text(fields.get("当前制作周期")),
            "申请日期": _parse_timestamp(fields.get("申请日期")),
            "需求提交时间": _parse_timestamp(fields.get("需求提交时间")),
            "整备完成时间": _parse_timestamp(fields.get("整备完成时间")),
            "制作完成时间": _parse_timestamp(fields.get("制作完成时间")),
            "制作总耗时": fields.get("制作总耗时"),
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
    - BGM处理耗时：BGM处理结束时间 - 识别角色结束时间（必须两个值都有）
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

    # 计算BGM处理耗时（处理BGM结束时间 - 识别角色结束时间）
    completed_df["BGM耗时"] = completed_df.apply(
        lambda row: (row["处理BGM结束时间"] - row["识别角色结束时间"]).total_seconds() / 3600
        if pd.notna(row["处理BGM结束时间"]) and pd.notna(row["识别角色结束时间"])
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


def _get_complete_overview_duration_records(df: pd.DataFrame) -> pd.DataFrame:
    """返回概览耗时指标共用的完整、有效生产链路记录。"""
    time_columns = [
        "入表时间", "开始生产时间", "识别角色结束时间",
        "BGM开始处理时间", "处理BGM结束时间"
    ]
    valid_df = df[(df["生产状态"] == "完成") & df[time_columns].notna().all(axis=1)].copy()
    if valid_df.empty:
        return valid_df
    for column in time_columns:
        valid_df[column] = pd.to_datetime(valid_df[column], errors="coerce")
    valid_df = valid_df[valid_df[time_columns].notna().all(axis=1)].copy()
    valid_df["识别角色耗时"] = (
        valid_df["识别角色结束时间"] - valid_df["入表时间"]
    ).dt.total_seconds() / 3600
    valid_df["BGM处理耗时"] = (
        valid_df["处理BGM结束时间"] - valid_df["识别角色结束时间"]
    ).dt.total_seconds() / 3600
    valid_df["生产总耗时_新口径"] = (
        valid_df["处理BGM结束时间"] - valid_df["入表时间"]
    ).dt.total_seconds() / 3600
    valid_df["识别角色等待耗时"] = (
        valid_df["开始生产时间"] - valid_df["入表时间"]
    ).dt.total_seconds() / 3600
    valid_df["处理BGM等待耗时"] = (
        valid_df["BGM开始处理时间"] - valid_df["识别角色结束时间"]
    ).dt.total_seconds() / 3600
    valid_df["识别角色实际耗时"] = (
        valid_df["识别角色结束时间"] - valid_df["开始生产时间"]
    ).dt.total_seconds() / 3600
    valid_df["处理BGM实际耗时"] = (
        valid_df["处理BGM结束时间"] - valid_df["BGM开始处理时间"]
    ).dt.total_seconds() / 3600
    metrics = [
        "识别角色耗时", "BGM处理耗时", "生产总耗时_新口径",
        "识别角色等待耗时", "处理BGM等待耗时",
        "识别角色实际耗时", "处理BGM实际耗时"
    ]
    return valid_df[((valid_df[metrics] >= 0) & (valid_df[metrics] < 1000)).all(axis=1)].copy()


def calculate_avg_waiting_times(df: pd.DataFrame) -> dict:
    """计算概览完整链路记录的两个阶段平均等待耗时。"""
    valid_df = _get_complete_overview_duration_records(df)
    if valid_df.empty:
        return {"avg_recognition_wait": None, "avg_bgm_wait": None, "valid_count": 0}
    return {
        "avg_recognition_wait": valid_df["识别角色等待耗时"].mean(),
        "avg_bgm_wait": valid_df["处理BGM等待耗时"].mean(),
        "valid_count": len(valid_df),
    }


def calculate_avg_actual_times(df: pd.DataFrame) -> dict:
    """计算概览完整链路记录的两个阶段平均实际处理耗时。"""
    valid_df = _get_complete_overview_duration_records(df)
    if valid_df.empty:
        return {
            "avg_recognition_actual": None, "avg_recognition_actual_no_error": None,
            "avg_recognition_actual_with_error": None, "no_error_count": 0,
            "with_error_count": 0, "avg_bgm_actual": None, "valid_count": 0,
        }
    no_error_mask = valid_df["失败类型"].isna() | (valid_df["失败类型"] == "")
    no_error_df = valid_df[no_error_mask]
    with_error_df = valid_df[~no_error_mask]
    return {
        "avg_recognition_actual": valid_df["识别角色实际耗时"].mean(),
        "avg_recognition_actual_no_error": (
            no_error_df["识别角色实际耗时"].mean() if not no_error_df.empty else None
        ),
        "avg_recognition_actual_with_error": (
            with_error_df["识别角色实际耗时"].mean() if not with_error_df.empty else None
        ),
        "no_error_count": len(no_error_df),
        "with_error_count": len(with_error_df),
        "avg_bgm_actual": valid_df["处理BGM实际耗时"].mean(),
        "valid_count": len(valid_df),
    }


def calculate_avg_recognition_time(df: pd.DataFrame) -> dict:
    """
    计算识别角色耗时统计
    筛选条件：
    - 入表时间、识别角色结束时间字段都不为空

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
    valid_df = _get_complete_overview_duration_records(df)

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
    """按概览统一完整链路样本计算BGM处理平均耗时。"""
    valid_df = _get_complete_overview_duration_records(df)

    if valid_df.empty:
        return {"avg_bgm_time": None, "valid_count": 0}

    return {
        "avg_bgm_time": valid_df["BGM处理耗时"].mean(),
        "valid_count": len(valid_df),
    }


def get_recognition_time_details(df: pd.DataFrame) -> pd.DataFrame:
    """返回与识别耗时指标口径一致的逐条明细。"""
    valid_df = _get_complete_overview_duration_records(df)

    if valid_df.empty:
        return valid_df

    valid_df["识别角色耗时(小时)"] = valid_df["识别角色耗时"]
    valid_df["是否报错"] = valid_df["失败类型"].notna() & (valid_df["失败类型"] != "")
    valid_df["识别角色耗时(小时)"] = valid_df["识别角色耗时(小时)"].round(1)
    return valid_df


def calculate_cycle_recognition_time(df: pd.DataFrame) -> pd.DataFrame:
    """
    计算每个生产周期的平均识别角色耗时
    筛选条件同上
    """
    # 筛选两个字段都不为空且有生产周期的记录
    valid_df = _get_complete_overview_duration_records(df)
    valid_df = valid_df[valid_df["生产周期"].notna()].copy()

    if valid_df.empty:
        return pd.DataFrame()

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
    valid_df = _get_complete_overview_duration_records(df)
    valid_df = valid_df[valid_df["生产周期"].notna()].copy()

    if valid_df.empty:
        return pd.DataFrame()

    valid_df["生产周期"] = valid_df["生产周期"].astype(str).str.strip()
    valid_df = valid_df[
        valid_df["识别角色结束时间"].notna() &
        valid_df["处理BGM结束时间"].notna() &
        valid_df["生产周期"].ne("") &
        valid_df["生产周期"].le(get_current_cycle())
    ].copy()

    if valid_df.empty:
        return pd.DataFrame()

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
    - 入表时间、开始生产时间、识别角色结束时间、BGM开始处理时间、处理BGM结束时间均不为空
    - 生产总耗时按“处理BGM结束时间 - 入表时间”重新计算
    """
    valid_df = _get_complete_overview_duration_records(df)

    if valid_df.empty:
        return {
            "avg_production_time": None,
            "valid_count": 0,
        }

    avg_time = valid_df["生产总耗时_新口径"].mean()

    return {
        "avg_production_time": avg_time,
        "valid_count": len(valid_df),
    }


def calculate_cycle_production_time(df: pd.DataFrame) -> pd.DataFrame:
    """
    计算每个生产周期的平均生产总耗时
    筛选条件同上
    """
    valid_df = _get_complete_overview_duration_records(df)
    valid_df = valid_df[valid_df["生产周期"].notna()].copy()

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
        平均生产总耗时=("生产总耗时_新口径", "mean"),
        记录数=("生产总耗时_新口径", "count")
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


def calculate_cycle_weekday_completion_rate(
    df: pd.DataFrame, now: datetime = None
) -> pd.DataFrame:
    """按周期实际入表边界计算截至周一、周五观察时点的生产完成率。"""
    required_cols = [
        "生产状态", "生产周期", "入表时间", "处理BGM结束时间"
    ]
    if df.empty or any(col not in df.columns for col in required_cols):
        return pd.DataFrame()

    now = now or get_local_now()
    now_timestamp = pd.Timestamp(now)
    current_cycle = get_current_cycle()
    valid_df = df[
        df["生产周期"].notna() & df["入表时间"].notna()
    ].copy()
    if valid_df.empty:
        return pd.DataFrame()

    valid_df["生产周期"] = valid_df["生产周期"].astype(str).str.strip()
    valid_df["入表时间"] = pd.to_datetime(valid_df["入表时间"], errors="coerce")
    valid_df["处理BGM结束时间"] = pd.to_datetime(
        valid_df["处理BGM结束时间"], errors="coerce"
    )
    valid_df["生产状态"] = valid_df["生产状态"].fillna("").astype(str).str.strip()
    valid_df = valid_df[
        valid_df["生产周期"].ne("")
        & valid_df["生产周期"].le(current_cycle)
        & valid_df["入表时间"].notna()
    ].copy()
    if valid_df.empty:
        return pd.DataFrame()

    valid_df["入表日期"] = valid_df["入表时间"].dt.date
    all_cycles = sorted(valid_df["生产周期"].unique().tolist())
    rows = []
    for cycle in all_cycles:
        try:
            cycle_friday = datetime.strptime(cycle, "%Y%m%d").date()
        except ValueError:
            continue

        last_friday = cycle_friday - timedelta(days=7)
        cycle_monday = last_friday + timedelta(days=3)
        cycle_df = valid_df[valid_df["生产周期"] == cycle]
        earliest_entry = cycle_df["入表时间"].min()
        latest_entry = cycle_df["入表时间"].max()

        def calculate_snapshot(observation_date, use_latest_entry=False):
            if now.date() < observation_date:
                return None, 0, 0

            observation_end = pd.Timestamp(observation_date) + pd.Timedelta(days=1)
            if now.date() == observation_date:
                observation_end = min(observation_end, now_timestamp)
            if use_latest_entry:
                # 周五口径覆盖当前周期从最早到最晚入表的全部记录。
                window_df = cycle_df[
                    (cycle_df["入表时间"] >= earliest_entry)
                    & (cycle_df["入表时间"] <= latest_entry)
                ]
            else:
                # 周一口径从当前周期实际最早入表记录开始，截至周一观察时点。
                window_df = cycle_df[
                    (cycle_df["入表时间"] >= earliest_entry)
                    & (cycle_df["入表时间"] < observation_end)
                ]
            total = len(window_df)
            completed = int((
                window_df["生产状态"].eq("完成")
                & window_df["处理BGM结束时间"].notna()
                & (window_df["处理BGM结束时间"] < observation_end)
            ).sum())
            rate = completed / total * 100 if total else None
            return rate, completed, total

        monday_rate, monday_completed, monday_total = calculate_snapshot(
            cycle_monday
        )
        friday_rate, friday_completed, friday_total = calculate_snapshot(
            cycle_friday, use_latest_entry=True
        )
        rows.append({
            "生产周期": cycle,
            "周一完成率": monday_rate,
            "周五完成率": friday_rate,
            "周一已完成": monday_completed,
            "周一总数": monday_total,
            "周五已完成": friday_completed,
            "周五总数": friday_total,
            "周一统计范围": f"{earliest_entry.strftime('%m-%d %H:%M')}~{cycle_monday.strftime('%m-%d')}结束",
            "周五统计范围": f"{earliest_entry.strftime('%m-%d %H:%M')}~{latest_entry.strftime('%m-%d %H:%M')}",
        })

    result = pd.DataFrame(rows)
    if result.empty:
        return result
    return result[
        result["周一总数"].gt(0) | result["周五总数"].gt(0)
    ].copy()


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


def calculate_current_cycle_metrics(df: pd.DataFrame) -> dict:
    """计算当前生产周期的完成率和识别错误率。"""
    current_cycle = get_current_cycle()
    if df.empty or "生产周期" not in df.columns:
        return {
            "current_cycle": current_cycle,
            "total_count": 0,
            "completed_count": 0,
            "completion_rate": None,
            "error_count": 0,
            "error_rate": None,
        }

    cycle_df = df[
        df["生产周期"].fillna("").astype(str).str.strip() == current_cycle
    ].copy()
    total_count = len(cycle_df)
    if total_count == 0:
        return {
            "current_cycle": current_cycle,
            "total_count": 0,
            "completed_count": 0,
            "completion_rate": None,
            "error_count": 0,
            "error_rate": None,
        }

    completed_count = int((cycle_df["生产状态"] == "完成").sum())
    failure_type_present = (
        cycle_df["失败类型"].fillna("").astype(str).str.strip().ne("")
    )
    error_count = int(failure_type_present.sum())
    return {
        "current_cycle": current_cycle,
        "total_count": total_count,
        "completed_count": completed_count,
        "completion_rate": completed_count / total_count * 100,
        "error_count": error_count,
        "error_rate": error_count / total_count * 100,
    }


def calculate_current_cycle_daily_completion_rate(df: pd.DataFrame) -> pd.DataFrame:
    """计算当前周期按天累计完成率，完成日期优先使用处理BGM结束时间。"""
    current_cycle = get_current_cycle()
    if df.empty or "生产周期" not in df.columns:
        return pd.DataFrame()

    cycle_df = df[
        df["生产周期"].fillna("").astype(str).str.strip() == current_cycle
    ].copy()
    if cycle_df.empty:
        return pd.DataFrame()

    total_count = len(cycle_df)
    entry_dates = pd.to_datetime(cycle_df["入表时间"], errors="coerce")
    completion_dates = pd.to_datetime(
        cycle_df["处理BGM结束时间"], errors="coerce"
    )
    recognition_end_dates = pd.to_datetime(
        cycle_df["识别角色结束时间"], errors="coerce"
    )
    completion_dates = completion_dates.fillna(recognition_end_dates)
    completed_mask = (
        cycle_df["生产状态"].eq("完成") & completion_dates.notna()
    )
    if not completed_mask.any():
        return pd.DataFrame()

    start_date = entry_dates.min()
    end_date = completion_dates[completed_mask].max()
    if pd.isna(start_date) or pd.isna(end_date):
        return pd.DataFrame()

    start_date = start_date.normalize()
    end_date = max(end_date.normalize(), start_date)
    rows = []
    for day in pd.date_range(start=start_date, end=end_date, freq="D"):
        completed_count = int(
            (completed_mask & (completion_dates <= day + pd.Timedelta(days=1) - pd.Timedelta(seconds=1))).sum()
        )
        rows.append({
            "日期": day.strftime("%m-%d"),
            "日期值": day,
            "已完成": completed_count,
            "总数": total_count,
            "完成率": completed_count / total_count * 100,
        })
    return pd.DataFrame(rows)


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


def get_recognition_failure_alert_details(
    df: pd.DataFrame, now: datetime = None
) -> pd.DataFrame:
    """返回识别失败、前期停滞及生产超时未完成的记录。"""
    if df.empty:
        return pd.DataFrame()

    now = now or get_local_now()
    alert_df = df.copy()
    alert_df["入表时间"] = pd.to_datetime(alert_df["入表时间"], errors="coerce")
    alert_df["距入表小时"] = (
        now - alert_df["入表时间"]
    ).dt.total_seconds() / 3600
    # 保留旧字段名，兼容已有展示和消息格式。
    alert_df["失败等待小时"] = alert_df["距入表小时"]

    failure_type_present = (
        alert_df["失败类型"].fillna("").astype(str).str.strip().ne("")
    )
    production_status = alert_df["生产状态"].fillna("").astype(str).str.strip()
    current_failure = production_status.eq("失败")
    waiting_failure = current_failure | (
        production_status.isin(["失败处理中", "未开始"])
        & failure_type_present
    )

    alert_df["当前失败"] = current_failure
    alert_df["失败等待超过24小时"] = (
        waiting_failure
        & (alert_df["距入表小时"] > RECOGNITION_FAILURE_WAIT_HOURS)
    )
    alert_df["前期阶段超过24小时"] = (
        production_status.isin(PRODUCTION_EARLY_STAGES)
        & (alert_df["距入表小时"] > PRODUCTION_EARLY_STAGE_ALERT_HOURS)
    )
    alert_df["生产未完成超过48小时"] = (
        production_status.ne("完成")
        & (alert_df["距入表小时"] > PRODUCTION_NOT_COMPLETE_ALERT_HOURS)
    )

    alert_columns = [
        "当前失败",
        "失败等待超过24小时",
        "前期阶段超过24小时",
        "生产未完成超过48小时",
    ]
    alert_df = alert_df[alert_df[alert_columns].any(axis=1)].copy()
    if alert_df.empty:
        return alert_df

    def build_alert_reason(row: pd.Series) -> str:
        reasons = []
        if bool(row["当前失败"]):
            reasons.append("当前识别失败")
        if bool(row["失败等待超过24小时"]):
            reasons.append("失败等待超过24小时")
        if bool(row["前期阶段超过24小时"]):
            reasons.append("识别完成前阶段停滞超过24小时")
        if bool(row["生产未完成超过48小时"]):
            reasons.append("生产超过48小时未完成")
        return "；".join(reasons)

    alert_df["警报原因"] = alert_df.apply(build_alert_reason, axis=1)
    return alert_df.sort_values(
        ["生产未完成超过48小时", "前期阶段超过24小时", "距入表小时"],
        ascending=[False, False, False],
    )


def _load_recognition_alert_state() -> dict:
    """读取生产任务告警发送状态。"""
    if not os.path.exists(RECOGNITION_ALERT_STATE_FILE):
        return {}

    try:
        with open(RECOGNITION_ALERT_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"读取生产任务告警状态失败: {e}")
        return {}


def _save_recognition_alert_state(state: dict):
    """保存生产任务告警发送状态。"""
    try:
        tmp_file = f"{RECOGNITION_ALERT_STATE_FILE}.tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp_file, RECOGNITION_ALERT_STATE_FILE)
    except Exception as e:
        print(f"保存生产任务告警状态失败: {e}")


def _get_recognition_alert_key(row: pd.Series) -> str:
    """生成稳定的告警去重键，优先使用飞书 record_id。"""
    record_id = row.get("record_id")
    if pd.notna(record_id) and str(record_id).strip():
        return f"record:{record_id}"

    fallback_values = [
        row.get("系统"),
        row.get("剧id"),
        row.get("剧名"),
        row.get("生产周期"),
    ]
    parts = []
    for value in fallback_values:
        parts.append("" if pd.isna(value) else str(value).strip())
    return "fallback:" + "|".join(parts)


def _format_recognition_alert_detail(row: pd.Series) -> str:
    """格式化单条生产任务告警。"""
    entry_time = row.get("入表时间")
    if pd.notna(entry_time):
        entry_time = pd.to_datetime(entry_time, errors="coerce")
    entry_time_text = (
        entry_time.strftime("%Y-%m-%d %H:%M:%S")
        if pd.notna(entry_time) else "未填写"
    )
    waiting_hours = row.get("距入表小时", row.get("失败等待小时"))
    waiting_text = (
        f"{float(waiting_hours):.1f}小时"
        if pd.notna(waiting_hours) else "未知"
    )
    failure_type = row.get("失败类型")
    failure_type = str(failure_type).strip() if pd.notna(failure_type) else "未填写"
    return (
        f"{row.get('系统', '未知系统')}｜{row.get('剧名', '未命名')}｜"
        f"周期 {row.get('生产周期', '未填写')}｜状态 {row.get('生产状态', '未填写')}｜"
        f"警报原因 {row.get('警报原因', '未填写')}｜失败类型 {failure_type}｜"
        f"入表 {entry_time_text}｜已等待 {waiting_text}"
    )


def _build_recognition_alert_message(alerts: list, now: datetime) -> str:
    """构建发送给小柯的生产任务告警消息，同一记录只展示一次。"""
    current_failure_count = sum(bool(row.get("当前失败")) for row in alerts)
    failure_overdue_count = sum(
        bool(row.get("失败等待超过24小时")) for row in alerts
    )
    early_stage_overdue_count = sum(
        bool(row.get("前期阶段超过24小时")) for row in alerts
    )
    production_overdue_count = sum(
        bool(row.get("生产未完成超过48小时")) for row in alerts
    )
    lines = [
        "🚨【生产任务告警】",
        f"检查时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
        f"本次告警记录：{len(alerts)} 条",
        f"当前识别失败：{current_failure_count} 条",
        f"失败等待超过24小时：{failure_overdue_count} 条",
        f"识别完成前阶段停滞超过24小时：{early_stage_overdue_count} 条",
        f"生产超过48小时未完成：{production_overdue_count} 条",
        "",
    ]

    if alerts:
        lines.append("⚠️ 告警明细：")
        for row in alerts[:20]:
            lines.append(f"  - {_format_recognition_alert_detail(row)}")
        if len(alerts) > 20:
            lines.append(f"  - 其余 {len(alerts) - 20} 条请查看生产监控面板")

    lines.extend([
        "",
        "规则1：入表超过24小时，状态仍为未开始/合并视频/识别字幕/识别角色。",
        "规则2：入表超过48小时，生产状态仍不等于完成。",
        f"提醒频率：生产任务告警每 {RECOGNITION_ALERT_COOLDOWN_HOURS} 小时最多推送一次。",
    ])
    return "\n".join(lines)


def send_recognition_failure_alert_to_xiaoke(content: str):
    """按 auto_produce.send_to_xiaoke 的 user_id 方式发送生产任务告警。"""
    try:
        lark_message_client = LarkMessage()
        response = lark_message_client.send_message(
            RECOGNITION_ALERT_CHAT_ID,
            content,
            receive_id_type=RECOGNITION_ALERT_RECEIVE_ID_TYPE,
        )
        if response is None:
            return False, "生产任务告警发送失败"
        return True, "生产任务告警已发送给小柯"
    except Exception as e:
        return False, f"生产任务告警发送异常: {e}"


def try_send_recognition_failure_alert(now: datetime = None):
    """检查生产任务告警，全部告警共享6小时推送冷却。"""
    now = now or get_local_now()
    df = fetch_all_systems_data_for_auto_report()
    if df.empty:
        return False, "未获取到生产数据"

    alert_df = get_recognition_failure_alert_details(df, now=now)
    if alert_df.empty:
        return False, "暂无生产任务告警"

    state = _load_recognition_alert_state()
    alert_meta = state.get("__meta__", {}) if isinstance(state, dict) else {}
    last_sent_at = pd.to_datetime(alert_meta.get("last_sent_at"), errors="coerce")
    if (
        pd.notna(last_sent_at)
        and now - last_sent_at.to_pydatetime()
        < timedelta(hours=RECOGNITION_ALERT_COOLDOWN_HOURS)
    ):
        return False, "生产任务告警仍在6小时冷却期内"

    alerts = [row for _, row in alert_df.iterrows()]
    message = _build_recognition_alert_message(alerts, now)
    success, send_message = send_recognition_failure_alert_to_xiaoke(message)
    if not success:
        print(send_message)
        return True, send_message

    sent_at = now.strftime("%Y-%m-%d %H:%M:%S")
    _save_recognition_alert_state({"__meta__": {"last_sent_at": sent_at}})
    print(send_message)
    return True, send_message


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
    """自动日报与生产任务告警后台调度循环"""
    print("自动生产日报定时器已启动：周一/周五 19:00 发送")
    print("生产任务告警定时器已启动：每分钟检查，每6小时最多推送一次")
    while True:
        try:
            try_send_recognition_failure_alert()
        except Exception as e:
            print(f"生产任务告警定时器异常: {e}")

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


def render_recognition_alert_tab(df: pd.DataFrame):
    """单独展示识别失败、前期停滞及超时未完成警报。"""
    st.header("🚨 识别警报")
    recognition_alert_df = get_recognition_failure_alert_details(df)
    current_alert_df = recognition_alert_df[
        recognition_alert_df["当前失败"]
    ].copy() if not recognition_alert_df.empty else pd.DataFrame()
    overdue_alert_df = recognition_alert_df[
        recognition_alert_df["失败等待超过24小时"]
    ].copy() if not recognition_alert_df.empty else pd.DataFrame()
    early_stage_alert_df = recognition_alert_df[
        recognition_alert_df["前期阶段超过24小时"]
    ].copy() if not recognition_alert_df.empty else pd.DataFrame()
    production_overdue_alert_df = recognition_alert_df[
        recognition_alert_df["生产未完成超过48小时"]
    ].copy() if not recognition_alert_df.empty else pd.DataFrame()

    alert_col1, alert_col2, alert_col3, alert_col4 = st.columns(4)
    with alert_col1:
        st.markdown(
            create_kpi_card(
                "当前识别失败",
                len(current_alert_df),
                color="#c0392b",
            ),
            unsafe_allow_html=True,
        )
    with alert_col2:
        st.markdown(
            create_kpi_card(
                "失败等待超过24小时",
                len(overdue_alert_df),
                color="#e67e22",
            ),
            unsafe_allow_html=True,
        )
    with alert_col3:
        st.markdown(
            create_kpi_card(
                "前期阶段超过24小时",
                len(early_stage_alert_df),
                color="#f39c12",
            ),
            unsafe_allow_html=True,
        )
    with alert_col4:
        st.markdown(
            create_kpi_card(
                "生产未完成超过48小时",
                len(production_overdue_alert_df),
                color="#8e44ad",
            ),
            unsafe_allow_html=True,
        )

    if recognition_alert_df.empty:
        st.success("当前没有识别失败、前期阶段停滞或生产超时未完成的记录。")
        return

    alert_detail_columns = [
        "系统", "剧名", "剧id", "生产周期", "生产状态",
        "警报原因", "失败类型", "入表时间", "距入表小时", "备注",
    ]
    alert_detail_columns = [
        column for column in alert_detail_columns
        if column in recognition_alert_df.columns
    ]
    alert_display_df = recognition_alert_df[alert_detail_columns].copy()
    if "入表时间" in alert_display_df.columns:
        alert_display_df["入表时间"] = pd.to_datetime(
            alert_display_df["入表时间"], errors="coerce"
        ).dt.strftime("%Y-%m-%d %H:%M:%S").fillna("")
    if "距入表小时" in alert_display_df.columns:
        alert_display_df["距入表小时"] = alert_display_df["距入表小时"].round(1)

    st.dataframe(
        alert_display_df,
        width="stretch",
        hide_index=True,
    )
    st.caption(
        "警报规则：① 当前识别失败；② 失败等待超过24小时；"
        "③ 入表超过24小时且状态仍为未开始/合并视频/识别字幕/识别角色；"
        "④ 入表超过48小时且生产状态不等于完成。时长均从入表时间开始计算；"
        f"生产任务告警每 {RECOGNITION_ALERT_COOLDOWN_HOURS} 小时最多推送一次。"
    )


def render_overview_tab(df: pd.DataFrame, trend_df: pd.DataFrame = None):
    """渲染概览标签页。"""
    st.header("📊 生产进度大盘")

    if df.empty:
        st.warning("暂无数据")
        return

    # ===== 生产进度 KPI =====
    col1, col2, col3 = st.columns(3)

    total_count = len(df)
    # 已完成：生产状态=完成
    completed_count = len(df[df["生产状态"] == "完成"])

    # 未完成统一按“非完成”计算，确保与“总剧数 - 已完成”完全一致。
    # 之前这里只累加了三种状态，会漏掉合并视频、识别字幕、识别角色、失败等记录。
    not_completed_count = total_count - completed_count
    incomplete_df = df[df["生产状态"] != "完成"].copy()
    final_avg_production_time = calculate_avg_production_time(df)
    current_cycle_metrics = calculate_current_cycle_metrics(df)

    with col1:
        st.markdown(create_kpi_card("总剧数", total_count, color="#5470c6"), unsafe_allow_html=True)
    with col2:
        st.markdown(create_kpi_card("已完成", completed_count, color="#91cc75"), unsafe_allow_html=True)
    with col3:
        st.markdown(create_kpi_card("未完成", not_completed_count, color="#f39c12"), unsafe_allow_html=True)

    if not incomplete_df.empty:
        incomplete_detail_columns = [
            "系统", "剧名", "剧id", "生产周期", "生产状态", "整备状态",
            "生产机器", "失败类型", "入表时间", "备注",
        ]
        incomplete_detail_columns = [
            column for column in incomplete_detail_columns
            if column in incomplete_df.columns
        ]
        status_counts = (
            incomplete_df["生产状态"].fillna("空状态").astype(str).value_counts()
        )
        status_summary = " | ".join(
            f"{status}: {count} 条" for status, count in status_counts.items()
        )
        with st.expander(f"查看未完成明细（{not_completed_count} 条）", expanded=False):
            st.caption(f"状态分布：{status_summary}")
            st.dataframe(
                incomplete_df[incomplete_detail_columns],
                width="stretch",
                hide_index=True,
            )

    st.markdown("---")
    st.subheader("📊 宏观统计")
    final_col1, final_col2, final_col3 = st.columns(3)

    with final_col1:
        avg_production_time = final_avg_production_time["avg_production_time"]
        st.markdown(
            create_kpi_card(
                "平均生产总耗时",
                f"{avg_production_time:.1f}h"
                if avg_production_time is not None else "暂无数据",
                color="#9a60b4",
            ),
            unsafe_allow_html=True,
        )
        st.caption(
            f"📊 基于 {final_avg_production_time['valid_count']} 条完整记录"
            if avg_production_time is not None else "暂无完整生产链路记录"
        )

    with final_col2:
        completion_rate = current_cycle_metrics["completion_rate"]
        st.markdown(
            create_kpi_card(
                "当前周期完成率",
                f"{completion_rate:.1f}%"
                if completion_rate is not None else "暂无数据",
                color="#27ae60",
            ),
            unsafe_allow_html=True,
        )
        st.caption(
            f"📅 周期 {current_cycle_metrics['current_cycle']}："
            f"{current_cycle_metrics['completed_count']}/{current_cycle_metrics['total_count']} 条"
            if current_cycle_metrics["total_count"] else
            f"📅 周期 {current_cycle_metrics['current_cycle']} 暂无记录"
        )

    with final_col3:
        error_rate = current_cycle_metrics["error_rate"]
        st.markdown(
            create_kpi_card(
                "当前周期错误率",
                f"{error_rate:.1f}%"
                if error_rate is not None else "暂无数据",
                color="#ee6666",
            ),
            unsafe_allow_html=True,
        )
        st.caption(
            f"📅 周期 {current_cycle_metrics['current_cycle']}："
            f"{current_cycle_metrics['error_count']}/{current_cycle_metrics['total_count']} 条失败类型非空"
            if current_cycle_metrics["total_count"] else
            f"📅 周期 {current_cycle_metrics['current_cycle']} 暂无记录"
        )

    st.markdown("---")

    # ===== 生产耗时分析 =====
    st.subheader("⏱️ 生产耗时分析")

    # 结构图使用当前筛选数据；周期趋势图使用所选周期及其之前三个周期的完整数据。
    recognition_time_stats = calculate_avg_recognition_time(df)
    bgm_time_stats = calculate_avg_bgm_time(df)
    waiting_time_stats = calculate_avg_waiting_times(df)
    actual_time_stats = calculate_avg_actual_times(df)

    selected_cycles = (
        df["生产周期"].dropna().astype(str).str.strip().unique().tolist()
        if "生产周期" in df.columns else []
    )
    selected_cycles = [cycle for cycle in selected_cycles if cycle]
    trend_anchor = selected_cycles[0] if len(selected_cycles) == 1 else get_current_cycle()
    try:
        trend_anchor_date = datetime.strptime(trend_anchor, "%Y%m%d")
    except ValueError:
        trend_anchor_date = datetime.strptime(get_current_cycle(), "%Y%m%d")
    trend_cycles = [
        (trend_anchor_date - timedelta(days=7 * offset)).strftime("%Y%m%d")
        for offset in range(3, -1, -1)
    ]
    duration_source_df = trend_df if trend_df is not None else df
    if not duration_source_df.empty and "生产周期" in duration_source_df.columns:
        duration_source_df = duration_source_df.copy()
        duration_source_df["生产周期"] = (
            duration_source_df["生产周期"].fillna("").astype(str).str.strip()
        )
        duration_source_df = duration_source_df[
            duration_source_df["生产周期"].isin(trend_cycles)
        ]

    cycle_recognition_stats = calculate_cycle_recognition_time(duration_source_df)
    cycle_bgm_stats = calculate_cycle_bgm_time(duration_source_df)
    cycle_time_stats = calculate_cycle_production_time(duration_source_df)
    weekday_cycle_time_stats = calculate_cycle_weekday_production_time(duration_source_df)
    weekday_cycle_completion_stats = calculate_cycle_weekday_completion_rate(
        duration_source_df
    )
    if not weekday_cycle_time_stats.empty:
        weekday_cycle_time_stats["生产周期"] = (
            weekday_cycle_time_stats["生产周期"].astype(str)
        )
        weekday_cycle_time_stats = (
            weekday_cycle_time_stats.set_index("生产周期")
            .reindex(trend_cycles)
            .rename_axis("生产周期")
            .reset_index()
        )
        for count_column in ["周一记录数", "周五记录数"]:
            weekday_cycle_time_stats[count_column] = (
                weekday_cycle_time_stats[count_column].fillna(0).astype(int)
            )
        for row_index, cycle in enumerate(trend_cycles):
            cycle_friday = datetime.strptime(cycle, "%Y%m%d")
            last_friday = cycle_friday - timedelta(days=7)
            cycle_monday = last_friday + timedelta(days=3)
            weekday_cycle_time_stats.loc[row_index, "周一统计范围"] = (
                f"{last_friday.strftime('%m-%d')}~{cycle_monday.strftime('%m-%d')}"
            )
            weekday_cycle_time_stats.loc[row_index, "周五统计范围"] = (
                f"{last_friday.strftime('%m-%d')}~{cycle_friday.strftime('%m-%d')}"
            )
    if not weekday_cycle_completion_stats.empty:
        weekday_cycle_completion_stats["生产周期"] = (
            weekday_cycle_completion_stats["生产周期"].astype(str)
        )
        weekday_cycle_completion_stats = (
            weekday_cycle_completion_stats.set_index("生产周期")
            .reindex(trend_cycles)
            .rename_axis("生产周期")
            .reset_index()
        )
        completion_count_columns = [
            "周一已完成", "周一总数", "周五已完成", "周五总数"
        ]
        for count_column in completion_count_columns:
            weekday_cycle_completion_stats[count_column] = (
                weekday_cycle_completion_stats[count_column].fillna(0).astype(int)
            )

    cycle_system_failure_stats = pd.DataFrame()
    failure_required_columns = {"生产周期", "系统", "失败类型"}
    if (
        not duration_source_df.empty
        and failure_required_columns.issubset(duration_source_df.columns)
    ):
        failure_source_df = duration_source_df.copy()
        failure_source_df["生产周期"] = (
            failure_source_df["生产周期"].fillna("").astype(str).str.strip()
        )
        failure_source_df["系统"] = (
            failure_source_df["系统"].fillna("").astype(str).str.strip()
        )
        failure_source_df = failure_source_df[
            failure_source_df["生产周期"].isin(trend_cycles)
            & failure_source_df["系统"].isin(SYSTEMS)
        ].copy()
        failure_source_df["是否失败"] = (
            failure_source_df["失败类型"]
            .fillna("")
            .astype(str)
            .str.strip()
            .ne("")
        )
        if not failure_source_df.empty:
            cycle_system_failure_stats = (
                failure_source_df.groupby(["生产周期", "系统"], as_index=False)
                .agg(
                    总记录数=("是否失败", "size"),
                    失败记录数=("是否失败", "sum"),
                )
            )
            cycle_system_failure_stats["失败率"] = (
                cycle_system_failure_stats["失败记录数"]
                .div(cycle_system_failure_stats["总记录数"])
                .mul(100)
            )
    cycle_completion_stats = calculate_cycle_completion_rate(df)

    def format_duration(value):
        return f"{value:.1f}h" if value is not None and pd.notna(value) else "暂无数据"

    def duration_number(value):
        if value is None or pd.isna(value):
            return 0.0
        return max(float(value), 0.0)

    complete_count = final_avg_production_time["valid_count"]
    total_duration = duration_number(final_avg_production_time["avg_production_time"])
    recognition_duration = duration_number(recognition_time_stats["avg_recognition_time"])
    bgm_duration = duration_number(bgm_time_stats["avg_bgm_time"])
    recognition_wait = duration_number(waiting_time_stats["avg_recognition_wait"])
    recognition_actual = duration_number(actual_time_stats["avg_recognition_actual"])
    bgm_wait = duration_number(waiting_time_stats["avg_bgm_wait"])
    bgm_actual = duration_number(actual_time_stats["avg_bgm_actual"])

    no_error_count = actual_time_stats["no_error_count"]
    with_error_count = actual_time_stats["with_error_count"]
    actual_count = no_error_count + with_error_count
    no_error_percentage = no_error_count / actual_count * 100 if actual_count else 0.0
    with_error_percentage = with_error_count / actual_count * 100 if actual_count else 0.0
    no_error_weight = (
        duration_number(actual_time_stats["avg_recognition_actual_no_error"])
        * no_error_count / actual_count
        if actual_count else 0.0
    )
    with_error_weight = (
        duration_number(actual_time_stats["avg_recognition_actual_with_error"])
        * with_error_count / actual_count
        if actual_count else 0.0
    )

    duration_map_html = f"""
    <style>
        .duration-map {{ width: 100%; color: #fff; font-family: sans-serif; }}
        .duration-map-root {{
            background: linear-gradient(135deg, #8e44ad, #9b59b6);
            border-radius: 10px; padding: 18px; text-align: center;
            font-weight: 700; font-size: 18px; margin-bottom: 8px;
        }}
        .duration-map-level {{ display: flex; gap: 8px; min-height: 255px; }}
        .duration-map-group {{
            flex-basis: 0; min-width: 0; border-radius: 10px;
            padding: 6px; display: flex; flex-direction: column;
        }}
        .duration-map-group-title {{
            border-radius: 7px; padding: 12px 6px; text-align: center;
            font-weight: 700; font-size: 15px; margin-bottom: 6px;
        }}
        .duration-map-children {{ display: flex; gap: 6px; flex: 1; min-width: 0; }}
        .duration-map-block {{
            flex-basis: 0; min-width: 0; border-radius: 7px; padding: 10px 5px;
            display: flex; flex-direction: column; align-items: center;
            justify-content: center; text-align: center; font-size: 13px;
            font-weight: 600; overflow: hidden;
        }}
        .duration-map-actual {{ justify-content: flex-start; padding: 0; }}
        .duration-map-actual-title {{ width: 100%; padding: 10px 4px; }}
        .duration-map-grandchildren {{
            display: flex; gap: 5px; width: 100%; flex: 1; padding: 0 5px 5px;
            box-sizing: border-box;
        }}
        .duration-map-small {{
            flex-basis: 0; min-width: 0; border-radius: 6px; padding: 8px 3px;
            display: flex; align-items: center; justify-content: center;
            text-align: center; font-size: 12px; overflow: hidden;
        }}
        .duration-map-value {{ font-size: 17px; margin-top: 4px; }}
        .duration-map-count {{ font-size: 11px; opacity: .9; margin-top: 2px; }}
    </style>
    <div class="duration-map">
        <div class="duration-map-root">
            平均生产总耗时
            <div class="duration-map-value">{format_duration(total_duration)}</div>
            <div class="duration-map-count">{complete_count} 条完整记录</div>
        </div>
        <div class="duration-map-level">
            <div class="duration-map-group" style="flex-grow:{max(recognition_duration, 0.001)}; background:#73c0de22; border:2px solid #73c0de;">
                <div class="duration-map-group-title" style="background:#73c0de;">
                    识别角色总耗时 · {format_duration(recognition_duration)}
                </div>
                <div class="duration-map-children">
                    <div class="duration-map-block" style="flex-grow:{max(recognition_wait, 0.001)}; background:#fac858;" title="识别等待耗时 {format_duration(recognition_wait)}">
                        识别等待耗时<div class="duration-map-value">{format_duration(recognition_wait)}</div>
                    </div>
                    <div class="duration-map-block duration-map-actual" style="flex-grow:{max(recognition_actual, 0.001)}; background:#3ba272;" title="识别实际耗时 {format_duration(recognition_actual)}">
                        <div class="duration-map-actual-title">识别实际耗时 · {format_duration(recognition_actual)}</div>
                        <div class="duration-map-grandchildren">
                            <div class="duration-map-small" style="flex-grow:{max(no_error_weight, 0.001)}; background:#91cc75;" title="无报错时实际耗时 {format_duration(actual_time_stats['avg_recognition_actual_no_error'])}，占比 {no_error_percentage:.1f}%">
                                无报错<br>{format_duration(actual_time_stats['avg_recognition_actual_no_error'])}<br>{no_error_percentage:.1f}%
                            </div>
                            <div class="duration-map-small" style="flex-grow:{max(with_error_weight, 0.001)}; background:#ee6666;" title="有报错时实际耗时 {format_duration(actual_time_stats['avg_recognition_actual_with_error'])}，占比 {with_error_percentage:.1f}%">
                                有报错<br>{format_duration(actual_time_stats['avg_recognition_actual_with_error'])}<br>{with_error_percentage:.1f}%
                            </div>
                        </div>
                    </div>
                </div>
            </div>
            <div class="duration-map-group" style="flex-grow:{max(bgm_duration, 0.001)}; background:#9a60b422; border:2px solid #9a60b4;">
                <div class="duration-map-group-title" style="background:#9a60b4;">
                    BGM处理总耗时 · {format_duration(bgm_duration)}
                </div>
                <div class="duration-map-children">
                    <div class="duration-map-block" style="flex-grow:{max(bgm_wait, 0.001)}; background:#fc8452;" title="BGM等待耗时 {format_duration(bgm_wait)}">
                        BGM等待耗时<div class="duration-map-value">{format_duration(bgm_wait)}</div>
                    </div>
                    <div class="duration-map-block" style="flex-grow:{max(bgm_actual, 0.001)}; background:#5470c6;" title="BGM实际处理耗时 {format_duration(bgm_actual)}">
                        BGM实际处理耗时<div class="duration-map-value">{format_duration(bgm_actual)}</div>
                    </div>
                </div>
            </div>
        </div>
    </div>
    """
    st.markdown(duration_map_html, unsafe_allow_html=True)

    st.caption(
        "📋 识别角色耗时：识别角色结束时间 − 入表时间 | "
        "识别角色等待耗时：开始生产时间 − 入表时间 | "
        "识别角色实际耗时：识别角色结束时间 − 开始生产时间 | "
        "BGM耗时：处理BGM结束时间 − 识别角色结束时间 | "
        "处理BGM等待耗时：BGM开始处理时间 − 识别角色结束时间 | "
        "处理BGM实际耗时：处理BGM结束时间 − BGM开始处理时间 | "
        "无报错：失败类型为空 | 有报错：失败类型不为空"
    )

    # 各周期三项平均耗时合并展示。
    cycle_sets = []
    for cycle_df in [cycle_recognition_stats, cycle_bgm_stats, cycle_time_stats]:
        if not cycle_df.empty:
            cycle_sets.extend(cycle_df["生产周期"].astype(str).tolist())
    duration_cycles = trend_cycles if cycle_sets else []

    if duration_cycles:
        def cycle_values(stats_df, value_column):
            if stats_df.empty:
                return [None] * len(duration_cycles)
            normalized_stats = stats_df.copy()
            normalized_stats["生产周期"] = normalized_stats["生产周期"].astype(str)
            value_map = normalized_stats.set_index("生产周期")[value_column].to_dict()
            return [
                round(value_map[cycle], 1)
                if cycle in value_map and pd.notna(value_map[cycle]) else None
                for cycle in duration_cycles
            ]

        duration_line = Line(
            init_opts=opts.InitOpts(width="100%", height="420px", theme="light")
        )
        duration_line.add_xaxis(duration_cycles)
        duration_line.add_yaxis(
            series_name="平均生产总耗时",
            y_axis=cycle_values(cycle_time_stats, "平均生产总耗时"),
            symbol="circle",
            symbol_size=8,
            label_opts=opts.LabelOpts(is_show=False),
            linestyle_opts=opts.LineStyleOpts(width=3, color="#9a60b4"),
            itemstyle_opts=opts.ItemStyleOpts(color="#9a60b4"),
        )
        duration_line.add_yaxis(
            series_name="识别角色总耗时",
            y_axis=cycle_values(cycle_recognition_stats, "平均识别角色耗时"),
            symbol="circle",
            symbol_size=8,
            label_opts=opts.LabelOpts(is_show=False),
            linestyle_opts=opts.LineStyleOpts(width=3, color="#73c0de"),
            itemstyle_opts=opts.ItemStyleOpts(color="#73c0de"),
        )
        duration_line.add_yaxis(
            series_name="BGM处理总耗时",
            y_axis=cycle_values(cycle_bgm_stats, "BGM处理平均耗时"),
            symbol="circle",
            symbol_size=8,
            label_opts=opts.LabelOpts(is_show=False),
            linestyle_opts=opts.LineStyleOpts(width=3, color="#fc8452"),
            itemstyle_opts=opts.ItemStyleOpts(color="#fc8452"),
        )
        duration_line.set_global_opts(
            title_opts=opts.TitleOpts(title="最近4个生产周期耗时变化"),
            xaxis_opts=opts.AxisOpts(
                name="生产周期",
                axislabel_opts=opts.LabelOpts(rotate=30),
            ),
            yaxis_opts=opts.AxisOpts(name="平均耗时（小时）", min_=0),
            tooltip_opts=opts.TooltipOpts(trigger="axis"),
            legend_opts=opts.LegendOpts(pos_top="top"),
        )
        render_pyecharts(duration_line)
        st.caption(f"统计周期：{'、'.join(duration_cycles)}")
    else:
        st.info("暂无各生产周期耗时数据")

    st.markdown("---")

    # ===== 周一/周五生产总耗时对比 =====
    st.subheader("📈 最近4个周期周一/周五生产总耗时对比")
    st.caption(
        "📋 按入表时间窗口统计：周一线=上个周五至本周一，"
        "周五线=上个周五至本周五；筛选条件与平均生产总耗时一致。"
        f"统计周期：{'、'.join(trend_cycles)}"
    )

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

    # ===== 周一/周五生产完成率对比 =====
    st.subheader("📊 最近4个周期周一/周五生产完成率对比")
    st.caption(
        "📋 按当前周期实际入表边界统计：周一线=从本周期最早入表记录开始，"
        "截至本周一观察时点；周五线=本周期最早至最晚入表记录；"
        "未到对应日期时不展示该数据点。完成率=观察时点前已完成记录数÷同期入表总记录数。"
        f"统计周期：{'、'.join(trend_cycles)}"
    )

    if not weekday_cycle_completion_stats.empty:
        completion_cycles = weekday_cycle_completion_stats["生产周期"].tolist()
        monday_rates = [
            round(value, 1) if pd.notna(value) else None
            for value in weekday_cycle_completion_stats["周一完成率"].tolist()
        ]
        friday_rates = [
            round(value, 1) if pd.notna(value) else None
            for value in weekday_cycle_completion_stats["周五完成率"].tolist()
        ]
        completion_line = (
            Line(init_opts=opts.InitOpts(width="100%", height="380px", theme="light"))
            .add_xaxis(completion_cycles)
            .add_yaxis(
                series_name="周一窗口生产完成率",
                y_axis=monday_rates,
                symbol="circle",
                symbol_size=8,
                linestyle_opts=opts.LineStyleOpts(width=2, color="#5470c6"),
                itemstyle_opts=opts.ItemStyleOpts(color="#5470c6"),
                label_opts=opts.LabelOpts(
                    is_show=True,
                    position="top",
                    formatter="{c}%",
                ),
            )
            .add_yaxis(
                series_name="周五窗口生产完成率",
                y_axis=friday_rates,
                symbol="diamond",
                symbol_size=8,
                linestyle_opts=opts.LineStyleOpts(width=2, color="#fc8452"),
                itemstyle_opts=opts.ItemStyleOpts(color="#fc8452"),
                label_opts=opts.LabelOpts(
                    is_show=True,
                    position="top",
                    formatter="{c}%",
                ),
            )
            .set_global_opts(
                title_opts=opts.TitleOpts(title=""),
                xaxis_opts=opts.AxisOpts(axislabel_opts=opts.LabelOpts(rotate=30)),
                yaxis_opts=opts.AxisOpts(name="完成率 (%)", min_=0, max_=100),
                tooltip_opts=opts.TooltipOpts(trigger="axis"),
                legend_opts=opts.LegendOpts(pos_top="top"),
            )
        )
        render_pyecharts(completion_line)
    else:
        st.info("暂无周一/周五生产完成率数据")

    st.markdown("---")

    # ===== 最近4个周期各系统失败率 =====
    st.subheader("📉 最近4个周期失败率变化（按系统）")
    st.caption(
        "所有系统平均失败率=四个系统失败记录总数÷四个系统记录总数；"
        "各系统失败率=失败类型非空的记录数÷对应系统、对应周期的总记录数；"
        f"统计周期：{'、'.join(trend_cycles)}"
    )
    if not cycle_system_failure_stats.empty:
        failure_rate_line = Line(
            init_opts=opts.InitOpts(width="100%", height="400px", theme="light")
        )
        failure_rate_line.add_xaxis(trend_cycles)
        failure_rate_colors = {
            "点众": "#5470c6",
            "红果": "#ee6666",
            "外部制作": "#9a60b4",
            "众益": "#91cc75",
        }
        overall_failure_stats = (
            cycle_system_failure_stats.groupby("生产周期", as_index=False)
            .agg(
                总记录数=("总记录数", "sum"),
                失败记录数=("失败记录数", "sum"),
            )
        )
        overall_failure_stats["失败率"] = (
            overall_failure_stats["失败记录数"]
            .div(overall_failure_stats["总记录数"])
            .mul(100)
        )
        overall_rate_map = overall_failure_stats.set_index("生产周期")["失败率"].to_dict()
        overall_rate_values = [
            round(float(overall_rate_map[cycle]), 1)
            if cycle in overall_rate_map and pd.notna(overall_rate_map[cycle]) else None
            for cycle in trend_cycles
        ]
        failure_rate_line.add_yaxis(
            series_name="所有系统平均失败率",
            y_axis=overall_rate_values,
            is_smooth=True,
            is_connect_nones=False,
            symbol="diamond",
            symbol_size=11,
            label_opts=opts.LabelOpts(
                is_show=True,
                position="top",
                formatter="{c}%",
                font_size=11,
                font_weight="bold",
            ),
            linestyle_opts=opts.LineStyleOpts(width=4, color="#2f4554"),
            itemstyle_opts=opts.ItemStyleOpts(color="#2f4554"),
        )
        displayed_failure_systems = 1
        for system in SYSTEMS:
            system_stats = cycle_system_failure_stats[
                cycle_system_failure_stats["系统"].eq(system)
            ]
            if system_stats.empty:
                continue
            rate_map = system_stats.set_index("生产周期")["失败率"].to_dict()
            rate_values = [
                round(float(rate_map[cycle]), 1)
                if cycle in rate_map and pd.notna(rate_map[cycle]) else None
                for cycle in trend_cycles
            ]
            failure_rate_line.add_yaxis(
                series_name=system,
                y_axis=rate_values,
                is_smooth=True,
                is_connect_nones=False,
                symbol="circle",
                symbol_size=8,
                label_opts=opts.LabelOpts(
                    is_show=True,
                    position="top",
                    formatter="{c}%",
                    font_size=10,
                ),
                linestyle_opts=opts.LineStyleOpts(
                    width=3,
                    color=failure_rate_colors.get(system, "#73c0de"),
                ),
                itemstyle_opts=opts.ItemStyleOpts(
                    color=failure_rate_colors.get(system, "#73c0de")
                ),
            )
            displayed_failure_systems += 1

        if displayed_failure_systems:
            failure_rate_line.set_global_opts(
                xaxis_opts=opts.AxisOpts(
                    name="生产周期",
                    axislabel_opts=opts.LabelOpts(rotate=30),
                ),
                yaxis_opts=opts.AxisOpts(name="失败率 (%)", min_=0, max_=100),
                tooltip_opts=opts.TooltipOpts(trigger="axis"),
                legend_opts=opts.LegendOpts(pos_top="top", type_="scroll"),
            )
            failure_rate_line.options["legend"][0]["selected"] = {
                "所有系统平均失败率": True,
                **{system: False for system in SYSTEMS},
            }
            render_pyecharts(failure_rate_line, height=430)
        else:
            st.info("最近4个周期暂无可按系统展示的失败率数据")
    else:
        st.info("最近4个周期暂无失败率数据")

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

def _render_system_backlog_summary(
    backlog_counts: pd.Series,
    stage_counts: dict,
    stage_names: list,
    stage_colors: dict,
    total_label: str,
    chart_title: str,
):
    """渲染四个系统的延期任务指标卡和状态堆叠柱。"""
    backlog_columns = st.columns(len(SYSTEMS) + 1)
    backlog_columns[0].metric(total_label, f"{int(backlog_counts.sum())} 条")
    for column, system in zip(backlog_columns[1:], SYSTEMS):
        column.metric(system, f"{int(backlog_counts[system])} 条")

    backlog_bar = Bar(
        init_opts=opts.InitOpts(width="100%", height="340px", theme="light")
    )
    backlog_bar.add_xaxis(SYSTEMS)
    for stage in stage_names:
        stage_values = stage_counts[stage].tolist()
        backlog_bar.add_yaxis(
            series_name=stage,
            y_axis=[value if value > 0 else None for value in stage_values],
            stack="延期状态",
            label_opts=opts.LabelOpts(is_show=True, position="inside"),
            itemstyle_opts=opts.ItemStyleOpts(color=stage_colors[stage]),
        )

    # 透明辅助柱只负责在堆叠柱顶显示各系统任务总数。
    backlog_bar.add_yaxis(
        series_name=total_label,
        y_axis=backlog_counts.tolist(),
        gap="-100%",
        z=20,
        label_opts=opts.LabelOpts(
            is_show=True,
            position="top",
            distance=4,
            color="#2c3e50",
            font_size=12,
            font_weight="bold",
            formatter="{c}",
        ),
        itemstyle_opts=opts.ItemStyleOpts(color="rgba(0, 0, 0, 0)"),
        tooltip_opts=opts.TooltipOpts(is_show=False),
    )
    backlog_bar.set_global_opts(
        title_opts=opts.TitleOpts(title=chart_title),
        xaxis_opts=opts.AxisOpts(name="系统"),
        yaxis_opts=opts.AxisOpts(name="任务数（条）", min_=0),
        tooltip_opts=opts.TooltipOpts(trigger="axis", axis_pointer_type="shadow"),
        legend_opts=opts.LegendOpts(pos_top="top"),
    )
    backlog_bar.options["legend"][0]["data"] = stage_names
    render_pyecharts(backlog_bar, height=370)


def _render_recognition_backlog(df: pd.DataFrame):
    """展示剧识别表中当前周期及以前尚未完成的延期记录。"""
    required_columns = {"系统", "生产状态", "生产周期"}
    missing_columns = required_columns.difference(df.columns)
    if df.empty or missing_columns:
        if missing_columns:
            st.info(f"剧识别表缺少字段：{'、'.join(sorted(missing_columns))}")
        else:
            st.info("暂无剧识别表数据。")
        return

    backlog_source_df = df.copy()
    for column in required_columns:
        backlog_source_df[column] = (
            backlog_source_df[column].fillna("").astype(str).str.strip()
        )
    backlog_cycle = get_current_cycle()
    overdue_cycle_mask = (
        backlog_source_df["生产周期"].str.fullmatch(r"\d{8}", na=False)
        & backlog_source_df["生产周期"].le(backlog_cycle)
    )
    processing_statuses = {
        "合并角色", "合并视频", "识别字幕", "识别角色", "处理BGM", "识别完成"
    }
    failure_statuses = {"失败", "处理BGM失败", "失败处理中"}
    backlog_df = backlog_source_df[
        backlog_source_df["系统"].isin(SYSTEMS)
        & backlog_source_df["生产状态"].ne("完成")
        & overdue_cycle_mask
    ].copy()
    backlog_df["积压阶段"] = ""
    backlog_df.loc[
        backlog_df["生产状态"].isin(["", "下载资源失败"]), "积压阶段"
    ] = "未入库"
    backlog_df.loc[backlog_df["生产状态"].eq("未开始"), "积压阶段"] = "未开始"
    backlog_df.loc[
        backlog_df["生产状态"].isin(processing_statuses), "积压阶段"
    ] = "处理中"
    backlog_df.loc[
        backlog_df["生产状态"].isin(failure_statuses), "积压阶段"
    ] = "失败"

    stage_names = ["未入库", "未开始", "处理中", "失败"]
    stage_colors = {
        "未入库": "#fac858",
        "未开始": "#95a5a6",
        "处理中": "#5470c6",
        "失败": "#ee6666",
    }

    def render_cycle_group(
        cycle_group_df: pd.DataFrame,
        section_title: str,
        total_label: str,
        chart_title: str,
    ):
        group_counts = (
            cycle_group_df.groupby("系统")
            .size()
            .reindex(SYSTEMS, fill_value=0)
            .astype(int)
        )
        group_stage_counts = {
            stage: (
                cycle_group_df[cycle_group_df["积压阶段"].eq(stage)]
                .groupby("系统")
                .size()
                .reindex(SYSTEMS, fill_value=0)
                .astype(int)
            )
            for stage in stage_names
        }
        st.markdown(f"#### {section_title}")
        _render_system_backlog_summary(
            backlog_counts=group_counts,
            stage_counts=group_stage_counts,
            stage_names=stage_names,
            stage_colors=stage_colors,
            total_label=total_label,
            chart_title=chart_title,
        )

    st.caption(
        f"当前周期：{backlog_cycle}。本周期任务按生产周期等于当前周期统计；"
        "历史延期按生产周期早于当前周期统计；两类均只统计生产状态不等于“完成”的记录。"
        "生产周期为空或晚于当前周期的记录不纳入。分段口径："
        "未入库（下载资源失败、生产状态为空）；未开始；"
        "处理中（合并角色/合并视频、识别字幕、识别角色、处理BGM、识别完成）；"
        "失败（失败、处理BGM失败、失败处理中）。"
    )
    current_cycle_backlog_df = backlog_df[
        backlog_df["生产周期"].eq(backlog_cycle)
    ].copy()
    historical_backlog_df = backlog_df[
        backlog_df["生产周期"].lt(backlog_cycle)
    ].copy()
    historical_tab, current_cycle_tab = st.tabs([
        "早于当前周期的历史延期", f"本周期待处理（{backlog_cycle}）"
    ])
    with historical_tab:
        render_cycle_group(
            historical_backlog_df,
            section_title="早于当前周期的历史延期",
            total_label="历史周期延期合计",
            chart_title="各系统历史周期识别延期构成",
        )
    with current_cycle_tab:
        render_cycle_group(
            current_cycle_backlog_df,
            section_title=f"本周期待处理（{backlog_cycle}）",
            total_label="本周期待处理合计",
            chart_title="各系统本周期识别任务构成",
        )

    unclassified_count = int(backlog_df["积压阶段"].eq("").sum())
    if unclassified_count:
        unclassified_status_counts = (
            backlog_df.loc[backlog_df["积压阶段"].eq(""), "生产状态"]
            .replace("", "空状态")
            .value_counts()
        )
        status_text = "、".join(
            f"{status}：{int(count)} 条"
            for status, count in unclassified_status_counts.items()
        )
        st.warning(
            f"另有 {unclassified_count} 条待处理记录状态不属于以上分类：{status_text}。"
            "这些记录已计入对应周期总数，未计入分段。"
        )


def _render_production_backlog(production_df: pd.DataFrame):
    """展示剧制作表-新中当前周期及以前仍为未开始的配音延期记录。"""
    required_columns = {"系统", "当前状态", "当前制作周期"}
    missing_columns = required_columns.difference(production_df.columns)
    if production_df.empty or missing_columns:
        if missing_columns:
            st.info(f"剧制作表-新缺少字段：{'、'.join(sorted(missing_columns))}")
        else:
            st.info("暂无剧制作表-新数据。")
        return

    backlog_source_df = production_df.copy()
    for column in required_columns:
        backlog_source_df[column] = (
            backlog_source_df[column].fillna("").astype(str).str.strip()
        )
    backlog_cycle = get_current_cycle()
    overdue_cycle_mask = (
        backlog_source_df["当前制作周期"].str.fullmatch(r"\d{8}", na=False)
        & backlog_source_df["当前制作周期"].le(backlog_cycle)
    )
    backlog_df = backlog_source_df[
        backlog_source_df["系统"].isin(SYSTEMS)
        & backlog_source_df["当前状态"].eq("未开始")
        & overdue_cycle_mask
    ].copy()
    st.caption(
        f"当前周期：{backlog_cycle}。历史延期按当前制作周期早于当前周期统计；"
        "本周期待处理按当前制作周期等于当前周期统计；两类均只统计当前状态为“未开始”的记录。"
        "当前制作周期为空或晚于当前周期的记录不纳入。"
    )
    current_cycle_backlog_df = backlog_df[
        backlog_df["当前制作周期"].eq(backlog_cycle)
    ].copy()
    historical_backlog_df = backlog_df[
        backlog_df["当前制作周期"].lt(backlog_cycle)
    ].copy()

    def render_production_cycle_group(
        cycle_group_df: pd.DataFrame,
        total_label: str,
        chart_title: str,
    ):
        group_counts = (
            cycle_group_df.groupby("系统")
            .size()
            .reindex(SYSTEMS, fill_value=0)
            .astype(int)
        )
        _render_system_backlog_summary(
            backlog_counts=group_counts,
            stage_counts={"未开始": group_counts.copy()},
            stage_names=["未开始"],
            stage_colors={"未开始": "#9a60b4"},
            total_label=total_label,
            chart_title=chart_title,
        )

    historical_tab, current_cycle_tab = st.tabs([
        "早于当前周期的历史延期", f"本周期待处理（{backlog_cycle}）"
    ])
    with historical_tab:
        render_production_cycle_group(
            historical_backlog_df,
            total_label="历史周期配音延期合计",
            chart_title="各系统历史周期配音延期数量",
        )
    with current_cycle_tab:
        render_production_cycle_group(
            current_cycle_backlog_df,
            total_label="本周期配音待处理合计",
            chart_title="各系统本周期配音待处理数量",
        )


def render_recognition_utilization_tab(df: pd.DataFrame):
    """展示每日产量、本周期机器日产量，以及当前周期电脑累计产量。"""
    st.header("🖥️ 识别产量情况")
    st.subheader("1. 各系统识别记录积压情况")
    _render_recognition_backlog(df)

    st.markdown("---")
    st.caption(
        "统计口径：生产机器不为空，且识别角色结束时间不为空的记录计为1条识别产出；"
        "按识别角色结束时间所在的自然日（00:00-24:00）统计。"
    )

    required_columns = {
        "生产机器", "识别角色结束时间", "生产周期", "系统", "入表时间", "生产状态"
    }
    missing_columns = required_columns.difference(df.columns)
    if df.empty or missing_columns:
        if missing_columns:
            st.info(f"缺少统计字段：{'、'.join(sorted(missing_columns))}")
        else:
            st.info("暂无可用于识别利用率统计的数据。")
        return

    utilization_df = df.copy()
    utilization_df["生产机器"] = (
        utilization_df["生产机器"]
        .fillna("")
        .astype(str)
        .str.strip()
        .str.replace(r"\.0+$", "", regex=True)
    )
    utilization_df["识别产出时间"] = pd.to_datetime(
        utilization_df["识别角色结束时间"], errors="coerce"
    )
    utilization_df = utilization_df[
        utilization_df["生产机器"].ne("")
        & utilization_df["生产机器"].isin(RECOGNITION_MACHINE_IDS)
        & utilization_df["识别产出时间"].notna()
    ].copy()

    if utilization_df.empty:
        st.info("暂无生产机器和识别角色结束时间均有效的记录。")
        return

    utilization_df["24小时周期"] = (
        utilization_df["识别产出时间"].dt.floor("D")
    )
    utilization_df["24小时周期显示"] = utilization_df["24小时周期"].dt.strftime(
        "%Y-%m-%d"
    )

    today = pd.Timestamp(get_local_now().date())
    earliest_day = today - pd.Timedelta(days=7)
    recent_daily_df = utilization_df[
        utilization_df["24小时周期"].between(earliest_day, today)
    ].copy()
    period_stats = (
        recent_daily_df.groupby(
            ["24小时周期", "24小时周期显示", "生产机器", "系统"],
            as_index=False,
        )
        .size()
        .rename(columns={"size": "识别产量"})
        .sort_values(["24小时周期", "生产机器", "系统"])
    )

    period_options = pd.date_range(earliest_day, today, freq="D").strftime(
        "%Y-%m-%d"
    ).tolist()
    selected_period = st.selectbox(
        "选择24小时周期",
        options=period_options,
        index=len(period_options) - 1,
        key="recognition_utilization_period",
    )

    st.subheader("2. 每天每台电脑的识别产量")
    st.caption(
        f"可选范围：{earliest_day.strftime('%Y-%m-%d')} 至 {today.strftime('%Y-%m-%d')}。"
    )
    recent_machine_options = RECOGNITION_MACHINE_IDS.copy()
    if not recent_machine_options:
        st.info("最近7天暂无识别产出记录。")
    else:
        selected_period_detail = period_stats[
            period_stats["24小时周期显示"] == selected_period
        ].copy()
        selected_period_total = int(selected_period_detail["识别产量"].sum())
        total_machine_count = len(recent_machine_options)
        selected_period_machine_average = (
            selected_period_total / total_machine_count
            if total_machine_count else 0
        )
        average_metric_column, _ = st.columns([1, 4])
        average_metric_column.metric(
            f"当天每台机器平均产量（{total_machine_count}台）",
            f"{selected_period_machine_average:.1f} 条",
            help=(
                f"当天总产量 {selected_period_total} 条 ÷ "
                f"当前统计机器总数 {total_machine_count} 台"
            ),
        )
        selected_period_pivot = selected_period_detail.pivot_table(
            index="生产机器",
            columns="系统",
            values="识别产量",
            aggfunc="sum",
            fill_value=0,
        ).reindex(
            index=recent_machine_options,
            columns=SYSTEMS,
            fill_value=0,
        )
        selected_period_pivot["电脑总产量"] = selected_period_pivot.sum(axis=1)
        selected_period_pivot = selected_period_pivot.sort_values(
            ["电脑总产量", "生产机器"], ascending=[False, True]
        )

        period_bar = Bar(
            init_opts=opts.InitOpts(width="100%", height="400px", theme="light")
        )
        period_machines = selected_period_pivot.index.astype(str).tolist()
        period_totals = selected_period_pivot["电脑总产量"].astype(int).tolist()
        period_system_colors = {
            "点众": "#5470c6",
            "红果": "#ee6666",
            "外部制作": "#9a60b4",
            "众益": "#91cc75",
        }
        period_bar.add_xaxis(period_machines)
        for system in SYSTEMS:
            system_values = selected_period_pivot[system].astype(int).tolist()
            period_bar.add_yaxis(
                series_name=system,
                y_axis=[value if value > 0 else None for value in system_values],
                stack="系统",
                label_opts=opts.LabelOpts(is_show=True, position="inside"),
                itemstyle_opts=opts.ItemStyleOpts(
                    color=period_system_colors.get(system, "#73c0de")
                ),
            )
        # 透明辅助柱与堆叠柱重合，用于在每台电脑柱顶显示当天总产量。
        period_bar.add_yaxis(
            series_name="总产量",
            y_axis=period_totals,
            gap="-100%",
            z=20,
            label_opts=opts.LabelOpts(
                is_show=True,
                position="top",
                distance=4,
                color="#2c3e50",
                font_size=12,
                font_weight="bold",
                formatter="{c}",
            ),
            itemstyle_opts=opts.ItemStyleOpts(color="rgba(0, 0, 0, 0)"),
            tooltip_opts=opts.TooltipOpts(is_show=False),
        )
        period_bar.set_global_opts(
            title_opts=opts.TitleOpts(
                title=f"{selected_period} 各电脑识别产量（按系统拆分）"
            ),
            xaxis_opts=opts.AxisOpts(
                name="生产电脑",
                axislabel_opts=opts.LabelOpts(rotate=45),
            ),
            yaxis_opts=opts.AxisOpts(name="识别产量（条）", min_=0),
            tooltip_opts=opts.TooltipOpts(
                trigger="axis", axis_pointer_type="shadow"
            ),
            legend_opts=opts.LegendOpts(pos_top="top", type_="scroll"),
        )
        # “总产量”仅用于柱顶标签，不显示在图例中。
        period_bar.options["legend"][0]["data"] = SYSTEMS
        render_pyecharts(period_bar)

    st.markdown("---")
    current_cycle = get_current_cycle()
    cycle_machine_df = utilization_df.copy()
    cycle_machine_df["生产周期"] = (
        cycle_machine_df["生产周期"].fillna("").astype(str).str.strip()
    )
    current_cycle_df = cycle_machine_df[
        cycle_machine_df["生产周期"].eq(current_cycle)
    ].copy()

    st.subheader(f"3. 本周期所有机器日产量曲线图（{current_cycle}）")
    all_cycle_records = df.copy()
    all_cycle_records["生产周期"] = (
        all_cycle_records["生产周期"].fillna("").astype(str).str.strip()
    )
    all_cycle_records["生产机器"] = (
        all_cycle_records["生产机器"]
        .fillna("")
        .astype(str)
        .str.strip()
        .str.replace(r"\.0+$", "", regex=True)
    )
    all_cycle_records["入表时间"] = pd.to_datetime(
        all_cycle_records["入表时间"], errors="coerce"
    )
    all_cycle_records = all_cycle_records[
        all_cycle_records["生产周期"].eq(current_cycle)
    ].copy()
    valid_cycle_entry_times = all_cycle_records["入表时间"].dropna()
    if valid_cycle_entry_times.empty:
        st.info(f"当前周期 {current_cycle} 没有有效入表时间，无法确定周期开始日期。")
    else:
        cycle_start_day = valid_cycle_entry_times.min().floor("D")
        curve_end_day = max(cycle_start_day, today)
        curve_days = pd.date_range(cycle_start_day, curve_end_day, freq="D")
        curve_day_labels = curve_days.strftime("%Y-%m-%d").tolist()
        curve_output_df = utilization_df[
            utilization_df["识别产出时间"].between(
                cycle_start_day,
                curve_end_day + pd.Timedelta(days=1),
                inclusive="left",
            )
        ].copy()
        daily_curve_machines = RECOGNITION_MACHINE_IDS.copy()

        st.caption(
            f"生产周期开始日期：{cycle_start_day.strftime('%Y-%m-%d')}（本周期最早入表日期）；"
            f"曲线范围：{cycle_start_day.strftime('%Y-%m-%d')} 至 {curve_end_day.strftime('%Y-%m-%d')}；"
            "产量按识别产出日期统计，不限制产出记录所属的生产周期。"
        )
        if not daily_curve_machines:
            st.info(f"当前周期 {current_cycle} 暂无已分配生产电脑的记录。")
        else:
            curve_output_df["识别产出日期"] = (
                curve_output_df["识别产出时间"].dt.floor("D")
            )
            curve_stats = (
                curve_output_df.groupby(
                    ["识别产出日期", "生产机器"], as_index=False
                )
                .size()
                .rename(columns={"size": "当日产量"})
            )
            machine_cycle_totals = (
                curve_stats.groupby("生产机器")["当日产量"].sum().to_dict()
            )
            daily_curve_machines = sorted(
                daily_curve_machines,
                key=lambda machine: (
                    -int(machine_cycle_totals.get(machine, 0)),
                    machine,
                ),
            )
            selected_curve_machines = daily_curve_machines
            st.caption("默认展示全部电脑；可直接点击图表顶部图例隐藏或显示对应电脑。")
            if not selected_curve_machines:
                st.info("请至少选择一台生产电脑。")
            else:
                curve_pivot = curve_stats.pivot_table(
                    index="识别产出日期",
                    columns="生产机器",
                    values="当日产量",
                    aggfunc="sum",
                    fill_value=0,
                ).reindex(
                    index=curve_days,
                    columns=daily_curve_machines,
                    fill_value=0,
                )

                daily_curve_line = Line(
                    init_opts=opts.InitOpts(
                        width="100%", height="460px", theme="light"
                    )
                )
                daily_curve_line.add_xaxis(curve_day_labels)
                for machine in selected_curve_machines:
                    daily_curve_line.add_yaxis(
                        series_name=machine,
                        y_axis=curve_pivot[machine].astype(int).tolist(),
                        is_smooth=False,
                        is_connect_nones=False,
                        symbol="circle",
                        symbol_size=7,
                        label_opts=opts.LabelOpts(is_show=False),
                        linestyle_opts=opts.LineStyleOpts(width=3),
                    )
                daily_curve_line.set_global_opts(
                    title_opts=opts.TitleOpts(title="本周期机器日产量对比"),
                    xaxis_opts=opts.AxisOpts(
                        name="识别产出日期",
                        axislabel_opts=opts.LabelOpts(rotate=30),
                    ),
                    yaxis_opts=opts.AxisOpts(name="当日识别产量（条）", min_=0),
                    tooltip_opts=opts.TooltipOpts(
                        trigger="axis",
                        axis_pointer_type="cross",
                        is_confine=True,
                        is_enterable=True,
                        hide_delay=300,
                        position=JsCode(
                            """
                            function(point, params, dom, rect, size) {
                                var gap = 12;
                                var contentWidth = size.contentSize[0];
                                var contentHeight = size.contentSize[1];
                                var viewWidth = size.viewSize[0];
                                var viewHeight = size.viewSize[1];
                                var x = point[0] + gap;
                                if (x + contentWidth > viewWidth - gap) {
                                    x = point[0] - contentWidth - gap;
                                }
                                x = Math.max(gap, Math.min(x, viewWidth - contentWidth - gap));
                                var y = point[1] - contentHeight / 2;
                                y = Math.max(gap, Math.min(y, viewHeight - contentHeight - gap));
                                return [x, y];
                            }
                            """
                        ),
                        formatter=JsCode(
                            """
                            function(params) {
                                if (!params || params.length === 0) {
                                    return '';
                                }
                                function getProductionValue(item) {
                                    var rawValue = item.value;
                                    if (Array.isArray(rawValue)) {
                                        return rawValue.length
                                            ? rawValue[rawValue.length - 1]
                                            : 0;
                                    }
                                    return rawValue == null ? 0 : rawValue;
                                }
                                var sortedParams = params.slice().sort(function(a, b) {
                                    return Number(getProductionValue(b))
                                        - Number(getProductionValue(a));
                                });
                                var columnCount = sortedParams.length > 18
                                    ? 3
                                    : (sortedParams.length > 7 ? 2 : 1);
                                var cells = sortedParams.map(function(item) {
                                    return '<div style="display:flex;align-items:center;'
                                        + 'justify-content:space-between;gap:12px;min-width:120px;">'
                                        + '<span style="white-space:nowrap;">'
                                        + item.marker + item.seriesName + '</span>'
                                        + '<strong>' + getProductionValue(item)
                                        + '</strong></div>';
                                }).join('');
                                return '<div style="min-width:180px;">'
                                    + '<div style="font-weight:700;margin-bottom:6px;">'
                                    + params[0].axisValue + '</div>'
                                    + '<div style="display:grid;grid-template-columns:repeat('
                                    + columnCount + ', minmax(120px, 1fr));'
                                    + 'column-gap:18px;row-gap:3px;">'
                                    + cells + '</div></div>';
                            }
                            """
                        ),
                        background_color="rgba(255, 255, 255, 0.98)",
                        border_color="#d1d5db",
                        border_width=1,
                        padding=10,
                        textstyle_opts=opts.TextStyleOpts(
                            color="#1f2937", font_size=12
                        ),
                        extra_css_text=(
                            "max-width: 520px; max-height: 420px; overflow-y: auto; "
                            "box-shadow: 0 6px 18px rgba(0, 0, 0, 0.18);"
                        ),
                    ),
                    legend_opts=opts.LegendOpts(pos_top="top", type_="scroll"),
                    datazoom_opts=[opts.DataZoomOpts(type_="inside")],
                )
                render_pyecharts(daily_curve_line, height=490)

    st.markdown("---")
    st.subheader(f"4. 当前周期各电脑累计识别产量（{current_cycle}）")
    cycle_total_metric_column, _ = st.columns([1, 4])
    cycle_total_metric_column.metric(
        "当前周期识别总产量",
        f"{len(current_cycle_df)} 条",
        help="生产机器和识别角色结束时间均有效，且生产周期为当前周期的记录总数。",
    )
    if current_cycle_df.empty:
        st.info(f"当前周期 {current_cycle} 暂无识别产出记录，以下电脑产量均为 0。")

    current_cycle_stats = (
        current_cycle_df.groupby(["生产机器", "系统"], as_index=False)
        .size()
        .rename(columns={"size": "识别产量"})
    )
    current_cycle_pivot = current_cycle_stats.pivot_table(
        index="生产机器",
        columns="系统",
        values="识别产量",
        aggfunc="sum",
        fill_value=0,
    ).reindex(
        index=RECOGNITION_MACHINE_IDS,
        columns=SYSTEMS,
        fill_value=0,
    )
    current_cycle_pivot["电脑总产量"] = current_cycle_pivot.sum(axis=1)
    current_cycle_pivot = current_cycle_pivot.sort_values(
        ["电脑总产量", "生产机器"], ascending=[False, True]
    )
    current_cycle_systems = [
        system for system in SYSTEMS if system in current_cycle_pivot.columns
    ]
    extra_systems = sorted(
        column for column in current_cycle_pivot.columns
        if column not in SYSTEMS and column != "电脑总产量"
    )
    current_cycle_systems.extend(extra_systems)

    system_colors = {
        "点众": "#5470c6",
        "红果": "#ee6666",
        "外部制作": "#9a60b4",
        "众益": "#91cc75",
    }
    current_cycle_bar = Bar(
        init_opts=opts.InitOpts(width="100%", height="440px", theme="light")
    )
    current_cycle_machines = current_cycle_pivot.index.astype(str).tolist()
    current_cycle_totals = current_cycle_pivot["电脑总产量"].astype(int).tolist()
    current_cycle_bar.add_xaxis(current_cycle_machines)
    for system in current_cycle_systems:
        system_values = current_cycle_pivot[system].astype(int).tolist()
        current_cycle_bar.add_yaxis(
            series_name=system,
            y_axis=[value if value > 0 else None for value in system_values],
            stack="系统",
            label_opts=opts.LabelOpts(is_show=True, position="inside"),
            itemstyle_opts=opts.ItemStyleOpts(
                color=system_colors.get(system, "#73c0de")
            ),
        )
    # 透明柱与堆叠柱重合，只负责在柱顶稳定显示每台电脑的汇总值。
    current_cycle_bar.add_yaxis(
        series_name="总产量",
        y_axis=current_cycle_totals,
        gap="-100%",
        z=20,
        label_opts=opts.LabelOpts(
            is_show=True,
            position="top",
            distance=4,
            color="#2c3e50",
            font_size=12,
            font_weight="bold",
            formatter="{c}",
        ),
        itemstyle_opts=opts.ItemStyleOpts(color="rgba(0, 0, 0, 0)"),
        tooltip_opts=opts.TooltipOpts(is_show=False),
    )
    # “总产量”是标签辅助系列，不放入图例。
    current_cycle_bar.options["legend"][0]["data"] = current_cycle_systems
    current_cycle_bar.set_global_opts(
        title_opts=opts.TitleOpts(title="当前周期各电脑累计产量（按系统拆分）"),
        xaxis_opts=opts.AxisOpts(
            name="生产电脑",
            axislabel_opts=opts.LabelOpts(rotate=45),
        ),
        yaxis_opts=opts.AxisOpts(name="累计识别产量（条）", min_=0),
        tooltip_opts=opts.TooltipOpts(trigger="axis", axis_pointer_type="shadow"),
        legend_opts=opts.LegendOpts(pos_top="top", type_="scroll"),
    )
    render_pyecharts(current_cycle_bar, height=470)


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

    online_col, detail_col = st.columns([1, 3])
    with detail_col:
        refresh_online = st.button(
            "刷新在线状态",
            key="refresh_auto_upload_online_clients",
        )
    if refresh_online:
        fetch_auto_upload_client_status.clear()
    client_status = fetch_auto_upload_client_status()

    with online_col:
        if client_status["supported"]:
            st.markdown(
                create_kpi_card(
                    "在线客户端电脑",
                    client_status["online_count"],
                    color="#3ba272",
                ),
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                create_kpi_card("在线客户端电脑", "状态未知", color="#95a5a6"),
                unsafe_allow_html=True,
            )

    with detail_col:
        if client_status["supported"]:
            worker_names = [
                str(item.get("worker_id") or "").strip()
                for item in client_status["clients"]
                if str(item.get("worker_id") or "").strip()
            ]
            if worker_names:
                st.caption(f"在线电脑：{'、'.join(worker_names)}")
            else:
                st.caption("当前没有客户端电脑在线")
            timeout_seconds = client_status.get("timeout_seconds")
            if timeout_seconds:
                st.caption(f"在线判定：最近 {timeout_seconds} 秒内有客户端请求")
        else:
            st.warning(client_status["error"])

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


def _get_production_analysis_cycles(df: pd.DataFrame) -> list:
    """从数据中的最新生产周期开始，返回向前 10 个周期。"""
    if df.empty or "生产周期" not in df.columns:
        return []

    cycle_values = pd.to_datetime(
        df["生产周期"].dropna().astype(str).str.strip(),
        format="%Y%m%d",
        errors="coerce",
    ).dropna()
    if cycle_values.empty:
        return []

    anchor = cycle_values.max()
    return [
        (anchor - timedelta(days=7 * offset)).strftime("%Y%m%d")
        for offset in range(PRODUCTION_ANALYSIS_CYCLE_COUNT - 1, -1, -1)
    ]


def _calculate_cycle_duration_hours(data: pd.DataFrame, end_col: str, start_col: str) -> pd.Series:
    """计算有效耗时；缺失、负耗时及超过 1000 小时的异常数据不进入平均值。"""
    start = pd.to_datetime(data[start_col], errors="coerce")
    end = pd.to_datetime(data[end_col], errors="coerce")
    hours = (end - start).dt.total_seconds() / 3600
    return hours.where((hours >= 0) & (hours < 1000))


def render_production_analysis_tab(df: pd.DataFrame):
    """展示从最新周期向前 10 个生产周期的耗时和错误率变化。"""
    st.header("📈 生产数据分析")
    cycles = _get_production_analysis_cycles(df)
    if not cycles:
        st.warning("暂无有效生产周期数据")
        return
    st.caption(
        f"统计范围：{cycles[0]} 至 {cycles[-1]}，共 {len(cycles)} 个周周期。"
        "耗时统计仅使用入表时间、开始生产时间、识别角色结束时间、处理BGM结束时间均不为空的完整链路记录，"
        "且四项耗时必须均有效；负耗时及 ≥1000 小时的数据视为异常值并排除。"
    )

    analysis_df = df[df["生产周期"].astype(str).isin(cycles)].copy()
    if analysis_df.empty:
        st.warning("所选 10 个周期暂无生产记录")
        return

    duration_definitions = {
        "识别总耗时": ("处理BGM结束时间", "入表时间"),
        "识别角色耗时": ("识别角色结束时间", "入表时间"),
        "BGM处理耗时": ("处理BGM结束时间", "识别角色结束时间"),
        "识别等待耗时": ("开始生产时间", "入表时间"),
    }
    for metric, (end_col, start_col) in duration_definitions.items():
        analysis_df[metric] = _calculate_cycle_duration_hours(analysis_df, end_col, start_col)

    # 四项耗时统一使用完全相同的完整链路样本，确保：
    # 平均识别总耗时 = 平均识别角色耗时 + 平均BGM处理耗时。
    required_time_columns = [
        "入表时间", "开始生产时间", "识别角色结束时间", "处理BGM结束时间"
    ]
    complete_chain_mask = analysis_df[required_time_columns].notna().all(axis=1)
    valid_duration_mask = analysis_df[list(duration_definitions)].notna().all(axis=1)
    complete_chain_mask &= valid_duration_mask
    for metric in duration_definitions:
        analysis_df.loc[~complete_chain_mask, metric] = None

    failure_type = analysis_df["失败类型"].fillna("").astype(str).str.strip()
    analysis_df["是否错误"] = failure_type.ne("")

    rows = []
    for cycle in cycles:
        cycle_df = analysis_df[analysis_df["生产周期"].astype(str) == cycle]
        total = len(cycle_df)
        error_count = int(cycle_df["是否错误"].sum()) if total else 0
        row = {
            "周期": cycle,
            "记录数": total,
            "错误数": error_count,
            "错误率": round(error_count / total * 100, 2) if total else None,
        }
        for metric in duration_definitions:
            value = cycle_df[metric].mean()
            row[metric] = round(float(value), 2) if pd.notna(value) else None
            row[f"{metric}样本数"] = int(cycle_df[metric].notna().sum())
        rows.append(row)

    summary_df = pd.DataFrame(rows)

    st.subheader("平均耗时变化曲线")
    duration_line = Line(init_opts=opts.InitOpts(width="100%", height="430px"))
    duration_line.add_xaxis(cycles)
    metric_colors = [COLORS["primary"], COLORS["danger"], COLORS["warning"], COLORS["cyan"]]
    for (metric, _), color in zip(duration_definitions.items(), metric_colors):
        duration_line.add_yaxis(
            metric,
            summary_df[metric].where(summary_df[metric].notna(), None).tolist(),
            is_smooth=True,
            is_connect_nones=False,
            symbol="circle",
            symbol_size=7,
            label_opts=opts.LabelOpts(is_show=False),
            linestyle_opts=opts.LineStyleOpts(width=3, color=color),
            itemstyle_opts=opts.ItemStyleOpts(color=color),
        )
    duration_line.set_global_opts(
        tooltip_opts=opts.TooltipOpts(trigger="axis"),
        legend_opts=opts.LegendOpts(pos_top="2%"),
        xaxis_opts=opts.AxisOpts(name="生产周期", boundary_gap=False),
        yaxis_opts=opts.AxisOpts(name="平均耗时（小时）", min_=0),
        datazoom_opts=[opts.DataZoomOpts(type_="inside")],
    )
    render_pyecharts(duration_line)
    st.caption("四项平均耗时均基于同一批完整链路记录；明细表中的四项耗时样本数应保持一致。")

    st.subheader("周期错误率变化曲线")
    error_line = Line(init_opts=opts.InitOpts(width="100%", height="380px"))
    error_line.add_xaxis(cycles)
    error_line.add_yaxis(
        "错误率",
        summary_df["错误率"].where(summary_df["错误率"].notna(), None).tolist(),
        is_smooth=True,
        symbol="circle",
        symbol_size=8,
        label_opts=opts.LabelOpts(is_show=True, formatter="{c}%"),
        linestyle_opts=opts.LineStyleOpts(width=3, color=COLORS["danger"]),
        itemstyle_opts=opts.ItemStyleOpts(color=COLORS["danger"]),
    )
    error_line.set_global_opts(
        tooltip_opts=opts.TooltipOpts(trigger="axis"),
        xaxis_opts=opts.AxisOpts(name="生产周期", boundary_gap=False),
        yaxis_opts=opts.AxisOpts(name="错误率（%）", min_=0, max_=100),
    )
    render_pyecharts(error_line)
    st.caption("错误率 = 失败类型非空的记录数 ÷ 周期总记录数。")

    st.subheader("错误记录明细")
    detail_cycle = st.selectbox(
        "查看周期",
        options=list(reversed(cycles)),
        index=0,
        key="production_analysis_error_detail_cycle",
    )
    error_detail_df = analysis_df[
        (analysis_df["生产周期"].astype(str) == detail_cycle) & analysis_df["是否错误"]
    ].copy()

    if error_detail_df.empty:
        st.success(f"周期 {detail_cycle} 无错误记录")
    else:
        error_detail_df["错误类型"] = error_detail_df["失败类型"].fillna("").astype(str).str.strip()
        missing_type = error_detail_df["错误类型"].eq("")
        error_detail_df.loc[missing_type, "错误类型"] = error_detail_df.loc[missing_type, "生产状态"].apply(
            lambda status: f"状态：{status}" if status else "未填写错误类型"
        )
        error_detail_df["入表时间"] = pd.to_datetime(
            error_detail_df["入表时间"], errors="coerce"
        ).dt.strftime("%Y-%m-%d %H:%M:%S").fillna("未填写")

        type_summary = (
            error_detail_df.groupby("错误类型", as_index=False)
            .size()
            .rename(columns={"size": "错误数量"})
            .sort_values("错误数量", ascending=False)
        )
        type_summary["占本周期错误比例(%)"] = (
            type_summary["错误数量"] / len(error_detail_df) * 100
        ).round(2)

        st.caption(f"周期 {detail_cycle} 共 {len(error_detail_df)} 条错误记录。")
        summary_col, detail_col = st.columns([1, 2])
        with summary_col:
            st.markdown("**错误类型汇总**")
            st.dataframe(type_summary, width="stretch", hide_index=True)
        with detail_col:
            st.markdown("**逐条错误明细**")
            error_columns = ["系统", "剧名", "生产状态", "错误类型", "入表时间"]
            st.dataframe(
                error_detail_df[error_columns].sort_values("入表时间"),
                width="stretch",
                hide_index=True,
            )

    with st.expander("查看各周期统计明细与有效样本数"):
        display_columns = ["周期", "记录数", "错误数", "错误率"]
        for metric in duration_definitions:
            display_columns.extend([metric, f"{metric}样本数"])
        display_df = summary_df[display_columns].rename(columns={"错误率": "错误率(%)"})
        st.dataframe(display_df, width="stretch", hide_index=True)


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


def get_recent_cycles(count: int = PANEL_RECENT_CYCLE_COUNT) -> list:
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


def _render_legacy_dubbing_tab(production_df: pd.DataFrame):
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


def _render_dubbing_duration_statistics(
    production_df: pd.DataFrame,
    language_label: str = "",
):
    """计算制作周期和系统平均制作总耗时。"""
    if production_df.empty:
        st.info(f"暂无{language_label}制作记录")
        return

    title_suffix = f"（{language_label}）" if language_label else ""
    if language_label:
        st.caption(f"当前耗时统计范围：{language_label}")

    required_columns = {
        "当前制作周期", "需求提交时间", "整备完成时间", "制作完成时间"
    }
    missing_columns = required_columns.difference(production_df.columns)
    if missing_columns:
        st.warning(f"剧制作表缺少字段：{'、'.join(sorted(missing_columns))}")
        return

    completed_df = production_df.copy()
    completed_df["申请日期"] = pd.to_datetime(
        completed_df["申请日期"], errors="coerce"
    )
    completed_df = completed_df[
        completed_df["系统"].eq("外部制作")
        | (
            completed_df["申请日期"].notna()
            & completed_df["申请日期"].ge(pd.Timestamp("2026-07-01"))
        )
    ].copy()
    if completed_df.empty:
        st.info("暂无符合申请日期范围的制作记录")
        return

    completed_df["当前制作周期"] = (
        completed_df["当前制作周期"].fillna("").astype(str).str.strip()
    )
    cycle_dates = pd.to_datetime(
        completed_df["当前制作周期"], format="%Y%m%d", errors="coerce"
    )
    available_cycles = sorted(
        completed_df.loc[cycle_dates.notna(), "当前制作周期"].unique().tolist()
    )
    recent_cycles = available_cycles[-8:]
    if not recent_cycles:
        st.info("暂无有效制作周期数据")
        return
    completed_df = completed_df[
        completed_df["当前制作周期"].isin(recent_cycles)
    ].copy()
    st.caption(f"图表数据范围：最近 {len(recent_cycles)} 个制作周期（{'、'.join(recent_cycles)}）")

    completed_df["需求提交时间"] = pd.to_datetime(
        completed_df["需求提交时间"], errors="coerce"
    )
    completed_df["整备完成时间"] = pd.to_datetime(
        completed_df["整备完成时间"], errors="coerce"
    )
    completed_df["制作完成时间"] = pd.to_datetime(
        completed_df["制作完成时间"], errors="coerce"
    )
    completed_df = completed_df[
        completed_df["需求提交时间"].notna() &
        completed_df["整备完成时间"].notna() &
        completed_df["制作完成时间"].notna()
    ].copy()

    if completed_df.empty:
        st.info("暂无需求提交时间、整备完成时间和制作完成时间均有效的制作记录")
        return

    # 以整备完成时间和需求提交时间中较晚的时间作为制作计时起点。
    completed_df["制作计时开始时间"] = completed_df[
        ["整备完成时间", "需求提交时间"]
    ].max(axis=1)
    completed_df["制作总耗时_小时"] = (
        completed_df["制作完成时间"] - completed_df["制作计时开始时间"]
    ).dt.total_seconds() / 3600

    def get_duration_exclusion_reason(row):
        reasons = []
        if not row["当前制作周期"]:
            reasons.append("当前制作周期为空")
        if row["制作总耗时_小时"] < 0:
            reasons.append("制作完成时间早于制作计时开始时间")
        elif row["制作总耗时_小时"] >= 1000:
            reasons.append("制作总耗时达到1000小时以上")
        return "；".join(reasons)

    completed_df["排除原因"] = completed_df.apply(
        get_duration_exclusion_reason, axis=1
    )
    completed_df["是否纳入曲线"] = completed_df["排除原因"].eq("")
    valid_duration_df = completed_df[completed_df["是否纳入曲线"]].copy()
    total_completed_count = len(completed_df)
    valid_duration_count = len(valid_duration_df)
    excluded_count = total_completed_count - valid_duration_count

    if excluded_count:
        st.warning(
            f"三个时间字段均有效的记录共 {total_completed_count} 条；"
            f"其中 {valid_duration_count} 条纳入曲线，{excluded_count} 条因周期或耗时异常被排除。"
        )

    cycle_stats = (
        valid_duration_df.groupby("当前制作周期", as_index=False)
        .agg(
            制作总耗时平均=("制作总耗时_小时", "mean"),
            完成记录数=("制作总耗时_小时", "count"),
        )
        .sort_values("当前制作周期")
    )
    cycle_stats = cycle_stats[cycle_stats["制作总耗时平均"].notna()].copy()

    if cycle_stats.empty:
        st.info("符合完成时间条件的记录暂无有效制作总耗时")
        return

    cycles = cycle_stats["当前制作周期"].tolist()
    average_times = cycle_stats["制作总耗时平均"].round(1).tolist()
    completed_counts = cycle_stats["完成记录数"].astype(int).tolist()

    line = (
        Line(init_opts=opts.InitOpts(width="100%", height="440px", theme="light"))
        .add_xaxis(cycles)
        .add_yaxis(
            series_name="制作总耗时平均",
            y_axis=average_times,
            symbol="circle",
            symbol_size=9,
            label_opts=opts.LabelOpts(
                is_show=True,
                position="top",
                formatter="{c}h",
            ),
            linestyle_opts=opts.LineStyleOpts(width=3, color="#9a60b4"),
            itemstyle_opts=opts.ItemStyleOpts(color="#9a60b4"),
        )
        .set_global_opts(
            title_opts=opts.TitleOpts(
                title=f"各制作周期平均制作总耗时{title_suffix}"
            ),
            xaxis_opts=opts.AxisOpts(
                name="制作周期",
                axislabel_opts=opts.LabelOpts(rotate=30),
            ),
            yaxis_opts=opts.AxisOpts(name="平均耗时（小时）", min_=0),
            tooltip_opts=opts.TooltipOpts(
                trigger="axis",
                formatter=JsCode(
                    """
                    function(params) {
                        var idx = params[0].dataIndex;
                        return '制作周期: ' + params[0].axisValue +
                            '<br/>制作总耗时平均: ' + params[0].value + 'h' +
                            '<br/>曲线样本数: ' + """ + str(completed_counts) + """[idx];
                    }
                    """
                ),
            ),
            legend_opts=opts.LegendOpts(pos_top="top"),
        )
    )
    render_pyecharts(line, height=470)
    st.caption(
        f"时间字段完整 {total_completed_count} 条，曲线纳入 {valid_duration_count} 条；"
        "统计条件：需求提交时间、整备完成时间和制作完成时间均不为空；"
        "制作总耗时按“制作完成时间 − max(整备完成时间, 需求提交时间)”计算，"
        "即使用两个起始时间中较晚的一个，异常耗时不纳入。"
    )

    st.subheader(f"🏢 各系统制作总耗时{title_suffix}")
    system_cycle_stats = (
        valid_duration_df.groupby(["系统", "当前制作周期"], as_index=False)
        .agg(
            制作总耗时平均=("制作总耗时_小时", "mean"),
            完成记录数=("制作总耗时_小时", "count"),
        )
    )
    system_colors = {
        "点众": "#5470c6",
        "红果": "#ee6666",
        "外部制作": "#9a60b4",
        "众益": "#91cc75",
    }
    system_line = Line(
        init_opts=opts.InitOpts(width="100%", height="440px", theme="light")
    )
    system_line.add_xaxis(cycles)
    displayed_system_count = 0
    for system in SYSTEMS:
        system_stats = system_cycle_stats[system_cycle_stats["系统"] == system]
        if system_stats.empty:
            continue
        value_map = system_stats.set_index("当前制作周期")[
            "制作总耗时平均"
        ].to_dict()
        values = [
            round(value_map[cycle], 1)
            if cycle in value_map and pd.notna(value_map[cycle]) else None
            for cycle in cycles
        ]
        count_map = system_stats.set_index("当前制作周期")["完成记录数"].to_dict()
        count_values = [
            int(count_map.get(cycle, 0))
            if pd.notna(count_map.get(cycle, 0)) else 0
            for cycle in cycles
        ]
        count_values_json = json.dumps(count_values)
        system_line.add_yaxis(
            series_name=system,
            y_axis=values,
            symbol="circle",
            symbol_size=8,
            is_connect_nones=False,
            label_opts=opts.LabelOpts(is_show=False),
            tooltip_opts={
                "valueFormatter": JsCode(
                    f"""
                    function(value, dataIndex) {{
                        var counts = {count_values_json};
                        var duration = Array.isArray(value)
                            ? value[value.length - 1]
                            : value;
                        return duration + 'h（' + (counts[dataIndex] || 0) + '条）';
                    }}
                    """
                )
            },
            linestyle_opts=opts.LineStyleOpts(
                width=3,
                color=system_colors.get(system, "#999999"),
            ),
            itemstyle_opts=opts.ItemStyleOpts(
                color=system_colors.get(system, "#999999")
            ),
        )
        displayed_system_count += 1

    if displayed_system_count:
        system_line.set_global_opts(
            title_opts=opts.TitleOpts(
                title=f"各系统各制作周期平均制作总耗时{title_suffix}"
            ),
            xaxis_opts=opts.AxisOpts(
                name="制作周期",
                axislabel_opts=opts.LabelOpts(rotate=30),
            ),
            yaxis_opts=opts.AxisOpts(name="平均耗时（小时）", min_=0),
            tooltip_opts=opts.TooltipOpts(trigger="axis"),
            legend_opts=opts.LegendOpts(pos_top="top", type_="scroll"),
        )
        render_pyecharts(system_line, height=470)
    else:
        st.info("暂无可按系统展示的有效制作耗时数据")


def render_dubbing_tab(production_df: pd.DataFrame):
    """展示配音积压，并按阿语/非阿语独立统计制作总耗时。"""
    st.header("🎤 配音情况")

    if production_df.empty:
        st.warning("暂无剧制作表数据")
        return

    st.subheader("📦 各系统配音记录积压情况")
    _render_production_backlog(production_df)
    st.markdown("---")

    if "语言" not in production_df.columns:
        st.warning("剧制作表缺少字段：语言")
        return

    def normalize_language(value) -> str:
        """兼容飞书单选、多选字段的字符串、字典和列表返回格式。"""
        if isinstance(value, list):
            language_values = []
            for item in value:
                if isinstance(item, dict):
                    item = item.get("text") or item.get("value") or ""
                item_text = str(item).strip()
                if item_text:
                    language_values.append(item_text)
            return "、".join(language_values)
        if isinstance(value, dict):
            value = value.get("text") or value.get("value") or ""
        if value is None or (not isinstance(value, str) and pd.isna(value)):
            return ""
        return str(value).strip()

    language_df = production_df.copy()
    language_df["语言"] = language_df["语言"].apply(normalize_language)
    non_arabic_df = language_df[language_df["语言"].ne("阿语")].copy()
    arabic_df = language_df[language_df["语言"].eq("阿语")].copy()

    non_arabic_tab, arabic_tab = st.tabs(["非阿语", "阿语"])
    with non_arabic_tab:
        if non_arabic_df.empty:
            st.info("暂无非阿语制作记录")
        else:
            st.caption(f"非阿语分组记录：{len(non_arabic_df)} 条")
            _render_dubbing_duration_statistics(non_arabic_df, "非阿语")
    with arabic_tab:
        if arabic_df.empty:
            st.info("暂无阿语制作记录")
        else:
            st.caption(f"阿语分组记录：{len(arabic_df)} 条")
            _render_dubbing_duration_statistics(arabic_df, "阿语")


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

    # 生产周期快捷选择，默认当前周期
    st.sidebar.markdown("### 生产周期")
    recent_cycle_options = get_recent_cycles()
    selected_cycle = st.sidebar.selectbox(
        "快捷选择最近周期",
        options=recent_cycle_options,
        index=0,
        format_func=format_recent_cycle_option,
        key="quick_cycle_select",
        help="默认选择当前周期，可切换最近五个生产周期"
    )

    # 刷新按钮
    if st.sidebar.button("🔄 刷新数据", key="refresh_data_btn"):
        st.cache_data.clear()
        st.rerun()
    st.sidebar.caption("数据缓存 30 分钟；如需最新数据，请点击“刷新数据”。")

    st.sidebar.markdown("---")
    st.sidebar.markdown("### 📝 说明")
    st.sidebar.markdown("- 默认显示当前生产周期")
    st.sidebar.markdown("- 可切换最近五个生产周期")
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

    tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs([
        "📊 概览", "🎤 配音情况", "🚨 识别警报", "🖥️ 识别产量情况",
        "📋 生产日报", "🚀 自动上传统计",
        "📅 生产任务排期"
    ])

    with tab1:
        render_overview_tab(df_filtered, trend_df=df)

    with tab2:
        with st.spinner("正在拉取各系统剧制作表数据..."):
            production_df = fetch_all_systems_production_data()
        render_dubbing_tab(production_df)

    with tab3:
        render_recognition_alert_tab(df_filtered)

    with tab4:
        render_recognition_utilization_tab(df)

    with tab5:
        render_daily_report_tab(df_filtered)

    with tab6:
        with st.spinner("正在拉取各系统上传表数据..."):
            upload_df = fetch_upload_statistics_data()
        render_upload_statistics_tab(upload_df)

    with tab7:
        render_production_schedule_tab(df)


if __name__ == "__main__":
    main()
