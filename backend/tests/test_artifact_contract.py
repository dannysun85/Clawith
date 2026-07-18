import uuid
from urllib.parse import quote

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


def test_authoritative_artifact_rejects_hallucinated_link_without_tool_artifacts():
    agent_id = uuid.uuid4()
    response = (
        "视频已生成：workspace/videos/missing.mp4\n"
        f"![missing](/api/agents/{agent_id}/files/download?path=workspace%2Fvideos%2Fmissing.mp4)"
    )

    result = artifact_contract.append_authoritative_artifacts(response, agent_id, [])

    assert "files/download" not in result
    assert "未验证的产物链接已移除" in result
    assert result.startswith("⚠️ 系统未验证到模型声明的产物，不能视为生成成功")


def test_authoritative_artifact_rejects_cross_agent_link():
    agent_id = uuid.uuid4()
    other_agent_id = uuid.uuid4()
    response = (
        f"![foreign](/api/agents/{other_agent_id}/files/download?"
        "path=workspace%2Fvideos%2Fforeign.mp4)"
    )

    result = artifact_contract.append_authoritative_artifacts(
        response,
        agent_id,
        ["workspace/videos/foreign.mp4"],
    )

    assert str(other_agent_id) not in result
    assert "未验证的产物链接已移除" in result


def test_authoritative_artifact_rejects_absolute_stale_same_agent_link():
    agent_id = uuid.uuid4()
    response = (
        "视频已生成："
        f"![old](https://app.example/api/agents/{agent_id}/files/download?"
        "filename=old.mp4&path=workspace%2Fvideos%2Fold.mp4)"
    )

    result = artifact_contract.append_authoritative_artifacts(response, agent_id, [])

    assert "https://app.example" not in result
    assert "files/download" not in result
    assert "未验证的产物链接已移除" in result


def test_authoritative_artifact_canonicalizes_verified_absolute_link():
    agent_id = uuid.uuid4()
    response = (
        f"![actual](https://old-host.example/api/agents/{agent_id}/files/download?"
        "path=workspace%2Fimages%2Factual.png&download=1)"
    )

    result = artifact_contract.append_authoritative_artifacts(
        response,
        agent_id,
        ["workspace/images/actual.png"],
    )

    assert "old-host.example" not in result
    assert result == (
        f"![actual](/api/agents/{agent_id}/files/download?"
        "path=workspace%2Fimages%2Factual.png)"
    )


def test_authoritative_artifact_rejects_encoded_absolute_cross_agent_link():
    agent_id = uuid.uuid4()
    other_agent_id = uuid.uuid4()
    linked_path = quote("workspace/videos/foreign ad.mp4", safe="")
    response = (
        f"![foreign](https://app.example/api/agents/{other_agent_id}/files/download?"
        f"download=1&path={linked_path})"
    )

    result = artifact_contract.append_authoritative_artifacts(
        response,
        agent_id,
        ["workspace/videos/foreign ad.mp4"],
    )

    assert str(other_agent_id) not in result
    assert "old-host.example" not in result
    assert "未验证的产物链接已移除" in result


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


@pytest.mark.asyncio
async def test_verified_response_artifacts_require_authoritative_storage(monkeypatch):
    agent_id = uuid.uuid4()

    class Storage:
        async def is_file(self, key: str) -> bool:
            return key.endswith("workspace/videos/actual.mp4")

    monkeypatch.setattr(artifact_contract, "get_storage_backend", lambda: Storage())

    result = await artifact_contract.verified_response_artifacts(
        agent_id,
        "生成完成：workspace/videos/actual.mp4；另有 workspace/videos/missing.mp4",
    )

    assert result == ["workspace/videos/actual.mp4"]


@pytest.mark.asyncio
async def test_response_artifact_batch_deduplicates_storage_checks(monkeypatch):
    agent_id = uuid.uuid4()

    class Storage:
        def __init__(self):
            self.calls: list[str] = []

        async def is_file(self, key: str) -> bool:
            self.calls.append(key)
            return key.endswith("workspace/videos/reused.mp4")

    storage = Storage()
    monkeypatch.setattr(artifact_contract, "get_storage_backend", lambda: storage)
    responses = [
        (
            f"视频：workspace/videos/reused.mp4\n"
            f"![](/api/agents/{agent_id}/files/download?"
            "path=workspace%2Fvideos%2Freused.mp4)"
        )
        for _ in range(500)
    ]

    results = await artifact_contract.sanitize_response_artifacts_batch(
        agent_id,
        responses,
    )

    assert len(results) == 500
    assert len(storage.calls) == 1
    assert all("系统已验证产物" not in result for result in results)
    assert all("files/download" in result for result in results)


