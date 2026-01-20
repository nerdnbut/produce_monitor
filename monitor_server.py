import uvicorn
from fastapi import FastAPI, HTTPException, Body
from pydantic import BaseModel
import pymysql
from datetime import datetime, date
import json
import os
import sys

# 添加项目根目录到 sys.path 以便导入
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from youtube_data.lark import LarkBitableClient

app = FastAPI(title="Production Monitor API", version="1.0.0")

# ==================== 数据库配置 ====================
DB_CONFIG = {
    'host': '127.0.0.1',
    'port': 3306,
    'user': 'root',
    'password': '',  # 可通过环境变量 DB_PASSWORD 覆盖
    'db': 'production_monitor',
    'charset': 'utf8mb4',
    'cursorclass': pymysql.cursors.DictCursor
}

# 从环境变量读取密码（如果设置了）
if os.getenv('DB_PASSWORD'):
    DB_CONFIG['password'] = os.getenv('DB_PASSWORD')
# =================================================

# 小柯的 open_id (制作人)
XIAOKE_OPEN_ID = "ou_59af223501db676959b94338a760bbea"

# --- 数据库连接 ---
def get_db_connection():
    """获取数据库连接"""
    return pymysql.connect(**DB_CONFIG)

# --- 数据库初始化 ---
def init_db():
    conn = get_db_connection()
    c = conn.cursor()

    # 创建数据库（如果不存在）
    c.execute("CREATE DATABASE IF NOT EXISTS `production_monitor` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    c.execute("USE `production_monitor`")

    # 生产日志表 (包含基础信息 + 质量数据)
    c.execute('''CREATE TABLE IF NOT EXISTS production_logs (
        id INT AUTO_INCREMENT PRIMARY KEY,
        machine_id VARCHAR(50),
        module VARCHAR(100),
        core_table VARCHAR(100) DEFAULT 'Default',
        movie_name VARCHAR(255),
        status VARCHAR(50),
        stage VARCHAR(255),
        nas_path TEXT,
        error_msg TEXT,

        -- ID 信息
        app_token VARCHAR(100),
        repo_table_id VARCHAR(100),
        produce_table_id VARCHAR(100),
        repo_record_id VARCHAR(100),
        produce_record_id VARCHAR(100),

        -- 质量指标 (存 JSON 字符串)
        ocr_check_pass BOOLEAN DEFAULT NULL,
        role_source_distribution TEXT,
        role_ratio_stats TEXT,

        -- 时间戳
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

        -- 索引
        INDEX idx_machine_id (machine_id),
        INDEX idx_status (status),
        INDEX idx_created_at (created_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci''')

    # 错误处理指令队列 (用于从面板下发指令给机器)
    c.execute('''CREATE TABLE IF NOT EXISTS command_queue (
        id INT AUTO_INCREMENT PRIMARY KEY,
        machine_id VARCHAR(50),
        target_record_id VARCHAR(100),
        command_type VARCHAR(50),
        status VARCHAR(20) DEFAULT 'PENDING',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,

        INDEX idx_machine_id (machine_id),
        INDEX idx_status (status)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci''')

    # 迁移：检查并添加新字段（如果已存在的表缺少这些字段）
    c.execute("SHOW COLUMNS FROM production_logs")
    existing_columns = [row['Field'] for row in c.fetchall()]

    new_columns = {
        "module": "VARCHAR(100)",
        "app_token": "VARCHAR(100)",
        "repo_table_id": "VARCHAR(100)",
        "produce_table_id": "VARCHAR(100)"
    }

    for col, col_type in new_columns.items():
        if col not in existing_columns:
            c.execute(f"ALTER TABLE production_logs ADD COLUMN {col} {col_type}")
            print(f"已添加字段: {col}")

    conn.commit()
    conn.close()
    print("数据库初始化完成")

init_db()

# --- 数据模型 ---
class ReportModel(BaseModel):
    machine_id: str
    module: str  # 模块名称，如"自动识别合并"、"BGM处理"
    core_table: str = "Default"
    status: str
    current_stage: str  # 对应数据库的 stage 字段
    movie_name: str = ""
    nas_path: str = ""
    error_msg: str = ""
    app_token: str = ""
    repo_table_id: str = ""
    produce_table_id: str = ""
    repo_record_id: str = ""
    produce_record_id: str = ""
    reset_start_time: bool = False  # 是否重置开始时间
    quality_metrics: dict = None  # {"ocr_pass": True, "source_distribution": {...}, "role_ratios": {...}}

class ResetModel(BaseModel):
    app_token: str
    repo_table_id: str
    produce_table_id: str
    repo_record_id: str
    produce_record_id: str
    machine_id: str
    mode: int  # 1: 通用重置, 2: 本机重置

# --- API 接口 ---

@app.post("/monitor/report")
def receive_report(data: ReportModel):
    conn = get_db_connection()
    c = conn.cursor()

    # 质量数据处理
    ocr_pass = None
    source_dist = "{}"
    ratio_stats = "{}"

    if data.quality_metrics:
        ocr_pass = data.quality_metrics.get("ocr_pass", True)
        source_dist = json.dumps(data.quality_metrics.get("source_distribution", {}), ensure_ascii=False)
        ratio_stats = json.dumps(data.quality_metrics.get("role_ratios", {}), ensure_ascii=False)

    try:
        c.execute('''INSERT INTO production_logs (
            machine_id, module, core_table, movie_name, status, stage, nas_path, error_msg,
            app_token, repo_table_id, produce_table_id,
            repo_record_id, produce_record_id, ocr_check_pass,
            role_source_distribution, role_ratio_stats
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)''', (
            data.machine_id, data.module, data.core_table, data.movie_name, data.status,
            data.current_stage, data.nas_path, data.error_msg,
            data.app_token, data.repo_table_id, data.produce_table_id,
            data.repo_record_id, data.produce_record_id,
            ocr_pass, source_dist, ratio_stats
        ))
        conn.commit()
        return {"msg": "ok"}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@app.get("/monitor/stats")
def get_stats():
    """获取 Dashboard 需要的所有数据"""
    conn = get_db_connection()
    c = conn.cursor()

    try:
        # 获取当天的日志
        today = date.today().strftime('%Y-%m-%d')
        c.execute("SELECT * FROM production_logs WHERE DATE(created_at) = %s ORDER BY id DESC", (today,))
        logs = c.fetchall()

        # 获取待处理的错误 (最近24小时失败的)
        c.execute("""SELECT * FROM production_logs
                   WHERE status = '失败' AND created_at > DATE_SUB(NOW(), INTERVAL 1 DAY)
                   GROUP BY movie_name
                   ORDER BY id DESC""")
        errors = c.fetchall()

        return {"logs": logs, "errors": errors}
    finally:
        conn.close()


@app.post("/monitor/reset")
def execute_reset(data: ResetModel):
    """
    执行任务重置，参考 dashboard_gui.py 的 execute_reset 方法
    mode 1: 通用重置 (任意机器可接)
    mode 2: 本机重置 (仅当前机器)
    """
    try:
        lark_client = LarkBitableClient()

        # 1. 修改剧制作表：制作人 -> 小柯
        updates_produce = {
            "record_id": data.produce_record_id,
            "fields": {
                "制作人": [{"id": XIAOKE_OPEN_ID}]
            }
        }
        result_produce = lark_client.batch_update_records(
            app_token=data.app_token,
            table_id=data.produce_table_id,
            updates=[updates_produce]
        )
        if not result_produce:
            raise Exception("更新剧制作表失败")

        # 2. 修改剧仓库表：状态 -> 文件结构已对齐
        repo_fields = {
            "剧检查状态": "文件结构已对齐"
        }

        if data.mode == 2:
            # 模式2：在备注中追加 "{machine_id}待处理"
            repo_fields["备注"] = f"{data.machine_id}待处理"

        updates_repo = {
            "record_id": data.repo_record_id,
            "fields": repo_fields
        }
        result_repo = lark_client.batch_update_records(
            app_token=data.app_token,
            table_id=data.repo_table_id,
            updates=[updates_repo]
        )
        if not result_repo:
            raise Exception("更新剧仓库表失败")

        return {
            "success": True,
            "msg": f"重置成功 (模式 {data.mode}: {'本机' if data.mode == 2 else '通用'})"
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/monitor/view")
def get_view_data():
    """
    获取 dashboard_gui.py 需要的数据
    返回所有机器的最新状态记录
    """
    conn = get_db_connection()
    c = conn.cursor()

    try:
        # 获取每台机器的最新记录（去重）
        c.execute('''SELECT t1.* FROM production_logs t1
                   INNER JOIN (
                       SELECT machine_id, MAX(id) as max_id
                       FROM production_logs
                       GROUP BY machine_id
                   ) t2 ON t1.id = t2.max_id
                   ORDER BY t1.machine_id''')
        view_data = []
        for row in c.fetchall():
            view_data.append({
                "machine_id": row["machine_id"],
                "module": row.get("module") or "",
                "core_table": row["core_table"],
                "status": row["status"],
                "movie_name": row.get("movie_name") or "",
                "current_stage": row.get("stage") or "",
                "nas_path": row.get("nas_path") or "",
                "error_msg": row.get("error_msg") or "",
                "last_update_time": row["updated_at"].strftime("%Y-%m-%d %H:%M:%S") if row["updated_at"] else "",
                # ID 信息
                "repo_record_id": row.get("repo_record_id") or "",
                "produce_record_id": row.get("produce_record_id") or "",
                "app_token": row.get("app_token") or "",
                "repo_table_id": row.get("repo_table_id") or "",
                "produce_table_id": row.get("produce_table_id") or "",
            })

        return view_data
    finally:
        conn.close()


@app.get("/")
def root():
    return {
        "message": "Production Monitor API is running",
        "endpoints": ["/monitor/report", "/monitor/stats", "/monitor/view", "/monitor/reset", "/health"]
    }


@app.get("/health")
def health():
    """健康检查端点"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT 1")
        conn.close()
        return {"status": "healthy", "database": "connected"}
    except Exception as e:
        return {"status": "unhealthy", "database": "disconnected", "error": str(e)}, 500


# 启动命令: uvicorn monitor_server:app --host 0.0.0.0 --port 8000 --workers 4
