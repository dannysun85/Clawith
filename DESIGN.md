# Design

## Source of truth

- Status: Active
- Last refreshed: 2026-07-31
- Primary product surfaces: Workbench, personal assistant, Agent direct chat, Group collaboration, deliverable brief drawer, deliverable run timeline, Workspace artifact preview, Enterprise Settings, SaaS Admin.
- Evidence reviewed: `frontend/src/App.tsx`, `frontend/src/index.css`, `frontend/src/styles/atlas.css`, `frontend/src/pages/Layout.tsx`, `frontend/src/pages/Onboarding.tsx`, `frontend/src/pages/agent-detail/AgentDetailPage.tsx`, `frontend/src/components/AgentSidePanel.tsx`, `frontend/src/components/WorkspaceOperationPanel.tsx`, `frontend/src/components/deliverables/DeliverableWorkbench.tsx`, `frontend/src/pages/enterprise-settings/tabs/SkillsTab.tsx`, `backend/app/api/onboarding.py`, `backend/app/api/tasks.py`, `backend/app/models/agent.py`, `backend/app/models/onboarding.py`, `backend/app/models/task.py`, `backend/app/models/group.py`, `backend/app/models/deliverable.py`, `backend/app/models/okr.py`, `backend/app/models/experience.py`, `backend/app/models/subscription.py`, `backend/app/services/deliverable_workflows.py`, `backend/app/services/tool_capability_policy.py`, `backend/app/services/tool_visibility.py`, `backend/app/services/skill_scope.py`, `backend/agent_templates/private-assistant/`, `backend/agent_template/skills/brand-safe-media/SKILL.md`, `docs/multimodal-product-flow-ledger.md`, `docs/agent-roster/organization-roster-business-prd-v2.md`, `.omx/plans/2026-07-24-image-video-ppt-provider-evaluation-plan.md`, the supplied WorkBuddy entry screenshot, and the previously reviewed feature-entry references.
- Authority: this file governs product/UI decisions. Runtime, security, billing, and data-model facts remain governed by `SKILL.md`, code, migrations, and tests.

### Product-line fact documents

The following six documents turn this design direction into auditable current-state and target-state contracts. They must be reviewed before navigation or work-entry implementation changes:

1. [`docs/product-line/01-product-role-system.md`](docs/product-line/01-product-role-system.md)
2. [`docs/product-line/02-product-entry-system.md`](docs/product-line/02-product-entry-system.md)
3. [`docs/product-line/03-core-objects-and-state-machine.md`](docs/product-line/03-core-objects-and-state-machine.md)
4. [`docs/product-line/04-navigation-and-page-ownership.md`](docs/product-line/04-navigation-and-page-ownership.md)
5. [`docs/product-line/05-capability-governance-and-provider-policy.md`](docs/product-line/05-capability-governance-and-provider-policy.md)
6. [`docs/product-line/06-browser-business-acceptance-matrix.md`](docs/product-line/06-browser-business-acceptance-matrix.md)

The four non-negotiable product boundaries are:

- `我的助理` is the private coordination relationship and default personal dispatcher.
- `数字员工` are persistent accountable executors; one-off work uses a task-scoped expert.
- `Deliverable` is the formal result contract with Artifact, review, approval, and delivery evidence.
- `Workspace` is the work site for sources, drafts, intermediate files, and revisions; it is not the delivery authority.

## Brand

- Personality: calm, capable, enterprise-grade, and collaborative. Astra should feel like a dependable digital-employee workspace rather than a model playground.
- Trust signals: explicit task stages, visible capability checks, expected-versus-actual Credits, approval gates, artifact versions, understandable failure states, and clear separation between user choices and platform routing.
- Avoid: provider/model names in ordinary-user flows; flat feature sprawl; fake success states; unexplained automation; decorative gradients that reduce readability; copying a consumer chatbot's information architecture; exposing raw API-key or infrastructure errors.

## Product goals

