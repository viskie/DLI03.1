"""Deterministic daily Oracle Plan 3 contract catalogue.

The purchase price, subscriber-controlled exchange capital and bot readiness
are deliberately explicit. DLI never receives or controls exchange capital
through this catalogue, and an available research contract is not a live bot.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
import math
from typing import Any


CONTRACT_DURATIONS = (1, 3, 9)
CONTRACT_CAP_BY_DAYS = {1: 30, 3: 100, 9: 300}
DEFAULT_CAPITAL_RESERVE_PERCENT = 10.0
DISCOUNT_BY_DAYS = {1: 1.0, 3: 0.90, 9: 0.75}
MAX_CONTRACT_PRICE_CENTS = 1_000_000


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _utc_day(now_ms: int) -> tuple[str, int, int]:
    value = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    start = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    start_ms = int(start.timestamp() * 1000)
    return start.date().isoformat(), start_ms, start_ms + 86_400_000


def _peak_concurrency(rows: list[dict[str, Any]]) -> int:
    """Measure overlapping positions once for the immutable contract batch."""
    events: list[tuple[int, int]] = []
    for row in rows:
        opened_at_ms = int(row.get("originAtMs") or 0)
        closed_at_ms = int(row.get("targetAtMs") or 0)
        if closed_at_ms <= opened_at_ms:
            closed_at_ms = opened_at_ms + max(
                1, int(row.get("forecastHorizonSeconds") or 0)
            ) * 1000
        events.append((opened_at_ms, 1))
        # Close events sort before opens at the same instant.
        events.append((closed_at_ms, -1))
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
    rows = rows[:max(CONTRACT_CAP_BY_DAYS.values())]
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
    asset_notional_inr = float(quantum.get("assetNotionalInr") or per_trade_inr)
    entry_fee_bps = float(quantum.get("entryFeeBps") or 0)
    exit_fee_bps = float(quantum.get("exitFeeBps") or 0)
    cost_buffer_inr = float(quantum.get("costBufferInr") or 0)
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
        eligible_rows = [
            row for row in rows
            if issued_at_ms <= int(row.get("targetAtMs") or 0) <= access_until_ms
        ]
        contract_rows = eligible_rows[:CONTRACT_CAP_BY_DAYS[days]]
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
        entitlements = len(contract_batch)
        peak = _peak_concurrency(contract_rows) if contract_rows else 0
        gross_batch_value = Decimal(str(per_trade_inr)) * len(contract_batch)
        gross_batch_capital = int(gross_batch_value.to_integral_value(rounding=ROUND_CEILING))
        capital = int((
            gross_batch_value
            * (Decimal("1") + Decimal(str(capital_reserve_percent)) / Decimal("100"))
        ).to_integral_value(rounding=ROUND_CEILING))
        per_trade_usd = per_trade_inr / usd_inr_rate if fx_ready else 0.0
        asset_notional_usd = asset_notional_inr / usd_inr_rate if fx_ready else 0.0
        cost_buffer_usd = cost_buffer_inr / usd_inr_rate if fx_ready else 0.0
        gross_batch_capital_usd = gross_batch_capital / usd_inr_rate if fx_ready else 0.0
        capital_usd = capital / usd_inr_rate if fx_ready else 0.0
        research_access_fee_cents = max(100, math.ceil(
            entitlements
            * max(0.01, float(prediction_quantum_reference_usd))
            * DISCOUNT_BY_DAYS[days]
            * 100
        )) if contract_batch else 0
        # Plan 3 contract value must cover at least the disclosed full-batch
        # capital requirement. The exchange capital itself remains separate
        # and subscriber-controlled; the admin UI therefore also discloses the
        # combined cash requirement rather than implying that purchase funds a
        # trading account.
        contract_price_cents = math.ceil(capital_usd * 100) if fx_ready and contract_batch else 0
        price_within_limit = 0 < contract_price_cents <= MAX_CONTRACT_PRICE_CENTS
        contract_ready = contract_ready and price_within_limit
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
            "name": f"Oracle 961 Plan 3 POC · {days}-day research list" if poc_mode else f"Oracle 961 Plan 3 · {days}-day bot contract",
            "planNumber": 3,
            "planId": "bot_contracts",
            "accessDays": days,
            "status": "available" if contract_ready else "held",
            "currency": "USD",
            "accessFeeCents": contract_price_cents,
            "contractPriceCents": contract_price_cents,
            "contractPriceLimitCents": MAX_CONTRACT_PRICE_CENTS,
            "researchAccessFeeCents": research_access_fee_cents,
            "predictionCap": CONTRACT_CAP_BY_DAYS[days],
            "predictionCount": len(contract_batch),
            "dailyBatchSize": len(contract_batch),
            "signalEntitlementCount": entitlements,
            "peakConcurrentPositions": peak,
            "perTradeCapitalInr": per_trade_inr,
            "grossBatchCapitalInr": gross_batch_capital,
            "requiredBatchCapitalInr": capital,
            "estimatedBotCapitalInr": capital,
            "perTradeCapitalUsd": round(per_trade_usd, 2),
            "assetNotionalUsd": round(asset_notional_usd, 6),
            "entryFeeBps": entry_fee_bps,
            "exitFeeBps": exit_fee_bps,
            "costBufferUsd": round(cost_buffer_usd, 6),
            "costsConfigured": bool(entry_fee_bps or exit_fee_bps or cost_buffer_inr),
            "grossBatchCapitalUsd": math.ceil(gross_batch_capital_usd * 100) / 100 if fx_ready else 0.0,
            "requiredBatchCapitalUsd": math.ceil(capital_usd * 100) / 100 if fx_ready else 0.0,
            "requiredExchangeCapitalCents": math.ceil(capital_usd * 100) if fx_ready else 0,
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
            "botDeploymentStatus": "not-defined",
            "exchangeCapitalCustody": "subscriber-controlled",
            "knownActualizationCashCents": contract_price_cents * 2,
            "batchSha256": batch_hash,
            "blockedReasons": [] if contract_ready else (
                blocked if not source_ready else
                ["No available predictions have a target inside this contract window"] if not contract_batch else
                [f"Required contract value exceeds the USD {MAX_CONTRACT_PRICE_CENTS / 100:,.0f} purchase limit"]
            ),
            "sourceWarnings": blocked if poc_mode else [],
            "notice": (
                "Proof-of-concept Plan 3 contract for a research-ranked prediction batch. Predictions are not trade-qualified; no bot execution or return is promised. Subscriber exchange capital remains separate."
                if poc_mode else
                "Plan 3 contract for a fixed prediction batch. A trading bot is not yet deployed. Subscriber exchange capital remains separate and is never collected by this contract."
            ),
        })
    return {
        "schemaVersion": 2,
        "catalogueVersion": "plan-3-bot-contracts-v5",
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
            "capitalFormula": "ceil(prediction count * per-trade INR budget * (1 + reserve percent)); no capital reuse or serialized execution is assumed",
            "contractPriceFormula": "ceil(required full-batch exchange capital in USD cents); held when above USD 10,000",
            "researchAccessFeeFormula": "ceil(signal entitlements * hourly prediction-quantum USD reference * duration discount); disclosed as a reference component",
            "actualizationFormula": "contract purchase price + separately subscriber-controlled required exchange capital; bot infrastructure and execution costs remain undefined",
            "maximumContractPriceUsd": MAX_CONTRACT_PRICE_CENTS / 100,
            "durationDiscounts": {str(key): value for key, value in DISCOUNT_BY_DAYS.items()},
            "pocMeaning": "Available means research-access inventory is published; it does not mean trade qualification or execution eligibility.",
            "contractCaps": {str(key): value for key, value in CONTRACT_CAP_BY_DAYS.items()},
            "batchSelection": "ranked predictions eligible for the contract window, capped at 30, 100 and 300 for 1, 3 and 9 days",
        },
    }
