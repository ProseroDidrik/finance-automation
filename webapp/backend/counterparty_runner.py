"""Bakgrunds-orkestrering för check_counterparties.py.

Trigger:
    POST /api/counterparties/run  →  start_run(period, ...)

State:
    Global, single-runner-at-a-time. Subprocess kör i en bakgrundstråd och
    läser stdout rad för rad till en bounded log-lista (senaste 200 rader).

Polling:
    GET /api/counterparties/run/status  →  get_status()
"""
from __future__ import annotations

import os
import subprocess
import threading
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "check_counterparties.py"
LOG_TAIL_MAX = 200


class _State:
    """Mutable singleton för pågående/senaste körning."""
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running: bool = False
        self.run_id: str | None = None
        self.period: str | None = None
        self.with_sanctions: bool = False
        self.include_customers: bool = False
        self.started_at: datetime | None = None
        self.completed_at: datetime | None = None
        self.log: deque[str] = deque(maxlen=LOG_TAIL_MAX)
        self.return_code: int | None = None
        self.error: str | None = None

_state = _State()


def get_status() -> dict:
    with _state.lock:
        return {
            "running":           _state.running,
            "run_id":            _state.run_id,
            "period":            _state.period,
            "with_sanctions":    _state.with_sanctions,
            "include_customers": _state.include_customers,
            "started_at":        _state.started_at.isoformat() if _state.started_at else None,
            "completed_at":      _state.completed_at.isoformat() if _state.completed_at else None,
            "log_tail":          list(_state.log),
            "return_code":       _state.return_code,
            "error":             _state.error,
        }


def start_run(
    period: str, with_sanctions: bool, include_customers: bool,
) -> dict:
    """Startar check_counterparties.py i bakgrundstråd. Returnerar status-dict.

    Höjer RuntimeError om en körning redan pågår eller scriptet saknas.
    """
    if not SCRIPT.exists():
        raise RuntimeError(f"Scriptet saknas: {SCRIPT}")

    with _state.lock:
        if _state.running:
            raise RuntimeError("En körning pågår redan")
        # Validera period (YYYYMM)
        if not (len(period) == 6 and period.isdigit()):
            raise ValueError(f"Ogiltigt period-format: {period!r}")

        _state.running           = True
        _state.run_id            = uuid.uuid4().hex[:12]
        _state.period            = period
        _state.with_sanctions    = with_sanctions
        _state.include_customers = include_customers
        _state.started_at        = datetime.now()
        _state.completed_at      = None
        _state.log.clear()
        _state.return_code       = None
        _state.error             = None
        run_id = _state.run_id

    threading.Thread(
        target=_run_subprocess,
        args=(run_id, period, with_sanctions, include_customers),
        daemon=True,
    ).start()
    return get_status()


def _run_subprocess(
    run_id: str, period: str, with_sanctions: bool, include_customers: bool,
) -> None:
    """Body för bakgrundstråden. Skriver stdout till _state.log."""
    cmd = ["py", str(SCRIPT), "--period", period]
    if with_sanctions:
        cmd.append("--with-sanctions")
    if include_customers:
        cmd.append("--include-customers")

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    rc: int | None = None
    err: str | None = None
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(REPO),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env=env, bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            with _state.lock:
                if _state.run_id != run_id:
                    # Ny körning startad medan den här fortfarande lever — överge
                    proc.terminate()
                    return
                _state.log.append(line)
        proc.wait()
        rc = proc.returncode
    except FileNotFoundError as e:
        err = f"Kunde inte starta python: {e}"
    except Exception as e:  # noqa: BLE001
        err = str(e)
    finally:
        with _state.lock:
            if _state.run_id == run_id:
                _state.running      = False
                _state.completed_at = datetime.now()
                _state.return_code  = rc
                _state.error        = err
