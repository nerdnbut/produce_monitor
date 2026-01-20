import streamlit as st
import pandas as pd
import requests
import json
import plotly.express as px
from datetime import datetime
import os
from dotenv import load_dotenv

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
env_path = os.path.join(parent_dir, ".env")
load_dotenv(env_path)
api_host = os.getenv("API_HOST")
api_port = os.getenv("API_PORT")
API_URL = f"http://{api_host}:{api_port}/monitor"

st.set_page_config(page_title="自动化生产监控面板", layout="wide")

# --- 数据获取 ---
def fetch_data():
    try:
        target_url = f"{API_URL}/stats"
        print(f"正在连接: {target_url}")
        r = requests.get(target_url)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        st.error(f"无法连接到监控服务器: {API_URL}")
        print(f"连接错误详情: {e}")
        return {"logs": [], "errors": []}
    return {"logs": [], "errors": []}

data = fetch_data()
df_logs = pd.DataFrame(data["logs"])

# 数据预处理
if not df_logs.empty:
    df_logs['created_at'] = pd.to_datetime(df_logs['created_at'])
    # 解析 JSON 字段
    df_logs['role_ratios'] = df_logs['role_ratio_stats'].apply(lambda x: json.loads(x) if x else {})
    df_logs['sources'] = df_logs['role_source_distribution'].apply(lambda x: json.loads(x) if x else {})

# --- 侧边栏：全局统计 ---
st.sidebar.title("🏭 生产概览")
if not df_logs.empty:
    today_count = df_logs[df_logs['status'] == '完成'].shape[0]
    fail_count = df_logs[df_logs['status'] == '失败'].shape[0]
    processing_count = df_logs[df_logs['status'] == '运行中'].drop_duplicates(subset=['machine_id']).shape[0]
    
    st.sidebar.metric("今日产出 (条)", today_count)
    st.sidebar.metric("今日失败", fail_count, delta_color="inverse")
    st.sidebar.metric("正在运行机器", processing_count)

# --- 主面板 Tabs ---
tab1, tab2, tab3 = st.tabs(["📊 数量与机器状态", "🛡️ 质量监控", "🚨 异常处理"])

# === Tab 1: 数量与状态 ===
with tab1:
    st.header("机器实时状态")
    if not df_logs.empty:
        # 获取每台机器最后一条日志
        latest_status = df_logs.sort_values('created_at').groupby('machine_id').tail(1)
        
        # 展示卡片
        cols = st.columns(5)
        for idx, (_, row) in enumerate(latest_status.iterrows()):
            col = cols[idx % 5]
            status_color = "🟢" if row['status'] == '完成' else "🔵" if row['status'] == '运行中' else "🔴"
            with col:
                st.info(f"**机器 {row['machine_id']}**\n\n"
                        f"{status_color} {row['status']}\n\n"
                        f"🎬 {row['movie_name']}\n\n"
                        f"⚡ {row['stage']}")
        
        st.divider()
        st.subheader("机器产出对比")
        completed_df = df_logs[df_logs['status'] == '完成']
        if not completed_df.empty:
            bar_chart = completed_df['machine_id'].value_counts()
            st.bar_chart(bar_chart)

# === Tab 2: 质量监控 ===
with tab2:
    st.header("剧集质量分析 (仅展示今日已完成)")
    if not df_logs.empty:
        completed_today = df_logs[(df_logs['status'] == '完成')].copy()
        
        # 提取关键指标用于表格展示
        quality_rows = []
        for _, row in completed_today.iterrows():
            ratios = row['role_ratios']
            srcs = row['sources']
            
            # 质量警报判断
            alerts = []
            if not row['ocr_check_pass']: alerts.append("OCR间隙异常")
            if ratios.get('男1', 0) < 0.15: alerts.append("男1戏份过少")
            if ratios.get('女1', 0) < 0.15: alerts.append("女1戏份过少")
            
            quality_rows.append({
                "剧名": row['movie_name'],
                "机器": row['machine_id'],
                "OCR状态": "✅ 通过" if row['ocr_check_pass'] else "❌ 异常",
                "男1比例": f"{ratios.get('男1', 0)*100:.1f}%",
                "女1比例": f"{ratios.get('女1', 0)*100:.1f}%",
                "AI占比": f"{srcs.get('AI', 0)*100:.1f}%",
                "嘴动占比": f"{srcs.get('mouth', 0)*100:.1f}%", # 假设你的Excel里叫mouth，需对应修改
                "质量警报": " | ".join(alerts) if alerts else "✨ 正常"
            })
            
        st.dataframe(pd.DataFrame(quality_rows), use_container_width=True)

