# Design

## Source of truth

- Status: Active
- Last refreshed: 2026-07-20
- Primary product surfaces: Agent direct chat, Group collaboration, deliverable brief drawer, deliverable run timeline, Workspace artifact preview, Enterprise Settings, SaaS Admin.
- Evidence reviewed: `frontend/src/index.css`, `frontend/src/styles/atlas.css`, `frontend/src/pages/agent-detail/AgentDetailPage.tsx`, `frontend/src/components/AgentSidePanel.tsx`, `frontend/src/components/WorkspaceOperationPanel.tsx`, `frontend/src/pages/enterprise-settings/tabs/SkillsTab.tsx`, `frontend/public/logo.svg`, `.omx/plans/2026-07-20-v1.11.1-creative-workbench-optimization-plan.md`, and the Qwen feature-entry screenshot supplied by the product owner.
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

- Primary navigation: keep the existing product navigation. Add work discovery inside the existing Agent/Group composer, not as a competing top-level application.
- Core routes/screens: existing Agent chat remains the start surface; `交付物` opens a right-side Brief Drawer; after confirmation, the chat timeline shows a request card and the existing side panel exposes stages and Workspace artifacts.
- Content hierarchy: current Agent/team and session first; conversation/run state second; composer and work entry third; structured brief and capability/cost preflight fourth; artifact preview and version actions in the side panel.
- Work-entry hierarchy: `交付物`, `调研分析`, `自动化`, `团队协作`. Under `交付物`: `PPT`, `海报/图片`, `短视频`, then reports/spreadsheets as later workflows. Modality is an implementation capability, not the user's primary task taxonomy.

## Design principles

- Intent before implementation: ask what the user wants to deliver, then let the backend choose Skills, tools, and provider routes.
- Progressive structure: free-form chat stays available; a structured brief appears only after choosing a work type and may be prefilled from natural language and attachments.
- Durable truth: request, run, approval, cost, and artifact state must come from persisted backend facts, never from an optimistic-only client timeline.
- Safe evolution: retain current direct media shortcuts until the new workflows prove parity; mark them as quick generation rather than silently changing their behavior.
- Local recovery: show the failed stage and allow targeted correction or retry; never force a whole expensive workflow to restart when only one page or shot failed.
- Tradeoffs: the first workbench favors clarity, traceability, and output validity over maximum visual density or a fully customizable workflow canvas.

## Visual language

- Color: reuse `--bg-*`, `--text-*`, `--border-*`, `--accent-*`, status, and semantic tokens from `frontend/src/index.css`. Workflow status must use semantic tokens and text/icon labels, never color alone.
- Typography: reuse `--font-family` and the existing 11/13/14/16/18/24/32px scale. Brief labels are 13px; primary task titles are 16-18px; status metadata is 11-13px.
- Spacing/layout rhythm: use the existing 4px-based spacing tokens. Cards use 12-16px internal spacing; related fields use 8-12px gaps; drawer sections use 20-24px separation.
- Shape/radius/elevation: reuse `--radius-md/lg/xl` and `--shadow-md/lg`. The composer remains the visual anchor; workflow chips and request cards must not create a second oversized hero surface.
- Motion: 120-200ms existing transitions for hover, drawer, and status changes. No continuous decorative animation. Respect reduced-motion settings.
- Imagery/iconography: use `@tabler/icons-react` with the existing 1.75 stroke. Use document/image/video semantics; do not use provider logos for product work types.

## Components

- Existing components to reuse: `TierSelector`, `AgentSidePanel`, `WorkspaceOperationPanel`, `FileBrowser`, Toast/Dialog providers, chat attachment pills, status badges, buttons, inputs, and design tokens.
- New/changed components: `DeliverableBriefDrawer`, work-type cards, `DeliverableRequestCard`, capability preflight panel, stage timeline, approval controls, artifact revision list.
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
- [ ] Security/legal: approve any external segmentation/PPT engine and its model-weight licenses before Phase 3 or dependency adoption.
