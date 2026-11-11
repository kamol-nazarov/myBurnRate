"""Read-only, effective-dated model coverage for CLI and source diagnostics.

Run with ``python -m spend_app.pricing_audit --database PATH``. No provider
requests, inferred rates, schema changes, or database writes are performed.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from bisect import bisect_right
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from spend_app.pricing import PricingEngine, UnpricedModelError
from spend_app.timeutil import epoch_micros, parse_utc

REASON_MESSAGES = {
    "unknown_model_identity": "The source did not identify the model.",
    "missing_current_rate": "No verified rate applies to this model now.",
    "missing_historical_rate": "No verified rate covers some recorded usage dates.",
    "incomplete_telemetry": "Some token components are unavailable; their cost cannot be inferred.",
    "invalid_timestamp": "Some usage timestamps are invalid; their effective rate is unknown.",
}
_COUNT_FIELDS = (
    "records",
    "pricedRecords",
    "missingRateRecords",
    "missingRateTokens",
    "incompleteRecords",
    "invalidTimestampRecords",
)


def _epoch(value):
    try:
        when = parse_utc(value)
        return epoch_micros(when) if when is not None else None
    except (ValueError, OverflowError):
        return None


def _reason(code, records):
    return {"code": code, "message": REASON_MESSAGES[code], "records": records}


def _current(model: str, pricing: PricingEngine, now: datetime) -> dict:
    try:
        price = pricing.resolve(model, now)
    except UnpricedModelError:
        return {"currentRate": False, "canonicalModel": None, "sourceUrl": None}
    return {
        "currentRate": True,
        "canonicalModel": price.model_key,
        "sourceUrl": price.source_url,
    }


def _model_reasons(model, counts, current, known_models):
    reasons = []
    missing = counts["missingRateRecords"] - counts["invalidTimestampRecords"]
    unknown = not model or model.lower().rsplit(":", 1)[-1] in {
        "unknown",
        "unidentified",
    }
    if unknown:
        reasons.append(_reason("unknown_model_identity", counts["records"]))
    else:
        if not current["currentRate"]:
            reasons.append(_reason("missing_current_rate", max(0, missing)))
        if missing and model in known_models:
            reasons.append(_reason("missing_historical_rate", missing))
    for field, code in (
        ("incompleteRecords", "incomplete_telemetry"),
        ("invalidTimestampRecords", "invalid_timestamp"),
    ):
        if counts[field]:
            reasons.append(_reason(code, counts[field]))
    return reasons


def coverage_by_source(
    connection: sqlite3.Connection, pricing: PricingEngine, *, now: datetime
) -> dict:
    """Group all observed model coverage in two scans, not one scan per source.

    SQL groups exact half-open interval counts, including aliases, instead
    of loading or repricing every event in Python. The interval predicate has
    a model lookup and binary search, not a scan over all rate cards for each
    event. There is no result cache:
    in-place corrections, deletions, newly verified cards and date boundaries
    are reflected on the next read. Existing epoch columns avoid date parsing;
    older/read-only databases use the same canonical parser without migration.
    """
    intervals = sorted(
        {
            (
                model,
                epoch_micros(price.effective_from),
                epoch_micros(price.effective_to)
                if price.effective_to
                else 253402300800000000,
            )
            for price in pricing.prices
            for model in (price.model_key, *price.aliases)
        }
    )
    known_models = {model for model, *_ in intervals}
    by_model = defaultdict(list)
    for model, start, stop in intervals:
        by_model[model].append((start, stop))
    starts = {
        model: tuple(start for start, _ in ranges) for model, ranges in by_model.items()
    }

    def has_rate(model, epoch):
        if epoch is None or model not in by_model:
            return 0
        index = bisect_right(starts[model], epoch) - 1
        return int(index >= 0 and epoch < by_model[model][index][1])

    connection.create_function("burnrate_pricing_epoch", 1, _epoch, deterministic=True)
    connection.create_function("burnrate_has_rate", 2, has_rate, deterministic=True)
    selects = []
    for table in ("usage_events", "unpriced_usage_events"):
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if not columns:
            continue
        source = "source" if "source" in columns else "'unknown'"
        complete = "telemetry_complete" if "telemetry_complete" in columns else "1"
        parsed = "burnrate_pricing_epoch(occurred_at)"
        epoch = (
            f"COALESCE(occurred_epoch_us, {parsed})"
            if "occurred_epoch_us" in columns
            else parsed
        )
        selects.append(
            f"SELECT {source} AS source, model_key, {epoch} AS epoch, "
            f"{complete} AS complete, COALESCE(input_tokens,0) + "
            "COALESCE(cache_write_tokens,0) + COALESCE(output_tokens,0) AS tokens "
            f"FROM {table}"
        )
    if not selects:
        return {}
    rows = connection.execute(
        "WITH observations AS ("
        + " UNION ALL ".join(selects)
        + ") SELECT source, model_key, complete, "
        "burnrate_has_rate(model_key,epoch) AS covered, epoch IS NULL AS invalid, "
        "COUNT(*), SUM(tokens) FROM observations "
        "GROUP BY source, model_key, complete, covered, invalid"
    ).fetchall()
    counts_by_model = defaultdict(lambda: dict.fromkeys(_COUNT_FIELDS, 0))
    for source, model, complete, covered, invalid, records, tokens in rows:
        counts = counts_by_model[(source, model)]
        counts["records"] += records
        counts["pricedRecords"] += records if covered else 0
        counts["missingRateRecords"] += 0 if covered else records
        counts["missingRateTokens"] += 0 if covered else tokens
        counts["incompleteRecords"] += 0 if complete else records
        counts["invalidTimestampRecords"] += records if invalid else 0
    grouped = defaultdict(list)
    for (source, model), counts in counts_by_model.items():
        current = _current(model, pricing, now)
        grouped[source].append(
            {
                "model": model,
                **counts,
                **current,
                "reasons": _model_reasons(model, counts, current, known_models),
            }
        )
    result = {}
    for source, models in grouped.items():
        reasons = {}
        for model in models:
            for reason in model["reasons"]:
                reasons[reason["code"]] = (
                    reasons.get(reason["code"], 0) + reason["records"]
                )
        result[source] = {
            **{field: sum(model[field] for model in models) for field in _COUNT_FIELDS},
            "models": sorted(models, key=lambda item: item["model"]),
            "reasons": [_reason(code, count) for code, count in reasons.items()],
        }
    return result


def audit(database: Path, pricing: PricingEngine, *, now: datetime) -> dict:
    # mode=ro refuses absent files, rather than silently creating a new DB.
    with sqlite3.connect(
        database.resolve().as_uri() + "?mode=ro", uri=True
    ) as connection:
        by_source = coverage_by_source(connection, pricing, now=now)
    models = {}
    for source in by_source.values():
        for row in source["models"]:
            item = models.setdefault(
                row["model"],
                {
                    "model": row["model"],
                    **_current(row["model"], pricing, now),
                    **dict.fromkeys(_COUNT_FIELDS, 0),
                },
            )
            for field in _COUNT_FIELDS:
                item[field] += row[field]
    result = sorted(models.values(), key=lambda item: item["model"])
    return {
        "models": result,
        "modelCount": len(result),
        "modelsWithoutCurrentRate": sum(not row["currentRate"] for row in result),
        "missingRateRecords": sum(row["missingRateRecords"] for row in result),
        "sources": by_source,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument(
        "--pricing", type=Path, default=Path(__file__).resolve().parents[1] / "pricing"
    )
    args = parser.parse_args()
    report = audit(
        args.database, PricingEngine.load(args.pricing), now=datetime.now(UTC)
    )
    print(json.dumps(report, indent=2))
    return 1 if report["missingRateRecords"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
