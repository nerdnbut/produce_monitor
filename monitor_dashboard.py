import streamlit as st
import pandas as pd
import json
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv

# 先加载环境变量，再导入数据库模块
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
env_path = os.path.join(parent_dir, ".env")

# 尝试加载 .env，如果失败则尝试从项目根目录加载
if not os.path.exists(env_path):
    env_path = os.path.join(current_dir, ".env")
if not os.path.exists(env_path):
    env_path = os.path.join(os.path.dirname(parent_dir), ".env")

loaded = load_dotenv(env_path, override=True)

# 导入数据库操作模块
from ali_rds_server import (
    get_production_records, get_dubbing_records,
    get_all_machines, execute_reset, delete_production_log,
    mark_production_record_correct, clear_request_id_cache,
    get_copyright_process_logs, get_copyright_process_stats
)

st.set_page_config(page_title="自动化生产监控面板", layout="wide")

# --- 会话状态初始化 ---
if "reset_results" not in st.session_state:
    st.session_state.reset_results = {}
if "processed_logs" not in st.session_state:
    st.session_state.processed_logs = set()
if "current_module" not in st.session_state:
    st.session_state.current_module = "生产监控"  # 生产监控 / 版权处理日志

# --- 获取数据 ---
@st.cache_data(ttl=10)  # 10秒缓存
def load_all_records(limit=500):
    """获取所有生产记录"""
    return get_production_records(limit=limit)

@st.cache_data(ttl=10)  # 10秒缓存
def load_copyright_logs(limit=500):
    """获取版权处理日志"""
    return get_copyright_process_logs(limit=limit)

@st.cache_data(ttl=60)  # 60秒缓存
def load_copyright_process_stats():
    """获取版权处理统计"""
    return get_copyright_process_stats()

def format_duration(seconds):
    """格式化时长"""
    if seconds is None or seconds == 0:
        return "-"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.1f}分钟"
    hours = minutes / 60
    return f"{hours:.2f}小时"

# --- 侧边栏统计 ---
with st.sidebar:
    st.title("📊 生产概览")

    all_records = load_all_records(limit=1000)
    today = datetime.now().strftime('%Y-%m-%d')

    today_records = [r for r in all_records if str(r.get('created_at', '')).startswith(today)]
    completed_count = len([r for r in today_records if r['status'] == '识别合并完成' or r['status'] == 'BGM完成'])
    running_count = len(set([r['machine_id'] for r in all_records if r['status'] in ['识别合并中']]))
    fail_count = len([r for r in today_records if r['status'] == '失败'])

    st.metric("今日产出", completed_count)
    st.metric("运行中机器", running_count)
    st.metric("今日失败", fail_count, delta_color="inverse")

# --- 主界面 ---
st.title("🏭 自动化生产监控面板")


# ==================== 面板函数 ====================

