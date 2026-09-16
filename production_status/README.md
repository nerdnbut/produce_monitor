# Production Status

第一版按确认的状态采样方案，监控 Recognition、BGM、Dubbing、Auto Upload 四个组件；点众、红果、众益、外部制作等作为业务系统维度。不是此前提出的入库/识别/BGM/配音十个步骤事件埋点版。

## 启动

使用运行原生产看板的 Python 环境，从项目根目录执行：

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m produce_monitor.production_status.collector
```

或双击本目录 `run_collector.bat`。脚本默认使用 `%LOCALAPPDATA%\Programs\Python\Python312\python.exe`，避免 PATH 中的 Anaconda Python 缺少 `lark_oapi`。需要使用其他环境时，先设置 `PRODUCTION_STATUS_PYTHON` 为对应 `python.exe` 的绝对路径。采集器是独立常驻进程，每5分钟整点采样，首次启动立即采一次。关闭浏览器不会停止采集；退出进程/关机则停止，重启后的缺口保留为 Unknown。部署时应由 Windows 任务计划程序在开机后启动该命令，并启用失败重启。任务计划程序应填写该环境 Python 的绝对路径，工作目录为项目根目录。

查看方式：

- 启动现有 `production_monitor.py` 后，点击侧边栏“🟢 Production Status”进入独立视图；页面提供“返回生产监控”按钮，不占用生产监控 Tab，也不依赖 Streamlit `page_link`。
- 双击 `run_status_page.bat`，然后打开 `http://localhost:8502`。
- 独立启动命令：

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m streamlit run produce_monitor/production_status/status_page.py --server.port 8502 --server.headless true
```

页面每60秒刷新本地状态（支持 Streamlit fragment 的环境），也支持手动刷新。旧版 Streamlit 不支持 fragment 时保留手动刷新。程序不自动注册系统服务、不自动发送新增飞书通知。

## 数据位置和配置

默认数据库：`produce_monitor/production_status/data/status.sqlite3`，已在本目录 `.gitignore` 中忽略。采集器与页面必须使用同一份数据库，建议部署在同一台机器的本地磁盘；不要把 SQLite WAL 数据库放到 NAS 共享目录。

可通过环境变量 `PRODUCTION_STATUS_DB` 指定绝对路径。`--db` 仅影响采集器，使用时页面也应设置同一路径。`AUTO_UPLOAD_COORDINATOR_URL` 沿用现有看板配置，默认 `http://127.0.0.1:8899`。

`PRODUCTION_STATUS_RULES` 可指向 JSON 文件，覆盖 `config.py` 的 `Rules` 字段。修改后重启采集器；每次快照保存规则版本及实际配置，历史不随新规则重新计算。例：

```json
{
  "min_rate_jobs": 20,
  "dubbing_overdue_hours": 48,
  "bgm_wait_degraded": 4,
  "bgm_wait_partial": 8,
  "bgm_wait_major": 16
}
```

管理命令：

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m produce_monitor.production_status.collector --init-only
& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m produce_monitor.production_status.collector --once
```

同一 SQLite 由操作系统文件锁保证单采集进程；同一5分钟时间桶幂等写入。快照与 Incident 在一个事务中提交。数据默认持续保留，建议定期备份和观察磁盘占用；如需文件复制备份，应停止采集器并同时保留数据库相关文件，或使用 SQLite backup API。

## 规则与统计含义

- 识别/BGM/配音失败占比：当前未结束任务 + 最近24小时完成任务作为统计范围，仅计算当前失败，已完成记录残留失败类型不算当前故障。
- 默认失败率 2% 起 Degraded、5% 起 Partial Outage、超过20% Major Outage。少于20个任务时，失败仅触发 Degraded，避免一条任务失败就按100%故障升级。
- 识别：当前失败、入表超过24小时仍在前期/失败等待、超过48小时仍未完成识别，进入积压规则。BGM 阶段的任务归 BGM，不再重复算识别故障。
- 各组件存在超时即 Degraded；至少5个且占未结束任务20%以上为 Partial Outage。
- BGM：当前等待从识别结束起算；已开始任务另算运行时间。平均等待4/8/超过16小时逐级升级，P95达到8小时或最长等待达到24小时触发 Degraded；运行超过24小时计超时。
- 配音：完成状态为“已完成/待检查者确认”，超时从 `max(需求提交时间, 整备完成时间)` 计算，默认48小时。没有整备完成时间时显示等待上游，不算配音执行超时。细分翻译/合成/剪辑当前没有独立采样源。
- 上传：排除运营手动上传，失败率按“最近24小时入表且已有结果”的任务计算，5%起 Degraded、超过20% Partial Outage。未解决历史失败也会提示异常。有未结束任务且在线执行端为0时 Major Outage；协调服务请求失败为 Unknown，不等同生产故障。
- 机器明细只表示识别任务归属、失败、超时和24小时产量，不代表机器在线/离线；没有接入固定心跳。上传在线数是协调服务提供的全局共享执行端数量。
- 无进行中任务、无近期产出且无心跳证据时为 Unknown，不用“零产量”推断宕机。
- 主来源全表读取，避免旧周期未完成任务被漏掉。采集器复用原看板读取函数的未缓存入口与字段转换，使用严格读取模式；失败不会当空任务。每轮最多4个并行读取，整轮超过5分钟时标记本轮 Unknown。

Availability = `(Operational + Degraded) / 有效5分钟采样桶`；Healthy Time = `Operational / 有效5分钟采样桶`。有效采样要求状态已知且数据完整。监控覆盖率 = `有效桶 / 所选期间应有桶`，展示范围包含所选第一天00:00至当前采样桶；上线前、断档、部分数据缺失均不算正常。

日/小时颜色按最严重已观测状态，缺口用灰色或斜纹提示。每日24个小时可以继续查看每次采样、任务、机器及原因。历史不会用当前状态补造，五分钟内发生且恢复的异常可能漏采；这里的 uptime 是业务规则采样口径，不是精确基础设施在线时长。

组件全系统状态按最严重子系统状态合并。有异常且部分来源未知时仍展示异常，但该桶不进入完整观测 uptime。所有来源正常时才展示 All Systems Operational。

Incident 在首次异常时创建，状态升级/下降记录时间线，首次完整观测到 Operational 时恢复；Unknown 不关闭 Incident。跨采集断档的事件会标记未知间隔，不声称期间持续故障。开始与恢复是观测时间，精度约为采样间隔。

## 文件职责

- `config.py`：组件、颜色、时区与阈值。
- `status_engine.py`：纯数据规则、子系统汇总、任务/机器明细。
- `status_store.py`：SQLite 表初始化、快照、Incident、时间桶统计。
- `collector.py`：独立采集循环、严格数据读取、单进程锁。
- `status_page.py`：本地只读页面、7/30/90天色条、小时详情和异常历史。

本模块使用独立 SQLite 建表，不涉及现有 MySQL 或 Alembic 迁移。