- Goals: let a user start from natural language or a structured brief; turn PPT, poster, video, report, and spreadsheet requests into durable, inspectable work; reuse Agent/Group Runtime, Skills, Workspace, Credits, and monitoring; make advanced configuration progressive rather than mandatory.
- Non-goals: a full PowerPoint editor in the first release; a node-based workflow builder for ordinary users; direct provider/model selection; a second task scheduler; automatic provider submission before a user confirms the brief.
- Success signals: users can discover the right work type without knowing tool names; a request survives refresh/reconnect; tenant boundaries remain intact; repeated actions are idempotent; every launched request links to one durable run; artifacts and approval decisions are traceable.

## Current-state baseline

This section records current implementation facts. Everything labelled `target` later in this file is a product decision for the next line and must not be reported as already shipped.

- The production release identity is `v1.11.9`. The repository root still redirects to `/dashboard`; `frontend/src/App.tsx` has no `/work` route or `WorkbenchPage`.
- Onboarding already provisions one private Assistant Agent and records it on `UserTenantOnboarding`, but the completed flow redirects directly into that Agent's chat.
- `Layout.tsx` currently renders the private assistant in the same searchable `智能体` list as company digital employees; the company tour explicitly describes that mixed placement.
- Agent chat, Agent-scoped tasks, Groups, OKR, Plaza/Experience, Enterprise Settings, subscription/Credits, Workspace artifacts, deliverable requests, quality reviews, and SaaS Admin already exist as separate runtime or management surfaces.
- Creative delivery now has provider-neutral image/audio/video routing controls, registered presentation/voiceover Skills, durable deliverable state, artifact preview/download, three-reviewer quality state, and creator delivery confirmation in the local `main` candidate. This proves the product state machine and local interaction path, not independent commercial-quality acceptance or a production release.
- The product problem is therefore not a missing collection of menu items. It is the lack of one user-facing work lifecycle connecting intent, responsible worker, execution, review, delivery, reuse, and organization governance.

## Product-line system map

The next product line is organized into seven layers. Each layer has one primary responsibility; a layer may read linked state from another layer but must not duplicate its authority.

| Layer | User-facing responsibility | Owns | Must not become |
|---|---|---|---|
| Workbench | Start, resume, and find work | intent capture, work-template selection, cross-runtime work index | another Agent, scheduler, provider console, or file store |
| My Assistant | Private coordination for one user in one tenant | personal context, follow-ups, drafts, lightweight delegation | the company employee roster or an all-powerful expert |
| Digital Employees | Persistent named business responsibility | role memory, granted Skills/Tools, triggers, channels, KPI, escalation | a flat collection of generic chatbots |
| Collaboration | Multi-party visible work | Group membership, shared sessions, handoffs, approvals | a duplicate company directory or hidden A2A graph |
| Work and Deliverables | Durable execution result | run linkage, request stages, artifacts, versions, review, approval, delivery | content embedded only in chat or composer-local state |
| Organization Governance | Configure who and what the company can use | members, employee visibility/management, templates, entitlements, credentials, policies | an ordinary-member work entry |
| Platform Operations | Operate the SaaS safely | provider accounts, routes, plans, Credits policy, health, audit and release evidence | a tenant-facing model playground |

Product entry names follow these boundaries:

- `工作台` is the default task entry and cross-runtime work index.
- `我的助理` is one private, persistent coordination relationship per `(tenant, user)`.
- `数字员工` is the company/private/custom roster of persistent business roles.
- `Groups` is visible collaboration; it is not a substitute for selecting one accountable executor.
- `工作与交付` is the result lifecycle surfaced from Workbench, Agent chat, Group sessions, and Workspace rather than a disconnected file gallery.
- `企业设置` and `SaaS 管理` remain role-gated control planes.

## Core product objects and authority

These are product objects, not a proposal to rename every existing database model. Existing models remain the implementation source of truth.

