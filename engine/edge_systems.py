"""
Alpha Engine — Edge Systems Shim
=================================
Provides `rug_detector` and `kelly` singletons used by paper_engine.py
to run in full feature mode without hitting the ImportError fallback.

The rug detector is a **synchronous wrapper** around the async on-chain
helpers in `engine/rug_checks.py`. It enforces a hard timeout and caches
verdicts per mint so the paper engine never blocks on slow RPCs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

try:
    from engine.rug_checks import (
        check_metadata_authorities,
        is_metadata_locked,
    )
except Exception:  # pragma: no cover - relative import for engine/ run dir
    try:
        from rug_checks import (  # type: ignore
            check_metadata_authorities,
            is_metadata_locked,
        )
    except Exception:
        check_metadata_authorities = None  # type: ignore
        is_metadata_locked = None  # type: ignore


# ── Rug detector ──────────────────────────────────────────────────────────────

@dataclass
class RugReport:
    score: int        # 0-100, higher = safer
    safe:  bool       # True if it passes the safety bar
    reason: str = ""


# Static creator/mint blacklist — extendable via RUG_BLACKLIST env (comma-sep).
_DEFAULT_BLACKLIST: set[str] = set()


def _load_blacklist() -> set[str]:
    raw = os.getenv("RUG_BLACKLIST", "")
    extra = {x.strip() for x in raw.split(",") if x.strip()}
    return _DEFAULT_BLACKLIST | extra


class _RugDetector:
    """
    Sync wrapper that runs the async on-chain checks with:
      * a hard per-mint timeout (default 2.5s)
      * an in-memory TTL cache (default 5 min) to avoid re-querying the same mint
      * a permissive fallback when Helius is unreachable so the paper engine
        keeps flowing — the 17-feature gate remains the primary blocker, but
        any *positive* on-chain signal is now reflected in the rug score.
    """

    SAFE_THRESHOLD     = 60
    TIMEOUT_SEC        = float(os.getenv("RUG_CHECK_TIMEOUT", "2.5"))
    CACHE_TTL_SEC      = float(os.getenv("RUG_CHECK_CACHE_TTL", "300"))

    # Score weights (sum = 100)
    BASE_SCORE         = 30
    W_AUTHORITIES      = 40
    W_METADATA_LOCKED  = 30

    def __init__(self) -> None:
        self._helius_url = os.getenv("HELIUS_RPC_URL", "").strip()
        self._blacklist  = _load_blacklist()
        self._cache: dict[str, tuple[float, RugReport]] = {}
        self._lock = threading.Lock()

    # ---- public API ----------------------------------------------------------

    def status(self) -> dict:
        """
        Report whether real on-chain checks are active or whether the detector
        is running in permissive fallback. Surfaced at startup and consumed by
        backtest stats so users can tell whether `rug_filter_alpha` was
        produced from real on-chain signals.
        """
        helpers_ok = (
            check_metadata_authorities is not None
            and is_metadata_locked is not None
        )
        active = bool(self._helius_url) and helpers_ok
        if not helpers_ok:
            mode, reason = "fallback", "rug_checks_import_failed"
        elif not self._helius_url:
            mode, reason = "fallback", "missing_HELIUS_RPC_URL"
        else:
            mode, reason = "active", "ok"
        return {
            "active": active,
            "mode": mode,
            "reason": reason,
            "timeout_sec": self.TIMEOUT_SEC,
            "cache_ttl_sec": self.CACHE_TTL_SEC,
            "blacklist_size": len(self._blacklist),
        }

    def check(self, mint: str) -> RugReport:
        if not mint:
            return RugReport(score=0, safe=False, reason="empty_mint")

        # Blacklist short-circuit
        if mint in self._blacklist:
            return RugReport(score=0, safe=False, reason="blacklisted_mint")

        # Cache hit?
        cached = self._cache_get(mint)
        if cached is not None:
            return cached

        # No on-chain endpoint configured → permissive fallback (preserves
        # previous paper-mode behavior so existing runs keep working).
        if not self._helius_url or check_metadata_authorities is None:
            report = RugReport(score=85, safe=True, reason="rpc_unavailable")
            self._cache_put(mint, report)
            return report

        try:
            report = self._run_async_with_timeout(self._evaluate(mint))
        except asyncio.TimeoutError:
            report = RugReport(score=70, safe=True, reason="rpc_timeout")
        except Exception as exc:  # pragma: no cover - defensive
            report = RugReport(score=70, safe=True, reason=f"rpc_error:{type(exc).__name__}")

        self._cache_put(mint, report)
        return report

    # ---- internals -----------------------------------------------------------

    async def _evaluate(self, mint: str) -> RugReport:
        # Run authority + metadata-lock checks concurrently for speed.
        auth_task = asyncio.create_task(
            check_metadata_authorities(mint, self._helius_url)
        )
        lock_task = asyncio.create_task(
            is_metadata_locked(mint, self._helius_url)
        )
        auth_result, lock_result = await asyncio.gather(
            auth_task, lock_task, return_exceptions=True
        )

        score      = self.BASE_SCORE
        reasons: list[str] = []

        # Authorities renounced (mint + freeze)
        if isinstance(auth_result, tuple):
            ok_auth, msg = auth_result
            if ok_auth:
                score += self.W_AUTHORITIES
                reasons.append("authorities_renounced")
            else:
                reasons.append(f"authority_active:{msg}")
        else:
            reasons.append("authority_check_error")

        # Metadata immutable (locked)
        if isinstance(lock_result, bool):
            if lock_result:
                score += self.W_METADATA_LOCKED
                reasons.append("metadata_locked")
            else:
                reasons.append("metadata_mutable")
        else:
            reasons.append("metadata_check_error")

        score = max(0, min(100, score))
        safe  = score >= self.SAFE_THRESHOLD
        return RugReport(score=score, safe=safe, reason=";".join(reasons))

    def _run_async_with_timeout(self, coro):
        """
        Run an async coroutine to completion with a hard timeout, regardless of
        whether the caller is itself inside a running event loop. The paper
        engine calls `rug_detector.check()` from sync code, but tests or other
        callers may invoke it from inside asyncio — handle both cases.
        """
        async def _runner():
            return await asyncio.wait_for(coro, timeout=self.TIMEOUT_SEC)

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No loop running — safe to use asyncio.run
            return asyncio.run(_runner())

        # We're inside a running loop. Off-load to a worker thread with its
        # own loop so we don't deadlock.
        result_box: dict = {}
        def _thread_target():
            try:
                result_box["value"] = asyncio.run(_runner())
            except BaseException as exc:  # noqa: BLE001
                result_box["error"] = exc

        t = threading.Thread(target=_thread_target, daemon=True)
        t.start()
        t.join(self.TIMEOUT_SEC + 0.5)
        if "error" in result_box:
            raise result_box["error"]
        if "value" not in result_box:
            raise asyncio.TimeoutError("rug check thread timed out")
        return result_box["value"]

    def _cache_get(self, mint: str) -> RugReport | None:
        with self._lock:
            entry = self._cache.get(mint)
            if entry is None:
                return None
            ts, report = entry
            if time.monotonic() - ts > self.CACHE_TTL_SEC:
                self._cache.pop(mint, None)
                return None
            return report

    def _cache_put(self, mint: str, report: RugReport) -> None:
        with self._lock:
            self._cache[mint] = (time.monotonic(), report)


rug_detector = _RugDetector()

# Emit a one-line startup signal so operators can immediately tell whether
# real on-chain rug checks are active or the detector is in fallback.
try:
    _status = rug_detector.status()
    log.info(
        "rug_detector startup: mode=%s reason=%s timeout=%.2fs cache_ttl=%.0fs blacklist=%d",
        _status["mode"], _status["reason"],
        _status["timeout_sec"], _status["cache_ttl_sec"], _status["blacklist_size"],
    )
except Exception:  # pragma: no cover - defensive, never block import
    pass


# ── Kelly criterion sizing ────────────────────────────────────────────────────

@dataclass
class KellyResult:
    position_sol: float   # SOL to risk on this trade
    full_kelly:   float   # full Kelly fraction (0-1)
    half_kelly:   float   # half-Kelly fraction (safer, 0-1)
    win_rate:     float
    avg_win:      float
    avg_loss:     float


class _KellyCriterion:
    """
    Computes Kelly position size from prior trade outcomes.
    Safe defaults (5% of capital) when there is not enough history.
    """

    DEFAULT_FRACTION = 0.05
    MAX_FRACTION     = 0.10
    MIN_TRADES       = 5

    def compute(self, capital_sol: float, prior_trades: list[dict]) -> KellyResult:
        if not prior_trades or len(prior_trades) < self.MIN_TRADES:
            size = round(capital_sol * self.DEFAULT_FRACTION, 4)
            return KellyResult(
                position_sol=size,
                full_kelly=self.DEFAULT_FRACTION,
                half_kelly=self.DEFAULT_FRACTION,
                win_rate=0.0,
                avg_win=0.0,
                avg_loss=0.0,
            )

        wins   = [t for t in prior_trades if t.get("pnl_sol", 0) > 0]
        losses = [t for t in prior_trades if t.get("pnl_sol", 0) <= 0]
        n      = len(prior_trades)

        win_rate = len(wins) / n if n else 0.0
        avg_win  = (sum(t["pnl_sol"] for t in wins) / len(wins)) if wins else 0.0
        avg_loss = (sum(abs(t["pnl_sol"]) for t in losses) / len(losses)) if losses else 0.0

        if avg_loss <= 0 or avg_win <= 0:
            full_k = self.DEFAULT_FRACTION
        else:
            b      = avg_win / avg_loss          # win/loss ratio
            p      = win_rate
            q      = 1.0 - p
            full_k = max(0.0, (b * p - q) / b)   # classic Kelly

        full_k = min(full_k, self.MAX_FRACTION)
        half_k = full_k * 0.5
        size   = round(capital_sol * half_k if half_k > 0 else capital_sol * self.DEFAULT_FRACTION, 4)

        return KellyResult(
            position_sol=size,
            full_kelly=round(full_k, 4),
            half_kelly=round(half_k if half_k > 0 else self.DEFAULT_FRACTION, 4),
            win_rate=round(win_rate, 4),
            avg_win=round(avg_win, 6),
            avg_loss=round(avg_loss, 6),
        )


kelly = _KellyCriterion()
