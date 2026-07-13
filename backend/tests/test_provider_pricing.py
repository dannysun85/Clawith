"""Provider pricing regression tests for Credits accounting.

MiniMax Token Plan defines 1000 credits = 1 USD and PAYG list prices are
converted to credits. Keep these tests pure so billing math is reviewable
without touching provider APIs.
"""

from app.services.provider_pricing import (
    minimax_image_credits,
    minimax_music_credits,
    minimax_text_credits,
    minimax_tts_credits,
    minimax_video_credits,
)
from app.services.token_tracker import TokenUsage


def test_minimax_text_credits_uses_input_output_and_cache_prices():
    usage = TokenUsage(
        input_tokens=3_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_creation_tokens=1_000_000,
        total_tokens=4_000_000,
    )

    assert minimax_text_credits("MiniMax-M2.7", usage) == 1935


def test_minimax_small_successful_text_call_has_minimum_one_credit():
    usage = TokenUsage(input_tokens=10, output_tokens=10, total_tokens=20)

    assert minimax_text_credits("MiniMax-M2.7", usage) == 1


def test_minimax_highspeed_text_uses_highspeed_input_output_prices():
    usage = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000, total_tokens=2_000_000)

    assert minimax_text_credits("MiniMax-M2.7-highspeed", usage) == 3000


def test_minimax_m3_uses_long_context_band_only_above_512k_input_tokens():
    boundary = TokenUsage(
        input_tokens=512_000,
        output_tokens=1_000,
        cache_read_tokens=500_000,
        total_tokens=513_000,
    )
    long_context = TokenUsage(
        input_tokens=512_001,
        output_tokens=1_000,
        cache_read_tokens=500_000,
        total_tokens=513_001,
    )

    assert minimax_text_credits("MiniMax-M3", boundary) == 35
    assert minimax_text_credits("MiniMax-M3", long_context) == 70


def test_minimax_m3_priority_delivery_applies_documented_price_multiplier():
    standard_usage = TokenUsage(
        input_tokens=512_000,
        output_tokens=1_000,
        cache_read_tokens=500_000,
        total_tokens=513_000,
    )
    long_context_usage = TokenUsage(
        input_tokens=512_001,
        output_tokens=1_000,
        cache_read_tokens=500_000,
        total_tokens=513_001,
    )

    assert minimax_text_credits(
        "MiniMax-M3", standard_usage, service_tier="priority"
    ) == 53
    assert minimax_text_credits(
        "MiniMax-M3", long_context_usage, service_tier="priority"
    ) == 105


def test_minimax_image_credits_rounds_official_image_01_price():
    assert minimax_image_credits("image-01", images=1) == 4
    assert minimax_image_credits("image-01", images=2) == 7


def test_minimax_tts_credits_are_character_metered():
    assert minimax_tts_credits("speech-2.8-turbo", characters=1000) == 60
    assert minimax_tts_credits("speech-2.8-hd", characters=1000) == 100
    assert minimax_tts_credits("speech-2.8-turbo", characters=1) == 1


def test_minimax_music_credits_are_per_song():
    assert minimax_music_credits("music-2.6") == 150


def test_minimax_video_credits_follow_duration_resolution_and_speed():
    assert minimax_video_credits("MiniMax-Hailuo-2.3", duration=6, resolution="1080P") == 490
    assert minimax_video_credits("MiniMax-Hailuo-2.3", duration=10, resolution="768P") == 560
    assert minimax_video_credits("MiniMax-Hailuo-2.3-fast", duration=6, resolution="768P") == 190