| Product object | Authoritative responsibility | Primary owner | Primary entry/consumer |
|---|---|---|---|
| Tenant | company boundary, plan, policy, branding | company administrator | all tenant surfaces |
| User identity and membership | person, tenant membership, role, onboarding state | user plus company administrator | Workbench, My Assistant, Enterprise Settings |
| Personal-assistant slot | idempotent link from one user in one tenant to one private Agent | current user | `我的助理`, onboarding |
| Agent | persistent worker identity, role, memory and granted capabilities | creator/authorized manager | digital-employee roster, direct chat |
| Task and Run | durable execution intent and runtime progress | selected Agent or Group runtime | Workbench index, chat/session timeline |
| Deliverable request | structured output contract and stage machine | producing Agent plus requester | chat result card, detail drawer |
| Artifact and revision | generated file, preview, version and integrity metadata | deliverable request | Workspace and delivery detail |
| Quality review and approval | independent evidence, reviewer decisions and delivery readiness | assigned reviewers and creator/manager | reviewer flow and delivery detail |
| Group and Group Run | shared membership, collaboration context and visible execution | group members/manager | Groups |
| Skill, Tool and credential grant | know-how, executable authority and secret scope | company administrator/platform policy | Agent configuration and runtime preflight |
| Route and provider account | platform-owned modality routing and readiness | SaaS operator | runtime only; summarized to tenant users |
| OKR | objectives, key results and progress evidence | company roles defined by OKR policy | OKR surface; linked work may provide evidence |
| Experience/Plaza entry | discoverable reusable Agent or company experience | publisher plus moderation policy | Plaza |
| Subscription, entitlement and Credits | allowed capability, quota, reservation and settlement | tenant billing owner/platform | preflight, account subscription, SaaS Admin |
| Audit and operational receipt | immutable explanation of sensitive or paid actions | platform and authorized administrators | progressive disclosure and operations |

Authority rules:

- Chat messages describe work; they do not replace `Task`, Run, Deliverable, Artifact, review, Credits, or approval state.
- A Skill supplies procedure, a Tool supplies executable capability, a grant supplies authority, and a provider route supplies infrastructure. None implies the others.
- The Workbench aggregates stable identifiers and links. It never copies mutable runtime state into a second client-owned workflow.
- Files remain owned by Workspace/storage and linked by Artifact records. They are not reconstructed from chat text.
- OKR may consume approved work evidence, but OKR state must not be inferred from an Agent claiming success.

## Primary business lifecycle

The common lifecycle across research, documents, media, automation, and collaboration is:

`提出意图 → 识别工作类型 → 选择责任主体 → 能力/权限/费用预检 → 用户确认 → 持久化任务/请求 → 分阶段执行 → 检查与修订 → 批准/确认交付 → 归档与复用`

Lifecycle rules:

1. `提出意图`: a user may start in Workbench, My Assistant, an Agent chat, or a Group. The origin is preserved.
2. `识别工作类型`: the platform resolves a registered work contract; ordinary users do not choose a Skill, Tool, provider, or model.
3. `选择责任主体`: personal coordination stays with My Assistant; one-off specialist work uses a task-scoped expert; durable responsibility uses a digital employee; multi-party work uses a Group.
4. `预检`: tenant scope, grants, entitlement, provider readiness, Credits, autonomy, approval points, expected output, and degradation are resolved before paid or external execution.
5. `确认`: the user confirms material cost, external writes, publishing, or changed quality/format. Confirmation creates or advances durable backend state.
6. `执行`: the selected runtime owns progress. Long-running provider acceptance enters reconciliation; it must not silently issue a second paid request.
7. `检查与修订`: automated checks and independent human review are different evidence. A failed page, shot, or artifact revision retries at the smallest safe unit.
8. `交付`: the producing Agent or Group reports the result in its timeline; the right-side detail surface owns review and delivery controls; the composer never displays completed work.
9. `归档与复用`: approved artifacts remain in Workspace; a successful contract may become a private shortcut or administrator-published company template without exposing internal Skills or provider configuration.

Every work item must retain deep links to its origin, responsible worker, runtime session, durable result, approval state, and artifact. Workbench filters are views over those facts, not new execution records.

## Entry and ownership matrix

| Entry | Ordinary member | Agent manager | Company administrator | SaaS operator |
|---|---|---|---|---|
| Workbench | create/resume authorized work | same, including managed employees | publish company templates and inspect tenant-wide policy state | no tenant work impersonation |
| My Assistant | use and configure own assistant preferences | no access to another user's assistant | no content access by default; policy/audit only where authorized | infrastructure health only |
| Digital Employees | use visible employees | configure explicitly managed employees | manage tenant roster, grants and lifecycle | platform policy only |
| Groups | join/use authorized groups | manage owned groups | company policy and membership administration | no ordinary participation |
| Work and Deliverables | view own/authorized results, review when assigned | revise/approve managed work | tenant-wide governance where policy permits | operational receipts, not business approval |
| OKR and Plaza | use according to their existing permissions | publish/link according to policy | configure tenant policy and moderation | global moderation/operations where applicable |
| Enterprise Settings | no access except explicitly delegated areas | scoped Agent management | members, templates, credentials, policies, subscription | no business-content ownership |
| SaaS Admin | none | none | tenant-facing subscription/account summary only | providers, routes, plans, health, release and audit |

