from app.services.modalities import modality_match_values


def test_multimodal_pool_capability_matches_each_concrete_modality():
    assert modality_match_values("image") == ["image", "vision", "multimodal"]
    assert modality_match_values("audio") == ["audio", "voice", "tts", "multimodal"]
    assert modality_match_values("text") == ["text", "multimodal"]


def test_multimodal_capability_does_not_duplicate_itself():
    assert modality_match_values("multimodal") == ["multimodal"]
