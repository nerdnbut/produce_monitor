import pymysql
from dbutils.pooled_db import PooledDB
import os
import json
from datetime import date

DB_CONFIG = {
    'host': os.getenv("AUTO_VIDEO_RDS_HOST"),
    'port': 3306,
    'user': os.getenv("AUTO_VIDEO_RDS_USER"),
    'password': os.getenv("AUTO_VIDEO_RDS_PASSWORD"),
    'database': os.getenv("AUTO_VIDEO_RDS_DATABASE_produce"),
    'charset': 'utf8mb4',
    'autocommit': True
}

pool = PooledDB(
    creator=pymysql,
    maxconnections=20,      # 最大连接数
    mincached=5,           # 初始化时缓存的连接数
    maxcached=10,          # 最大缓存连接数
    maxshared=10,          # 最大共享连接数
    blocking=True,         # 连接池满时是否阻塞等待
    **DB_CONFIG
)

def execute_query(sql, params=None):
    """执行查询操作"""
    conn = pool.connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()
    finally:
        conn.close()

def execute_update(sql, params=None):
    """执行增删改操作"""
    conn = pool.connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.rowcount
    finally:
        conn.close()

def execute_query_dict(sql, params=None):
    """执行查询操作，返回字典列表"""
    conn = pool.connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()
    finally:
        conn.close()

# ==================== 生产监控相关函数 ====================

def insert_production_log(data):
    """
    插入生产监控日志

    Args:
        data: dict 包含以下字段:
            - machine_id: 机器ID
            - module: 模块名称
            - core_table: 核心表名称
            - movie_name: 剧名
            - status: 状态
            - stage: 当前阶段
            - nas_path: NAS路径
            - error_msg: 错误信息
            - app_token: 飞书app_token
            - repo_table_id: 剧仓库表ID
            - produce_table_id: 剧制作表ID
            - repo_record_id: 剧仓库记录ID
            - produce_record_id: 剧制作记录ID
            - quality_metrics: 质量指标字典 (可选)
            - duration_h: 剧时长（小时）(可选)
            - subtitle_count: 字幕总条数 (可选)

    Returns:
        bool: 插入是否成功
    """
    conn = pool.connection()
    try:
        with conn.cursor() as c:
            # 处理嵌套的质量数据
            ocr_pass = None
            source_dist = "{}"
            ratio_stats = "{}"
            av_sync_result = "{}"
            used_volc_ocr = False
            median_h = None
            duration_h = data.get("duration_h", 0)
            subtitle_count = data.get("subtitle_count", 0)

            quality_metrics = data.get("quality_metrics")
            if quality_metrics:
                ocr_pass = quality_metrics.get("ocr_pass", True)
                source_dist = json.dumps(quality_metrics.get("source_distribution", {}), ensure_ascii=False)
                ratio_stats = json.dumps(quality_metrics.get("role_ratios", {}), ensure_ascii=False)
                # 音画同步结果
                av_sync_data = quality_metrics.get("av_sync", {})
                av_sync_result = json.dumps(av_sync_data, ensure_ascii=False)
                # OCR类型
                used_volc_ocr = quality_metrics.get("used_volc_ocr", False)
                # 字幕高度中位数（仅本地OCR有值）
                median_h = quality_metrics.get("median_h", None)
                # 时长一致性检查结果
                duration_consistency = quality_metrics.get("duration_consistency")
                duration_consistency_json = json.dumps(duration_consistency, ensure_ascii=False) if duration_consistency else "{}"

            c.execute('''INSERT INTO production_logs (
                machine_id, module, core_table, movie_name, status, stage, nas_path, error_msg,
                app_token, repo_table_id, produce_table_id, repo_record_id, produce_record_id,
                ocr_check_pass, role_source_distribution, role_ratio_stats, av_sync_result, used_volc_ocr, median_h,
                duration_h, subtitle_count, duration_consistency
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)''', (
                data.get("machine_id", ""),
                data.get("module", ""),
                data.get("core_table", "Default"),
                data.get("movie_name", ""),
                data.get("status", ""),
                data.get("stage", ""),
                data.get("nas_path", ""),
                data.get("error_msg", ""),
                data.get("app_token", ""),
                data.get("repo_table_id", ""),
                data.get("produce_table_id", ""),
                data.get("repo_record_id", ""),
                data.get("produce_record_id", ""),
                ocr_pass, source_dist, ratio_stats, av_sync_result, used_volc_ocr, median_h,
                duration_h, subtitle_count, duration_consistency_json
            ))
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print(f"插入生产日志失败: {e}")
        return False
    finally:
        conn.close()