## Next-version boundary and migration

Release boundaries must remain explicit:

- `v1.11.9` is the current production release.
- The provider-neutral creative-delivery work after `v1.11.9` is a local clean candidate. It needs an immutable release preflight, production configuration parity check, authorized deployment, release-identity verification, and fresh browser business flow before it can be called released.
- The product-line restructuring is the proposed `v1.12.0` scope. It must not be mixed into a patch release merely because both are present on local `main`.

Recommended implementation sequence:

1. **Stabilize the creative candidate**: publish only after release authorization and prove actual production routes, Credits settlement, artifacts, review, and delivery with the deployed release identity.
2. **Separate identities in navigation**: expose `我的助理` independently from `数字员工`; preserve all existing Agent IDs, chats, permissions, and deep links.
3. **Add the Workbench read model and `/work` route**: aggregate existing Tasks, Runs, Deliverables, approvals, and artifacts; keep `/dashboard` as company overview and retain old entry routes.
4. **Register work contracts and templates**: surface task outcomes, required inputs, executor policy, approvals, entitlement and cost; keep provider and raw Skill/Tool selection out of ordinary-user flows.
5. **Unify result discovery**: add `等待我处理`, `进行中`, `最近完成`, and reusable shortcuts without moving authoritative state out of the existing runtimes.
6. **Complete organization governance**: define roster/visibility, template publishing, reviewer eligibility, role permissions, and external organization mapping only after current Agent access semantics are reconciled with the retained roster PRD.

No-flow-break guards:

- Existing `/agents/:id/*`, `/groups/*`, `/quality-reviews/:reviewId`, `/okr`, `/plaza`, `/enterprise`, Workspace artifact URLs, and session identifiers remain valid.
- Do not migrate or merge personal-assistant content into a company employee; only change presentation and routing.
- Root/default-route changes are feature-gated and reversible; onboarding failure to provision an assistant cannot block Workbench access.
- Provider selection, grants, entitlement, approval and Credits settlement stay server-owned.
- Old direct media shortcuts remain as `快速生成` until registered workflows pass feature parity and production evidence.
- Each slice requires tenant-isolation, permission, idempotency, state-transition, TypeScript/build, desktop, narrow-viewport, and fresh browser business-flow evidence.

## Personas and jobs

- Primary personas: tenant member using a published workflow; tenant administrator configuring Skills, templates, and brand assets; SaaS operator governing provider readiness, routes, safety, and cost.
- User jobs: create a presentation or campaign asset from files and intent; inspect and approve a plan before expensive generation; recover from partial failure; revise or export a real file; understand what is unavailable without seeing internal infrastructure details.
- Key contexts of use: desktop-first knowledge work, Chinese/English content, long-running jobs, unstable networks, multi-file inputs, tenant-sensitive business material, and occasional mobile review/approval.

## Information architecture

- Primary navigation: add `工作台` as the first tenant-level task entry. Keep `仪表盘`, `OKR`, `广场`, and `Groups` as company-level destinations. Move the current user's private assistant into a dedicated `我的助理` row between company navigation and the `数字员工` roster; it must not remain mixed into the employee list.
- Core routes/screens: `/work` is the default post-onboarding task entry; `/dashboard` remains the company overview. Existing Agent chat remains the execution/conversation surface for named employees. `交付物` opens a right-side Brief Drawer; after confirmation, the chat timeline shows a request card and the existing side panel exposes stages and Workspace artifacts.
- Content hierarchy: current Agent/team and session first; conversation/run state second; composer and work entry third; structured brief and capability/cost preflight fourth; artifact preview and version actions in the side panel.
- Work-entry hierarchy: `交付物`, `调研分析`, `自动化`, `团队协作`. Under `交付物`: `PPT`, `海报/图片`, `短视频`, then reports/spreadsheets as later workflows. Modality is an implementation capability, not the user's primary task taxonomy.

