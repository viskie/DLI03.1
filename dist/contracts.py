"""Deterministic daily Oracle research-access contract catalogue.

The access fee and the subscriber's estimated exchange capital are deliberately
separate.  DLI never receives or controls the latter through this catalogue.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Any


CONTRACT_DURATIONS = (1, 3, 9)
DEFAULT_DAILY_SIGNAL_CAP = 30
DEFAULT_CAPITAL_RESERVE_PERCENT = 10.0
DISCOUNT_BY_DAYS = {1: 1.0, 3: 0.90, 9: 0.75}


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _utc_day(now_ms: int) -> tuple[str, int, int]:
    value = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    start = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    start_ms = int(start.timestamp() * 1000)
    return start.date().isoformat(), start_ms, start_ms + 86_400_000


def _peak_concurrency(rows: list[dict[str, Any]], days: int) -> int:
    """Repeat today's batch daily and measure the peak overlapping positions."""
    events: list[tuple[int, int]] = []
    for day in range(days):
        day_offset = day * 86_400_000
        for row in rows:
            horizon_ms = max(1, int(row.get("forecastHorizonSeconds") or 0)) * 1000
            events.append((day_offset, 1))
            # Close events sort before opens at the same instant.
            events.append((day_offset + horizon_ms, -1))
    active = peak = 0
    for _, change in sorted(events, key=lambda item: (item[0], item[1])):
        active += change
        peak = max(peak, active)
    return peak


