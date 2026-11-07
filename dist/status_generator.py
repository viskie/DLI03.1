"""Build, encrypt, archive, and publish the hourly Oracle 961 game status."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from typing import Any
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet

from status_artifact import decrypt_status, encrypt_status, payload_sha256, validate_key
from contracts import build_daily_contracts


ROOT = Path(__file__).resolve().parent
DEFAULT_INSTANCE = Path("/src/DLi-portal/.local-runtime/instances/instance-2")
DEFAULT_OUTPUT = ROOT / "instance/oracle961-status/stat.i"
DEFAULT_ARCHIVE = ROOT / "instance/oracle961-status/archive"
DEFAULT_KEY_FILE = ROOT / "instance/oracle961-status/status.key"
DEFAULT_BLOB_PATH = "oracle691/status/stat.i"
OUTCOME_FIELDS = (
    "outcome_id", "scope_id", "cycle_at_ms", "rank", "target_at_ms",
    "verified_at_ms", "origin_at_ms", "market", "horizon_seconds",
    "base_price", "predicted_price", "actual_price", "deviation_percent",
    "actual_move_percent", "direction_correct", "absolute_error",
    "forecast_config_id", "model_sha256", "status", "was_validated",
    "validation_failures", "actual_observed_at_ms", "resolution_source",
)


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _hour_floor(now_ms: int) -> int:
    return now_ms - now_ms % 3_600_000


def _atomic_write(path: Path, content: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _camel(name: str) -> str:
    first, *rest = name.split("_")
    return first + "".join(part.title() for part in rest)


PUBLISHED_OUTCOME_LIMIT = 1_000


def _outcomes(database: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
    if not database.is_file():
        raise FileNotFoundError(f"prediction outcome database is unavailable: {database}")
    uri = f"file:{database}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(outcomes)")}
        missing = set(OUTCOME_FIELDS) - columns
        if missing:
            raise RuntimeError("outcome schema is missing: " + ", ".join(sorted(missing)))
        selected = ",".join(OUTCOME_FIELDS)
        actualized_count = int(connection.execute(
            "SELECT COUNT(*) FROM outcomes WHERE status <> 'pending'"
        ).fetchone()[0])
        pending_count = int(connection.execute(
            "SELECT COUNT(*) FROM outcomes WHERE status = 'pending'"
        ).fetchone()[0])
        actualized_rows = connection.execute(
            f"SELECT {selected} FROM outcomes WHERE status <> 'pending' "
            "ORDER BY COALESCE(verified_at_ms, actual_observed_at_ms, target_at_ms) DESC, "
            "cycle_at_ms DESC, rank ASC, outcome_id LIMIT ?", (PUBLISHED_OUTCOME_LIMIT,)
        ).fetchall()
        pending_rows = connection.execute(
            f"SELECT {selected} FROM outcomes WHERE status = 'pending' "
            "ORDER BY cycle_at_ms DESC, rank ASC, outcome_id LIMIT ?", (PUBLISHED_OUTCOME_LIMIT,)
        ).fetchall()
    finally:
        connection.close()
    actualized = [{_camel(field): row[field] for field in OUTCOME_FIELDS} for row in actualized_rows]
    pending = [{_camel(field): row[field] for field in OUTCOME_FIELDS} for row in pending_rows]
    return actualized, pending, actualized_count, pending_count


def _latest_scope(scope_dir: Path) -> dict[str, Any]:
    latest: tuple[int, dict[str, Any]] | None = None
    for path in scope_dir.glob("*.json"):
        value = _read_json(path)
        if not isinstance(value, dict) or not isinstance(value.get("createdAtMs"), int):
            continue
        candidate = (value["createdAtMs"], value)
        if latest is None or candidate[0] > latest[0]:
            latest = candidate
    return latest[1] if latest else {}


def _wilson_lower(successes: int, total: int, z: float = 1.96) -> float:
    if total <= 0:
        return 0.0
    probability = successes / total
    denominator = 1 + z * z / total
    centre = probability + z * z / (2 * total)
    adjustment = z * math.sqrt(probability * (1 - probability) / total + z * z / (4 * total * total))
    return max(0.0, (centre - adjustment) / denominator)


def _allocation(usd: float, reference_usd: float) -> float:
    if usd < 0 or reference_usd <= 0:
        raise ValueError("allocation inputs must be positive")
    return math.floor((usd / reference_usd) * 1_000_000) / 1_000_000


def _scoring(actualized: list[dict[str, Any]], pending: list[dict[str, Any]],
             scope: dict[str, Any], shadow: dict[str, Any], now_ms: int,
             *, actualized_count: int | None = None, pending_count: int | None = None) -> dict[str, Any]:
    judged = [row for row in actualized if row.get("directionCorrect") in (0, 1)]
    correct = sum(int(row["directionCorrect"]) for row in judged)
    errors = [float(row["absoluteError"]) for row in actualized
              if isinstance(row.get("absoluteError"), (int, float)) and math.isfinite(row["absoluteError"])]
    mean_error = sum(errors) / len(errors) if errors else None
    direction_lower = _wilson_lower(correct, len(judged))
    error_quality = math.exp(-20 * mean_error) if mean_error is not None else 0.0
    evidence = min(1.0, math.sqrt(len(actualized) / 500))
    selected = max(0, int(scope.get("selectedCount") or 0))
    coverage = min(1.0, selected / max(1, int(scope.get("requestedCount") or 100)))
    last_processed = int(shadow.get("lastProcessedAtMs") or shadow.get("cursorObservedAtMs") or 0)
    age_seconds = max(0.0, (now_ms - last_processed) / 1000) if last_processed else None
    freshness = math.exp(-age_seconds / 3600) if age_seconds is not None else 0.0
    performance = 0.60 * direction_lower + 0.40 * error_quality
    quality = max(0.0, min(1.0, evidence * (0.85 * performance + 0.10 * coverage + 0.05 * freshness)))
    reference = round(0.50 + quality, 6)
    return {
        "actualizedCount": len(actualized) if actualized_count is None else actualized_count,
        "pendingCount": len(pending) if pending_count is None else pending_count,
        "directionJudgedCount": len(judged),
        "directionCorrectCount": correct,
        "directionAccuracy": round(correct / len(judged), 8) if judged else None,
        "directionAccuracyWilsonLower95": round(direction_lower, 8),
        "meanAbsoluteLogError": round(mean_error, 10) if mean_error is not None else None,
        "errorQuality": round(error_quality, 8),
        "evidenceFactor": round(evidence, 8),
        "currentRankedCoverage": round(coverage, 8),
        "sourceAgeSeconds": round(age_seconds, 3) if age_seconds is not None else None,
        "sourceFreshness": round(freshness, 8),
        "qualityScore": round(quality, 8),
        "quantumIndex": round(quality * 1000, 4),
        "predictionQuantumReferenceUsd": reference,
        "allocationFormula": "floor((purchaseUsd / predictionQuantumReferenceUsd) * 1e6) / 1e6",
        "purchaseExamples": [{"purchaseUsd": amount, "oracle961QUnits": _allocation(amount, reference)}
                             for amount in (300, 700, 5000)],
    }


def build_status(instance: Path = DEFAULT_INSTANCE, now_ms: int | None = None,
                 previous_artifact_sha256: str | None = None,
                 *, remote_outcomes: dict[str, Any] | None = None,
                 contract_source: dict[str, Any] | None = None) -> dict[str, Any]:
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    hour = _hour_floor(now_ms)
    results = instance / "results/prediction_scopes"
    if remote_outcomes is None:
        actualized, pending, actualized_count, pending_count = _outcomes(results / "outcomes.sqlite")
    else:
        source_rows = remote_outcomes.get("rows") if isinstance(remote_outcomes, dict) else []
        actualized = []
        for row in source_rows if isinstance(source_rows, list) else []:
            if not isinstance(row, dict):
                continue
            mapped = dict(row)
            predicted, actual = row.get("predictedPrice"), row.get("actualPrice")
            if isinstance(predicted, (int, float)) and isinstance(actual, (int, float)) and predicted > 0 and actual > 0:
                mapped["absoluteError"] = abs(math.log(predicted / actual))
            actualized.append(mapped)
        pending = []
        actualized_count = int(remote_outcomes.get("totalActualized") or len(actualized))
        pending_count = 0
    state = _read_json(instance / "state.json", {}) or {}
    shadow = _read_json(instance / "shadow.json", {}) or {}
    scope = _latest_scope(results)
    valuation = _scoring(actualized, pending, scope, shadow, now_ms,
                         actualized_count=actualized_count, pending_count=pending_count)
    contract_catalogue = build_daily_contracts(
        contract_source,
        now_ms=now_ms,
        prediction_quantum_reference_usd=valuation["predictionQuantumReferenceUsd"],
    )
    scope_summary = {key: scope.get(key) for key in (
        "scopeId", "createdAtMs", "status", "selectedCount", "tradeEligibleCount",
        "minimumDesiredCount", "shortfall", "forecastConfigId", "modelSha256",
        "policyId", "blockedReasons", "rejections", "allGateFailures") if key in scope}
    return {
        "schemaVersion": 1,
        "product": {
            "route": "Oracle691",
            "displayName": "Oracle 961",
            "gameUnitSymbol": "O961-Q",
            "gameUnitName": "Oracle 961 prediction quantum",
            "classification": "non-transferable research game unit",
            "notice": "Reference units are informational, non-redeemable, and do not represent currency, equity, a security, or a guaranteed return.",
        },
        "round": {
            "id": time.strftime("%Y-%m-%dT%H:00:00Z", time.gmtime(hour / 1000)),
            "generatedAtMs": now_ms,
            "effectiveFromMs": hour,
            "effectiveUntilMs": hour + 3_600_000,
            "previousArtifactSha256": previous_artifact_sha256,
        },
        "source": {
            "instance": "instance-2",
            "state": {key: state.get(key) for key in ("phase", "stage", "status", "updatedAtMs")},
            "shadow": {key: shadow.get(key) for key in ("status", "desiredRunning", "executionEnabled", "lastProcessedAtMs", "cursorObservedAtMs", "ticksConsumed")},
            "scope": scope_summary,
        },
        "valuation": valuation,
        "predictionLogs": {
            "actualized": {"count": actualized_count, "publishedCount": len(actualized), "rows": actualized},
            "pending": {"count": pending_count, "publishedCount": len(pending), "rows": pending},
        },
        "dailyContracts": contract_catalogue,
        "methodology": {
            "version": "oracle961-quantum-v1",
            "summary": "Evidence-weighted prediction quality, recomputed once per UTC hour.",
            "qualityFormula": "evidence * (0.85 * (0.60 * WilsonLower95(direction accuracy) + 0.40 * exp(-20 * mean absolute log error)) + 0.10 * current ranked coverage + 0.05 * source freshness)",
            "referenceFormula": "USD 0.50 + qualityScore; allocation is a game simulation only.",
        },
    }


def _get_json(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": "DLi-Oracle691-Publisher/1.0", **(headers or {})})
    with urlopen(request, timeout=30) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid JSON object returned by {url}")
    return value


def _production_sources() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    base = os.getenv("ORACLE_PRODUCTION_BASE_URL", "").rstrip("/")
    if not base:
        return None, None
    outcomes = _get_json(base + "/api/public/prediction-outcomes")
    secret = os.getenv("DLI_ORACLE_EXPORT_SECRET", "")
    if not secret:
        raise RuntimeError("DLI_ORACLE_EXPORT_SECRET is required for production contract sizing")
    path = "/api/integrations/oracle-contract-source"
    timestamp = str(int(time.time()))
    import hmac
    signature = hmac.new(
        secret.encode("utf-8"), f"{timestamp}\n{path}".encode("utf-8"), hashlib.sha256
    ).hexdigest()
    source = _get_json(base + path, {
        "X-DLI-Oracle-Timestamp": timestamp,
        "X-DLI-Oracle-Signature": signature,
    })
    ticker_request = Request(
        "https://api.coindcx.com/exchange/ticker",
        headers={"User-Agent": "Mozilla/5.0 DLi-Oracle691-Publisher/1.0"},
    )
    with urlopen(ticker_request, timeout=30) as response:
        tickers = json.load(response)
    fx_row = next((row for row in tickers if row.get("market") == "USDTINR"), None)
    if not fx_row:
        raise RuntimeError("CoinDCX USDT/INR conversion is unavailable")
    fx_timestamp = int(float(fx_row["timestamp"]))
    if fx_timestamp < 10_000_000_000:
        fx_timestamp *= 1000
    source["usdInr"] = {
        "rate": float(fx_row["last_price"]),
        "observedAtMs": fx_timestamp,
        "basis": "CoinDCX USDT/INR spot proxy",
    }
    return outcomes, source


def _load_key(path: Path, initialize: bool = False) -> bytes:
    environment = os.getenv("ORACLE_STATUS_KEY")
    if environment:
        return validate_key(environment)
    if path.is_file():
        return validate_key(path.read_bytes().strip())
    if not initialize:
        raise RuntimeError(f"status encryption key is unavailable: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(key + b"\n")
    return key


def _publish_blob(artifact: bytes, pathname: str) -> dict[str, Any]:
    from blob_store import BlobHTTPClient
    return BlobHTTPClient().put(pathname, artifact, allow_overwrite=True)


def generate(*, instance: Path, output: Path, archive: Path, key_file: Path,
             initialize_key: bool = False, upload: bool = False,
             blob_path: str = DEFAULT_BLOB_PATH, now_ms: int | None = None,
             retention_hours: int = 24 * 90) -> dict[str, Any]:
    key = _load_key(key_file, initialize_key)
    chain_head = output.parent / ".chain-head"
    previous = chain_head.read_text().strip() if chain_head.is_file() else None
    remote_outcomes, contract_source = _production_sources()
    payload = build_status(
        instance,
        now_ms=now_ms,
        previous_artifact_sha256=previous,
        remote_outcomes=remote_outcomes,
        contract_source=contract_source,
    )
    if output.is_file():
        try:
            previous_payload = decrypt_status(output.read_bytes(), key)
            previous_contracts = previous_payload.get("dailyContracts", {})
            current_contracts = payload.get("dailyContracts", {})
            if (
                previous_contracts.get("issueDate") == current_contracts.get("issueDate")
                and previous_contracts.get("available") is True
            ):
                # Once a day's three contracts are available, their prediction
                # batch, IDs, prices, and capital disclosure stay immutable.
                payload["dailyContracts"] = previous_contracts
        except Exception:
            # The current round still publishes; authenticated artifact parsing
            # below remains the authoritative integrity boundary.
            pass
    artifact = encrypt_status(payload, key)
    digest = hashlib.sha256(artifact).hexdigest()
    archive_name = payload["round"]["id"].replace(":", "-") + ".stat.i"
    # Publication is the commit point.  A failed network write must not advance
    # the local hash chain and make the next successful round reference an
    # artifact that the web side never received.
    upload_result = _publish_blob(artifact, blob_path) if upload else None
    _atomic_write(archive / archive_name, artifact)
    _atomic_write(output, artifact)
    _atomic_write(chain_head, (digest + "\n").encode("ascii"))
    cutoff = time.time() - max(1, retention_hours) * 3600
    for old in archive.glob("*.stat.i"):
        if old.stat().st_mtime < cutoff:
            old.unlink()
    result = {
        "artifactSha256": digest,
        "payloadSha256": payload_sha256(payload),
        "roundId": payload["round"]["id"],
        "actualized": payload["valuation"]["actualizedCount"],
        "pending": payload["valuation"]["pendingCount"],
        "qualityScore": payload["valuation"]["qualityScore"],
        "predictionQuantumReferenceUsd": payload["valuation"]["predictionQuantumReferenceUsd"],
        "purchase700Oracle961QUnits": payload["valuation"]["purchaseExamples"][1]["oracle961QUnits"],
        "dailyContractsAvailable": payload["dailyContracts"]["available"],
        "dailyContractCount": len(payload["dailyContracts"]["contracts"]),
        "output": str(output),
        "uploaded": bool(upload_result),
        "blobPath": blob_path if upload_result else None,
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", type=Path, default=DEFAULT_INSTANCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE)
    parser.add_argument("--init-key", action="store_true")
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--blob-path", default=DEFAULT_BLOB_PATH)
    parser.add_argument("--retention-hours", type=int, default=24 * 90)
    args = parser.parse_args()
    print(json.dumps(generate(instance=args.instance, output=args.output, archive=args.archive,
                              key_file=args.key_file, initialize_key=args.init_key,
                              upload=args.upload, blob_path=args.blob_path,
                              retention_hours=args.retention_hours),
                     sort_keys=True, allow_nan=False), flush=True)


if __name__ == "__main__":
    main()