## Workbench and personal-assistant architecture

- Naming: the top-level surface is `工作台`, not `助手`, `超级助手`, or `AI 助手`. It is a product entry and work index, not another person. The persistent private role is labelled `我的助理`; its user-chosen name appears as secondary identity.
- Workbench responsibility: accept natural-language intent and attachments, expose outcome-oriented shortcuts, recover recent/in-progress work, clarify the brief, perform capability/cost preflight, and route the confirmed work. It owns no personality, private memory, provider choice, or independent execution authority.
- Personal-assistant responsibility: private coordination, personal memory, follow-ups, lightweight planning, drafting, reminders, and helping the user find or dispatch work they are authorized to access. It remains one persistent Agent per `(tenant, user)` so identities and memory never cross companies.
- Digital-employee responsibility: named, persistent business ownership with role-specific memory, Skills, minimal Tool grants, triggers, channels, relationships, autonomy, KPI, and escalation. Employees are company/private/custom resources and remain in the `数字员工` roster.
- Task-scoped experts: one-off specialist work should normally use a task-scoped expert selected by the platform. Do not force users to hire a permanent employee or turn the personal assistant into every profession.
- Routing policy:
  - personal coordination or follow-up → `我的助理`;
  - one-off specialist outcome → task-scoped expert;
  - durable named responsibility → an existing or newly hired digital employee;
  - multi-party planning or visible collaboration → Group;
  - PPT/image/video/report/spreadsheet → the registered Deliverable workflow, with the producing Agent attached to the result.
- The Workbench may aggregate `Task`, `DeliverableRequest`, Agent Run, Group Run, approval, and Artifact facts into one read model, but it must not invent a second scheduler or copy mutable execution state into the client.
- Custom shortcuts are saved task/work templates, not exposed Skills or Tools. A user may save a successful brief as `我的快捷工作`; an administrator may publish `公司工作模板`. Each template declares understandable inputs, output, scope, approval, and capability requirements.

## Workbench interaction model

- Hero prompt: `今天想完成什么？`, with attachments and voice where already supported. Do not expose a model selector, Provider logo, API Key, Skill picker, or raw permission level.
- Primary categories: `我的常用`, `公司模板`, `制作交付物`, `调研分析`, `文档与数据`, `协作与自动化`. Category chips filter work templates; they are not execution modes.
- Suggested shortcuts come from registered workflow/capability manifests and tenant entitlements. Disabled shortcuts explain plan or configuration requirements without showing infrastructure details.
- Before launch, show a compact confirmation: expected output, selected work scope, proposed executor, estimated Credits/range, approval points, and degraded/unavailable differences. `由谁处理` is an understandable confirmation field, not a required first choice.
- Below the composer show `等待我处理`, `进行中`, `最近完成`, and saved shortcuts. Each item links to its real Agent/Group conversation, detail drawer, or Artifact; the Workbench does not duplicate those full surfaces.
- Work scope uses existing product boundaries: `我的私有工作`, a selected `数字员工`, or a selected `协作群组`. Company-wide file access must never be implied where only Agent or Group Workspace access exists.

## Personal-assistant onboarding

- Do not ask a new user to design an Agent role. The product-owned role is fixed as `personal_assistant`; users only customise identity and working preferences.
- Provision one private assistant for every user in every tenant, not one shared assistant for the whole company. A user belonging to two companies receives two isolated assistants because tenant context, files, memory, permissions, and billing differ.
- Recommended onboarding sequence: company created/joined → idempotently provision assistant → optional quick setup → enter Workbench. Quick setup asks optional display name, how to address the user, response style, proactive level, timezone/working hours, and explicit boundaries.
- The visible default is `我的助理`; a custom name is optional and can be changed later. Skipping customisation must create/use the safe default and must not masquerade as a no-op.
- Personal assistants are a product companion slot, not a hired employee seat. They consume normal model/Tool/Credits usage and obey Plan entitlements, but should not reduce the tenant's digital-employee headcount quota.
- Assistant creation must be idempotent under `(tenant_id, user_id)`, private by construction, excluded from Plaza and Groups, and recoverable if provisioning fails. A provisioning failure must not block entry to the Workbench.
- The assistant never inherits all company Tools or all future capabilities. Delegation, external messages, calendar writes, paid generation, publishing, and destructive actions still pass tenant, grant, entitlement, Credits, autonomy, and approval gates.