def build_daily_contracts(
    source: dict[str, Any] | None,
    *,
    now_ms: int,
    prediction_quantum_reference_usd: float,
    daily_signal_cap: int = DEFAULT_DAILY_SIGNAL_CAP,
    capital_reserve_percent: float = DEFAULT_CAPITAL_RESERVE_PERCENT,
) -> dict[str, Any]:
    source = source if isinstance(source, dict) else {}
    source_instance = str(source.get("sourceInstance") or "o369")
    source_instance_key = str(source.get("sourceInstanceKey") or "instance-2")
    day_id, issued_at_ms, purchase_until_ms = _utc_day(now_ms)
    source_rows = source.get("rows") if isinstance(source.get("rows"), list) else []
    rows = [row for row in source_rows if isinstance(row, dict)]
    rows.sort(key=lambda row: (int(row.get("rank") or 10**9), str(row.get("market") or "")))
    rows = rows[:max(1, int(daily_signal_cap))]
    generated_at_ms = int(source.get("generatedAtMs") or 0)
    source_age_seconds = max(0.0, (now_ms - generated_at_ms) / 1000) if generated_at_ms else None
    fx = source.get("usdInr") if isinstance(source.get("usdInr"), dict) else {}
    usd_inr_rate = float(fx.get("rate") or 0)
    fx_observed_at_ms = int(fx.get("observedAtMs") or 0)
    fx_age_seconds = max(0.0, (now_ms - fx_observed_at_ms) / 1000) if fx_observed_at_ms else None
    fx_ready = usd_inr_rate > 0 and fx_age_seconds is not None and fx_age_seconds <= 330
    blocked = [str(value) for value in source.get("blockedReasons", []) if value]
    source_ready = (
        source.get("scopeStatus") == "ready"
        and int(source.get("tradeEligibleNowCount") or 0) > 0
        and bool(rows)
        and source_age_seconds is not None
        and source_age_seconds <= 330
        and fx_ready
    )
    if not source_ready and not blocked:
        blocked = ["No fresh production trade-qualified prediction batch or USD/INR conversion"]
    elif not fx_ready:
        blocked.append("Fresh production USD/INR conversion unavailable")

    quantum = source.get("tradeQuantum") if isinstance(source.get("tradeQuantum"), dict) else {}
    per_trade_inr = float(quantum.get("investmentInr") or 1055.0)
    batch_rows = [
        {
            key: row.get(key)
            for key in (
                "rank", "market", "direction", "originAtMs", "targetAtMs",
                "forecastHorizonSeconds", "spotPrice", "predictedPrice",
                "deltaPercent", "confidence", "signalId",
            )
        }
        for row in rows
    ]
    batch_hash = _canonical_digest(batch_rows)
    contracts = []
    for days in CONTRACT_DURATIONS:
        entitlements = len(rows) * days
        peak = _peak_concurrency(rows, days) if rows else 0
        capital = math.ceil(peak * per_trade_inr * (1 + capital_reserve_percent / 100))
        per_trade_usd = per_trade_inr / usd_inr_rate if fx_ready else 0.0
        capital_usd = capital / usd_inr_rate if fx_ready else 0.0
        access_fee_cents = max(100, math.ceil(
            entitlements
            * max(0.01, float(prediction_quantum_reference_usd))
            * DISCOUNT_BY_DAYS[days]
            * 100
        )) if rows else 0
        identity = {
            "issueDate": day_id,
            "accessDays": days,
            "scopeId": source.get("scopeId"),
            "batchSha256": batch_hash,
            "sourceInstance": source_instance,
        }
        contracts.append({
            "contractId": "O961-" + _canonical_digest(identity)[:20].upper(),
            "name": f"Oracle 961 · {days}-day bot list",
            "accessDays": days,
            "status": "available" if source_ready else "held",
            "currency": "USD",
            "accessFeeCents": access_fee_cents,
            "dailyBatchSize": len(rows),
            "signalEntitlementCount": entitlements,
            "peakConcurrentPositions": peak,
            "perTradeCapitalInr": per_trade_inr,
            "estimatedBotCapitalInr": capital,
            "perTradeCapitalUsd": round(per_trade_usd, 2),
            "estimatedBotCapitalUsd": math.ceil(capital_usd * 100) / 100 if fx_ready else 0.0,
            "usdInrRate": usd_inr_rate or None,
            "usdInrObservedAtMs": fx_observed_at_ms or None,
            "usdInrBasis": fx.get("basis"),
            "capitalReservePercent": capital_reserve_percent,
            "issuedAtMs": issued_at_ms,
            "purchaseUntilMs": purchase_until_ms,
            "accessUntilMs": issued_at_ms + days * 86_400_000,
            "scopeId": source.get("scopeId"),
            "forecastConfigId": source.get("forecastConfigId"),
            "modelSha256": source.get("modelSha256"),
            "sourceInstance": source_instance,
            "sourceInstanceKey": source_instance_key,
            "batchSha256": batch_hash,
            "blockedReasons": [] if source_ready else blocked,
            "notice": "Access to a research prediction list. Subscriber exchange capital remains separate and is never collected by this contract.",
        })
    return {
        "schemaVersion": 1,
        "issueDate": day_id,
        "generatedAtMs": now_ms,
        "sourceGeneratedAtMs": generated_at_ms or None,
        "sourceAgeSeconds": round(source_age_seconds, 3) if source_age_seconds is not None else None,
        "usdInrRate": usd_inr_rate or None,
        "usdInrObservedAtMs": fx_observed_at_ms or None,
        "sourceScopeId": source.get("scopeId"),
        "sourceInstance": source_instance,
        "sourceInstanceKey": source_instance_key,
        "sourceBatchSha256": batch_hash,
        "available": source_ready,
        "contracts": contracts,
        # Kept only inside the encrypted status artifact. Public endpoints must
        # remove this field unless the signed-in account owns the contract.
        "predictionBatch": batch_rows,
        "methodology": {
            "dailyIssueCount": 3,
            "durationsDays": list(CONTRACT_DURATIONS),
            "capitalFormula": "ceil(peak overlapping positions * per-trade INR budget * (1 + reserve percent))",
            "accessFeeFormula": "ceil(signal entitlements * hourly prediction-quantum USD reference * duration discount)",
            "durationDiscounts": {str(key): value for key, value in DISCOUNT_BY_DAYS.items()},
        },
    }
