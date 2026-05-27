import logging
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Any

_log_init_done = False
_log_job_id: ContextVar[str] = ContextVar("log_job_id", default="")
_log_step: ContextVar[str] = ContextVar("log_step", default="")
_correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="-")


class CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "correlation_id"):
            try:
                from flask import g as _flask_g
                record.correlation_id = getattr(_flask_g, "correlation_id", "-")
            except (ImportError, RuntimeError):
                record.correlation_id = _correlation_id_var.get("-")
        if not hasattr(record, "job_context"):
            try:
                from flask import g as _flask_g
                jid = getattr(_flask_g, "log_job_id", "")
                step = getattr(_flask_g, "log_step", "")
                if jid or step:
                    record.job_context = f"{jid}:{step}" if step else jid
                else:
                    tid = _log_job_id.get("")
                    tstep = _log_step.get("")
                    record.job_context = f"{tid}:{tstep}" if tid and tstep else (tid or "-")
            except (ImportError, RuntimeError):
                tid = _log_job_id.get("")
                tstep = _log_step.get("")
                record.job_context = f"{tid}:{tstep}" if tid and tstep else (tid or "-")
        return True


class StructuredLogger:
    def __init__(self, name: str):
        self._logger = logging.getLogger(name)

    def _log(self, level: int, msg: str, *args: Any, **kwargs: Any) -> None:
        structured: dict[str, Any] = {}
        exc_info = False
        for k, v in kwargs.items():
            if k == "exc_info":
                exc_info = v
            elif k == "level":
                structured["log_level_override"] = v
            else:
                structured[k] = v
        self._logger.log(level, msg, *args, extra={"structured": structured}, exc_info=exc_info)

    def trace(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log(5, msg, *args, **kwargs)

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log(logging.DEBUG, msg, *args, **kwargs)

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log(logging.INFO, msg, *args, **kwargs)

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log(logging.WARNING, msg, *args, **kwargs)

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log(logging.ERROR, msg, *args, **kwargs)

    def exception(self, msg: str, *args: Any, **kwargs: Any) -> None:
        kwargs["exc_info"] = True
        self._log(logging.ERROR, msg, *args, **kwargs)

    def set_context(self, job_id: str | None = None, step: str | None = None) -> None:
        _log_job_id.set(job_id or "")
        _log_step.set(step or "")
        try:
            from flask import g as _flask_g
            _flask_g.log_job_id = job_id or ""
            _flask_g.log_step = step or ""
        except (ImportError, RuntimeError):
            pass


def get_structured_logger(name: str) -> StructuredLogger:
    return StructuredLogger(name)


class StructuredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        corr_id = getattr(record, "correlation_id", "-")
        job_ctx = getattr(record, "job_context", "-")
        structured = getattr(record, "structured", {})

        payload = ""
        if structured:
            parts = []
            for k, v in structured.items():
                if v is not None:
                    parts.append(f"{k}={v}")
            if parts:
                payload = " | " + ", ".join(parts)

        line = (
            f"{self.formatTime(record, self.datefmt)} "
            f"[{record.levelname}] "
            f"{corr_id} "
            f"{record.name}:{job_ctx} "
            f"{record.getMessage()}"
            f"{payload}"
        )

        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
            if record.exc_text:
                line += "\n" + record.exc_text

        return line


def setup_logging(
    debug: bool = False,
    log_file: str | None = None,
    log_level: int | str | None = None,
) -> None:
    global _log_init_done

    if _log_init_done:
        return

    if log_level is not None:
        if isinstance(log_level, str):
            log_level = getattr(logging, log_level.upper(), logging.INFO)
        level = log_level
    else:
        level = logging.DEBUG if debug else logging.INFO

    formatter = StructuredFormatter(
        fmt="%(asctime)s [%(levelname)s] %(correlation_id)s %(name)s:%(job_context)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers.clear()

    corr_filter = CorrelationFilter()
    root_logger.addFilter(corr_filter)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)
    root_logger.addHandler(stdout_handler)

    if log_file:
        from logging.handlers import RotatingFileHandler

        file_handler = RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=3
        )
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    for noisy in ("urllib3", "matplotlib", "PIL", "httpx", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _log_init_done = True


def inject_correlation_id() -> str:
    try:
        from flask import g as _flask_g
        from flask import request as _flask_request

        existing = getattr(_flask_g, "correlation_id", None)
        if existing:
            return existing

        header = _flask_request.headers.get("X-Correlation-ID", "")
        corr_id = header or str(uuid.uuid4())[:12]
        _flask_g.correlation_id = corr_id
        _correlation_id_var.set(corr_id)
        return corr_id
    except (ImportError, RuntimeError):
        corr_id = str(uuid.uuid4())[:12]
        _correlation_id_var.set(corr_id)
        return corr_id


def log_step_start(logger_instance: StructuredLogger, step_name: str, **kwargs: Any) -> float:
    logger_instance.info(f"DÉBUT {step_name}", **kwargs)
    return time.monotonic()


def log_step_end(
    logger_instance: StructuredLogger,
    step_name: str,
    start_time: float,
    success: bool = True,
    **kwargs: Any,
) -> float:
    elapsed = time.monotonic() - start_time
    status = "OK" if success else "ÉCHEC"
    logger_instance.info(
        f"FIN {step_name}",
        status=status,
        duree_secondes=round(elapsed, 2),
        **kwargs,
    )
    return elapsed
