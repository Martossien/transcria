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

    # ── Aides « la page répond aux questions du gestionnaire » (C3.6) ──────────

    def next_change(self, windows: list[SchedulingWindow] | None = None,
                    now: datetime | None = None) -> dict | None:
        """La PROCHAINE bascule de créneau (« ce soir 19:00 : suspension des
        départs ») — question gestionnaire n°3. Balaye les 8 prochains jours par
        pas de minute sur les bornes des créneaux (peu de créneaux : trivialement
        rapide et exact, y compris fenêtres à cheval sur minuit)."""
        if not self.enabled:
            return None
        from datetime import timedelta

        windows = [w for w in (windows if windows is not None
                               else SchedulingWindowStore.list_windows()) if w.enabled]
        if not windows:
            return None
        current = (now or self.now()).replace(second=0, microsecond=0)
        active_now = self._active_id(windows, current)
        probe = current
        for _ in range(8 * 24 * 60):
            probe = probe + timedelta(minutes=1)
            active_then = self._active_id(windows, probe)
            if active_then != active_now:
                window = next((w for w in windows if w.id == active_then), None)
                return {
                    "at": probe,
                    "kind": "start" if window is not None else "end",
                    "window": window,
                }
        return None

    def _active_id(self, windows: list[SchedulingWindow], current: datetime) -> int | None:
        best: SchedulingWindow | None = None
        for window in windows:
            if self._is_in_window(window, current):
                if best is None or self._action_rank(window.action) > self._action_rank(best.action):
                    best = window
        return best.id if best else None

    def estimate_queue_resume(self, now: datetime | None = None) -> datetime | None:
        """Si la file est SUSPENDUE par un créneau : l'instant estimé de reprise
        (fin de la période de pause, pauses enchaînées comprises) — question
        gestionnaire n°2. None si la file n'est pas suspendue par l'agenda."""
        if not self.enabled:
            return None
        from datetime import timedelta

        current = (now or self.now()).replace(second=0, microsecond=0)
        if not self.is_queue_paused(current):
            return None
        probe = current
        for _ in range(8 * 24 * 60):
            probe = probe + timedelta(minutes=1)
            if not self.is_queue_paused(probe):
                return probe
        return None

