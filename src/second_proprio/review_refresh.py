"""节流检查复盘来源变化，只登记稳定且到期的更新任务。"""

import asyncio
import copy
import datetime as dt
import time
from typing import Any

from second_proprio import config as configuration
from second_proprio import review_sources
from second_proprio import scheduler


def settle_waiting_sources(service: Any, group: list[dict], existing: dict,
                           day: str, fingerprint: str) -> None:
    """把同日同来源的等待副本指向已有成果，撤销尚未投递的旧提醒。"""
    for duplicate in group:
        if duplicate["status"] != "waiting_source":
            continue
        result = {
            "summary": "同日相同来源已有成果或候选，未重复生成",
            "reused_task_id": existing["id"],
            "review_date": day,
            "review_source_fingerprint": fingerprint,
            "artifacts": existing["result"].get("artifacts", [])
        }
        for field in ("candidate_id", "publication_id"):
            if existing["result"].get(field):
                result[field] = existing["result"][field]
        service.store.finish(duplicate["id"], "skipped", result)
        service.store.event(duplicate["id"], "skipped", "已有同日同来源成果，取消重复的缺来源提示")
        with service.store.connection:
            service.store.connection.execute(
                "UPDATE notifications SET status='resolved',"
                "error=NULL "
                "WHERE id=? AND status IN ('pending','failed')",
                (f"source:{duplicate['id']}",))


async def refresh(service: Any) -> None:
    """通过后台所有者登记复盘更新，解析文件期间不阻塞事件循环。"""
    service.review_scanning = True
    config = copy.deepcopy(service.config)
    try:
        if not config["schedules"]["review"].get("enabled", True):
            return
        report = await asyncio.to_thread(review_sources.scan, service.root,
                                         config)
        tasks = service.store.export_tasks()
        now = scheduler.now_local(config)
        for offset, (day, source) in enumerate(report["dates"].items()):
            if day > str(now.date()):
                continue
            group = [
                task for task in tasks if task["kind"] == "review" and
                (task["payload"].get("date") or
                 dt.datetime.fromisoformat(task["scheduled_at"]).astimezone(
                     now.tzinfo).date().isoformat()) == day
            ]
            if not group:
                continue
            fingerprint = source["fingerprint"]
            key = f"desktop.review_source:{day}"
            previous = service.store.get_setting(key)
            matching = [
                task for task in group
                if task["status"] in {"completed", "waiting_review"} and
                task["result"].get("review_source_fingerprint") == fingerprint
            ]
            existing = next(
                (task for task in matching if task["status"] == "completed"),
                matching[0] if matching else None)
            if existing:
                settle_waiting_sources(service, group, existing, day,
                                       fingerprint)
                service.store.set_setting(key, fingerprint)
                continue
            if any(task["status"] in {"pending", "running", "cancel_requested"}
                   for task in group) or previous == fingerprint:
                continue
            waiting = next(
                (task for task in group if task["status"] == "waiting_source"),
                None)
            if not waiting:
                changed = service.review_changes.get(day)
                if not changed or changed[0] != fingerprint:
                    service.review_changes[day] = (fingerprint,
                                                   time.monotonic())
                    continue
                if (time.monotonic() - changed[1]
                        < config["review"].get("source_debounce_seconds", 300)):
                    continue
                if day == str(now.date()) and not any(
                        clock <= now.strftime("%H:%M")
                        for clock in config["schedules"]["review"]["times"]):
                    continue
            if waiting:
                service.store.retry(waiting["id"])
                identifier = waiting["id"]
            elif any(task["status"] in
                     {"completed", "waiting_review", "skipped"}
                     for task in group) or (previous and any(
                         task["status"] == "failed" for task in group)):
                identifier = service.store.add(
                    "review",
                    (now + dt.timedelta(microseconds=offset)).isoformat(),
                    configuration.revision(config), {
                        "date": day,
                        "mode": "source_update",
                        "review_source_fingerprint": fingerprint
                    })
            else:
                service.store.set_setting(key, fingerprint)
                continue
            service.store.event(identifier, "pending", "该日聊天来源更新，已登记复盘")
            service.store.set_setting(key, fingerprint)
    finally:
        service.review_scanning = False
