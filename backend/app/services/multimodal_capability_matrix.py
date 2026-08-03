"""Provider-free capability matrix for the product's multimodal contract.

The matrix is deliberately narrower than a provider health check.  It answers
one local governance question: are the user-facing capabilities registered,
typed, and reachable through the intended Agent entrypoint/role grants?  It
does not claim that a provider account can currently generate a media asset.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from app.services.tool_capability_policy import EXPLICIT_GRANT_TOOL_NAMES


@dataclass(frozen=True)
class SpecialistRequirement:
    """The minimum contract for one reviewed execution role."""

    template: str
    skills: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapabilitySpec:
    """A user-facing capability and its local authorization contract."""

    key: str
    label: str
    route_policy: str
    entrypoint_template: str | None = "private-assistant"
    entrypoint_skills: tuple[str, ...] = ()
    entrypoint_tools: tuple[str, ...] = ()
    expected_default_tools: tuple[str, ...] = ()
    expected_explicit_tools: tuple[str, ...] = ()
    specialist_requirements: tuple[SpecialistRequirement, ...] = ()


@dataclass(frozen=True)
class CapabilityMatrixReport:
    """Structured output suitable for CI and human review."""

    rows: tuple[dict[str, object], ...]
    errors: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, object]:
        return {
            "status": "ready" if self.ready else "invalid",
            "verification_scope": "registry_and_agent_authorization_only",
            "provider_health_verified": False,
            "rows": list(self.rows),
            "errors": list(self.errors),
        }


# Product capabilities are provider-neutral.  Provider/model wording is kept
# here only as an internal route-policy label, never as an Agent-facing input.
CAPABILITY_MATRIX: tuple[CapabilitySpec, ...] = (
    CapabilitySpec(
        key="text",
        label="文字理解与生成",
        route_policy="MiniMax-M3 primary -> Volcengine Agent Plan fallback",
        specialist_requirements=(
            SpecialistRequirement("content-creator", skills=("content-writing",)),
        ),
    ),
    CapabilitySpec(
        key="image",
        label="图片/海报",
        route_policy="Volcengine Agent Plan image -> MiniMax fallback",
        entrypoint_skills=("brand-safe-media",),
        entrypoint_tools=("generate_image_minimax",),
        expected_default_tools=("generate_image_minimax",),
        specialist_requirements=(
            SpecialistRequirement(
                "douyin-operator",
                skills=("brand-safe-media", "volcengine-seedream-commercial"),
                tools=("generate_image_minimax",),
            ),
        ),
    ),
    CapabilitySpec(
        key="video",
        label="视频",
        route_policy="Entitled Volcengine Seedance -> MiniMax compatible fallback",
        entrypoint_skills=("brand-safe-media",),
        entrypoint_tools=(
            "generate_video_minimax",
            "check_video_minimax",
            "compose_video_audio",
        ),
        expected_default_tools=(
            "generate_video_minimax",
            "check_video_minimax",
            "compose_video_audio",
        ),
        specialist_requirements=(
            SpecialistRequirement(
                "douyin-operator",
                skills=("brand-safe-media", "volcengine-seedance-commercial"),
                tools=(
                    "generate_video_minimax",
                    "check_video_minimax",
                    "compose_video_audio",
                ),
            ),
        ),
    ),
    CapabilitySpec(
        key="voice",
        label="语音/旁白",
        route_policy="Volcengine Agent Plan TTS -> MiniMax fallback",
        entrypoint_skills=("commercial-voiceover",),
        entrypoint_tools=("generate_speech_minimax",),
        expected_default_tools=("generate_speech_minimax",),
        specialist_requirements=(
            SpecialistRequirement(
                "content-creator",
                skills=("commercial-voiceover",),
                tools=("generate_speech_minimax",),
            ),
        ),
    ),
    CapabilitySpec(
        key="music",
        label="音乐",
        route_policy="MiniMax-only",
        # Music is intentionally not an ambient Private Assistant grant; it
        # remains an explicit role capability with a MiniMax-only route.
        entrypoint_template=None,
        expected_explicit_tools=("generate_music_minimax",),
        specialist_requirements=(
            SpecialistRequirement("content-creator", tools=("generate_music_minimax",)),
        ),
    ),
    CapabilitySpec(
        key="presentation",
        label="PPT/演示文稿",
        route_policy="Provider-independent HTML/PPTX/PDF conversion",
        entrypoint_skills=("commercial-presentation",),
        entrypoint_tools=("convert_html_to_pptx", "convert_html_to_pdf"),
        expected_default_tools=("convert_html_to_pptx", "convert_html_to_pdf"),
        specialist_requirements=(
            SpecialistRequirement(
                "content-creator",
                skills=("commercial-presentation",),
                tools=("convert_html_to_pptx", "convert_html_to_pdf"),
            ),
        ),
    ),
)


def _template_value(template: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = template.get(key, ())
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def validate_capability_matrix(
    *,
    templates: Mapping[str, Mapping[str, object]],
    known_skills: set[str],
    tool_definitions: Mapping[str, Mapping[str, object]],
    runtime_typed_tools: set[str],
) -> CapabilityMatrixReport:
    """Validate registry, runtime adapter and template authorization facts.

    Product-default Tools are allowed to be absent from a template's explicit
    ``default_tools`` list.  Explicit-grant Tools must be present on the
    specialist template, so a role cannot accidentally inherit paid music or
    another role-scoped capability.
    """

    errors: list[str] = []
    rows: list[dict[str, object]] = []

    def check_skill(name: str, context: str) -> None:
        if name not in known_skills:
            errors.append(f"{context}: skill {name!r} is not registered")

    def check_tool(name: str, context: str) -> None:
        definition = tool_definitions.get(name)
        if definition is None:
            errors.append(f"{context}: tool {name!r} is not registered")
            return
        if name not in runtime_typed_tools:
            errors.append(f"{context}: tool {name!r} has no typed runtime adapter")

    def template_has_tool(template: Mapping[str, object], name: str) -> bool:
        # Product-wide defaults are intentionally not copied into every
        # AgentTemplate.  They are still visible unless explicitly disabled.
        definition = tool_definitions.get(name)
        return name in _template_value(template, "default_tools") or bool(
            definition and definition.get("is_default") is True
        )

    def check_authorization_mode(spec: CapabilitySpec) -> None:
        for name in spec.expected_default_tools:
            definition = tool_definitions.get(name)
            check_tool(name, f"{spec.key} default policy")
            if definition is not None and definition.get("is_default") is not True:
                errors.append(
                    f"{spec.key}: tool {name!r} must remain a product default"
                )
            if name in EXPLICIT_GRANT_TOOL_NAMES:
                errors.append(
                    f"{spec.key}: default tool {name!r} is incorrectly explicit-grant"
                )
        for name in spec.expected_explicit_tools:
            definition = tool_definitions.get(name)
            check_tool(name, f"{spec.key} explicit policy")
            if name not in EXPLICIT_GRANT_TOOL_NAMES:
                errors.append(
                    f"{spec.key}: tool {name!r} is missing explicit-grant policy"
                )
            if definition is not None and definition.get("is_default") is True:
                errors.append(
                    f"{spec.key}: explicit tool {name!r} cannot be a product default"
                )

    for spec in CAPABILITY_MATRIX:
        row_errors_before = len(errors)
        check_authorization_mode(spec)
        entrypoint_state: dict[str, object] = {
            "template": spec.entrypoint_template,
            "skills": list(spec.entrypoint_skills),
            "tools": list(spec.entrypoint_tools),
        }

        if spec.entrypoint_template:
            entrypoint = templates.get(spec.entrypoint_template)
            if entrypoint is None:
                errors.append(
                    f"{spec.key}: entrypoint template {spec.entrypoint_template!r} is missing"
                )
            else:
                entrypoint_skills = set(_template_value(entrypoint, "default_skills"))
                for name in spec.entrypoint_skills:
                    check_skill(name, f"{spec.key} entrypoint")
                    if name not in entrypoint_skills:
                        errors.append(
                            f"{spec.key}: entrypoint {spec.entrypoint_template!r} lacks skill {name!r}"
                        )
                for name in spec.entrypoint_tools:
                    check_tool(name, f"{spec.key} entrypoint")
                    if not template_has_tool(entrypoint, name):
                        errors.append(
                            f"{spec.key}: entrypoint {spec.entrypoint_template!r} cannot see tool {name!r}"
                        )

        specialist_rows: list[dict[str, object]] = []
        specialist_ready = not spec.specialist_requirements
        for requirement in spec.specialist_requirements:
            template = templates.get(requirement.template)
            requirement_errors_before = len(errors)
            if template is None:
                errors.append(
                    f"{spec.key}: specialist template {requirement.template!r} is missing"
                )
                specialist_rows.append(
                    {"template": requirement.template, "ready": False}
                )
                continue

            template_skills = set(_template_value(template, "default_skills"))
            for name in requirement.skills:
                check_skill(name, f"{spec.key} specialist {requirement.template}")
                if name not in template_skills:
                    errors.append(
                        f"{spec.key}: specialist {requirement.template!r} lacks skill {name!r}"
                    )
            for name in requirement.tools:
                check_tool(name, f"{spec.key} specialist {requirement.template}")
                if not template_has_tool(template, name):
                    errors.append(
                        f"{spec.key}: specialist {requirement.template!r} lacks authorized tool {name!r}"
                    )
            requirement_ready = len(errors) == requirement_errors_before
            specialist_ready = specialist_ready or requirement_ready
            specialist_rows.append(
                {
                    "template": requirement.template,
                    "skills": list(requirement.skills),
                    "tools": list(requirement.tools),
                    "ready": requirement_ready,
                }
            )

        row_ready = len(errors) == row_errors_before and specialist_ready
        rows.append(
            {
                "key": spec.key,
                "label": spec.label,
                "route_policy": spec.route_policy,
                "entrypoint": entrypoint_state,
                "specialists": specialist_rows,
                "ready": row_ready,
            }
        )

    return CapabilityMatrixReport(rows=tuple(rows), errors=tuple(errors))


__all__ = [
    "CAPABILITY_MATRIX",
    "CapabilityMatrixReport",
    "CapabilitySpec",
    "SpecialistRequirement",
    "validate_capability_matrix",
]
