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
    daily_signal_cap: int | None = None,
    capital_reserve_percent: float = DEFAULT_CAPITAL_RESERVE_PERCENT,
) -> dict[str, Any]:
    source = source if isinstance(source, dict) else {}
    source_instance = str(source.get("sourceInstance") or "o369")
    source_instance_key = str(source.get("sourceInstanceKey") or "instance-2")
    issuance_mode = str(source.get("issuanceMode") or "qualified")
    poc_mode = issuance_mode == "poc-research"
    day_id, issued_at_ms, purchase_until_ms = _utc_day(now_ms)
    source_rows = source.get("rows") if isinstance(source.get("rows"), list) else []
    rows = [row for row in source_rows if isinstance(row, dict)]
    rows.sort(key=lambda row: (int(row.get("rank") or 10**9), str(row.get("market") or "")))
    if daily_signal_cap is not None and int(daily_signal_cap) > 0:
        rows = rows[:int(daily_signal_cap)]
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
        and (poc_mode or int(source.get("tradeEligibleNowCount") or 0) > 0)
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
    all_batch_rows = [
        {
            key: row.get(key)
            for key in (
                "rank", "market", "direction", "originAtMs", "targetAtMs",
                "forecastHorizonSeconds", "spotPrice", "predictedPrice",
                "deltaPercent", "confidence", "signalId", "tradeQualified",
                "tradeBlockers",
            )
        }
        for row in rows
    ]
    source_batch_hash = _canonical_digest(all_batch_rows)
    contracts = []
    prediction_batches: dict[str, list[dict[str, Any]]] = {}
    for days in CONTRACT_DURATIONS:
        access_until_ms = issued_at_ms + days * 86_400_000
        contract_rows = [
            row for row in rows
            if issued_at_ms <= int(row.get("targetAtMs") or 0) <= access_until_ms
        ]
        contract_batch = [
            {
                key: row.get(key)
                for key in (
                    "rank", "market", "direction", "originAtMs", "targetAtMs",
                    "forecastHorizonSeconds", "spotPrice", "predictedPrice",
                    "deltaPercent", "confidence", "signalId", "tradeQualified",
                    "tradeBlockers",
                )
            }
            for row in contract_rows
        ]
        batch_hash = _canonical_digest(contract_batch)
        contract_ready = source_ready and bool(contract_batch)
        entitlements = len(contract_batch) * days
        peak = _peak_concurrency(contract_rows, days) if contract_rows else 0
        capital = math.ceil(peak * per_trade_inr * (1 + capital_reserve_percent / 100))
        per_trade_usd = per_trade_inr / usd_inr_rate if fx_ready else 0.0
        capital_usd = capital / usd_inr_rate if fx_ready else 0.0
        access_fee_cents = max(100, math.ceil(
            entitlements
            * max(0.01, float(prediction_quantum_reference_usd))
            * DISCOUNT_BY_DAYS[days]
            * 100
        )) if contract_batch else 0
        identity = {
            "issueDate": day_id,
            "accessDays": days,
            "scopeId": source.get("scopeId"),
            "batchSha256": batch_hash,
            "sourceInstance": source_instance,
        }
        contract_id = "O961-" + _canonical_digest(identity)[:20].upper()
        prediction_batches[contract_id] = contract_batch
        contracts.append({
            "contractId": contract_id,
            "name": f"Oracle 961 POC · {days}-day research list" if poc_mode else f"Oracle 961 · {days}-day bot list",
            "accessDays": days,
            "status": "available" if contract_ready else "held",
            "currency": "USD",
            "accessFeeCents": access_fee_cents,
            "dailyBatchSize": len(contract_batch),
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
            "accessUntilMs": access_until_ms,
            "scopeId": source.get("scopeId"),
            "forecastConfigId": source.get("forecastConfigId"),
            "modelSha256": source.get("modelSha256"),
            "sourceInstance": source_instance,
            "sourceInstanceKey": source_instance_key,
            "issuanceMode": issuance_mode,
            "proofOfConcept": poc_mode,
            "tradeQualified": not poc_mode,
            "executionEligible": False,
            "batchSha256": batch_hash,
            "blockedReasons": [] if contract_ready else (
                blocked if not source_ready else
                ["No available predictions have a target inside this contract window"]
            ),
            "sourceWarnings": blocked if poc_mode else [],
            "notice": (
                "Proof-of-concept access to a research-ranked prediction list. Predictions are not trade-qualified; no execution or return is promised. Subscriber exchange capital remains separate."
                if poc_mode else
                "Access to a research prediction list. Subscriber exchange capital remains separate and is never collected by this contract."
            ),
        })
    return {
        "schemaVersion": 2,
        "catalogueVersion": "all-window-targets-v2",
        "issueDate": day_id,
        "generatedAtMs": now_ms,
        "sourceGeneratedAtMs": generated_at_ms or None,
        "sourceAgeSeconds": round(source_age_seconds, 3) if source_age_seconds is not None else None,
        "usdInrRate": usd_inr_rate or None,
        "usdInrObservedAtMs": fx_observed_at_ms or None,
        "sourceScopeId": source.get("scopeId"),
        "sourceInstance": source_instance,
        "sourceInstanceKey": source_instance_key,
        "issuanceMode": issuance_mode,
        "proofOfConcept": poc_mode,
        "sourceBatchSha256": source_batch_hash,
        "available": any(item["status"] == "available" for item in contracts),
        "contracts": contracts,
        # Kept only inside the encrypted status artifact. Public endpoints must
        # remove this field unless the signed-in account owns the contract.
        "predictionBatch": all_batch_rows,
        "predictionBatches": prediction_batches,
        "methodology": {
            "dailyIssueCount": 3,
            "durationsDays": list(CONTRACT_DURATIONS),
            "capitalFormula": "ceil(peak overlapping positions * per-trade INR budget * (1 + reserve percent))",
            "accessFeeFormula": "ceil(signal entitlements * hourly prediction-quantum USD reference * duration discount)",
            "durationDiscounts": {str(key): value for key, value in DISCOUNT_BY_DAYS.items()},
            "pocMeaning": "Available means research-access inventory is published; it does not mean trade qualification or execution eligibility.",
            "batchSelection": "all ranked predictions whose targetAtMs falls between the UTC issue boundary and the contract accessUntilMs; no fixed row cap",
        },
    }