def get_monitor_stats():
    """
    获取监控面板数据

    Returns:
        dict: 包含 logs (今日日志) 和 errors (最近24小时失败记录)
    """
    conn = pool.connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as c:
            today = date.today().strftime('%Y-%m-%d')

            # 1. 获取今日所有日志
            c.execute("SELECT * FROM production_logs WHERE DATE(created_at) = %s ORDER BY id DESC", (today,))
            logs = c.fetchall()

            # 2. 获取最近24小时的失败聚合
            c.execute("""SELECT * FROM production_logs
                       WHERE status = '失败' AND created_at > DATE_SUB(NOW(), INTERVAL 1 DAY)
                       GROUP BY movie_name ORDER BY id DESC""")
            errors = c.fetchall()

            return {"logs": logs, "errors": errors}
    except Exception as e:
        print(f"获取监控统计失败: {e}")
        return {"logs": [], "errors": []}
    finally:
        conn.close()

def get_quality_stats():
    """
    获取所有已完成的质量数据

    Returns:
        dict: 包含 logs (已完成记录)
    """
    conn = pool.connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as c:
            # 获取所有已完成的记录
            c.execute("""SELECT * FROM production_logs
                       WHERE status = '完成'
                       ORDER BY id DESC LIMIT 1000""")
            logs = c.fetchall()
            return {"logs": logs}
    except Exception as e:
        print(f"获取质量统计失败: {e}")
        return {"logs": []}
    finally:
        conn.close()

# ==================== 生产记录表相关函数 ====================

def sanitize_movie_name(name):
    """
    净化剧名（去除标点符号，用于匹配文件夹名）
    与 auto_produce.py 中的 sanitize_filename 逻辑一致
    """
    import re
    invalid_chars = r'''[\\/:*?"<>|\s.,!@#$%^&*()_+`~\-= {}[\]'";:，。？、]'''
    return re.sub(invalid_chars, "", name).replace(" ", "")

