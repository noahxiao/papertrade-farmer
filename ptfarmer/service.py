"""Long-running service loop: ticks, heartbeat, daily summary, graceful shutdown.

On SIGTERM/SIGINT the farmer stops opening rounds and keeps ticking until no
unhedged exposure remains (pending opens cancelled, single legs closed),
or SHUTDOWN_GRACE_S elapses. Hedged pairs are left open unless
FLATTEN_HEDGED_ON_SHUTDOWN=true; the next process resumes them.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .app import Components
from .budget import utc_day_start
from .reporting import daily_summary_text, summarize_day

log = logging.getLogger(__name__)


def write_heartbeat(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, default=str))
    os.replace(tmp, path)


def heartbeat_age_s(path: Path, now: float | None = None) -> float | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return (now or time.time()) - float(data.get("ts", 0))


class Service:
    def __init__(self, components: Components, clock=time.time):
        self.c = components
        self.cfg = components.cfg
        self.farmer = components.farmer
        self.clock = clock
        self.stop_event = threading.Event()
        self.shutdown_started: float | None = None

    def request_shutdown(self, *_args) -> None:
        if self.shutdown_started is None:
            log.warning("shutdown requested; closing any unhedged exposure before exiting")
            self.shutdown_started = self.clock()
            self.farmer.shutting_down = True
        self.stop_event.set()

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self.request_shutdown)
        signal.signal(signal.SIGINT, self.request_shutdown)

    def heartbeat(self) -> None:
        write_heartbeat(self.cfg.heartbeat_path, {"ts": self.clock(), "pid": os.getpid(), **self.farmer.status()})

    def maybe_daily_summary(self) -> None:
        now = self.clock()
        if datetime.fromtimestamp(now, tz=timezone.utc).hour < self.cfg.daily_summary_hour_utc:
            return
        day_start = utc_day_start(now) - 86400
        day = datetime.fromtimestamp(day_start, tz=timezone.utc).strftime("%Y-%m-%d")
        if self.c.store.kv_get("last_summary_day") == day:
            return
        active = self.farmer.active_rounds()
        text = daily_summary_text(self.cfg, summarize_day(self.c.store, day_start), self.farmer.tranche_usage(active),
                                  self.farmer.last_ctx, self.farmer.last_account, self.c.baseline, now)
        log.info("daily summary\n%s", text)
        self.c.notifier.post(text)
        self.c.store.log_event("daily_summary", day=day, text=text)
        self.c.store.kv_set("last_summary_day", day)

    def acquire_instance_lock(self) -> bool:
        """One farmer per data dir: a second engine would treat the first one's legs as orphans."""
        self.cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock_file = open(self.cfg.data_dir / "farmer.lock", "w")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        self._lock_file.write(str(os.getpid()))
        self._lock_file.flush()
        return True

    def run(self) -> int:
        if not self.acquire_instance_lock():
            log.error("another farmer is running on %s; refusing to start", self.cfg.data_dir)
            return 4
        self.install_signal_handlers()
        self.c.store.log_event("startup", mode=self.cfg.mode, config=self.cfg.redacted())
        self.c.notifier.alert("startup", f"farmer started in {self.cfg.mode} mode", level="info", cooldown_s=0)
        while True:
            try:
                self.farmer.tick()
            except Exception as exc:  # keep the loop alive; systemd restarts on a hard crash
                log.exception("tick failed")
                self.c.notifier.alert("tick_error", f"{type(exc).__name__}: {exc}", level="error", cooldown_s=900)
            self.heartbeat()
            try:
                self.maybe_daily_summary()
            except Exception:
                log.exception("daily summary failed")
            if self.shutdown_started is not None:
                if self.farmer.safe_to_exit():
                    break
                if self.clock() - self.shutdown_started > self.cfg.shutdown_grace_s:
                    exposure = "; ".join(self.farmer.exposure_summary())
                    self.c.notifier.alert("shutdown_with_exposure", f"grace expired with open work: {exposure}. "
                                          "Restart the service to resume management.", level="critical", cooldown_s=0)
                    break
                time.sleep(min(self.cfg.poll_interval_s, 5))
                continue
            self.stop_event.wait(self.cfg.poll_interval_s)
        self.c.notifier.alert("shutdown", "farmer stopped", level="info", cooldown_s=0)
        self.heartbeat()
        return 0
