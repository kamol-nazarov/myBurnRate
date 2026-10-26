"""Weekly price check: which rate cards need a person's attention.

Reports, without changing any card:

- cards whose effective window has ended or ends within 14 days (those models
  read as unpriced afterwards, by design);
- OpenRouter-sourced cards whose per-token prices no longer match OpenRouter's
  public model list (https://openrouter.ai/api/v1/models, no key needed).

Prints Markdown, and exits 0 with an empty report when nothing needs review.
Run by .github/workflows/prices.yml, which files the report as an issue.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

ROOT = Path(__file__).resolve().parents[1]
SOON = timedelta(days=14)
MILLION = Decimal(1_000_000)
# OpenRouter pricing field -> rate card field
FIELDS = {"prompt": "input_per_mtok", "completion": "output_per_mtok",
          "input_cache_read": "cached_input_per_mtok", "input_cache_write": "cache_write_per_mtok"}


def _when(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def card_rows(pricing: Path):
    for path in sorted(pricing.glob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for row in document.get("prices") or []:
            yield path.name, document.get("provider"), row


def _covers(row: dict, moment: datetime) -> bool:
    start, end = _when(row.get("effective_from")), _when(row.get("effective_to"))
    return (start is None or start <= moment) and (end is None or end > moment)


def ending(pricing: Path, now: datetime) -> list[str]:
    rows = list(card_rows(pricing))
    lines = []
    for name, _provider, row in rows:
        end = _when(row.get("effective_to"))
        if end is None or end - now > SOON:
            continue
        # A later row for the same model takes over; nothing to review then.
        after = end + timedelta(seconds=1)
        if any(other is not row and other.get("model_key") == row.get("model_key") and _covers(other, after)
               for _name, _p, other in rows):
            continue
        state = "ended" if end <= now else "ends"
        lines.append(f"- `{row.get('model_key')}` ({name}) {state} {end:%Y-%m-%d %H:%M} UTC")
    return lines


def _openrouter_id(source_url: str) -> str | None:
    parsed = urlparse(source_url or "")
    if parsed.netloc != "openrouter.ai" or not parsed.path.startswith("/api/v1/models"):
        return None
    query = parse_qs(parsed.query).get("q")
    return query[0] if query else None


def openrouter_drift(pricing: Path, models: list[dict], now: datetime) -> list[str]:
    by_suffix: dict[str, dict] = {}
    for model in models:
        identifier = str(model.get("id") or "")
        by_suffix.setdefault(identifier.rsplit("/", 1)[-1], model)
        by_suffix.setdefault(identifier, model)
    lines = []
    for name, _provider, row in card_rows(pricing):
        wanted = _openrouter_id(str(row.get("source_url") or ""))
        end = _when(row.get("effective_to"))
        if not wanted or (end is not None and end <= now):
            continue
        model = by_suffix.get(wanted)
        if model is None:
            lines.append(f"- `{row.get('model_key')}` ({name}): `{wanted}` is no longer in OpenRouter's model list")
            continue
        prices = model.get("pricing") or {}
        for remote, local in FIELDS.items():
            if remote not in prices or row.get(local) is None:
                continue
            live = (Decimal(str(prices[remote])) * MILLION).normalize()
            card = Decimal(str(row[local])).normalize()
            if live != card:
                lines.append(f"- `{row.get('model_key')}` ({name}): {local} is {card} on the card, {live} on OpenRouter")
    return lines


def report(pricing: Path, models: list[dict] | None, now: datetime) -> str:
    sections = []
    ends = ending(pricing, now)
    if ends:
        sections.append("### Rate windows ending\n\nAfter its window a model reads as unpriced until a new card row is added.\n\n" + "\n".join(ends))
    if models is not None:
        drift = openrouter_drift(pricing, models, now)
        if drift:
            sections.append("### OpenRouter prices changed\n\n" + "\n".join(drift))
    if not sections:
        return ""
    return ("Automated weekly price check. Nothing was changed: update `pricing/*.yaml` from the provider's "
            "official source (see the card comments), then close this issue.\n\n" + "\n\n".join(sections) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pricing", type=Path, default=ROOT / "pricing")
    parser.add_argument("--offline", action="store_true", help="skip the OpenRouter comparison")
    args = parser.parse_args()
    models = None
    if not args.offline:
        import httpx

        response = httpx.get("https://openrouter.ai/api/v1/models", timeout=30, follow_redirects=False)
        response.raise_for_status()
        models = response.json().get("data") or []
    sys.stdout.write(report(args.pricing, models, datetime.now(UTC)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