def insert_production_record(movie_name, machine_id="", nas_path="", core_table="Default", **kwargs):
    """
    创建新的生产记录

    Args:
        movie_name: 剧名
        machine_id: 机器标识
        nas_path: NAS路径
        core_table: 核心表名称（自有/外部制作/Default）
        **kwargs: 其他字段 (app_token, repo_table_id, produce_table_id, recognition_merge_start_time, etc.)

    Returns:
        int: 新记录的ID，失败返回None
    """
    conn = pool.connection()
    try:
        with conn.cursor() as cursor:
            sanitized_name = sanitize_movie_name(movie_name)
            sql = """
                INSERT INTO production_records
                (movie_name, sanitized_name, machine_id, core_table, nas_path, app_token, repo_table_id, produce_table_id, repo_record_id, produce_record_id, recognition_merge_start_time, demand_party)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            cursor.execute(sql, (
                movie_name, sanitized_name, machine_id, core_table, nas_path,
                kwargs.get("app_token", ""),
                kwargs.get("repo_table_id", ""),
                kwargs.get("produce_table_id", ""),
                kwargs.get("repo_record_id", ""),
                kwargs.get("produce_record_id", ""),
                kwargs.get("recognition_merge_start_time"),
                kwargs.get("demand_party", "")
            ))
        conn.commit()
        return cursor.lastrowid
    except Exception as e:
        conn.rollback()
        print(f"创建生产记录失败: {e}")
        return None
    finally:
        conn.close()

def update_production_status(movie_name, status, **kwargs):
    """
    更新生产记录状态

    Args:
        movie_name: 剧名（可以是原始剧名或净化后的剧名）
        status: 新状态
        **kwargs: 其他要更新的字段 (stage, error_msg, quality_metrics, duration_h, subtitle_count, bgm_result, bgm_copyright_count, recognition_merge_end_time)

    Returns:
        bool: 更新是否成功（找不到记录时静默跳过，返回True）
    """
    conn = pool.connection()
    try:
        with conn.cursor() as cursor:
            # 构建动态UPDATE语句
            update_fields = ["status = %s"]
            update_values = [status]

            if "stage" in kwargs:
                update_fields.append("stage = %s")
                update_values.append(kwargs["stage"])
            if "error_msg" in kwargs:
                update_fields.append("error_msg = %s")
                update_values.append(kwargs["error_msg"])
            if "duration_h" in kwargs:
                update_fields.append("duration_h = %s")
                update_values.append(kwargs["duration_h"])
            if "subtitle_count" in kwargs:
                update_fields.append("subtitle_count = %s")
                update_values.append(kwargs["subtitle_count"])
            if "bgm_result" in kwargs:
                update_fields.append("bgm_result = %s")
                update_values.append(kwargs["bgm_result"])
            if "bgm_copyright_count" in kwargs:
                update_fields.append("bgm_copyright_count = %s")
                update_values.append(kwargs["bgm_copyright_count"])
            if "recognition_merge_end_time" in kwargs:
                update_fields.append("recognition_merge_end_time = %s")
                update_values.append(kwargs["recognition_merge_end_time"])

            # 质量指标
            if "quality_metrics" in kwargs and kwargs["quality_metrics"]:
                metrics = kwargs["quality_metrics"]
                if "ocr_pass" in metrics:
                    update_fields.append("ocr_check_pass = %s")
                    update_values.append(metrics["ocr_pass"])
                if "source_distribution" in metrics:
                    update_fields.append("role_source_distribution = %s")
                    update_values.append(json.dumps(metrics["source_distribution"], ensure_ascii=False))
                if "role_ratios" in metrics:
                    update_fields.append("role_ratio_stats = %s")
                    update_values.append(json.dumps(metrics["role_ratios"], ensure_ascii=False))
                if "av_sync" in metrics:
                    update_fields.append("av_sync_result = %s")
                    update_values.append(json.dumps(metrics["av_sync"], ensure_ascii=False))
                if "used_volc_ocr" in metrics:
                    update_fields.append("used_volc_ocr = %s")
                    update_values.append(metrics["used_volc_ocr"])
                if "median_h" in metrics:
                    update_fields.append("median_h = %s")
                    update_values.append(metrics["median_h"])
                if "subtitle_duration_abnormal" in metrics:
                    update_fields.append("subtitle_duration_abnormal = %s")
                    update_values.append(json.dumps(metrics["subtitle_duration_abnormal"], ensure_ascii=False))
                if "duration_consistency" in metrics:
                    update_fields.append("duration_consistency = %s")
                    update_values.append(json.dumps(metrics["duration_consistency"], ensure_ascii=False))

            # 同时匹配原始剧名和净化后的剧名
            sanitized = sanitize_movie_name(movie_name)
            update_values.extend([movie_name, sanitized])

            sql = f"UPDATE production_records SET {', '.join(update_fields)} WHERE movie_name = %s OR sanitized_name = %s ORDER BY id DESC LIMIT 1"
            cursor.execute(sql, update_values)

            # 检查是否真的更新了记录
            if cursor.rowcount == 0:
                # 找不到记录，静默跳过（兼容旧数据，等脚本重启后会正常创建）
                pass

        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        # 静默处理错误，不影响生产流程
        return False
    finally:
        conn.close()

def get_production_records(machine_id=None, status=None, limit=None):
    """
    获取生产记录

    Args:
        machine_id: 机器标识（可选）
        status: 状态筛选（可选）
        limit: 返回数量限制（可选）

    Returns:
        list: 生产记录列表
    """
    conn = pool.connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            conditions = []
            params = []

            if machine_id:
                conditions.append("machine_id = %s")
                params.append(machine_id)
            if status:
                conditions.append("status = %s")
                params.append(status)

            where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            limit_clause = f"LIMIT {limit}" if limit else ""

            sql = f"SELECT * FROM production_records {where_clause} ORDER BY id DESC {limit_clause}"
            cursor.execute(sql, params)
            return cursor.fetchall()
    except Exception as e:
        print(f"获取生产记录失败: {e}")
        return []
    finally:
        conn.close()

def get_all_machines():
    """
    获取所有有记录的机器ID列表

    Returns:
        list: 机器ID列表
    """
    conn = pool.connection()
    try:
        with conn.cursor() as cursor:
            sql = "SELECT DISTINCT machine_id FROM production_records WHERE machine_id != '' ORDER BY machine_id"
            cursor.execute(sql)
            return [row[0] for row in cursor.fetchall()]
    except Exception as e:
        print(f"获取机器列表失败: {e}")
        return []
    finally:
        conn.close()

# ==================== 配音记录表相关函数 ====================

def insert_dubbing_record(movie_name, language, production_id=None, **kwargs):
    """
    创建新的配音记录

    Args:
        movie_name: 剧名（可以是原始剧名或净化后的剧名）
        language: 配音语言
        production_id: 关联的生产记录ID（可选）
        **kwargs: 其他字段 (start_time, status)

    Returns:
        int: 新记录的ID，失败返回None
    """
    conn = pool.connection()
    try:
        with conn.cursor() as cursor:
            sanitized_name = sanitize_movie_name(movie_name)
            sql = """
                INSERT INTO dubbing_records
                (movie_name, sanitized_name, language, production_id, start_time, status)
                VALUES (%s, %s, %s, %s, %s, %s)
            """
            cursor.execute(sql, (
                movie_name,
                sanitized_name,
                language,
                production_id,
                kwargs.get("start_time"),
                kwargs.get("status", "配音中")
            ))
        conn.commit()
        return cursor.lastrowid
    except Exception as e:
        conn.rollback()
        print(f"创建配音记录失败: {e}")
        return None
    finally:
        conn.close()

def update_dubbing_record(dubbing_id, **kwargs):
    """
    更新配音记录

    Args:
        dubbing_id: 配音记录ID
        **kwargs: 要更新的字段 (status, end_time, azure_calls, elevenlabs_calls, fishaudio_calls, total_calls)

    Returns:
        bool: 更新是否成功
    """
    if not kwargs:
        return True

    conn = pool.connection()
    try:
        with conn.cursor() as cursor:
            update_fields = []
            update_values = []

            field_mapping = {
                "status": "status",
                "end_time": "end_time",
                "azure_calls": "azure_calls",
                "elevenlabs_calls": "elevenlabs_calls",
                "fishaudio_calls": "fishaudio_calls",
                "total_calls": "total_calls"
            }

            for key, db_field in field_mapping.items():
                if key in kwargs:
                    update_fields.append(f"{db_field} = %s")
                    update_values.append(kwargs[key])

            if not update_fields:
                return True

            update_values.append(dubbing_id)

            sql = f"UPDATE dubbing_records SET {', '.join(update_fields)} WHERE id = %s"
            cursor.execute(sql, update_values)
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print(f"更新配音记录失败: {e}")
        return False
    finally:
        conn.close()

def get_dubbing_records(movie_name=None, production_id=None, language=None):
    """
    获取配音记录

    Args:
        movie_name: 剧名（可选，可以是原始剧名或净化后的剧名）
        production_id: 生产记录ID（可选）
        language: 语言（可选）

    Returns:
        list: 配音记录列表
    """
    conn = pool.connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            conditions = []
            params = []

            if movie_name:
                sanitized = sanitize_movie_name(movie_name)
                conditions.append("(movie_name = %s OR sanitized_name = %s)")
                params.extend([movie_name, sanitized])
            if production_id:
                conditions.append("production_id = %s")
                params.append(production_id)
            if language:
                conditions.append("language = %s")
                params.append(language)

            where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

            sql = f"SELECT * FROM dubbing_records {where_clause} ORDER BY id DESC"
            cursor.execute(sql, params)
            return cursor.fetchall()
    except Exception as e:
        print(f"获取配音记录失败: {e}")
        return []
    finally:
        conn.close()

def get_dubbing_record_by_movie_and_language(movie_name, language):
    """
    根据剧名和语言获取最新的配音记录

    Args:
        movie_name: 剧名（可以是原始剧名或净化后的剧名）
        language: 语言

    Returns:
        dict: 配音记录，不存在返回None
    """
    conn = pool.connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            sanitized = sanitize_movie_name(movie_name)
            sql = """
                SELECT * FROM dubbing_records
                WHERE (movie_name = %s OR sanitized_name = %s) AND language = %s
                ORDER BY id DESC LIMIT 1
            """
            cursor.execute(sql, (movie_name, sanitized, language))
            return cursor.fetchone()
    except Exception as e:
        print(f"获取配音记录失败: {e}")
        return None
    finally:
        conn.close()

# ==================== TTS 日志相关函数 ====================

def insert_tts_log(movie_name, dubbing_language, azure_calls=0, elevenlabs_calls=0,
                   fishaudio_calls=0, total_calls=0, start_time=None, end_time=None):
    """
    插入TTS使用日志

    Args:
        movie_name: 剧名
        dubbing_language: 配音语言
        azure_calls: Azure调用次数
        elevenlabs_calls: ElevenLabs调用次数
        fishaudio_calls: FishAudio调用次数
        total_calls: 总调用次数
        start_time: 开始配音时间
        end_time: 结束配音时间

    Returns:
        bool: 插入是否成功
    """
    conn = pool.connection()
    try:
        with conn.cursor() as cursor:
            sql = """
                INSERT INTO tts_api_logs
                (movie_name, dubbing_language, azure_calls, elevenlabs_calls, fishaudio_calls, total_calls, start_time, end_time)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """
            cursor.execute(sql, (
                movie_name, dubbing_language, azure_calls, elevenlabs_calls,
                fishaudio_calls, total_calls, start_time, end_time
            ))
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print(f"插入TTS日志失败: {e}")
        return False
    finally:
        conn.close()

# ==================== 辅助函数 ====================

# 小柯 OpenID (用于重置任务)
XIAOKE_OPEN_ID = "ou_59af223501db676959b94338a760bbea"

def count_srt_file(srt_path):
    """
    统计SRT文件中的字幕条数

    Args:
        srt_path: SRT文件路径

    Returns:
        int: 字幕条数
    """
    if not srt_path or not os.path.exists(srt_path):
        return 0
    try:
        with open(srt_path, 'r', encoding='utf-8') as f:
            content = f.read()
        # SRT格式：每条字幕以序号开头，后跟时间轴
        # 计算序号出现次数即为字幕条数
        import re
        blocks = re.split(r'\n\s*\n', content.strip())
        # 过滤掉空块
        entries = [b for b in blocks if b.strip()]
        return len(entries)
    except Exception as e:
        print(f"统计SRT文件失败: {e}")
        return 0

def execute_reset(app_token, repo_table_id, produce_table_id, repo_record_id, produce_record_id, machine_id, mode):
    """
    执行任务重置（通过飞书API）

    Args:
        app_token: 飞书app_token
        repo_table_id: 剧仓库表ID
        produce_table_id: 剧制作表ID
        repo_record_id: 剧仓库记录ID
        produce_record_id: 剧制作记录ID
        machine_id: 机器ID
        mode: 重置模式 (1:通用, 2:指定机器)

    Returns:
        dict: {"success": bool, "msg": str}
    """
    try:
        # 导入飞书客户端
        import sys
        sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from youtube_data.lark import LarkBitableClient

        # 初始化飞书客户端
        lark_client = LarkBitableClient()

        # 1. 更新剧制作表 (重置制作人)
        updates_produce = {
            "record_id": produce_record_id,
            "fields": {"制作人": [{"id": XIAOKE_OPEN_ID}]}
        }
        lark_client.batch_update_records(app_token, produce_table_id, [updates_produce])

        # 2. 更新剧仓库表 (重置状态)
        repo_fields = {"剧检查状态": "文件结构已对齐"}

        # 模式2: 指定机器处理
        if mode == 2:
            repo_fields["备注"] = f"{machine_id}待处理"

        updates_repo = {
            "record_id": repo_record_id,
            "fields": repo_fields
        }
        lark_client.batch_update_records(app_token, repo_table_id, [updates_repo])

        return {"success": True, "msg": f"重置成功 (模式: {mode})"}
    except Exception as e:
        print(f"重置错误: {e}")
        import traceback
        traceback.print_exc()
        return {"success": False, "error": str(e)}

def delete_production_log(log_id):
    """
    删除指定的生产记录（用于重置后从异常队列移除）

    Args:
        log_id: 生产记录ID

    Returns:
        bool: 删除是否成功
    """
    conn = pool.connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM production_records WHERE id = %s", (log_id,))
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print(f"删除生产记录失败: {e}")
        return False
    finally:
        conn.close()

def mark_production_record_correct(record_id, recognition_merge_end_time=None):
    """
    标记生产记录为正确（状态改为"识别合并完成"）
    用于处理因缺少某些角色（如男2、女2）而误判为失败的情况

    Args:
        record_id: 生产记录ID
        recognition_merge_end_time: 识别合并结束时间（可选，默认使用当前时间）

    Returns:
        bool: 更新是否成功
    """
    from datetime import datetime
    conn = pool.connection()
    try:
        with conn.cursor() as cursor:
            if recognition_merge_end_time is None:
                recognition_merge_end_time = datetime.now()
            sql = """
                UPDATE production_records
                SET status = '识别合并完成', stage = '', error_msg = '', recognition_merge_end_time = %s, updated_at = %s
                WHERE id = %s
            """
            cursor.execute(sql, (recognition_merge_end_time, recognition_merge_end_time, record_id))
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print(f"标记记录为正确失败: {e}")
        return False
    finally:
        conn.close()

# ==================== 请求ID缓存相关函数 ====================

NAS_CACHE_DIR = r"\\172.16.8.9\snapread\小柯"
REQUEST_ID_FILE = os.path.join(NAS_CACHE_DIR, "request_ids.json")

def clear_request_id_cache(movie_name):
    """
    清除指定剧集的请求ID缓存
    防止重置后使用老的缓存数据

    Args:
        movie_name: 剧名（可以是原始剧名或净化后的剧名）

    Returns:
        dict: {"success": bool, "msg": str}
    """
    try:
        # 净化剧名（用于匹配文件夹名）
        sanitized_name = sanitize_movie_name(movie_name)

        # 检查文件是否存在
        if not os.path.exists(REQUEST_ID_FILE):
            return {"success": True, "msg": "缓存文件不存在，无需清除"}

        # 读取现有缓存
        with open(REQUEST_ID_FILE, "r", encoding="utf-8") as f:
            cache_data = json.load(f)

        # 检查并删除对应剧名的缓存（支持原始剧名和净化后的剧名）
        removed_keys = []
        for key in list(cache_data.keys()):
            if key == movie_name or key == sanitized_name:
                removed_keys.append(key)
                del cache_data[key]

        if not removed_keys:
            return {"success": True, "msg": "未找到该剧的缓存数据"}

        # 保存更新后的缓存
        with open(REQUEST_ID_FILE, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=4)

        return {
            "success": True,
            "msg": f"已清除缓存: {', '.join(removed_keys)}"
        }
    except Exception as e:
        print(f"清除缓存失败: {e}")
        import traceback
        traceback.print_exc()
        return {"success": False, "error": str(e)}

# ==================== 版权处理日志相关函数 ====================

def insert_copyright_process_log(data):
    """
    插入版权处理日志

    Args:
        data: dict 包含以下字段:
            - task_id: 任务ID (可选)
            - channel: 频道名称
            - video_title: 视频标题
            - music_names: 音乐名称列表 (JSON字符串)
            - owner: 版权发起者
            - status: 状态 (收到邮件/开始处理/部分完成/完成/失败等)
            - processed_songs: 已处理的音乐列表 (JSON字符串)
            - failed_count: 失败数量
            - fail_reason: 失败原因
            - process_result: 处理结果描述
            - start_time: 开始时间
            - end_time: 结束时间 (可选)
            - email_received_time: 邮件接收时间 (可选)
            - copyright_type: 版权类型 (【音乐】/【视频】/【版权解除】)

    Returns:
        int: 新记录的ID，失败返回None
    """
    conn = pool.connection()
    try:
        with conn.cursor() as cursor:
            sql = """
                INSERT INTO copyright_process_logs
                (task_id, channel, video_title, music_names, owner, status,
                 processed_songs, failed_count, fail_reason, process_result,
                 start_time, end_time, email_received_time, copyright_type)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            cursor.execute(sql, (
                data.get("task_id", ""),
                data.get("channel", ""),
                data.get("video_title", ""),
                json.dumps(data.get("music_names", []), ensure_ascii=False),
                data.get("owner", ""),
                data.get("status", ""),
                json.dumps(data.get("processed_songs", []), ensure_ascii=False),
                data.get("failed_count", 0),
                data.get("fail_reason", ""),
                data.get("process_result", ""),
                data.get("start_time"),
                data.get("end_time"),
                data.get("email_received_time"),
                data.get("copyright_type", "")
            ))
        conn.commit()
        return cursor.lastrowid
    except Exception as e:
        conn.rollback()
        print(f"插入版权处理日志失败: {e}")
        return None
    finally:
        conn.close()