@pytest.mark.asyncio
async def test_response_artifact_batch_caps_and_bounds_concurrency(monkeypatch):
    agent_id = uuid.uuid4()

    class Storage:
        def __init__(self):
            self.calls = 0
            self.active = 0
            self.max_active = 0

        async def is_file(self, _key: str) -> bool:
            import asyncio

            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0)
            self.active -= 1
            return False

    storage = Storage()
    monkeypatch.setattr(artifact_contract, "get_storage_backend", lambda: storage)
    responses = [
        " ".join(
            f"workspace/images/image-{batch_index}-{item_index}.png"
            for item_index in range(32)
        )
        for batch_index in range(20)
    ]

    results = await artifact_contract.sanitize_response_artifacts_batch(
        agent_id,
        responses,
    )

    assert len(results) == len(responses)
    assert storage.calls == artifact_contract.MAX_RESPONSE_ARTIFACTS_PER_BATCH
    assert storage.max_active <= artifact_contract.ARTIFACT_VERIFICATION_CONCURRENCY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name",
    [
        "generate_image_siliconflow",
        "generate_image_openai",
        "generate_image_google",
        "generate_image_custom",
        "generate_image_minimax",
    ],
)
async def test_all_registered_image_providers_count_as_media_evidence(tool_name):
    agent_id = uuid.uuid4()

    result = await artifact_contract.sanitize_response_artifacts(
        agent_id,
        f"图片已生成成功，已调用 {tool_name}。",
        ["workspace/images/current.png"],
        allow_stored_response_artifacts=False,
        completed_tool_names={tool_name},
        generated_media_artifact_paths=["workspace/images/current.png"],
    )

    assert "未检测到本轮" not in result
    assert "没有验证到本轮" not in result
    assert "系统已验证产物" in result


@pytest.mark.asyncio
async def test_sanitize_response_artifacts_fails_closed_when_storage_is_unavailable(monkeypatch):
    agent_id = uuid.uuid4()

    class Storage:
        async def exists(self, key: str) -> bool:
            raise RuntimeError("storage unavailable")

        async def is_file(self, key: str) -> bool:
            raise AssertionError("must not be reached")

    monkeypatch.setattr(artifact_contract, "get_storage_backend", lambda: Storage())
    response = (
        "视频已生成"
        f"![missing](/api/agents/{agent_id}/files/download?path=workspace%2Fvideos%2Fmissing.mp4)"
    )

    result = await artifact_contract.sanitize_response_artifacts(agent_id, response)

    assert "files/download" not in result
    assert result.startswith("⚠️ 系统未验证到模型声明的产物，不能视为生成成功")


@pytest.mark.asyncio
async def test_live_response_rejects_stale_stored_artifact_without_current_tool_evidence(monkeypatch):
    agent_id = uuid.uuid4()

    class Storage:
        async def exists(self, key: str) -> bool:
            return key.endswith("workspace/videos/old.mp4")

        async def is_file(self, key: str) -> bool:
            return True

    monkeypatch.setattr(artifact_contract, "get_storage_backend", lambda: Storage())
    response = (
        "视频已生成"
        f"![old](/api/agents/{agent_id}/files/download?path=workspace%2Fvideos%2Fold.mp4)"
    )

    result = await artifact_contract.sanitize_response_artifacts(
        agent_id,
        response,
        allow_stored_response_artifacts=False,
        completed_tool_names=set(),
    )

    assert "files/download" not in result
    assert result.startswith("⚠️ 系统未验证到模型声明的产物，不能视为生成成功")


