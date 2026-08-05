"""Provider-native Credits pricing helpers.

Credits are stored as integers. MiniMax Token Plan defines 1000 credits = 1 USD,
so PAYG USD list prices are converted with ceil rounding and a minimum 1 credit
for successful non-free calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING

from app.services.token_tracker import TokenUsage

MINIMAX_CREDITS_PER_USD = Decimal("1000")
VIDEO_PRICING_VERSION = "video-provider-v2-2026-08-06"


@dataclass(frozen=True, slots=True)
class VideoGenerationQuote:
    provider: str
    model: str
    resolution: str
    duration_seconds: int
    credits: int
    pricing_version: str
    billing_basis: str


def _credits_from_usd(amount_usd: Decimal | str | float | int) -> int:
    amount = Decimal(str(amount_usd))
    if amount <= 0:
        return 0
    credits = amount * MINIMAX_CREDITS_PER_USD
    return max(1, int(credits.to_integral_value(rounding=ROUND_CEILING)))


def _usd_for_units(units: int, price_per_million_units: Decimal) -> Decimal:
    if units <= 0:
        return Decimal("0")
    return (Decimal(units) / Decimal("1000000")) * price_per_million_units


def minimax_text_credits(
    model: str | None,
    usage: TokenUsage,
    *,
    service_tier: str | None = None,
) -> int:
    """Return MiniMax LLM credits from token usage.

    Standard M2.x and ``-highspeed`` use different provider input/output rates.
    Cache tokens are billed using their own rates and excluded from ordinary
    input to avoid double-charging prompt cache hits/writes.
    """
    model_name = (model or "").lower()
    # MiniMax-M3 switches to the documented long-context band once the whole
    # request input (including cache hits) exceeds 512K tokens.
    is_m3_long_context = "minimax-m3" in model_name and usage.input_tokens > 512_000
    if "highspeed" in model_name or is_m3_long_context:
        input_per_m = Decimal("0.6")
        output_per_m = Decimal("2.4")
    else:
        input_per_m = Decimal("0.3")
        output_per_m = Decimal("1.2")
    cache_read_per_m = Decimal("0.12") if is_m3_long_context else Decimal("0.06")
    cache_write_per_m = Decimal("0.375")

    cache_read = max(usage.cache_read_tokens, 0)
    cache_write = max(usage.cache_creation_tokens, 0)
    billable_input = max(usage.input_tokens - cache_read - cache_write, 0)
    if usage.input_tokens <= 0 and usage.total_tokens > 0:
        billable_input = max(usage.total_tokens - usage.output_tokens, 0)

    amount = (
        _usd_for_units(billable_input, input_per_m)
        + _usd_for_units(max(usage.output_tokens, 0), output_per_m)
        + _usd_for_units(cache_read, cache_read_per_m)
        + _usd_for_units(cache_write, cache_write_per_m)
    )
    if amount <= 0 and usage.total_tokens > 0:
        amount = _usd_for_units(usage.total_tokens, input_per_m)
    # MiniMax documents Priority delivery at 1.5x Standard PAYG pricing.
    # Apply the multiplier before integer Credits rounding so both reservation
    # estimates and exact settlement use the same financial contract.
    if str(service_tier or "standard").strip().lower() == "priority":
        amount *= Decimal("1.5")
    return _credits_from_usd(amount)


def minimax_image_credits(model: str | None, images: int = 1) -> int:
    count = max(int(images or 1), 1)
    # image-01: $0.0035 per image
    return _credits_from_usd(Decimal("0.0035") * count)


def minimax_tts_credits(model: str | None, characters: int) -> int:
    chars = max(int(characters or 0), 0)
    if chars <= 0:
        return 0
    model_name = (model or "").lower()
    per_m = Decimal("100") if "hd" in model_name else Decimal("60")
    return _credits_from_usd(_usd_for_units(chars, per_m))


def minimax_music_credits(model: str | None) -> int:
    # music-2.6: $0.15 per generated song up to 5 minutes.
    return _credits_from_usd(Decimal("0.15"))


def minimax_video_credits(model: str | None, duration: int, resolution: str) -> int:
    model_name = (model or "").lower()
    speed = "fast" if "fast" in model_name else "normal"
    key = (speed, int(duration or 0), str(resolution or "").upper())
    prices = {
        ("fast", 6, "768P"): Decimal("0.19"),
        ("fast", 10, "768P"): Decimal("0.32"),
        ("fast", 6, "1080P"): Decimal("0.33"),
        ("normal", 6, "768P"): Decimal("0.28"),
        ("normal", 10, "768P"): Decimal("0.56"),
        ("normal", 6, "1080P"): Decimal("0.49"),
    }
    if key not in prices:
        raise ValueError(
            f"Unsupported MiniMax video billing combination: model={model or '(default)'}, "
            f"duration={duration}, resolution={resolution}"
        )
    return _credits_from_usd(prices[key])


def video_generation_quote(
    provider: str,
    model: str,
    *,
    duration: int,
    resolution: str,
) -> VideoGenerationQuote:
    """Return the versioned customer quote for a concrete video route.

    MiniMax retains provider-native PAYG-equivalent pricing even when a plan
    allowance lowers the platform's marginal cost. Agent Plan is subscription
    AFP/token funded and does not expose a stable per-task cash receipt, so its
    customer tariff is a server-owned, cost-controlled SKU matrix. This keeps
    a 1080p request from being charged as a 480p MiniMax task while preserving
    historical reservations under their original pricing version.
    """

    normalized_provider = str(provider or "").strip().lower()
    normalized_model = str(model or "").strip()
    normalized_resolution = str(resolution or "").strip().lower()
    normalized_duration = max(int(duration or 0), 1)
    if normalized_provider == "minimax":
        credits = minimax_video_credits(
            normalized_model,
            normalized_duration,
            normalized_resolution.upper(),
        )
        basis = "minimax_provider_native"
    elif normalized_provider == "volcengine_agent_plan":
        model_key = normalized_model.lower()
        family = (
            "mini"
            if model_key.endswith("-mini")
            else "fast"
            if model_key.endswith("-fast")
            else "standard"
        )
        six_second_prices = {
            ("mini", "480p"): 420,
            ("mini", "720p"): 560,
            ("fast", "480p"): 480,
            ("fast", "720p"): 680,
            ("standard", "480p"): 560,
            ("standard", "720p"): 880,
            ("standard", "1080p"): 1480,
            ("standard", "4k"): 2960,
        }
        try:
            base = six_second_prices[(family, normalized_resolution)]
        except KeyError as exc:
            raise ValueError(
                "Unsupported Volcengine Agent Plan video billing combination: "
                f"model={normalized_model}, duration={normalized_duration}, "
                f"resolution={resolution}"
            ) from exc
        credits = max(1, (base * normalized_duration + 5) // 6)
        basis = "volcengine_agent_plan_budget_tariff"
    else:
        raise ValueError(f"Unsupported video pricing provider: {provider}")
    return VideoGenerationQuote(
        provider=normalized_provider,
        model=normalized_model,
        resolution=normalized_resolution,
        duration_seconds=normalized_duration,
        credits=credits,
        pricing_version=VIDEO_PRICING_VERSION,
        billing_basis=basis,
    )


def provider_text_credits(
    provider: str | None,
    model: str | None,
    usage: TokenUsage,
    *,
    service_tier: str | None = None,
) -> int | None:
    """Return provider-native text credits, or None to use configured rules."""
    if (provider or "").lower() == "minimax":
        return minimax_text_credits(model, usage, service_tier=service_tier)
    return None
