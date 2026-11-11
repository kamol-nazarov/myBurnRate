from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from spend_app.pricing import PricingEngine, UnpricedModelError

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("suffix", ["", "-low", "-medium", "-high"])
def test_argon_intro_and_scheduled_standard_rate(suffix):
    engine = PricingEngine.load(ROOT / "pricing")
    kwargs = dict(model_key="antigravity:gemini-4-argon" + suffix,
                  input_tokens=100_000, cached_input_tokens=80_000,
                  cache_write_tokens=0, output_tokens=1_000)
    assert engine.compute(**kwargs, occurred_at=datetime(2026, 11, 30, 23, 59, 59, tzinfo=UTC)) == Decimal("0.058")
    assert engine.compute(**kwargs, occurred_at=datetime(2026, 12, 1, tzinfo=UTC)) == Decimal("0.116")
    assert not engine.resolve(kwargs["model_key"], datetime(2026, 11, 9, tzinfo=UTC)).is_exact
    with pytest.raises(UnpricedModelError):
        engine.resolve(kwargs["model_key"], datetime(2026, 10, 12, tzinfo=UTC))