@pytest.mark.asyncio
async def test_live_response_marks_media_tool_claim_without_current_turn_event():
    agent_id = uuid.uuid4()
    response = (
        "调用次数：1（仅一次 generate_video_minimax）\n"
        "工具回包任务 ID：59a89d5d6e9f42269745a8e78622c7b0"
    )

    result = await artifact_contract.sanitize_response_artifacts(
        agent_id,
        response,
        allow_stored_response_artifacts=False,
        completed_tool_names={"find_files"},
    )

    assert result.startswith("⚠️ 系统未检测到本轮 generate_video_minimax 的完成事件")
    assert "本轮不能计入生成调用次数" in result
    assert "59a89d5d6e9f42269745a8e78622c7b0" not in result
    assert "[未验证任务号已移除]" in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "task_id",
    [
        "59a89d5d-6e9f-4226-9745-a8e78622c7b0",
        "MiniMax-video_01-ABCDEF123456",
        "task.provider:ABC12345",
    ],
)
async def test_live_response_removes_unverified_provider_task_id_variants(task_id):
    agent_id = uuid.uuid4()

    result = await artifact_contract.sanitize_response_artifacts(
        agent_id,
        f"已调用 generate_video_minimax，任务 ID：{task_id}",
        allow_stored_response_artifacts=False,
        completed_tool_names=set(),
    )

    assert task_id not in result
    assert "[未验证任务号已移除]" in result


@pytest.mark.asyncio
async def test_live_response_allows_explicit_media_tool_negation_without_event():
    agent_id = uuid.uuid4()

    result = await artifact_contract.sanitize_response_artifacts(
        agent_id,
        "本轮没有实际调用 generate_video_minimax，因此没有任务号。",
        allow_stored_response_artifacts=False,
        completed_tool_names=set(),
    )

    assert "系统未检测到本轮" not in result


@pytest.mark.asyncio
async def test_live_response_rejects_success_claim_after_media_tool_without_artifact():
    agent_id = uuid.uuid4()

    result = await artifact_contract.sanitize_response_artifacts(
        agent_id,
        "视频已生成成功，工具真实返回成功。",
        allow_stored_response_artifacts=False,
        completed_tool_names={"generate_video_minimax"},
    )

    assert result.startswith("⚠️ 系统没有验证到本轮媒体生成工具产物")


@pytest.mark.asyncio
async def test_live_response_checks_success_claim_per_line_after_older_failure():
    agent_id = uuid.uuid4()

    result = await artifact_contract.sanitize_response_artifacts(
        agent_id,
        "上一轮视频未生成成功。\n本轮视频已生成成功。",
        allow_stored_response_artifacts=False,
        completed_tool_names={"generate_video_minimax"},
    )

    assert result.startswith("⚠️ 系统没有验证到本轮媒体生成工具产物")


@pytest.mark.asyncio
async def test_live_response_rejects_media_success_without_any_tool_event():
    agent_id = uuid.uuid4()

    result = await artifact_contract.sanitize_response_artifacts(
        agent_id,
        (
            "视频已生成成功，已保存到 workspace/videos/fake.mp4。\n"
            "任务 ID：59a89d5d6e9f42269745a8e78622c7b0"
        ),
        allow_stored_response_artifacts=False,
        completed_tool_names=set(),
    )

    assert result.startswith("⚠️ 系统未检测到本轮媒体工具完成事件")
    assert "59a89d5d6e9f42269745a8e78622c7b0" not in result
    assert "[未验证任务号已移除]" in result


@pytest.mark.asyncio
async def test_live_response_does_not_treat_unrelated_success_as_media_claim():
    agent_id = uuid.uuid4()

    result = await artifact_contract.sanitize_response_artifacts(
        agent_id,
        "The database migration was a success.",
        allow_stored_response_artifacts=False,
        completed_tool_names=set(),
    )

    assert result == "The database migration was a success."


@pytest.mark.asyncio
async def test_live_response_cannot_present_find_files_artifact_as_new_generation():
    agent_id = uuid.uuid4()

    result = await artifact_contract.sanitize_response_artifacts(
        agent_id,
        "视频已生成成功，已保存到 workspace/videos/old.mp4。",
        ["workspace/videos/old.mp4"],
        allow_stored_response_artifacts=False,
        completed_tool_names={"find_files"},
        generated_media_artifact_paths=[],
    )

    assert result.startswith("⚠️ 系统未检测到本轮媒体工具完成事件")
    assert "系统已验证产物" in result


@pytest.mark.asyncio
async def test_live_response_accepts_success_claim_with_current_verified_artifact():
    agent_id = uuid.uuid4()

    result = await artifact_contract.sanitize_response_artifacts(
        agent_id,
        "视频已生成成功。",
        ["workspace/videos/current.mp4"],
        allow_stored_response_artifacts=False,
        completed_tool_names={"generate_video_minimax"},
        generated_media_artifact_paths=["workspace/videos/current.mp4"],
    )

    assert "没有验证到本轮媒体生成工具产物" not in result
    assert "系统已验证产物" in result
