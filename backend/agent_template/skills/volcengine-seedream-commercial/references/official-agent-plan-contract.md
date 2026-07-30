# Official Agent Plan Seedream contract

- Source: https://skills.volces.com/skills/volcengine/agentplan
- Upstream Skill: `byted-ark-seedream-skill`
- Upstream version: `3.0.0`
- Reviewed lock hash:
  `4a150ace8b7d8ffa28e7fab87ec0398e5dff72221a032ee41a3013a617329798`
- Agent Plan model used by Astra: `doubao-seedream-5.0-lite`
- Agent Plan endpoint used by the managed provider:
  `POST /api/plan/v3/images/generations`

## Adaptation boundary

Astra retains the upstream intent triggers, reference-guided workflow,
style/lighting prompt guidance, exact-group consistency rule, and provider
protocol. Astra intentionally does not execute or copy the upstream credential
discovery, home-directory output, preference persistence, or JavaScript
wrapper. Provider selection, encrypted credentials, tenant storage, durable
delivery, Credits, exact copy, protected brand assets, and failover remain
owned by Astra.

The upstream Skill can describe 1-15 sequential images and up to 14 references.
The current Astra managed Tool deliberately exposes one durable image and one
creative reference per call. An Agent must not advertise the larger upstream
surface until Astra's Tool and artifact contract support it end to end.

## Reviewed API surface versus Astra Tool

| Official Seedream v3.0.0 surface | Astra managed image Tool |
| --- | --- |
| Text-to-image and reference-guided image | exposed |
| 2K / 3K / 4K product-tier output | server-routed |
| Up to 14 references and `reference_strength` | adapter understands it; Agent Tool currently exposes one creative reference |
| 1–15 sequential images | not exposed until multi-artifact Credits, recovery, selection, and delivery are durable |
| Web-search grounding | not exposed as an image Tool argument |
| Prompt optimization / streaming | server-owned; not an Agent toggle |
| Watermark selection | fixed to no watermark for Astra deliverables |
| Exact copy / unchanged brand asset | Astra deterministic overlay and protected layer, not model redraw |

The customization is required for commercial output. The official Skill is a
useful model/API protocol, but it does not supply Astra's product brief,
pixel-faithful brand layer, exact typography, tenant storage, Credits,
provider fallback, artifact QA, or approval record.
