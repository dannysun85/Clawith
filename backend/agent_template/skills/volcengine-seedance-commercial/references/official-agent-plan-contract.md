# Official Agent Plan Seedance contract

- Source: https://skills.volces.com/skills/volcengine/agentplan
- Upstream Skill: `byted-ark-seedance-skill`
- Upstream version: `4.0.0`
- Reviewed lock hash:
  `cc4b905b8fbec7cc7c9fe94f16c94353a986001df03bebfcba38871b7c86b82d`
- Agent Plan endpoint:
  `POST /api/plan/v3/contents/generations/tasks`
- Query endpoint:
  `GET /api/plan/v3/contents/generations/tasks/{task_id}`

## Current official package matrix

- Small / Medium: lightweight packages without new video generation.
- Astra's reviewed new-task policy (2026-08-04): Large / Max route to the
  standard `doubao-seedance-2.0` model. Fast / Mini remain optional models
  inside those eligible packages and require an administrator-owned
  speed/cost policy rather than package-name inference. The exact account
  acceptance still requires a provider receipt.
- `doubao-seedance-1.5-pro` is retained only for reconciling an already accepted
  legacy task and must not be selected for a new task.

## Reviewed model/API capability matrix

| Capability | 1.5 Pro | 2.0 | 2.0 Fast | 2.0 Mini |
| --- | --- | --- | --- | --- |
| Text / first-frame / first+last-frame video | yes | yes | yes | yes |
| Three-or-more image reference | no | yes | yes | yes |
| Video or audio reference | no | yes | yes | yes |
| Web search | no | yes | yes | yes |
| Draft preview / flex service tier | yes | no | no | no |
| Generated audio | yes | yes | yes | yes |
| Max duration | 12s | 15s | 15s | 15s |
| Resolution | 480/720/1080p | 480/720/1080p/4K | 480/720p | 480/720p |
| Edit / extend | no | yes | yes | yes |

All four reviewed models accept the fixed ratios `21:9`, `16:9`, `4:3`,
`1:1`, `3:4`, and `9:16`. The current provider-neutral Astra Tool exposes
text, first-frame, first+last-frame, fixed ratio/resolution/duration, generated
audio intent, async polling, exact copy, and protected product layers. It does
not yet expose multi-modal reference, edit/extend, draft, flex, or web-search
video generation as Agent arguments.

The upstream wrapper maps public names to dated provider model IDs, including
`doubao-seedance-1.5-pro -> doubao-seedance-1-5-pro-251215`. Astra keeps that
mapping and the capability validation inside its provider adapter rather than
exposing either to an Agent. The previously shipped
`doubao-seedance-1-0-pro-250528` ID remains readable only as a legacy alias for
persisted tasks and receipts; new submissions use the official 1.5 Pro ID.
Entitlement is still decided by the provider response for the encrypted account
credential.

## Adaptation boundary

Astra retains semantic parameter extraction, model-capability routing intent,
first/last-frame workflow, audio choice, asynchronous task identity, and the
official video endpoint contract. Astra intentionally excludes upstream
home-directory downloads, environment/API-key discovery, local cron, local
pending queues, and cross-session preference files.

Provider selection, entitlement circuits, encrypted credentials, tenant
storage, durable polling, Credits, exact copy, protected assets, approval, and
fallback are Astra responsibilities.

This is a customized adoption, not a verbatim Skill installation. The
customization is required because an upstream CLI can own a single user's API
key, desktop downloads, preference file, and cron queue; Astra must preserve
tenant isolation, one accepted provider task, Credits, resumability, Tool
authorization, and a verified MP4 Artifact.