## Design principles

- Intent before implementation: ask what the user wants to deliver, then let the backend choose Skills, tools, and provider routes.
- Task first, expert second, Skill hidden, Agent persistent only when the work needs memory, triggers, channels, or recurring ownership. A user asks for an outcome; the product selects the capable worker and execution path.
- Progressive structure: free-form chat stays available; a structured brief appears only after choosing a work type and may be prefilled from natural language and attachments.
- Durable truth: request, run, approval, cost, and artifact state must come from persisted backend facts, never from an optimistic-only client timeline.
- Safe evolution: retain current direct media shortcuts until the new workflows prove parity; mark them as quick generation rather than silently changing their behavior.
- Local recovery: show the failed stage and allow targeted correction or retry; never force a whole expensive workflow to restart when only one page or shot failed.
- Tradeoffs: the first workbench favors clarity, traceability, and output validity over maximum visual density or a fully customizable workflow canvas.

## Capability delivery model

- User flow: `提出任务 → 补齐关键约束 → 确认工作说明/费用 → 分阶段执行 → 验收/修改 → 归档产物`. Do not make ordinary users choose a Skill, Tool, provider, or model.
- Worker selection: use a task-scoped expert for one-off work. Use or create a persistent Agent employee only when the responsibility needs durable memory, scheduled/event triggers, communication channels, or named accountability.
- Execution gates: an Agent may execute only when Skill resolution, Tool visibility/grant, tenant scope, plan entitlement, provider readiness, Credits, autonomy/approval, and durable workflow state all pass. Skill possession never grants Tool permission.
- Availability states:
  - `available`: the confirmed contract can be executed.
  - `degraded`: a materially different quality, cost, format, or turnaround is available and must be disclosed before execution.
  - `unavailable`: preserve the brief and create useful planning/source artifacts, but never claim that the requested media was generated.
- Provider routing is platform-owned. Equivalent healthy routes may replace a provider only before dispatch or after an explicit rejection. `acceptance_unknown` enters reconciliation and must not trigger a second paid generation.
- Every capability produces inspectable intermediate state and a final Artifact. Partial failure retries the smallest failed page, shot, candidate, or conversion stage.

### Target provider policy for the next implementation phase

This is a target decision, not a claim about the currently deployed route:

- text reasoning and writing: `MiniMax-M3` primary; a compatible Volcengine text route may be fallback;
- image/poster: Volcengine Seedream primary; MiniMax is degraded/emergency rather than silently equivalent;
- video: Volcengine Seedance primary once the Agent Plan entitlement is Medium or higher and behaviorally verified; the current Small account does not make Volcengine video available;
- speech/TTS: Volcengine primary, MiniMax fallback when voice identity remains compatible;
- music: MiniMax only;
- PPT: provider-neutral workflow using M3 for planning, the image policy for visuals, and deterministic PPTX/PDF generation and QA.

Current code still seeds Agent Plan text routes above the non-Agent-Plan fallback. Changing the text primary therefore requires a real route migration, integrity tests, control-plane verification, and fresh runtime evidence; UI copy alone is insufficient.

## Creative quality contracts

- Image/poster: structured creative brief, reference inventory, provider-specific prompt compilation, candidate policy, deterministic exact-copy/logo/product composition, automated quality checks, selection receipt, approval, and revision. Exact text and logos must not depend on a generative model drawing them correctly.
- Video: approved script/storyboard and shot specifications precede paid generation; references and keyframes anchor identity; generation, validation, editing, captions/audio, and packaging are separate stages; failed shots can be redone without regenerating the whole video.
- PPT: source inventory and fact references precede outline approval; pages are built from `DeckOutline`/`SlideSpec`, theme and layout rules, editable charts/tables/shapes, and selectively generated decorative visuals; PPTX/PDF parity, overflow, alignment, contrast, fonts, citations, and page-level revision are part of the delivery contract.
- A clean text/shape/chart PPT without generated imagery is a valid professional fallback. Broken image placeholders, invented facts, unlabelled raster-only pages, and “file created” without structural validation are not.

