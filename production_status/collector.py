"""Run with: python -m produce_monitor.production_status.collector

One collector per local database. It survives closed browser tabs and uses the
existing dashboard's fetchers without their Streamlit caches or alert scheduler.
"""
import argparse
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from .config import COMPONENTS, INTERVAL_SECONDS, bucket_time, load_rules, local_now
from .status_engine import (aggregate, calculate_bgm_status, calculate_dubbing_status,
                            calculate_recognition_status, calculate_upload_status, unknown)
from .status_store import StatusStore

logger = logging.getLogger("production_status")


@contextmanager
def collector_lock(path):
    """OS-held lock releases automatically on crash; prevents concurrent collectors."""
    path = Path(str(path) + ".collector.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("该数据库已有 Status 采集器运行") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def collect_once(store, rules):
    # Lazy import avoids requiring any production integrations for the read-only page.
    from produce_monitor import production_monitor as source

    started = time.monotonic()
    frames, errors = {}, {}
    # Source functions provide explicit strict failure semantics. None means no
    # cycle filter: old unfinished tasks must not disappear after five cycles.
    def fetch(kind, system):
        if kind == "recognition":
            records = source.fetch_recognition_data.__wrapped__(system, None, strict=True)
            return source._recognition_records_to_dataframe(records)
        if kind == "dubbing":
            records = source.fetch_production_data.__wrapped__(system, None, strict=True)
            return source._production_records_to_dataframe(records)
        return source.fetch_upload_statistics_data.__wrapped__(strict=True, system_names=[system])

    jobs = [(kind, system) for system in source.SYSTEMS for kind in ("recognition", "dubbing")]
    jobs.extend(("upload", system) for system in source.UPLOAD_STAT_SYSTEMS)
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="status-source") as pool:
        futures = {pool.submit(fetch, kind, system): (kind, system) for kind, system in jobs}
        coordinator_future = pool.submit(source.fetch_auto_upload_client_status.__wrapped__)
        for future in as_completed(futures):
            kind, system = futures[future]
            try:
                frames[(kind, system)] = future.result()
            except Exception as exc:
                # The UI exposes the failing source, not credentials or request URLs.
                errors[(kind, system)] = f"{system} / {kind} 数据读取失败（{type(exc).__name__}），本次为未知"
                logger.warning("Source failed: %s / %s (%s)", kind, system, type(exc).__name__)
        try:
            coordinator = coordinator_future.result()
        except Exception:
            coordinator = {"supported": False, "error": "上传协调服务状态读取失败"}

    now = local_now()
    elapsed = time.monotonic() - started
    samples = []
    for kind, system in jobs:
        components = ("recognition", "bgm") if kind == "recognition" else (kind,)
        for component in components:
            if (kind, system) in errors or elapsed > INTERVAL_SECONDS:
                reason = errors.get((kind, system), "本轮采集耗时超过5分钟，数据时效不足")
                samples.append(unknown(component, system, reason))
                continue
            df = frames[(kind, system)]
            try:
                if component == "upload":
                    sample = calculate_upload_status(df, system, coordinator, now, rules)
                else:
                    calculator = {"recognition": calculate_recognition_status,
                                  "bgm": calculate_bgm_status,
                                  "dubbing": calculate_dubbing_status}[component]
                    sample = calculator(df, system, now, rules)
                samples.append(sample)
            except Exception as exc:
                logger.warning("Rule evaluation failed: %s / %s (%s)", component, system, type(exc).__name__)
                samples.append(unknown(component, system, "状态计算失败，等待下次采集"))

    for component in COMPONENTS:
        samples.append(aggregate(component, [item for item in samples if item.component == component]))
    store.save_snapshots(samples, now, elapsed, asdict(rules))
    logger.info("Snapshot saved: %s; samples=%d; incomplete=%d; seconds=%.1f",
                bucket_time(now).isoformat(), len(samples), sum(not item.data_complete for item in samples), elapsed)


def main():
    parser = argparse.ArgumentParser(description="Production Status 每5分钟独立采集")
    parser.add_argument("--once", action="store_true", help="仅采集一次（同一个时间桶不会重复写入）")
    parser.add_argument("--db", help="SQLite 路径；必须与 Status 页面使用同一路径")
    parser.add_argument("--init-only", action="store_true", help="仅初始化本地数据库，不访问业务系统")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = StatusStore(Path(args.db).resolve() if args.db else None)
    store.initialize()
    if args.init_only:
        logger.info("Status database initialized: %s", store.path)
        return
    rules = load_rules()
    try:
        with collector_lock(store.path):
            while True:
                try:
                    if not store.has_bucket(bucket_time(local_now())):
                        collect_once(store, rules)
                    else:
                        logger.info("Current bucket already collected")
                except Exception:
                    logger.exception("Collection failed; missing bucket remains Unknown")
                    if args.once:
                        raise
                if args.once:
                    break
                # Align to the next wall-clock five-minute boundary; never backfill
                # missed buckets with today's state. Short sleeps allow clean shutdown.
                wait = INTERVAL_SECONDS - (local_now() - bucket_time(local_now())).total_seconds()
                deadline = time.monotonic() + max(1, wait)
                while time.monotonic() < deadline:
                    time.sleep(min(1, max(0, deadline - time.monotonic())))
    except KeyboardInterrupt:
        logger.info("Status collector stopped")


if __name__ == "__main__":
    main()
