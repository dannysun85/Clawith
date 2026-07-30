# Design

## Source of truth

- Status: Active
- Last refreshed: 2026-07-28
- Primary product surfaces: Workbench, personal assistant, Agent direct chat, Group collaboration, deliverable brief drawer, deliverable run timeline, Workspace artifact preview, Enterprise Settings, SaaS Admin.
- Evidence reviewed: `frontend/src/index.css`, `frontend/src/styles/atlas.css`, `frontend/src/pages/Layout.tsx`, `frontend/src/pages/Onboarding.tsx`, `frontend/src/pages/agent-detail/AgentDetailPage.tsx`, `frontend/src/components/AgentSidePanel.tsx`, `frontend/src/components/WorkspaceOperationPanel.tsx`, `frontend/src/components/deliverables/DeliverableWorkbench.tsx`, `frontend/src/pages/enterprise-settings/tabs/SkillsTab.tsx`, `backend/app/api/onboarding.py`, `backend/app/api/tasks.py`, `backend/app/models/agent.py`, `backend/app/models/onboarding.py`, `backend/app/services/deliverable_workflows.py`, `backend/app/services/tool_capability_policy.py`, `backend/app/services/tool_visibility.py`, `backend/app/services/skill_scope.py`, `backend/agent_templates/private-assistant/`, `backend/agent_template/skills/brand-safe-media/SKILL.md`, `.omx/plans/2026-07-24-image-video-ppt-provider-evaluation-plan.md`, the supplied WorkBuddy entry screenshot, and the previously reviewed feature-entry references.
- Authority: this file governs product/UI decisions. Runtime, security, billing, and data-model facts remain governed by `SKILL.md`, code, migrations, and tests.

## Brand

- Personality: calm, capable, enterprise-grade, and collaborative. Astra should feel like a dependable digital-employee workspace rather than a model playground.
- Trust signals: explicit task stages, visible capability checks, expected-versus-actual Credits, approval gates, artifact versions, understandable failure states, and clear separation between user choices and platform routing.
- Avoid: provider/model names in ordinary-user flows; flat feature sprawl; fake success states; unexplained automation; decorative gradients that reduce readability; copying a consumer chatbot's information architecture; exposing raw API-key or infrastructure errors.

## Product goals

- Goals: let a user start from natural language or a structured brief; turn PPT, poster, video, report, and spreadsheet requests into durable, inspectable work; reuse Agent/Group Runtime, Skills, Workspace, Credits, and monitoring; make advanced configuration progressive rather than mandatory.
- Non-goals: a full PowerPoint editor in the first release; a node-based workflow builder for ordinary users; direct provider/model selection; a second task scheduler; automatic provider submission before a user confirms the brief.
- Success signals: users can discover the right work type without knowing tool names; a request survives refresh/reconnect; tenant boundaries remain intact; repeated actions are idempotent; every launched request links to one durable run; artifacts and approval decisions are traceable.

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