def show_copyright_panel():
    """显示版权处理日志面板"""
    st.markdown("## ©️ 版权处理日志")

    # 获取版权日志数据
    copyright_logs = load_copyright_logs(limit=500)
    copyright_stats = load_copyright_process_stats()

    # 统计卡片
    stat_col1, stat_col2, stat_col3, stat_col4 = st.columns(4)
    with stat_col1:
        st.metric("今日收到邮件", copyright_stats.get("today_received", 0))
    with stat_col2:
        st.metric("今日处理完成", copyright_stats.get("today_completed", 0))
    with stat_col3:
        st.metric("今日处理失败", copyright_stats.get("today_failed", 0), delta_color="inverse")
    with stat_col4:
        st.metric("正在处理中", copyright_stats.get("processing", 0))

    # 筛选区域
    st.markdown("### 🔍 筛选条件")
    filter_col1, filter_col2, filter_col3, filter_col4 = st.columns(4)

    with filter_col1:
        search_video = st.text_input("🎬 视频标题搜索", placeholder="输入视频标题...", key="copyright_search_video")
    with filter_col2:
        status_filter = st.selectbox(
            "📌 状态筛选",
            ["全部", "收到邮件", "开始处理", "部分完成", "完成", "失败"],
            key="copyright_status_filter"
        )
    with filter_col3:
        channel_filter = st.text_input("📺 频道筛选", placeholder="输入频道名称...", key="copyright_channel_filter")
    with filter_col4:
        date_filter = st.selectbox(
            "📅 时间范围",
            ["全部", "今天", "最近3天", "最近7天"],
            key="copyright_date_filter"
        )

    # 筛选数据
    filtered_logs = copyright_logs

    if search_video:
        filtered_logs = [l for l in filtered_logs if search_video.lower() in l.get('video_title', '').lower()]

    if status_filter != "全部":
        filtered_logs = [l for l in filtered_logs if l.get('status') == status_filter]

    if channel_filter:
        filtered_logs = [l for l in filtered_logs if channel_filter.lower() in l.get('channel', '').lower()]

    if date_filter != "全部":
        today = datetime.now().date()
        if date_filter == "今天":
            cutoff = today
        elif date_filter == "最近3天":
            cutoff = today - timedelta(days=3)
        else:  # 最近7天
            cutoff = today - timedelta(days=7)
        filtered_logs = [l for l in filtered_logs if l.get('email_received_time') and pd.to_datetime(l['email_received_time']).date() >= cutoff]

    # 显示统计
    st.markdown(f"**共 {len(filtered_logs)} 条记录**")

    # 构建表格数据
    table_data = []
    for log in filtered_logs:
        # 解析JSON字段
        music_names = log.get('music_names')
        if isinstance(music_names, str):
            try:
                music_names = json.loads(music_names)
            except:
                music_names = []

        processed_songs = log.get('processed_songs')
        if isinstance(processed_songs, str):
            try:
                processed_songs = json.loads(processed_songs)
            except:
                processed_songs = []

        # 格式化时间
        email_time_str = "-"
        if log.get('email_received_time'):
            try:
                if hasattr(log['email_received_time'], 'strftime'):
                    email_time_str = log['email_received_time'].strftime('%m-%d %H:%M')
                else:
                    email_dt = datetime.fromisoformat(str(log['email_received_time'])) if isinstance(log['email_received_time'], str) else log['email_received_time']
                    email_time_str = email_dt.strftime('%m-%d %H:%M')
            except:
                email_time_str = str(log['email_received_time'])[-16:] if len(str(log['email_received_time'])) > 16 else "-"

        start_time_str = "-"
        if log.get('start_time'):
            try:
                if hasattr(log['start_time'], 'strftime'):
                    start_time_str = log['start_time'].strftime('%H:%M')
                else:
                    start_dt = datetime.fromisoformat(str(log['start_time'])) if isinstance(log['start_time'], str) else log['start_time']
                    start_time_str = start_dt.strftime('%H:%M')
            except:
                pass

        end_time_str = "-"
        if log.get('end_time'):
            try:
                if hasattr(log['end_time'], 'strftime'):
                    end_time_str = log['end_time'].strftime('%H:%M')
                else:
                    end_dt = datetime.fromisoformat(str(log['end_time'])) if isinstance(log['end_time'], str) else log['end_time']
                    end_time_str = end_dt.strftime('%H:%M')
            except:
                pass

        # 计算耗时
        duration_str = "-"
        if log.get('start_time') and log.get('end_time'):
            try:
                if hasattr(log['start_time'], 'total_seconds'):
                    duration_sec = (log['end_time'] - log['start_time']).total_seconds()
                else:
                    start_dt = datetime.fromisoformat(str(log['start_time'])) if isinstance(log['start_time'], str) else log['start_time']
                    end_dt = datetime.fromisoformat(str(log['end_time'])) if isinstance(log['end_time'], str) else log['end_time']
                    duration_sec = (end_dt - start_dt).total_seconds()
                if duration_sec > 0:
                    duration_min = duration_sec / 60
                    duration_str = f"{duration_min:.1f}分钟"
            except:
                pass

        # 格式化音乐列表
        music_str = ", ".join(music_names) if music_names else "-"
        processed_str = ", ".join(processed_songs) if processed_songs else "-"

        # 计算剩余音乐
        remaining_count = len(music_names) - len(processed_songs) if music_names else 0

        table_data.append({
            "频道": log.get('channel', '-'),
            "视频标题": log.get('video_title', '-'),
            "音乐列表": music_str,
            "版权方": log.get('owner', '-'),
            "状态": log.get('status', '-'),
            "已处理": processed_str,
            "剩余": str(remaining_count) if music_names else "-",
            "失败数": str(log.get('failed_count', 0)),
            "失败原因": log.get('fail_reason', '')[:30] if log.get('fail_reason') else "",
            "邮件时间": email_time_str,
            "开始时间": start_time_str,
            "结束时间": end_time_str,
            "耗时": duration_str,
            "处理结果": log.get('process_result', '')[:50] if log.get('process_result') else "",
            "raw_log": log
        })

    # 显示表格
    if table_data:
        def color_status_copyright(val):
            if val == "完成":
                return "background-color: #d4edda"
            elif val == "失败":
                return "background-color: #f8d7da"
            elif val in ["开始处理", "部分完成"]:
                return "background-color: #fff3cd"
            return ""

        df = pd.DataFrame(table_data)
        display_df = df.drop(columns=['raw_log'])

        styled_df = display_df.style.map(
            color_status_copyright,
            subset=['状态']
        )

        st.dataframe(
            styled_df,
            width='stretch',
            height=400,
            column_config={
                "频道": st.column_config.TextColumn("频道", width="small"),
                "视频标题": st.column_config.TextColumn("视频标题", width="large"),
                "音乐列表": st.column_config.TextColumn("音乐列表", width="large"),
                "版权方": st.column_config.TextColumn("版权方", width="medium"),
                "状态": st.column_config.TextColumn("状态", width="small"),
                "已处理": st.column_config.TextColumn("已处理", width="medium"),
                "剩余": st.column_config.TextColumn("剩余", width="small"),
                "失败数": st.column_config.TextColumn("失败数", width="small"),
                "失败原因": st.column_config.TextColumn("失败原因", width="medium"),
                "邮件时间": st.column_config.TextColumn("邮件时间", width="small"),
                "开始时间": st.column_config.TextColumn("开始时间", width="small"),
                "结束时间": st.column_config.TextColumn("结束时间", width="small"),
                "耗时": st.column_config.TextColumn("耗时", width="small"),
                "处理结果": st.column_config.TextColumn("处理结果", width="large"),
            }
        )

        # 详情展开区域
        st.markdown("---")
        st.subheader("📋 详情查看")

        # 按状态分组显示
        for status in ["收到邮件", "开始处理", "部分完成", "完成", "失败"]:
            status_logs = [t for t in table_data if t['raw_log'].get('status') == status]
            if status_logs:
                with st.expander(f"{status} ({len(status_logs)}条)", expanded=False):
                    for item in status_logs[:20]:  # 最多显示20条
                        log_data = item['raw_log']
                        st.markdown(f"**频道**: {item['频道']} | **视频**: {item['视频标题']}")
                        col_a, col_b = st.columns(2)
                        with col_a:
                            st.write(f"**音乐列表**: {item['音乐列表']}")
                            st.write(f"**版权方**: {item['版权方']}")
                            st.write(f"**已处理**: {item['已处理']}")
                        with col_b:
                            st.write(f"**邮件时间**: {item['邮件时间']}")
                            st.write(f"**处理时间**: {item['开始时间']} - {item['结束时间']}")
                            st.write(f"**耗时**: {item['耗时']}")
                        if status == "失败":
                            st.error(f"**失败原因**: {item['失败原因']}")
                            if item['处理结果']:
                                st.write(f"**处理结果**: {item['处理结果']}")
                        st.markdown("---")
    else:
        st.info("📭 没有找到匹配的版权处理记录")


