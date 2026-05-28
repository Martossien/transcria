from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from transcria.database import db
from transcria.queue.models import SchedulingWindow

DAY_TO_INDEX = {
    "lundi": 0,
    "mardi": 1,
    "mercredi": 2,
    "jeudi": 3,
    "vendredi": 4,
    "samedi": 5,
    "dimanche": 6,
}


@dataclass
class ActiveWindow:
    id: int | None
    name: str
    action: str
    action_params: dict


class SchedulingWindowStore:
    @staticmethod
    def list_windows() -> list[SchedulingWindow]:
        return list(
            db.session.execute(
                db.select(SchedulingWindow).order_by(SchedulingWindow.id.asc())
            ).scalars().all()
        )

    @staticmethod
    def get(window_id: int) -> SchedulingWindow | None:
        return db.session.get(SchedulingWindow, int(window_id))

    @staticmethod
    def create(data: dict) -> SchedulingWindow:
        window = SchedulingWindow(
            name=str(data.get("name", "")).strip(),
            start_time=str(data.get("start", "00:00")),
            end_time=str(data.get("end", "23:59")),
            action=str(data.get("action", "none")),
            enabled=bool(data.get("enabled", True)),
        )
        window.set_days(list(data.get("days") or []))
        window.set_action_params(dict(data.get("action_params") or {}))
        db.session.add(window)
        db.session.commit()
        return window

    @staticmethod
    def update(window_id: int, data: dict) -> SchedulingWindow | None:
        window = SchedulingWindowStore.get(window_id)
        if window is None:
            return None
        if "name" in data:
            window.name = str(data.get("name", "")).strip()
        if "days" in data:
            window.set_days(list(data.get("days") or []))
        if "start" in data:
            window.start_time = str(data.get("start"))
        if "end" in data:
            window.end_time = str(data.get("end"))
        if "action" in data:
            window.action = str(data.get("action"))
        if "action_params" in data:
            window.set_action_params(dict(data.get("action_params") or {}))
        if "enabled" in data:
            window.enabled = bool(data.get("enabled"))
        db.session.commit()
        return window

    @staticmethod
    def delete(window_id: int) -> bool:
        window = SchedulingWindowStore.get(window_id)
        if window is None:
            return False
        db.session.delete(window)
        db.session.commit()
        return True


class SchedulingCalendar:
    def __init__(self, config: dict):
        self.enabled = bool(config.get("enabled", False))
        self.timezone_name = str(config.get("timezone", "Europe/Paris"))
        self.timezone = ZoneInfo(self.timezone_name)

    def now(self) -> datetime:
        return datetime.now(self.timezone)

    def get_active_window(self, now: datetime | None = None) -> ActiveWindow | None:
        if not self.enabled:
            return None
        current = now.astimezone(self.timezone) if now else self.now()
        matches = []
        for window in SchedulingWindowStore.list_windows():
            if not window.enabled:
                continue
            if self._is_in_window(window, current):
                matches.append(window)
        if not matches:
            return None
        matches.sort(key=lambda item: (self._action_rank(item.action), item.id or 0), reverse=True)
        selected = matches[0]
        return ActiveWindow(
            id=selected.id,
            name=selected.name,
            action=selected.action,
            action_params=selected.get_action_params(),
        )

    def is_queue_paused(self, now: datetime | None = None) -> bool:
        active = self.get_active_window(now)
        return bool(active and active.action == "pause_queue")

    def is_force_gpu_allowed(self, now: datetime | None = None) -> bool:
        active = self.get_active_window(now)
        return bool(active and active.action == "force_gpu")

    def get_effective_max_workers(self, base_max: int, now: datetime | None = None) -> int:
        active = self.get_active_window(now)
        if not active or active.action != "limit_concurrency":
            return base_max
        try:
            limit = int(active.action_params.get("max_concurrent_jobs", base_max))
        except (TypeError, ValueError):
            return base_max
        return max(1, min(base_max, limit))

    @staticmethod
    def _action_rank(action: str) -> int:
        return {
            "pause_queue": 4,
            "limit_concurrency": 3,
            "force_gpu": 2,
            "none": 1,
        }.get(action, 0)

    def _is_in_window(self, window: SchedulingWindow, current: datetime) -> bool:
        days = {DAY_TO_INDEX.get(day) for day in window.get_days()}
        days.discard(None)
        if not days:
            return False
        start = self._parse_time(window.start_time)
        end = self._parse_time(window.end_time)
        current_t = current.time().replace(second=0, microsecond=0)
        weekday = current.weekday()
        if start <= end:
            return weekday in days and start <= current_t <= end
        previous_weekday = (weekday - 1) % 7
        return (weekday in days and current_t >= start) or (
            previous_weekday in days and current_t <= end
        )

    @staticmethod
    def _parse_time(value: str) -> time:
        hour, minute = [int(part) for part in value.split(":", 1)]
        return time(hour=hour, minute=minute)