def update_copyright_process_log(log_id, **kwargs):
    """
    更新版权处理日志

    Args:
        log_id: 日志记录ID
        **kwargs: 要更新的字段

    Returns:
        bool: 更新是否成功
    """
    if not kwargs:
        return True

    conn = pool.connection()
    try:
        with conn.cursor() as cursor:
            update_fields = []
            update_values = []

            field_mapping = {
                "status": "status",
                "processed_songs": "processed_songs",
                "failed_count": "failed_count",
                "fail_reason": "fail_reason",
                "process_result": "process_result",
                "end_time": "end_time",
                "music_names": "music_names"
            }

            for key, db_field in field_mapping.items():
                if key in kwargs:
                    if key in ["processed_songs", "music_names"]:
                        update_fields.append(f"{db_field} = %s")
                        update_values.append(json.dumps(kwargs[key], ensure_ascii=False))
                    else:
                        update_fields.append(f"{db_field} = %s")
                        update_values.append(kwargs[key])

            if not update_fields:
                return True

            update_values.append(log_id)

            sql = f"UPDATE copyright_process_logs SET {', '.join(update_fields)} WHERE id = %s"
            cursor.execute(sql, update_values)
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print(f"更新版权处理日志失败: {e}")
        return False
    finally:
        conn.close()

