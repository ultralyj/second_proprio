"""按本地日期生成到期任务并合并离线提醒。"""

import datetime as dt
from zoneinfo import ZoneInfo

from second_proprio import config as configuration
from second_proprio.store import Store


def now_local(config: dict) -> dt.datetime:
    """返回配置时区的当前时间。"""
    return dt.datetime.now(ZoneInfo(config["timezone"]))


def enqueue(store: Store, config: dict, now: dt.datetime) -> int:
    """从持久游标补齐到期任务；当天始终重扫以接受时间配置变化。"""
    if not config["scheduler"]["enabled"]:
        return 0
    start = dt.date.fromisoformat(config["scheduler"]["start_date"])
    cursor = store.get_setting("schedule_date")
    if cursor:
        start = max(start, dt.date.fromisoformat(cursor))
    end = now.date()
    count = 0
    revision = configuration.revision(config)
    while start <= end:
        for kind, schedule in config["schedules"].items():
            if not schedule.get("enabled", True):
                continue
            if start.weekday() not in schedule["weekdays"]:
                continue
            for clock in schedule["times"]:
                scheduled = dt.datetime.combine(start,
                                                dt.time.fromisoformat(clock),
                                                tzinfo=ZoneInfo(
                                                    config["timezone"]))
                if scheduled <= now:
                    store.add(kind, scheduled.isoformat(), revision)
                    count += 1
        start += dt.timedelta(days=1)
    store.set_setting("schedule_date", end.isoformat())
    return count


def ready_tasks(store: Store, config: dict, now: dt.datetime) -> list[dict]:
    """合并课程与过期提醒，返回到期且允许重试的任务。"""
    available = []
    zone = ZoneInfo(config["timezone"])
    for task in store.export_tasks():
        if task["status"] in {
                "completed", "skipped", "running", "cancel_requested",
                "cancelled", "waiting_review"
        }:
            continue
        if task.get("source",
                    "schedule") == "schedule" and not config["schedules"][
                        task["kind"]].get("enabled", True):
            continue
        scheduled = dt.datetime.fromisoformat(task["scheduled_at"])
        if scheduled > now:
            continue
        if task["status"] == "failed":
            if task.get(
                    "failures",
                    task["attempts"]) >= config["scheduler"]["max_attempts"]:
                continue
        if task.get("retry_at"):
            if dt.datetime.fromisoformat(task["retry_at"]) > now:
                continue
        if task["kind"] == "care" and scheduled.astimezone(
                zone).date() < now.date():
            store.finish(task["id"], "skipped", {"reason": "历史关心提醒已过期"})
            continue
        available.append(task)
    for kind in ("course", "care"):
        group = [
            task for task in available if task["kind"] == kind and
            task.get("source", "schedule") == "schedule"
        ]
        if len(group) > 1:
            for old in group[:-1]:
                store.finish(old["id"], "skipped", {"reason": "离线任务合并"})
                available.remove(old)
    return sorted(available,
                  key=lambda task: ({
                      "care": 0,
                      "review": 1,
                      "course": 2,
                      "papers": 3
                  }[task["kind"]], task["scheduled_at"]))
