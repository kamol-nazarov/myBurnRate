"""Local collection in its own process.

Reading transcripts is CPU-heavy pure Python. Run on the web server's threads,
a long pass starved the HTTP server: setup and HUD requests waited up to a
minute, and the watchdog restarted a service that was only busy. The scheduler
still lives in the web process; each local pass is handed to a long-lived
worker process and awaited there. The worker keeps the collectors' caches warm
between passes. The web process keeps everything that touches its own memory:
quota lane nudges, the Hub proxy cache, live-update events and the heartbeat.

Enabled by the windowless service (``BURNRATE_COLLECTOR_PROCESS=1``); tests and
the CLI keep collecting in-process.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import threading

ENV_FLAG = "BURNRATE_COLLECTOR_PROCESS"
# A first read of a large history can take many minutes; a pass that has not
# answered after this long is killed and the worker restarted.
PASS_TIMEOUT_SECONDS = 60 * 60

LOG = logging.getLogger("burnrate.collector")


def enabled() -> bool:
    return os.getenv(ENV_FLAG, "").strip().lower() in {"1", "true", "yes"}


def run_local_pass(settings, pricing) -> dict:
    """One local collection pass; runs in the worker (or in-process for tests)."""
    from spend_app.db import migrate_usage_keys_step
    from spend_app.providers import REGISTRY
    from spend_app.scheduler import _run_ingest_specs

    timings: dict[str, float] = {}
    results: dict[str, dict] = {}
    errors: list[str] = []
    try:
        # Moves identity records out of app_meta in small steps until done.
        migrate_usage_keys_step(settings.database_path)
    except Exception as exc:
        errors.append(f"identity migration step failed ({type(exc).__name__})")
    run_profile_pass(settings, pricing, timings, results, errors)
    try:
        _run_ingest_specs(
            [(spec, {}) for spec in REGISTRY if spec.kind in {"local", "manual"}],
            settings, pricing, timings=timings, results=results, side_effects=False,
        )
    except Exception as exc:
        errors.append(str(exc) or type(exc).__name__)
    return {"timings": timings, "results": results, "errors": errors}


def run_profile_pass(settings, pricing, timings: dict, results: dict, errors: list) -> None:
    """Usage from extra Claude Code accounts and OpenCode profiles (before the default collectors)."""
    import time

    from spend_app.adapters import profiles_local

    began = time.perf_counter()
    try:
        result = profiles_local.ingest(database_path=settings.database_path, pricing=pricing)
        written = (result.get("eventsWritten") or 0) + (result.get("unpricedEventsWritten") or 0)
        results[profiles_local.NAME] = {"written": written, "status": result.get("status")}
    except Exception as exc:
        errors.append(f"extra profile collection failed ({type(exc).__name__})")
    finally:
        timings[profiles_local.NAME] = time.perf_counter() - began


def _worker_main(connection, settings) -> None:
    import logging.handlers

    from spend_app.pricing import PricingEngine

    try:
        settings.logs_path.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            settings.logs_path / "collector.log", maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)
    except Exception:
        pass
    pricing = PricingEngine.load(settings.pricing_path)
    while True:
        try:
            message = connection.recv()
        except (EOFError, OSError):
            return  # the web process ended
        if not isinstance(message, dict) or message.get("op") != "pass":
            return
        try:
            reply = run_local_pass(settings, pricing)
        except Exception as exc:  # never leave the web process waiting
            reply = {"timings": {}, "results": {}, "errors": [f"collector pass failed ({type(exc).__name__})"]}
        try:
            connection.send(reply)
        except (EOFError, OSError):
            return


class CollectorWorker:
    """A long-lived worker process that runs local passes on request."""

    def __init__(self, settings) -> None:
        self._settings = settings
        self._context = multiprocessing.get_context("spawn")
        self._process = None
        self._connection = None
        self._lock = threading.Lock()

    def _ensure(self) -> None:
        if self._process is not None and self._process.is_alive():
            return
        self._close()
        parent, child = self._context.Pipe()
        process = self._context.Process(
            target=_worker_main, args=(child, self._settings), name="burnrate-collector", daemon=True)
        process.start()
        child.close()
        self._process, self._connection = process, parent
        LOG.info("collector worker started (pid %s)", process.pid)

    def _close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            except OSError:
                pass
        if self._process is not None and self._process.is_alive():
            self._process.kill()
            self._process.join(timeout=5)
        self._process, self._connection = None, None

    def run_pass(self, timeout: float = PASS_TIMEOUT_SECONDS) -> dict:
        with self._lock:
            self._ensure()
            try:
                self._connection.send({"op": "pass"})
                if not self._connection.poll(timeout):
                    raise TimeoutError(f"collector pass did not finish within {int(timeout)} s")
                return self._connection.recv()
            except (EOFError, OSError, TimeoutError):
                # A dead or stuck worker is replaced on the next pass.
                self._close()
                raise

    def stop(self) -> None:
        with self._lock:
            self._close()
