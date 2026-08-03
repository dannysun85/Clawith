from __future__ import annotations

import subprocess

from PIL import Image

from app.services.creative_evidence_collection import (
    _prepare_ocr_variants,
    collect_ocr_evidence,
    parse_tesseract_tsv,
)


def test_parse_tesseract_tsv_filters_low_confidence_tokens() -> None:
    payload = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
        "left\ttop\twidth\theight\tconf\ttext\n"
        "5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t95.2\tApproved\n"
        "5\t1\t1\t1\t1\t2\t0\t0\t10\t10\t12.0\tNoise\n"
    )

    assert parse_tesseract_tsv(
        payload,
        minimum_confidence=50,
    ) == ("Approved",)


def test_parse_tesseract_tsv_treats_quote_as_plain_ocr_text() -> None:
    payload = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
        "left\ttop\twidth\theight\tconf\ttext\n"
        "5\t1\t1\t1\t1\t1\t0\t0\t10\t10\t80.0\t\"\n"
        "5\t1\t1\t1\t1\t2\t0\t0\t10\t10\t90.0\t豆\n"
    )

    assert parse_tesseract_tsv(payload) == ('"', "豆")


def test_unsupported_ocr_language_remains_partial(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "candidate.png"
    Image.new("RGB", (128, 128), color=(255, 255, 255)).save(path)
    monkeypatch.setattr(
        "app.services.creative_evidence_collection.shutil.which",
        lambda binary: f"/fake/{binary}",
    )
    monkeypatch.setattr(
        "app.services.creative_evidence_collection._available_tesseract_languages",
        lambda _path: ("eng",),
    )
    monkeypatch.setattr(
        "app.services.creative_evidence_collection._run_tesseract",
        lambda *_args, **_kwargs: (),
    )

    receipt = collect_ocr_evidence(
        artifact_type="image",
        path=path,
        expected_languages=("zh-CN",),
    )

    assert receipt.status == "partial"
    assert receipt.language_coverage == ()
    assert any("required_languages=chi_sim" in item for item in receipt.findings)
    assert any("absence_is_not_visual_proof" in item for item in receipt.findings)


def test_ocr_evidence_is_bound_to_exact_artifact_hash(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "candidate.png"
    Image.new("RGB", (128, 128), color=(255, 255, 255)).save(path)
    monkeypatch.setattr(
        "app.services.creative_evidence_collection.shutil.which",
        lambda binary: f"/fake/{binary}",
    )
    monkeypatch.setattr(
        "app.services.creative_evidence_collection._available_tesseract_languages",
        lambda _path: ("eng",),
    )
    monkeypatch.setattr(
        "app.services.creative_evidence_collection._run_tesseract",
        lambda *_args, **_kwargs: ("SALE",),
    )

    receipt = collect_ocr_evidence(
        artifact_type="image",
        path=path,
        expected_languages=("en-US",),
        prohibited_terms=("sale",),
    )

    assert receipt.status == "complete"
    assert len(receipt.artifact_hashes["image"]) == 64
    assert "prohibited_term_detected=sale" in receipt.findings
    assert all(str(path) not in finding for finding in receipt.findings)


def test_ocr_matches_prohibited_term_across_split_tokens(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "candidate.png"
    Image.new("RGB", (128, 128), color=(255, 255, 255)).save(path)
    monkeypatch.setattr(
        "app.services.creative_evidence_collection.shutil.which",
        lambda binary: f"/fake/{binary}",
    )
    monkeypatch.setattr(
        "app.services.creative_evidence_collection._available_tesseract_languages",
        lambda _path: ("chi_sim", "eng"),
    )
    monkeypatch.setattr(
        "app.services.creative_evidence_collection._run_tesseract",
        lambda *_args, **_kwargs: ("豆", "包", "AI", "生成"),
    )

    receipt = collect_ocr_evidence(
        artifact_type="image",
        path=path,
        expected_languages=("zh-CN", "en-US"),
        prohibited_terms=("豆包", "AI生成"),
    )

    assert "prohibited_term_detected=豆包" in receipt.findings
    assert "prohibited_term_detected=AI生成" in receipt.findings


def test_ocr_flags_possible_prohibited_term_after_character_misread(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "candidate.png"
    Image.new("RGB", (128, 128), color=(255, 255, 255)).save(path)
    monkeypatch.setattr(
        "app.services.creative_evidence_collection.shutil.which",
        lambda binary: f"/fake/{binary}",
    )
    monkeypatch.setattr(
        "app.services.creative_evidence_collection._available_tesseract_languages",
        lambda _path: ("chi_sim", "eng"),
    )
    monkeypatch.setattr(
        "app.services.creative_evidence_collection._run_tesseract",
        lambda *_args, **_kwargs: ("豆电", "AI", "主成"),
    )

    receipt = collect_ocr_evidence(
        artifact_type="image",
        path=path,
        expected_languages=("zh-CN", "en-US"),
        prohibited_terms=("AI生成",),
    )

    assert "prohibited_term_possible_match=AI生成" in receipt.findings


def test_ocr_variants_cover_all_four_corners(tmp_path) -> None:
    path = tmp_path / "candidate.png"
    Image.new("RGB", (1000, 600), color=(20, 20, 20)).save(path)
    output_dir = tmp_path / "variants"
    output_dir.mkdir()

    variants = _prepare_ocr_variants(path, output_dir=output_dir)

    assert variants[0] == path
    assert {variant.name for variant in variants[1:]} == {
        "full-enhanced.png",
        "top-left-enhanced.png",
        "top-right-enhanced.png",
        "bottom-left-enhanced.png",
        "bottom-right-enhanced.png",
    }
    assert all(variant.is_file() for variant in variants)


def test_tesseract_execution_failure_is_fail_closed(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "candidate.png"
    Image.new("RGB", (128, 128), color=(255, 255, 255)).save(path)
    monkeypatch.setattr(
        "app.services.creative_evidence_collection.shutil.which",
        lambda binary: f"/fake/{binary}",
    )
    monkeypatch.setattr(
        "app.services.creative_evidence_collection._available_tesseract_languages",
        lambda _path: ("eng",),
    )
    monkeypatch.setattr(
        "app.services.creative_evidence_collection.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, ["tesseract"])
        ),
    )

    receipt = collect_ocr_evidence(
        artifact_type="image",
        path=path,
        expected_languages=("en-US",),
    )

    assert receipt.status == "unavailable"
    assert any("ocr_execution_failed=tesseract_exit_1" in item for item in receipt.findings)
    assert any("absence_is_not_visual_proof" in item for item in receipt.findings)
