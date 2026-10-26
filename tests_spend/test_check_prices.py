"""scripts/check_prices.py: the weekly rate-card review report."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_prices", ROOT / "scripts" / "check_prices.py")
check_prices = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_prices)

NOW = datetime(2026, 11, 2, tzinfo=UTC)
CARD = """provider: openrouter
prices:
  - model_key: openrouter:ending-soon
    input_per_mtok: 1.00
    output_per_mtok: 2.00
    effective_from: 2026-10-01T00:00:00Z
    effective_to: 2026-11-10T00:00:00Z
    source_url: https://openrouter.ai/api/v1/models?q=ending-soon
  - model_key: openrouter:handed-over
    input_per_mtok: 1.00
    effective_from: 2026-09-01T00:00:00Z
    effective_to: 2026-10-01T00:00:00Z
    source_url: https://openrouter.ai/api/v1/models?q=handed-over
  - model_key: openrouter:handed-over
    input_per_mtok: 1.50
    effective_from: 2026-10-01T00:00:00Z
    source_url: https://openrouter.ai/api/v1/models?q=handed-over
  - model_key: openrouter:steady
    input_per_mtok: 0.30
    cached_input_per_mtok: 0.03
    output_per_mtok: 1.20
    effective_from: 2026-09-01T00:00:00Z
    source_url: https://openrouter.ai/api/v1/models?q=steady
"""


def test_the_report_lists_ending_windows_and_moved_prices(tmp_path: Path) -> None:
    (tmp_path / "openrouter.yaml").write_text(CARD, encoding="utf-8")
    models = [
        {"id": "vendor/steady", "pricing": {"prompt": "0.0000003", "completion": "0.0000015", "input_cache_read": "0.00000003"}},
        {"id": "vendor/handed-over", "pricing": {"prompt": "0.0000015"}},
    ]
    text = check_prices.report(tmp_path, models, NOW)
    assert "`openrouter:ending-soon` (openrouter.yaml) ends 2026-11-10" in text
    # The ended row has a successor, so it needs no review.
    assert "handed-over` (openrouter.yaml) ended" not in text
    assert "`openrouter:steady` (openrouter.yaml): output_per_mtok is 1.2 on the card, 1.5 on OpenRouter" in text
    assert "input_per_mtok is 0.3" not in text  # unchanged prices are not reported
    assert "`ending-soon` is no longer in OpenRouter's model list" in text


def test_nothing_to_review_is_an_empty_report(tmp_path: Path) -> None:
    (tmp_path / "anthropic.yaml").write_text("provider: anthropic\nprices:\n  - model_key: claude-x\n    input_per_mtok: 3\n",
                                             encoding="utf-8")
    assert check_prices.report(tmp_path, [], NOW) == ""