## Visual language

- Color: reuse `--bg-*`, `--text-*`, `--border-*`, `--accent-*`, status, and semantic tokens from `frontend/src/index.css`. Workflow status must use semantic tokens and text/icon labels, never color alone.
- Typography: reuse `--font-family` and the existing 11/13/14/16/18/24/32px scale. Brief labels are 13px; primary task titles are 16-18px; status metadata is 11-13px.
- Spacing/layout rhythm: use the existing 4px-based spacing tokens. Cards use 12-16px internal spacing; related fields use 8-12px gaps; drawer sections use 20-24px separation.
- Shape/radius/elevation: reuse `--radius-md/lg/xl` and `--shadow-md/lg`. The composer remains the visual anchor; workflow chips and request cards must not create a second oversized hero surface.
- Motion: 120-200ms existing transitions for hover, drawer, and status changes. No continuous decorative animation. Respect reduced-motion settings.
- Imagery/iconography: use `@tabler/icons-react` with the existing 1.75 stroke. Use document/image/video semantics; do not use provider logos for product work types.

## Components

- Existing components to reuse: `TierSelector`, `AgentSidePanel`, `WorkspaceOperationPanel`, `FileBrowser`, Toast/Dialog providers, chat attachment pills, status badges, buttons, inputs, and design tokens.
- New/changed components: `WorkbenchPage`, `WorkComposer`, `WorkTemplateRail`, `WorkItemList`, `ExecutorConfirmation`, dedicated `PersonalAssistantNavItem`, separated `DigitalEmployeeRoster`, `PersonalAssistantSetup`, `DeliverableBriefDrawer`, work-type cards, `DeliverableRequestCard`, capability preflight panel, stage timeline, approval controls, artifact revision list.
- Variants and states: PPT/poster/video workflow manifests; Lite/Pro/Ultra policy summaries; draft/ready/running/waiting approval/succeeded/failed/cancelled request states; available/degraded/unavailable preflight; no-artifact/previewable/download-only artifacts.
- Token/component ownership: global visual primitives remain in `frontend/src/index.css`; deliverable-specific layout classes use a `deliverable-` prefix; product schemas and transitions are backend-owned and typed in the frontend API layer.

## Accessibility

- Target standard: WCAG 2.2 AA for new surfaces.
- Keyboard/focus behavior: work entry and cards are real buttons; modal focus remains inside the drawer; Escape closes only an unsubmitted drawer; fields follow DOM order; focus returns to the launcher; primary action is reachable without pointer input.
- Contrast/readability: use opaque surfaces and existing semantic tokens; do not place text directly on generated previews without a deterministic scrim; preserve 4.5:1 body-text contrast.
- Screen-reader semantics: drawer uses `role="dialog"`, an accessible title, field labels, `aria-live` for preflight/result changes, and text equivalents for stage/status icons.
- Reduced motion and sensory considerations: disable drawer transforms and progress animation under `prefers-reduced-motion`; status meaning always includes text.

## Responsive behavior

- Supported breakpoints/devices: desktop is primary; preserve current 900px and 720/768px adaptations; support review/approval on tablet and mobile.
- Layout adaptations: desktop uses a right drawer up to 440px; below 900px it becomes an inset full-width sheet; below 720px work-type cards become one column and action buttons stack without covering the composer.
- Touch/hover differences: minimum 40px actionable height for new controls; hover is supplemental; tooltips cannot contain the only explanation.

## Interaction states

- Loading: use a stable skeleton or disabled action with explicit `正在检查能力`/`正在保存工作说明`; do not clear entered fields.
- Empty: explain that the user can describe a job directly or choose a structured deliverable; an empty artifact area names the next expected stage.
- Error: preserve the brief, show a user-safe reason and retry, attach a correlation identifier only when available, and send privacy-safe telemetry.
- Success: show the persisted request ID/state and a concise next action; creating a brief itself must not claim that a PPT/image/video was generated.
- Disabled: explain whether the cause is plan, Agent tool, platform capability, or an in-progress request using user-facing terms rather than API-key details.
- Offline/slow network: block duplicate submission, retain the local brief until persistence succeeds, reload request state on reconnect, and derive the final state from the backend.

