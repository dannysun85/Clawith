from app.services.agent_tools import _minimax_default_base_url
from app.services.minimax_media_profiles import resolve_minimax_media_profile


def test_minimax_base_url_strips_openai_compatible_v1_suffix():
    assert _minimax_default_base_url("https://api.minimaxi.com/v1") == "https://api.minimaxi.com"
    assert _minimax_default_base_url("https://api.minimaxi.com/v1/") == "https://api.minimaxi.com"


def test_minimax_media_profiles_are_tier_specific_and_provider_valid():
    assert resolve_minimax_media_profile("image", "lite").model == "image-01"
    assert resolve_minimax_media_profile("image", "ultra").model == "image-01"

    lite_audio = resolve_minimax_media_profile("audio", "lite")
    ultra_audio = resolve_minimax_media_profile("audio", "ultra")
    assert lite_audio.model == "speech-2.8-turbo"
    assert lite_audio.bitrate == 64000
    assert ultra_audio.model == "speech-2.8-hd"
    assert ultra_audio.bitrate == 256000

    assert resolve_minimax_media_profile("music", "lite").model == "music-2.6"
    assert resolve_minimax_media_profile("music", "pro").model == "music-2.6"

    lite_video = resolve_minimax_media_profile("video", "lite")
    pro_video = resolve_minimax_media_profile("video", "pro")
    ultra_video = resolve_minimax_media_profile("video", "ultra")
    assert (lite_video.model, lite_video.duration, lite_video.resolution) == (
        "MiniMax-Hailuo-02",
        6,
        "768P",
    )
    assert (pro_video.model, pro_video.duration, pro_video.resolution) == (
        "MiniMax-Hailuo-2.3",
        6,
        "768P",
    )
    assert (ultra_video.model, ultra_video.duration, ultra_video.resolution) == (
        "MiniMax-Hailuo-2.3",
        6,
        "1080P",
    )


def test_legacy_seeded_defaults_do_not_override_tier_profiles():
    assert resolve_minimax_media_profile(
        "music",
        "lite",
        {"model": "music-2.6"},
    ).model == "music-2.6"
    assert resolve_minimax_media_profile(
        "video",
        "lite",
        {"model": "MiniMax-Hailuo-2.3", "duration": 6, "resolution": "1080P"},
    ).resolution == "768P"


def test_tier_specific_admin_override_is_supported_without_model_authorization():
    profile = resolve_minimax_media_profile(
        "audio",
        "ultra",
        {"ultra_model": "speech-2.8-turbo", "ultra_bitrate": 192000},
    )
    assert profile.model == "speech-2.8-turbo"
    assert profile.bitrate == 192000
