import uuid

import pytest

from app.services import artifact_contract


def test_artifact_candidates_use_success_result_and_exact_conversion_target():
    assert artifact_contract.artifact_candidates(
        "convert_html_to_pptx",
        {"target_path": "workspace/presentations/launch deck.pptx"},
        "✅ Successfully converted HTML to PPTX: workspace/presentations/launch deck.pptx",
    ) == ["workspace/presentations/launch deck.pptx"]
    assert artifact_contract.artifact_candidates(
        "generate_image_minimax",
        {"save_path": "workspace/images/ignored.png"},
        "❌ Image generation failed",
    ) == []


def test_authoritative_artifact_replaces_hallucinated_link():
    agent_id = uuid.uuid4()
    response = (
        "图片已生成："
        f"![wrong](/api/agents/{agent_id}/files/download?path=workspace%2Fimages%2Fwrong.png)"
    )

    result = artifact_contract.append_authoritative_artifacts(
        response,
        agent_id,
        ["workspace/images/actual image.png"],
    )

    assert "wrong.png" not in result
    assert "未验证的产物链接已移除" in result
    assert "path=workspace%2Fimages%2Factual+image.png" in result
    assert "系统已验证产物" in result


@pytest.mark.asyncio
async def test_verified_tool_artifacts_require_authoritative_storage(monkeypatch):
    agent_id = uuid.uuid4()

    class Storage:
        async def exists(self, key: str) -> bool:
            return key.endswith("workspace/images/actual.png")

        async def is_file(self, key: str) -> bool:
            return True

    monkeypatch.setattr(artifact_contract, "get_storage_backend", lambda: Storage())

    result = await artifact_contract.verified_tool_artifacts(
        agent_id,
        "generate_image_minimax",
        {},
        "✅ Image generated and saved to: workspace/images/actual.png",
    )

    assert result == ["workspace/images/actual.png"]