## Deliverable review experience

- A completed deliverable belongs to the Agent that produced it. The chat timeline renders its compact result summary as an attachment in that Agent's message row: deliverable type, current outcome/status, direct preview/download shortcuts, and one `查看交付详情` action. The composer may contain only the user's next input and a pre-send `DeliverableRequestCard`; it must never render a completed result.
- The chat timeline must not contain reviewer assignment, quality forms, approval controls, or a full workflow panel that displaces conversation and the composer.
- The right-side deliverable detail drawer is the customer handoff surface. It presents the three understandable stages `预览文件 → 质量检查 → 确认交付`, highlights the current stage, and contains quality progress, reviewer assignment, revision, and approval actions without mixing them into the conversation stream.
- Customers see outcome language: `正在生成`, `等待质量检查`, `检查中`, `需要修改`, `可以交付`, and `已交付`. They do not see raw lifecycle values such as `candidate`, `open`, `blocked`, provider terms, hashes, or evidence implementation details.
- File actions describe the user intent (`在线预览`, `下载 PPTX`, `查看视频`, `下载图片`) rather than exposing artifact internals. Revision and hash details remain available only in a secondary technical disclosure.
- A creator or manager can arrange independent reviewers and see completion progress. Ineligible-reviewer reasons are translated into an actionable explanation; when fewer than three eligible colleagues exist, the UI points to enterprise member management instead of leaving a disabled button unexplained.
- An assigned reviewer enters a separate guided flow: `查看文件 → 逐项检查 → 评分并提交`. Only one step is primary at a time, unfinished fields are counted, and the final irreversible submission clearly states that the result cannot be edited.
- Automated OCR/frame evidence, storage references, artifact hashes, immutable receipt identifiers, and other audit details are administrator-only progressive disclosures. They remain persisted and inspectable without dominating the reviewer or customer workflow.

## Content voice

- Tone: direct, calm, operational, and outcome-oriented. Chinese is the default product language; English strings remain complete and equivalent.
- Terminology: use `交付物`, `工作说明`, `阶段`, `预计 Credits`, `产物`, `版本`, `批准`, `修改`, and `重做`; reserve `模型路由`, `provider`, and credential terminology for SaaS Admin.
- Microcopy rules: distinguish `已保存工作说明`, `已开始执行`, and `已生成产物`; label estimates as estimates; errors state what the user can do next; avoid anthropomorphic promises and unexplained technical codes.

## Implementation constraints

- Framework/styling system: React 19, TypeScript, Vite, TanStack Query, repository CSS, and Tabler icons. Do not add a second component library or styling runtime.
- Design-token constraints: use existing CSS variables; add a token only when it has cross-surface meaning. Deliverable-only values belong to scoped classes.
- Performance constraints: workflow manifests are small/cacheable; full specs are persisted through authenticated REST before WebSocket launch; WebSocket carries only a stable request ID; generated binaries remain in Workspace/storage rather than JSON payloads.
- Compatibility constraints: retain existing chat, attachments, Tier persistence, direct image/audio/music/video shortcuts, Agent/Group Runtime, Credits settlement, and Workspace preview. No direct model/provider field is accepted from the new user-facing contract.
- Test/screenshot expectations: unit-test manifest validation and state transitions; integration-test tenant isolation/idempotency/request-run linkage; test drawer keyboard and persistence behavior; run TypeScript/build; use real browser checks at desktop and narrow viewport before a release claim.

## Open questions

- [ ] Product owner: choose and approve the first three built-in PPT visual themes before Phase 2 quality acceptance; impacts visual benchmark fixtures, not the Phase 1 contract.
- [ ] Product/finance: define how much of an execution estimate may be shown as a range before provider reservation; impacts estimate copy but not exactly-once settlement.
- [ ] Product owner: decide when legacy direct media shortcuts may move under `快速生成`; they remain visible until workflow parity is proven.
- [ ] Product/finance: approve the personal-assistant companion slot as excluded from digital-employee headcount while retaining normal usage/Credits accounting.
- [ ] Product/admin: define who may publish or retire company work templates and whether approval is required for templates containing paid or external-write steps.
- [ ] Security/legal: approve any external segmentation/PPT engine and its model-weight licenses before Phase 3 or dependency adoption.