# === Tab 3: 异常处理 (快速修复) ===
with tab3:
    st.header("异常任务队列")

    # 使用 session_state 存储重置结果
    if "reset_results" not in st.session_state:
        st.session_state.reset_results = {}

    errors = pd.DataFrame(data["errors"])

    if not errors.empty:
        for _, row in errors.iterrows():
            # 生成唯一 key
            row_key = f"{row['id']}_{row['movie_name']}"

            with st.expander(f"❌ {row['movie_name']} (机器: {row['machine_id']}) - {row['error_msg'][:50]}..."):
                st.write(f"**完整错误信息:** {row['error_msg']}")
                st.write(f"**NAS路径:** {row['nas_path']}")
                st.write(f"**发生时间:** {row['created_at']}")

                # 显示重置结果（如果有）
                if row_key in st.session_state.reset_results:
                    result = st.session_state.reset_results[row_key]
                    if result.get("success"):
                        st.success(f"✅ {result['msg']}")
                    else:
                        st.error(f"❌ {result.get('error', '重置失败')}")

                # 检查是否有必要的 ID 字段
                has_required_ids = all([
                    row.get("app_token"),
                    row.get("repo_table_id"),
                    row.get("produce_table_id"),
                    row.get("repo_record_id"),
                    row.get("produce_record_id")
                ])

                if not has_required_ids:
                    st.warning("⚠️ 该记录缺少关键 ID 信息，可能是旧版数据，无法执行重置")
                    continue

                col1, col2 = st.columns(2)

                with col1:
                    if st.button("🔄 方案A: 通用重置 (任意机器可接)", key=f"reset_a_{row['id']}", use_container_width=True):
                        payload = {
                            "app_token": row["app_token"],
                            "repo_table_id": row["repo_table_id"],
                            "produce_table_id": row["produce_table_id"],
                            "repo_record_id": row["repo_record_id"],
                            "produce_record_id": row["produce_record_id"],
                            "machine_id": row["machine_id"],
                            "mode": 1
                        }
                        try:
                            resp = requests.post(f"{API_URL}/reset", json=payload, timeout=10)
                            if resp.status_code == 200:
                                st.toast("✅ 通用重置成功！")
                                st.session_state.reset_results[row_key] = {"success": True, "msg": "已重置为通用任务"}
                            else:
                                error_msg = resp.json().get("detail", "未知错误")
                                st.toast(f"❌ 重置失败: {error_msg}")
                                st.session_state.reset_results[row_key] = {"success": False, "error": error_msg}
                        except Exception as e:
                            st.toast(f"❌ 请求失败: {e}")
                            st.session_state.reset_results[row_key] = {"success": False, "error": str(e)}

                with col2:
                    if st.button("💻 方案B: 本机重置 (仅当前机器)", key=f"reset_b_{row['id']}", use_container_width=True):
                        payload = {
                            "app_token": row["app_token"],
                            "repo_table_id": row["repo_table_id"],
                            "produce_table_id": row["produce_table_id"],
                            "repo_record_id": row["repo_record_id"],
                            "produce_record_id": row["produce_record_id"],
                            "machine_id": row["machine_id"],
                            "mode": 2
                        }
                        try:
                            resp = requests.post(f"{API_URL}/reset", json=payload, timeout=10)
                            if resp.status_code == 200:
                                st.toast(f"✅ 已指定给 {row['machine_id']} 号机处理")
                                st.session_state.reset_results[row_key] = {"success": True, "msg": f"已指定给 {row['machine_id']} 号机"}
                            else:
                                error_msg = resp.json().get("detail", "未知错误")
                                st.toast(f"❌ 重置失败: {error_msg}")
                                st.session_state.reset_results[row_key] = {"success": False, "error": error_msg}
                        except Exception as e:
                            st.toast(f"❌ 请求失败: {e}")
                            st.session_state.reset_results[row_key] = {"success": False, "error": str(e)}
    else:
        st.success("目前没有待处理的异常任务！")

# 启动命令: streamlit run monitor_dashboard.py