def get_copyright_process_logs(channel=None, status=None, copyright_type=None, limit=None):
    """
    获取版权处理日志

    Args:
        channel: 频道名称（可选）
        status: 状态筛选（可选）
        copyright_type: 版权类型筛选（可选）
        limit: 返回数量限制（可选）

    Returns:
        list: 版权处理日志列表
    """
    conn = pool.connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            conditions = []
            params = []

            if channel:
                conditions.append("channel = %s")
                params.append(channel)
            if status:
                conditions.append("status = %s")
                params.append(status)
            if copyright_type:
                conditions.append("copyright_type = %s")
                params.append(copyright_type)

            where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
            limit_clause = f"LIMIT {limit}" if limit else ""

            sql = f"SELECT * FROM copyright_process_logs {where_clause} ORDER BY id DESC {limit_clause}"
            cursor.execute(sql, params)
            return cursor.fetchall()
    except Exception as e:
        print(f"获取版权处理日志失败: {e}")
        return []
    finally:
        conn.close()

def get_copyright_process_stats():
    """
    获取版权处理统计信息

    Returns:
        dict: 包含统计数据
    """
    conn = pool.connection()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            today = date.today().strftime('%Y-%m-%d')

            # 今日收到邮件数量
            cursor.execute("""
                SELECT COUNT(*) as count FROM copyright_process_logs
                WHERE DATE(email_received_time) = %s
            """, (today,))
            today_received = cursor.fetchone()['count']

            # 今日处理完成数量
            cursor.execute("""
                SELECT COUNT(*) as count FROM copyright_process_logs
                WHERE DATE(end_time) = %s AND status IN ('完成', 'DONE')
            """, (today,))
            today_completed = cursor.fetchone()['count']

            # 今日处理失败数量
            cursor.execute("""
                SELECT COUNT(*) as count FROM copyright_process_logs
                WHERE DATE(end_time) = %s AND status IN ('失败', 'ERROR')
            """, (today,))
            today_failed = cursor.fetchone()['count']

            # 正在处理中数量
            cursor.execute("""
                SELECT COUNT(*) as count FROM copyright_process_logs
                WHERE status IN ('开始处理', '部分完成', 'PROCESSING', 'PENDING')
            """)
            processing = cursor.fetchone()['count']

            return {
                "today_received": today_received,
                "today_completed": today_completed,
                "today_failed": today_failed,
                "processing": processing
            }
    except Exception as e:
        print(f"获取版权处理统计失败: {e}")
        return {
            "today_received": 0,
            "today_completed": 0,
            "today_failed": 0,
            "processing": 0
        }
    finally:
        conn.close()

# 使用示例
if __name__ == "__main__":
    # 测试数据库连接
    try:
        result = execute_query("SELECT VERSION()")
        print("数据库连接成功!")
        print("数据库版本:", result[0][0])
        print("当前数据库:", os.getenv("AUTO_VIDEO_RDS_DATABASE_produce") or DB_CONFIG.get('database', 'produce_monitor'))

        # 测试插入生产日志
        test_data = {
            "machine_id": "test",
            "module": "测试模块",
            "core_table": "Default",
            "movie_name": "测试剧名",
            "status": "运行中",
            "stage": "测试阶段",
            "nas_path": "/test/path",
            "error_msg": "",
            "duration_h": 1.5,
            "subtitle_count": 100
        }
        if insert_production_log(test_data):
            print("测试数据插入成功!")

        # 测试获取统计数据
        stats = get_monitor_stats()
        print(f"今日日志数量: {len(stats['logs'])}")

    except Exception as e:
        print("数据库连接失败:", e)