def show_production_panel():
    """显示生产监控面板（原有代码）"""
    # 筛选区域
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        search_name = st.text_input("🔍 剧名搜索", placeholder="输入剧名...")

    with col2:
        status_filter = st.selectbox(
            "📌 状态筛选",
            ["全部", "识别合并中", "识别合并完成", "处理BGM中", "BGM完成", "配音中", "配音完成", "失败"]
        )

    with col3:
        date_range = st.selectbox(
            "📅 时间范围",
            ["全部", "今天", "最近3天", "最近7天", "最近30天"]
        )

    with col4:
        machine_filter = st.selectbox(
            "🖥️ 机器筛选",
            ["全部"] + get_all_machines()
        )

    # 筛选数据
    filtered_records = all_records

    if search_name:
        filtered_records = [r for r in filtered_records if search_name.lower() in r.get('movie_name', '').lower()]

    if status_filter != "全部":
        filtered_records = [r for r in filtered_records if r['status'] == status_filter]

    if date_range != "全部":
        today = datetime.now().date()
        if date_range == "今天":
            cutoff = today
        elif date_range == "最近3天":
            cutoff = today - timedelta(days=3)
        elif date_range == "最近7天":
            cutoff = today - timedelta(days=7)
        else:  # 最近30天
            cutoff = today - timedelta(days=30)

        filtered_records = [r for r in filtered_records if r.get('created_at') and pd.to_datetime(r['created_at']).date() >= cutoff]

    if machine_filter != "全部":
        filtered_records = [r for r in filtered_records if r.get('machine_id') == machine_filter]

    # === 构建表格数据 ===
    table_data = []
    for record in filtered_records:
        # 解析JSON字段
        role_source = record.get('role_source_distribution')
        if isinstance(role_source, str):
            try:
                role_source = json.loads(role_source)
            except:
                role_source = {}

        role_ratio = record.get('role_ratio_stats')
        if isinstance(role_ratio, str):
            try:
                role_ratio = json.loads(role_ratio)
            except:
                role_ratio = {}

        av_sync = record.get('av_sync_result')
        if isinstance(av_sync, str):
            try:
                av_sync = json.loads(av_sync)
            except:
                av_sync = {}

        # 计算识别合并耗时和格式化时间
        # 开始时间 = recognition_merge_start_time
        # 结束时间 = recognition_merge_end_time
        recognition_merge_start_time = record.get('recognition_merge_start_time')
        recognition_merge_end_time = record.get('recognition_merge_end_time')
        status = record.get('status', '')

        duration_str = "-"
        start_time_str = "-"
        end_time_str = "-"

        # 格式化开始时间 (recognition_merge_start_time)
        if recognition_merge_start_time:
            try:
                if hasattr(recognition_merge_start_time, 'strftime'):
                    start_time_str = recognition_merge_start_time.strftime('%m-%d %H:%M')
                else:
                    start_dt = datetime.fromisoformat(str(recognition_merge_start_time)) if isinstance(recognition_merge_start_time, str) else recognition_merge_start_time
                    start_time_str = start_dt.strftime('%m-%d %H:%M')
            except:
                start_time_str = str(recognition_merge_start_time)[-16:] if len(str(recognition_merge_start_time)) > 16 else "-"

        # 格式化结束时间 (recognition_merge_end_time)
        if recognition_merge_end_time:
            try:
                if hasattr(recognition_merge_end_time, 'strftime'):
                    end_time_str = recognition_merge_end_time.strftime('%m-%d %H:%M')
                else:
                    end_dt = datetime.fromisoformat(str(recognition_merge_end_time)) if isinstance(recognition_merge_end_time, str) else recognition_merge_end_time
                    end_time_str = end_dt.strftime('%m-%d %H:%M')
            except:
                end_time_str = str(recognition_merge_end_time)[-16:] if len(str(recognition_merge_end_time)) > 16 else "-"
        elif status not in ['识别合并完成', 'BGM完成']:
            end_time_str = "进行中"

        # 计算耗时（小时）
        if recognition_merge_start_time and recognition_merge_end_time:
            try:
                if hasattr(recognition_merge_start_time, 'total_seconds'):
                    duration_sec = (recognition_merge_end_time - recognition_merge_start_time).total_seconds()
                else:
                    start_dt = datetime.fromisoformat(str(recognition_merge_start_time)) if isinstance(recognition_merge_start_time, str) else recognition_merge_start_time
                    end_dt = datetime.fromisoformat(str(recognition_merge_end_time)) if isinstance(recognition_merge_end_time, str) else recognition_merge_end_time
                    duration_sec = (end_dt - start_dt).total_seconds()
                if duration_sec > 0:
                    duration_hours = duration_sec / 3600
                    duration_str = f"{duration_hours:.2f}h"
            except:
                pass
        else:
            duration_str = "-"

        # 格式化角色来源
        source_str = ", ".join([f"{k}:{v}" for k, v in role_source.items()]) if role_source else "-"

        # 格式化角色比例
        ratio_str = ", ".join([f"{k}:{v*100:.0f}%" for k, v in role_ratio.items()]) if role_ratio else "-"

        # 格式化音画同步
        av_sync_str = "-"
        if av_sync:
            results = []
            for video_name, sync_info in av_sync.items():
                if isinstance(sync_info, dict):
                    pass_status = "通过" if sync_info.get("pass") else "失败"
                    diff = sync_info.get("diff_sec", 0)
                    display_name = "完整" if video_name == "完整_mp4" else "抹字"
                    results.append(f"{display_name}:{pass_status}({diff:+.1f}s)")
            av_sync_str = ", ".join(results) if results else "-"

        # 格式化字幕持续时长异常
        subtitle_abnormal_str = "-"
        subtitle_abnormal = record.get('subtitle_duration_abnormal')
        if subtitle_abnormal:
            if isinstance(subtitle_abnormal, str):
                try:
                    subtitle_abnormal = json.loads(subtitle_abnormal)
                except:
                    subtitle_abnormal = {}
            if isinstance(subtitle_abnormal, dict):
                abnormal_count = subtitle_abnormal.get("abnormal_count", 0)
                total_count = subtitle_abnormal.get("total_count", 0)
                if abnormal_count > 0:
                    subtitle_abnormal_str = f"⚠️ {abnormal_count}/{total_count}"

        table_data.append({
            "剧名": str(record.get('movie_name', '-')),
            "状态": str(record['status']),
            "机器": str(record.get('machine_id', '-')),
            "需求方": str(record.get('demand_party', '')),
            "时长(h)": str(f"{record.get('duration_h', 0):.2f}") if record.get('duration_h') else "-",
            "字幕数": str(int(record.get('subtitle_count', 0))) if record.get('subtitle_count') else "-",
            "开始时间": str(start_time_str),
            "结束时间": str(end_time_str),
            "识别耗时": str(duration_str),
            "OCR类型": str("火山" if record.get('used_volc_ocr') else "本地"),
            "OCR检查": str("通过" if record.get('ocr_check_pass') else "异常" if record.get('ocr_check_pass') is not None else "-"),
            "字幕高度": str(f"{record.get('median_h'):.0f}px") if record.get('median_h') else "-",
            "角色来源": str(source_str),
            "角色比例": str(ratio_str),
            "音画同步": str(av_sync_str),
            "字幕异常": str(subtitle_abnormal_str),
            "来源表": str(record.get('core_table', 'Default')),
            "创建时间": str(str(record.get('created_at', ''))[-8:] if record.get('created_at') else "-"),
            "错误信息": str(record.get('error_msg', '')[:50] if record.get('error_msg') else ""),
            "id": record.get('id'),
            "raw_record": record
        })

    # === 显示统计 ===
    st.markdown(f"**共 {len(table_data)} 条记录**")

    # === 主表格 ===
    if table_data:
        # 状态颜色映射
        def color_status(val):
            if val == "配音完成":
                return "background-color: #d4edda"
            elif val == "失败":
                return "background-color: #f8d7da"
            elif "中" in val:
                return "background-color: #fff3cd"
            return ""

        # 显示表格
        df = pd.DataFrame(table_data)

        # 删除内部使用的列
        display_df = df.drop(columns=['id', 'raw_record'])

        # 应用状态颜色
        styled_df = display_df.style.map(
            color_status,
            subset=['状态']
        )

        st.dataframe(
            styled_df,
            width='stretch',
            height=400,
            column_config={
                "剧名": st.column_config.TextColumn("剧名", width="medium"),
                "状态": st.column_config.TextColumn("状态", width="small"),
                "机器": st.column_config.TextColumn("机器", width="small"),
                "需求方": st.column_config.TextColumn("需求方", width="small"),
                "时长(h)": st.column_config.TextColumn("时长(h)", width="small"),
                "字幕数": st.column_config.TextColumn("字幕数", width="small"),
                "开始时间": st.column_config.TextColumn("开始时间", width="small"),
                "结束时间": st.column_config.TextColumn("结束时间", width="small"),
                "识别耗时": st.column_config.TextColumn("识别耗时", width="small"),
                "OCR类型": st.column_config.TextColumn("OCR", width="small"),
                "OCR检查": st.column_config.TextColumn("OCR检查", width="small"),
                "字幕高度": st.column_config.TextColumn("字幕高度", width="small"),
                "角色来源": st.column_config.TextColumn("角色来源", width="medium"),
                "角色比例": st.column_config.TextColumn("角色比例", width="medium"),
                "音画同步": st.column_config.TextColumn("音画同步", width="medium"),
                "字幕异常": st.column_config.TextColumn("字幕异常", width="small"),
                "来源表": st.column_config.TextColumn("来源表", width="small"),
                "创建时间": st.column_config.TextColumn("创建时间", width="small"),
                "错误信息": st.column_config.TextColumn("错误信息", width="large"),
            }
        )

        # === 失败任务操作区 ===
        st.markdown("---")
        st.subheader("🚨 失败任务处理 (仅识别合并阶段)")

        # 只显示识别合并阶段失败的记录（排除BGM处理失败的）
        failed_records = []
        for r in table_data:
            raw = r['raw_record']
            if raw['status'] == '失败':
                stage = raw.get('stage', '')
                # 排除BGM相关阶段失败的记录
                if 'BGM' not in stage and 'bgm' not in stage:
                    failed_records.append(r)
        failed_records = [r for r in failed_records if r['id'] not in st.session_state.processed_logs]

        if failed_records:
            # 按来源表分组
            from collections import defaultdict
            grouped_records = defaultdict(list)
            for item in failed_records:
                core_table = item['raw_record'].get('core_table', 'Default')
                grouped_records[core_table].append(item)

            # 按来源表分组显示
            for core_table, records in grouped_records.items():
                table_display_name = core_table if core_table and core_table != "Default" else "Default (默认)"
                st.markdown(f"### 📁 {table_display_name} ({len(records)}条)")
                st.markdown("---")

                for item in records:
                    record = item['raw_record']
                    record_id = record.get('id')
                    movie_name = record.get('movie_name', '未知')
                    machine_id = record.get('machine_id', '')
                    error_msg = record.get('error_msg', '')

                    with st.expander(f"❌ {movie_name} (机器: {machine_id})", expanded=False):
                        col_info1, col_info2 = st.columns(2)
                        with col_info1:
                            st.write(f"**错误:** {error_msg}")
                            st.write(f"**NAS:** {record.get('nas_path', '')}")
                        with col_info2:
                            st.write(f"**时间:** {record.get('created_at', '')}")
                            st.write(f"**阶段:** {record.get('stage', '')}")

                        # 检查是否有必要的 ID 字段
                        has_required_ids = all([
                            record.get("app_token"),
                            record.get("repo_table_id"),
                            record.get("produce_table_id"),
                            record.get("repo_record_id"),
                            record.get("produce_record_id")
                        ])

                        if not has_required_ids:
                            st.warning("⚠️ 缺少关键 ID 信息，无法重置")
                            continue

                        col1, col2, col3, col4 = st.columns(4)

                        with col1:
                            if st.button("🔄 通用重置", key=f"reset_a_{record_id}", use_container_width=True):
                                result = execute_reset(
                                    app_token=record["app_token"],
                                    repo_table_id=record["repo_table_id"],
                                    produce_table_id=record["produce_table_id"],
                                    repo_record_id=record["repo_record_id"],
                                    produce_record_id=record["produce_record_id"],
                                    machine_id=machine_id,
                                    mode=1
                                )
                                if result.get("success"):
                                    st.toast("✅ 通用重置成功！")
                                else:
                                    st.toast(f"❌ 重置失败: {result.get('error', '未知错误')}")
                                st.rerun()

                        with col2:
                            if st.button("💻 本机重置", key=f"reset_b_{record_id}", use_container_width=True):
                                result = execute_reset(
                                    app_token=record["app_token"],
                                    repo_table_id=record["repo_table_id"],
                                    produce_table_id=record["produce_table_id"],
                                    repo_record_id=record["repo_record_id"],
                                    produce_record_id=record["produce_record_id"],
                                    machine_id=machine_id,
                                    mode=2
                                )
                                if result.get("success"):
                                    st.toast(f"✅ 已指定给 {machine_id} 号机处理")
                                else:
                                    st.toast(f"❌ 重置失败: {result.get('error', '未知错误')}")
                                st.rerun()

                        with col3:
                            if st.button("✅ 标记正确", key=f"mark_correct_{record_id}", use_container_width=True):
                                if mark_production_record_correct(record_id):
                                    st.toast(f"✅ 已标记为正确")
                                else:
                                    st.toast("❌ 标记失败")
                                st.rerun()

                        with col4:
                            if st.button("🧹 清除缓存", key=f"clear_cache_{record_id}", use_container_width=True):
                                result = clear_request_id_cache(movie_name)
                                if result.get("success"):
                                    st.toast(f"✅ {result.get('msg', '缓存清除成功')}")
                                else:
                                    st.toast(f"❌ 清除失败: {result.get('error', '未知错误')}")

                        # 从列表移除
                        if st.button("🗑️ 移除", key=f"remove_{record_id}", use_container_width=True):
                            if delete_production_log(record_id):
                                st.session_state.processed_logs.add(record_id)
                                st.toast("🗑️ 已移除")
                                st.rerun()
        else:
            st.success("✅ 目前没有待处理的失败任务！")
    else:
        st.info("📭 没有找到匹配的记录")


# ==================== 主界面调用 ====================

# 模块切换按钮
col1, col2, col3, col4 = st.columns([1, 1, 1, 2])
with col1:
    if st.button("📊 生产监控", use_container_width=True, type="primary" if st.session_state.current_module == "生产监控" else "secondary"):
        st.session_state.current_module = "生产监控"
        st.rerun()
with col2:
    if st.button("©️ 版权处理日志", use_container_width=True, type="primary" if st.session_state.current_module == "版权处理日志" else "secondary"):
        st.session_state.current_module = "版权处理日志"
        st.rerun()

st.markdown("---")

# 根据模块显示不同内容
if st.session_state.current_module == "版权处理日志":
    show_copyright_panel()
else:
    show_production_panel()

# 底部刷新按钮
st.markdown("---")
if st.button("🔄 刷新数据", use_container_width=True):
    st.cache_data.clear()
    st.rerun()
