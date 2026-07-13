# v1.10.9 — Explicit SaaS Media Entitlements

- The SaaS plan editor now shows chat-model permissions and media-generation permissions as separate controls. Administrators can directly verify and edit image, audio, music, video, and Lite / Pro / Ultra generation access without interpreting raw `features` JSON.
- Saving a plan requires a minimal compare-and-swap update: unrelated feature flags and future extension values are preserved, while missing or stale administrator snapshots are rejected before they can overwrite newer settings. `generation_modalities` and `generation_tiers` continue through the existing entitlement contract; no production balances, subscriptions, model routes, or credential ownership rules are migrated or rewritten.
- The PostgreSQL release gate now verifies the plan-update row lock, successful compare-and-swap, stale-snapshot rejection, and restoration against a disposable migrated database.
- Includes all shared-pool isolation, bounded media recovery, Credits exactly-once, model-tier persistence, and production monitoring changes from v1.10.8.

# v1.10.8 — Shared-Pool Isolation and Production Issue Monitoring

## What's New

- **Production issue monitoring**: Browser, API, WebSocket, LLM, background-worker, and media failures are aggregated into privacy-safe rollups in the SaaS console. The monitor keeps bounded occurrence evidence, sends a first-alert notification to the SaaS owner, writes a structured alert log, and can optionally deliver a webhook.
- **Actionable incident workflow**: SaaS owners can review open/acknowledged/resolved/ignored problems, see affected-company counts and release/Trace metadata, and reopen a resolved issue when it recurs.
- **Structured account-pool diagnostics**: Missing configuration, unhealthy credentials, provider quota exhaustion, rate saturation, and capability mismatch now have distinct internal reason codes and customer-safe messages.

## Bug Fixes

- **Shared credential isolation**: MiniMax transient, rate-limit, validation, content-policy, network, and unknown task failures no longer degrade the global credential pool. Only confirmed authentication failures isolate a credential; confirmed provider billing or plan exhaustion is tracked separately.
- **Per-modality quota isolation**: MiniMax error `2056` now opens only the affected text/image/audio/music/video circuit. Independent provider quotas no longer let an exhausted video allowance disable chat or unrelated media, and the account-pool UI shows the limited modality while daily/provider verification recovery clears it safely.
- **Cross-Agent tier consistency**: the latest explicit Lite / Pro / Ultra choice is now stored on the tenant user and follows that user across Agents, sessions, navigation, and reloads. Sessions retain the last effective route as a reconnect/continuity hint, while Agent defaults continue to govern automations and background work.
- **Safe credential recovery**: A degraded or externally exhausted account returns to routing only after an explicit read-only verification succeeds. Daily usage resets no longer re-admit invalid credentials, API-key replacements require re-verification, and the account-pool health route can no longer be shadowed by the credential UUID route.
- **Bounded media recovery**: Asynchronous media tasks check their absolute deadline before calling the provider, stop after a configurable consecutive-error budget, and release reserved Credits transactionally when they fail. A valid `Processing` response resets the consecutive-error counter.
- **Exactly-once media settlement**: Concurrent success or failure reconciliation cannot double-consume, double-release, or duplicate the user notification. Previously poisoned production tasks remain terminal and cannot be claimed again.
- **Credits clarity**: Historical product-incident refunds keep their original amounts while the subscription ledger now identifies them as platform incident compensation initiated by the system administrator.
- **Monitoring safety**: Secret-shaped route, operation, error-code, and metadata values are redacted before persistence. Client-side duplicate errors are suppressed for 30 seconds so a render or network error storm cannot flood the intake endpoint.
- **Connector visibility**: Caught Feishu, WeCom, and DingTalk connection failures now enter the same privacy-safe issue rollup with Agent and derived tenant attribution; provider response text and credentials are never persisted.
- **Retry correctness**: Incidental words such as `load balancing` or `authoritative` no longer cause timeout failures to be misclassified as non-retryable.

## Validation

- The complete backend test suite and frontend unit/build suites pass.
- Fresh PostgreSQL upgrade, downgrade/re-upgrade, A2A durability, media success/failure concurrency, Credits exactly-once, and production-monitor aggregation/alert smoke tests pass through `add_user_chat_tier_preference`.
- Production release uses the committed blue/green deployment path with database backup, candidate migrations, health checks, public cutover verification, and rollback protection.
- Post-cutover gates include credential verification, ledger/reservation drift checks, two-tenant product flows, browser media/file access, and continuous issue monitoring.

## Upgrade Notes

- Alembic migration `089_bound_media_generation_retries.py` adds the media consecutive-error counter; migration `090_add_production_issue_monitoring.py` adds the monitoring rollup and occurrence tables; migration `091_add_credential_modality_status.py` adds per-modality provider circuit state; migration `092_add_user_chat_tier_preference.py` stores the tenant user's latest explicit chat tier and conflict-detection revision. All are additive and retain evidence during an application rollback.
- Monitoring defaults to enabled with a 30-second scan interval, first-event alerting, and 30-day occurrence retention. Configure `PRODUCTION_ISSUE_ALERT_WEBHOOK_URL` only with a reviewed operations endpoint.
- This release preserves the centrally funded shared account pool and does **not** add tenant-level or model-object-level authorization.
- Cross-tenant failure pollution is fixed, but production currently has one verified MiniMax credential. Provider-account high availability still requires a genuinely independent second credential.
- A browser loaded before this deployment remains on legacy last-arrival-wins tier updates until it refreshes. If the application is rolled back below v1.10.8, tier changes are session-scoped during the rollback; the preserved pre-rollback user preference resumes after re-upgrade until the user explicitly selects another tier.

---

# v1.10.7 — SaaS Media Routing and Reliable Creative Assets

## What's New

- **Explicit media routing matrix**: The SaaS console now manages MiniMax image, speech, music, and video routes independently for Lite, Pro, and Ultra. Text-only M2.5/M2.7 models remain text routes; this release does not add model-object authorization.
- **Platform-owned routing with a shared account pool**: Provider model and quality policy are controlled centrally, while credentials remain secret in the shared account pool. Tenant or Agent tool overrides can no longer silently replace the platform media model.
- **Reference-driven video**: Agents can send a workspace image, public URL, or validated data URL as a first frame, and optionally a last frame, so an uploaded product or visual subject can drive the video instead of being treated as text.
- **Deterministic media copy**: Exact Chinese or English copy can be rendered after image/video generation with installed fonts and ffmpeg, avoiding diffusion-generated garbled text.

## Bug Fixes

- Binary images, videos, presentations, and documents are no longer decoded as UTF-8 text and returned as mojibake. Agents receive the correct reference/preview workflow instead.
- Generated images are validated, normalized to match their filename format, and persisted before Credits and quota usage settle. Invalid provider output or a failed workspace write does not charge the tenant.
- Video generation validates reference dimensions before the paid provider call, stores only non-sensitive reference metadata, and keeps Credits reserved until a valid MP4 is durably stored.
- SaaS text-model APIs now reject incompatible media-generation routes. Video quality controls expose only provider-valid duration/resolution combinations for each tier.
- The stale `tts/tts/pro` fixed billing rule is disabled; MiniMax media continues to use provider-parameter-based dynamic Credits.

## Validation

- Complete backend and frontend unit suites pass.
- PostgreSQL migrations pass historical upgrade, downgrade, and re-upgrade smoke tests through `seed_minimax_media_routes`.
- Frontend production compilation and the focused media/settlement regression suite pass.
- Production deployment remains blue/green with database backup, candidate migration, health checks, and rollback before traffic cutover.

## Upgrade Notes

- Alembic migration `088_seed_minimax_media_routes.py` writes explicit Lite/Pro/Ultra defaults onto the four global MiniMax media tools without storing credentials.
- The production backend image now installs `ffmpeg`; the existing Noto CJK font package is used for deterministic video copy rendering.

---

# v1.10.6 — Production Stability, Artifact Integrity, and Safe Delivery

## What's New

### Reliable product workflows
- **Server-authoritative artifacts**: Agent replies now use verified document, image, audio, and video paths returned by tools instead of model-invented download links. Existing successful media jobs can be backfilled into the durable task ledger.
- **Consistent model selection**: Lite, Pro, and Ultra remain session-scoped across navigation and refresh. SaaS routing continues to use the shared credential pool and Credits ledger; this release does not introduce per-model object authorization.
- **Honest multimodal capabilities**: The runtime tells Agents exactly which native vision and media tools are available, and MiniMax image, speech, music, and video profiles remain available to eligible Lite, Pro, and Ultra plans.
- **Visible automation controls**: Managers can enable or disable Agent schedules from the Agent detail page, including schedules whose historical Focus record no longer exists. Global heartbeat and company-assignment background execution remain disabled by default in production.

## Bug Fixes

- **Credits correctness**: Failed, cancelled, malformed, or circuit-broken LLM runs no longer consume chat Credits. Repeated invalid tool calls stop after a bounded threshold instead of burning tokens indefinitely. Refund grants are now idempotent and production incident remediation is auditable and safe to rerun.
- **Channel model routing**: Fixed the shared model-route contract used by WeChat, WeCom, DingTalk, Discord, Slack, Teams, and WhatsApp adapters.
- **Media exactly-once settlement**: Prevented duplicate provider task identities from charging the same generated media more than once, released failed reservations, and made legacy finalized tasks recoverable.
- **PPT and file delivery**: Canonicalized generated-document links, removed hallucinated same-Agent URLs, and made stale frontend chunks recover with one guarded reload after deployment.
- **Authentication transport**: Browser sessions now use secure HttpOnly cookies or WebSocket subprotocol authentication, and cookie bootstrap/cleanup always return an explicit successful status. Download and WebSocket URLs no longer add bearer tokens to query strings, and Nginx access logs omit query parameters.
- **Tenant secret protection**: Company names and slugs that resemble API keys or bearer credentials are rejected without echoing the value. A remediation command can sanitize historical records and disable any matching shared model credential without exposing it.
- **Background routing**: OKR generation now uses the unified SaaS route and failover path rather than tenant-level legacy keys.
- **Deployment continuity**: Production delivery now uses blue/green application slots, database backup and migration before startup, candidate health checks before cutover, exact Nginx upstream replacement, connection draining, rollback, separate API/worker roles, and a one-time JWT rotation for the historical URL-token exposure.

## Validation

- Complete backend test suite and frontend unit suite pass.
- Historical PostgreSQL migrations pass both full upgrade and downgrade/re-upgrade smoke tests through the new refund-idempotency migration.
- Frontend production compilation and production Compose validation pass.
- Production release requires candidate health checks before traffic is switched and preserves the previous slot for rollback/draining.

## Upgrade Notes

- Alembic migration `087_make_refunds_idempotent.py` extends the ledger uniqueness guard to incident refunds.
- Existing browser bearer tokens are invalidated once during the first production deployment of this release; users may need to sign in again.
- Review Agent schedules after upgrade. System schedules may be disabled but cannot be edited or deleted; internal A2A delivery triggers remain protected.

---

# v1.10.5 — SaaS Credits, Runtime Hardening, and Release Reliability

## What's New

### Unified SaaS Model Access and Credits
- **Platform-managed model pool**: Model credentials and routes are managed centrally by SaaS administrators and consumed through the shared Credits balance. Agents no longer require per-model object authorization.
- **Subscription and billing foundation**: Added plan entitlements, seat reconciliation, credit reservations/ledger reconciliation, provider pricing, and guarded billing lifecycle jobs.
- **Safe first-run onboarding**: The first account can bootstrap a deployment without an invitation code; later registrations remain invitation-controlled. New Free tenants receive 1,000 Credits and one active default Agent.
- **Session-scoped model tier**: Lite, Pro, and Ultra selection is persisted per chat session, survives navigation and refresh, and remains constrained by the tenant plan. The managed MiniMax text routes are differentiated as M2.5, M2.7, and M2.7-highspeed respectively.
- **MiniMax media generation**: Added plan-aware image, speech, music, and video tools. Free users can try the Lite profile; successful work settles Credits, asynchronous video reserves before settlement, and provider failures do not create media charges.
- **Verified shared credentials**: New provider accounts remain `unverified` until a read-only provider probe succeeds. Existing accounts can be relabeled or have capabilities edited without returning or re-entering their secret key.

### Runtime and Integration Reliability
- **Durable A2A and trigger execution**: Added delivery state, idempotent trigger execution records, per-session realtime routing, and recovery diagnostics for interrupted Agent work.
- **Channel hardening**: Improved Feishu, DingTalk, WeCom, Discord, webhook, email, and local-model compatibility, including bounded uploads/streams and safer signature validation.
- **Tenant-safe skills and files**: Scoped tenant skills, tightened upload/path/delete checks, and repaired document pagination, workspace collaboration, and published Page URLs.

## Bug Fixes

- **Sandbox and process lifecycle**: Reap subprocess trees on completion, timeout, and cancellation; validate bubblewrap with a real release smoke test; keep Uvicorn as PID 1 and allow graceful container shutdown.
- **Reproducible backend images**: Added a universal `uv.lock`, fixed Python to the supported 3.12 line, and made production images install only frozen dependency versions.
- **Authentication and authorization**: Protected platform administrators, trigger mutations, private chat sessions, logout state, WebSocket sessions, tenant resources, and unsigned webhook entry points.
- **LLM/tool execution**: Restored multi-turn tool-call context, normalized provider payloads and token limits, improved MiniMax/local-provider failover, and made missing-code/tool errors actionable.
- **MiniMax routing stability**: Normalized media base URLs to prevent `/v1/v1`, made Lite/Pro/Ultra media profiles deterministic, billed highspeed text at its provider rate, and reset account health errors after a successful call so unrelated transient failures cannot accumulate forever.
- **Background Credits billing**: Heartbeat and other autonomous Agent runs now settle against the tenant Credits ledger; reconciliation covers balances and in-flight reservations without charging failed media generation.
- **Agent stability**: Prevented duplicate default Agents, repaired native Agent lifecycle handling, made A2A delivery durable, and surfaced recoverable Agent failures.
- **Frontend stability**: Fixed approval payload crashes, local-time session titles, A2A message placement, mobile navigation/layout overlap, deep workspace trees, stale Agent profile metadata, unavailable media actions that previously gave users no path to enable the required tool, and the expected platform-domain SSO fallback that produced a login-page 404 console error.
- **Deployment safety**: Removed shared Helm passwords, required deployment secrets, fixed PostgreSQL credential encoding, added frontend probes/upstreams, aligned Docker/Kubernetes sandbox permissions, propagated authenticated smoke-test inputs safely, stripped macOS archive metadata, kept long SSH deploys alive, and made `/api/version` report the deployed commit from the release environment.

## Upgrade Guide

### Docker Deployment

```bash
git pull origin main
docker compose down
docker compose up -d --build
```

### Source Deployment

```bash
git pull origin main

cd backend
uv sync --frozen --extra dev
cd ../frontend
npm ci
npm run build
cd ..

./restart.sh
```

### Kubernetes / Helm

```bash
helm upgrade clawith helm/clawith/ -f values.yaml
```

## Notes

- Database migrations run automatically on backend startup; no manual Alembic command is required.
- Helm deployments must set `backend.secrets.secretKey`, `backend.secrets.jwtSecretKey`, and `postgresql.auth.password`, or reference an existing Secret.
- Local `execute_code` remains fail-closed. Docker/Kubernetes deployments must enable the chart/Compose sandbox security settings or configure a supported remote sandbox.
- Model access follows the SaaS global model route and Credits ledger. This release intentionally does not add per-model object-level authorization.
- MiniMax text routes and media-generation tools are deliberately separate: `model_routes` remains a Chat Completion routing table, while image/audio/music/video generation is exposed through Agent tools and plan capabilities.

---

# v1.10.3 — Agent-to-Agent Messaging Session Consistency

## What's New

### Optimizations
- **Agent-to-Agent Messaging Database Session Handling**: Refined database session management for agent-to-agent (A2A) messaging events. This optimization reduces the risk of transactional inconsistencies during message triggers, improving backend reliability and ensuring clean session boundaries for agent interactions.

## Bug Fixes

- **A2A Messaging Stability**: Fixed an issue where database sessions in agent-to-agent messaging could become mismanaged, preventing potential side effects such as lingering database transactions or message delivery failures. This results in more robust and predictable agent messaging.

## Upgrade Guide

### Docker Deployment

```bash
git pull origin main

cd deploy
# Rebuild and restart services
docker compose down && docker compose up -d --build
```

### Source Deployment

```bash
git pull origin main

cd backend
cd ..

cd frontend
npm install
npm run build
cd ..

./restart.sh
```

### Kubernetes / Helm

```bash
helm upgrade clawith helm/clawith/ -f values.yaml
```

## Notes

- **Agent Messaging Reliability**: Tenants using agent-to-agent messaging will benefit from improved backend consistency and reduced risk of message-related faults.
- **No manual database migration required**: Schema migrations run automatically on application startup.

---

---

---
# v1.10.2 — Transaction Granularity & Sandbox Stability Enhancements

## What's New

### Core Features
- **Database ContextVar DAO Layer & Transaction Granularity Optimization**: Introduced a ContextVar-based DAO abstraction, leading to cleaner, safer transaction handling throughout the application. This reduces risk of cross-request interference and improves backend reliability in concurrent environments.
- **Expanded Soul.md Capacity for Agent Context**: Increased the character limit for `soul.md` in agent context-building from 2,000 to 30,000 characters, allowing for richer agent context and more complex behavior modeling.

### Optimizations
- **Sandbox Process Tree Cleanup on Timeout**: Significantly improved bwrap sandbox process cleanup to ensure all subprocesses are reliably terminated after execution timeout. This prevents zombie processes and resource leaks in environments with high code execution activity.
- **Tool Call Pairing Integrity in LLM Routing**: Enhanced LLM payload handling to ensure tool call pairs are always valid, preventing mismatches during agent tool calling and reducing backend errors.
- **Workspace Deletion Permissions Refinement**: Clarified and enforced workspace deletion permissions, ensuring only authorized users may delete workspaces or workspace files.

### UI/UX Enhancements
- **Sidebar Focus List Expansion Option**: Users can now view more than 12 items in the sidebar Focus list, with an expand option for easier navigation.
- **Chat Timestamp Localization**: Chat message timestamps now strictly align with the selected application language, improving global user experience and clarity.

## Bug Fixes

- **A2A Infinite Loop Prevention**: Addressed an issue where agent-to-agent message triggers could recurse infinitely, ensuring stable message routing and preventing backend exhaustion.
- **System Email Validation for Invitations**: Added pre-validation for system email addresses before sending invites, preventing invalid invitation cycles and reducing bounce rates.
- **Agent Settings Permissions Consistency**: Fixed permission handling for agent settings, preventing unauthorized edits and ensuring consistent access control.
- **Browser Extract Schema Compatibility**: Migrated browser extract schema to use Pydantic `RootModel[Any]`, resolving SDK typing issues for proper API serialization/deserialization.
- **Workspace File Delete Authorization**: Ensured workspace file deletions honor manager permissions, fixing cases where unauthorized deletes could occur.
- **History Message Order Correction**: Fixed double-reverse logic in chat gateway message ordering, restoring correct transcript sequencing in chat histories.

## Upgrade Guide

### Docker Deployment

```bash
git pull origin main

cd deploy
# Rebuild and restart services
docker compose down && docker compose up -d --build
```

### Source Deployment

```bash
git pull origin main

cd backend
cd ..

cd frontend
npm install
npm run build
cd ..

./restart.sh
```

### Kubernetes / Helm

```bash
helm upgrade clawith helm/clawith/ -f values.yaml
```

## Notes

- **Database Transaction Logic**: Custom integrations or plugins interacting with the backend database should review transaction scope logic for compatibility with the new ContextVar-based DAO layer.
- **Sandbox Process Cleanup**: No configuration changes are needed for sandbox improvements, but heavy code execution tenants may notice improved resource management.
- **Soul.md Expansion**: Applications or agents using large context files can now take advantage of the raised character limit for richer agent capabilities.
- **No manual database migration required**: Schema migrations run automatically on application startup.

---

---

# v1.10.1 — Chat Model Switcher & Entrypoint Permission Optimizations

## What's New

### Core Features
- **Live Chat Model Switching via WebSocket**: Enables users to change the active chat model in real time through websockets, improving flexibility and responsiveness in ongoing chat sessions.

### Optimizations
- **Faster Entrypoint Permissions Check**: Refactored and optimized entrypoint permissions verification, providing faster and leaner permission handling during request routing and task dispatch.
- **Deployment Config Adjustments**: Updated deployment configuration for improved reliability and compatibility with diverse environments.

## Bug Fixes

- **Chat Model Switcher Stability**: Resolved issues related to toggling chat models via websocket, ensuring seamless switching without session drops or inconsistent UI states.
- **Entrypoint Permissions Issue**: Fixed minor permission validation defects that could block valid requests in specific workflows.
- **Config Consistency**: Addressed deployment config edge cases related to environment-specific overrides and fallback handling.

## Upgrade Guide

### Docker Deployment

```bash
git pull origin main

cd deploy
# Rebuild and restart services
docker compose down && docker compose up -d --build
```

### Source Deployment

```bash
git pull origin main

cd backend
alembic upgrade heads
cd ..

cd frontend
npm install
npm run build
cd ..

./restart.sh
```

### Kubernetes / Helm

```bash
helm upgrade clawith helm/clawith/ -f values.yaml
```

## Notes

- **Live Model Switching**: No special configuration is required for enabling the websocket-based chat model switcher; feature is enabled by default.
- **Entrypoint Permissions**: Permission check routines have changed under the hood. If you maintain custom permission middleware or gateway logic, audit integration points for compatibility.
- **No manual database migration required**: Schema migrations run automatically on application startup.

---

---

---
# v1.10.0 — Async Agent Messaging, Atlas Onboarding & Robust File/Code Streaming

## What's New

### Async Agent-to-Agent Communication
- **A2A (Agent-to-Agent) async messaging enabled by default**: Modernizes inter-agent communication, ensuring agents can message each other asynchronously. Existing tenants are auto-repaired on startup for seamless transition and compatibility.
- **Optimized trigger logic and error handling**: Improves reliability when invoking agent triggers, handling edge cases more gracefully across communication workflows.

### Onboarding Experience — Atlas Design System
- **Complete onboarding rewrite using Atlas design system**: Revamped 4-screen onboarding with paper/night foundations, cosmographic visuals, personality chips, animated SVG brand marks, and responsive layouts.
- **OriginPlate and UniverseMap branding**: Login and multi-screen flows now match latest mockups with upgraded illustrations, decorative motifs, and increased accessibility.
- **Phase-wise UI enhancements**: Phases 1–3 implemented for core onboarding journey, improving engagement and brand cohesion.

### Streaming & Workspace File Delivery
- **Real-time file delivery injection in A2A chat sessions**: Files are now sent directly into agent-to-agent conversations, enhancing collaborative workflows.
- **Live code execution streaming**: Code output is streamed to the right-side Code panel in real-time, including improved error handling, truncated output on timeout, and user-facing retry hints.
- **Chromium PDF sandboxing improvements**: Improved Linux compatibility by adding `--no-sandbox` argument, ensuring stable PDF generation for workspace files.

### UI/UX Enhancements
- **Atlas login/dialog polish**: Login screens unified with refined chrome, cosmography plates, compass motifs, and improved brand mark SVG.
- **Multi-select personality chips and dynamic transitions**: Boosts agent creation flexibility and onboarding clarity.
- **Notification bar stabilization**: Top notification now stays fixed, with sticky elements offset below for consistent experience.
- **Agent and enterprise settings refactoring**: Settings tabs and detail page shells recalibrated for clarity.

### Chat & Pagination Improvements
- **Cursor-based pagination for chat history**: Allows smooth scrolling through long chat sessions, reduces page load times, and supports scalable transcript navigation for end users.

### Authentication & Provider Management
- **Global Single Sign-On (SSO) custom domain toggle**: Administrators can now switch SSO redirect behavior platform-wide, including adaptive UI theming.
- **OAuth multi-tenant flow and provider support**: Added platform-level OAuth providers for Google and GitHub, improving identity integration for organizations.
- **Google Workspace SSO routing hardening**: Refined org member links and provider routing to support enterprise teams using Google Workspace.

### Workspace & Tool Reliability
- **Workspace file deletion restricted to managers**: Tightens workspace security by limiting destructive actions to those with management rights.
- **S3/GCS endpoint auto-detection and compatibility**: Removes ‘SignatureDoesNotMatch’ errors; GCS endpoints now auto-configure for correct V4 signing.
- **AgentTool relationship backfill and dynamic loading**: Ensures all configured agents have proper tool records; disables tools respected in LLM payloads.

### Optimizations
- **Reduce DB connection pool exhaustion**: Lowers risk of backend overload during LLM calls, ensuring more stable service.
- **High-availability (HA) runtime improvements**: Backend deployment logic cleaned up for smoother scaling and reliability.
- **Dynamic tool log persistence and optimized skill seeding**: Tool logs now persisted for channels with faster skill relationship loading, improving auditability and first-run experience.
- **Sandbox and workspace fallback logic**: Allows local fallback when sandbox environment (bwrap) is unavailable, relaxes subprocess restrictions for broader compatibility.
- **Improved release workflow and auto-tagging**: Protected branch deployment, auto PR tagging, and smoother release ops.

## Bug Fixes

- **Workspace file deletion**: Only users with manager permissions can delete workspace files, preventing unauthorized data loss.
- **DB migration & tool record issues**: Alembic migration conflicts resolved, tool backfill now uses `commit()` for consistency, skips missing AgentTool records, and honor user-disabled tools in LLM call payloads.
- **Chat message/file injection errors**: Corrected DetachedInstanceError and import paths for chat/file delivery, preventing communication and file transfer failures.
- **Live event handling in Agent Detail**: Fixed ghost user bubble artifacts caused by agentbay_live events.
- **Sandbox streaming & timeout**: Proper capturing of code execution stream output on timeout, descriptions now respect config limits (default 60s, max 1h).
- **PDF rendering fallback logging**: Improved diagnostic messages and error traces for PDF generation under Linux.
- **UI/UX Minor Fixes**: Numerous adjustments across Atlas screens — logo, ring gaps, cosmography, section labels, indicator lines, and login plate visuals revised for coherence.
- **SSO, OAuth, and deployment**: Vercel env var type updated to ‘encrypted’, Google Workspace SSO provider routing adjusted, global SSO and reset password theme fixes.
- **GCS/S3 signature errors**: GCS signature configuration auto-corrects endpoint and resolves API mismatch.
- **Markdown rendering and workflow**: Improved markdown rendering and refined release workflow triggers.

## Upgrade Guide

### Docker Deployment

```bash
git pull origin main

# Rebuild and restart services
docker compose down && docker compose up -d --build
```

### Source Deployment

```bash
git pull origin main

# Rebuild frontend
cd frontend && npm install && npm run build
cd ..

# Restart backend / frontend services
```

### Kubernetes / Helm

```bash
helm upgrade clawith helm/clawith/ -f values.yaml
```

## Notes

- **Atlas onboarding and agent creation screens**: UI/design foundation changed substantially. Custom themes or branding may require review.
- **Agent-to-Agent async messaging (A2A)** is now standard. Legacy tenant configs are auto-repaired; review downstream automations if you rely on custom agent communication logic.
- **OAuth/SSO behavior and domain redirects**: New global toggle and improved routing; check your organization’s identity provider setup for compatibility.
- **Code execution sandboxes**: Timeout is now read from config, max timeout raised to 1h. Ensure configs are up-to-date if you leverage extended runtimes.
- **Workspace permissions**: Only managers may delete workspace files. Review role assignments to ensure proper access control.
- **Release workflow improvements**: Protected branch and PR auto-tagging are now supported. Update any internal release scripts if needed.
- **GCS/S3 endpoint auto-detection**: GCS storage integrations will now self-configure for correct signature version. If you use custom endpoints, verify compatibility.
- **No manual database migration required**: Schema migrations run automatically on application startup.

---

---

# v1.9.2 — Workspace Governance, Tool UX & Token Cache Accounting

## What's New

### Enterprise Info & Workspace Governance
- **Shared `enterprise_info/` workspace area** now appears as tenant-level company context for agents and users.
- **Agent-side enterprise info is read-only**: agents can list and read company context, but cannot create, edit, or delete shared enterprise files.
- **Admin-managed enterprise knowledge base**: platform and org admins can update enterprise info while regular users and agents are protected from accidental modification.
- **Legacy task files no longer appear in new agent workspaces**: new agents no longer receive `todo.json` / `tasks.json`; existing `tasks.json` files remain supported as legacy snapshots.
- **Workspace file handling polish** improves preview/download behavior for shared enterprise files and preserves read-only boundaries.

### Agent Management & Permissions
- **Company admins can manage company-visible agents** even when those agents were created by regular users.
- **Private user-only agents remain private** to their creator.
- Agent permission APIs now return effective management capability, so the UI can distinguish creator ownership from admin management rights.
- Start, stop, and permission update actions now use effective manager permission instead of creator-only checks.

### Tool Management Experience
- **Agent and company tool lists now share a cleaner grouped UI** with category headers, search, status filters, counts, and bulk toggles.
- Tool categories are easier to scan and can be expanded only when needed, reducing very long tool-list pages.
- Per-tool emoji icons were removed from the main list in favor of calmer category icons and compact labels.
- **`Update Objective` is now a global default tool**, so newly created employees have the OKR objective update capability enabled by default.
- Tool loading now avoids exposing disabled or agent-only tools to the LLM fallback path.

### Chat & Agent UX
- **New and existing chat sessions focus the composer automatically**, so users can type immediately after opening a session.
- **Existing sessions open at the latest message** more reliably.
- **Expanded tool chains now keep following the bottom only while appropriate**: if the user scrolls up intentionally, new tool updates no longer force the viewport back down.
- Duplicate assistant avatars after a tool-chain block were removed for a cleaner transcript.
- Tool-chain copy was refined from "Ran X agents" to clearer activity language.
- Agent expiry quick-renew buttons now show selected state.
- The dashboard's secondary "New Digital Employee" button was removed; creation remains available from the sidebar entry point.

### Token Accounting & Cache Visibility
- Token usage tracking now records input, output, estimated, cache-read, and cache-creation token counters.
- Agent stats expose cache hit information for providers that return cache usage.
- Qwen / Alibaba Bailian compatible calls now support provider-specific prompt cache control while preserving stable prompt prefixes.
- Daily and monthly token reset logic now resets cache counters alongside total token counters.

### Prompting, Webpage Generation & Tool Reliability
- Default webpage/rich-document style guidance moved into the system prompt, reducing repeated tool-description text while keeping generated pages visually consistent.
- Agent-facing reply guidelines now discourage emoji-heavy normal replies.
- Web search instructions now refer to currently enabled tools instead of hardcoding unavailable tool names.
- Tool-call execution now blocks disabled tool names and asks the model to retry malformed JSON tool arguments cleanly.
- HTML-to-PDF and HTML-to-PPT conversion descriptions and parameters were expanded for higher-fidelity Chrome-based rendering.
- Restart script now starts backend and frontend as detached daemons, avoiding local dev servers exiting after the restart command completes.

## Upgrade Guide

> **Database migration required.** Run `alembic upgrade heads` before restarting application services.

This release adds or updates schema/data defaults for:
- agent cache token counters
- daily token usage input/output/cache/estimated counters
- default agent TTL changing to permanent (`0`)
- default daily LLM call limit changing to `1000`

### Docker Deployment

```bash
git pull origin main

# Run database migrations
docker exec clawith-backend-1 alembic upgrade heads

# Rebuild and restart services
docker compose down && docker compose up -d --build
```

### Source Deployment

```bash
git pull origin main

# Run database migrations
cd backend && alembic upgrade heads
cd ..

# Rebuild frontend
cd frontend && npm install && npm run build
cd ..

# Restart backend / frontend services
```

### Kubernetes / Helm

```bash
helm upgrade clawith helm/clawith/ -f values.yaml
# Run migration job / command: alembic upgrade heads
```

### Notes
- `enterprise_info/` is now shared tenant context. Review who has platform or org admin roles, because only admins should update those shared files.
- New agents are permanent by default. If your deployment requires expiring agents, set tenant/user TTL defaults explicitly after migration.
- Token cache counters depend on provider usage payloads. Providers that do not return cache fields will continue to show zero cache usage.
- Existing legacy `tasks.json` files are preserved, but new agents will not get `todo.json` or `tasks.json` automatically.
- If you run from source, use the updated `restart.sh` or your own process manager to keep frontend/backend processes detached.

---

# v1.9.1 — Talent Market, Per-User Onboarding & Template Automation

## What's New

### Talent Market & Agent Templates
- **Talent Market** added to the hiring flow, letting teams browse, compare, and hire curated agents directly from the product UI
- **Folder-based template loader** for agent templates, making template packaging and rollout more maintainable
- **19 new curated templates** across business, engineering, content, and trading scenarios, including:
  - backend architect, chief of staff, code reviewer, content creator, devops automator, frontend developer, growth hacker, rapid prototyper, SEO specialist, TikTok strategist, LinkedIn content creator
  - macro watcher, market intel aggregator, technical analyst, pre-market briefer, watchlist monitor, risk manager, trading journal coach, tilt-bias coach, COT report analyst, earnings/filings analyst
- **Trading-focused built-in skills** added for market data and financial calendar workflows
- **Post-hire settings** now supported, so newly hired agents can be configured immediately after creation

### Per-User Onboarding & Default Model Experience
- **Per-(user, agent) onboarding** introduced, so onboarding runs once per user-agent relationship instead of once per agent globally
- **Two-turn onboarding ritual** added for newly hired or newly contacted agents: a focused introduction followed by an immediate deliverable
- **Onboarding backfill logic** prevents historical agent-user pairs from being re-onboarded after upgrade
- **Tenant default LLM model** support added, including backend APIs and frontend selection flows
- **Model switcher UI** added and refined to better reflect tenant and agent defaults during chat

### Template Automation & MCP Provisioning
- **Template-defined default MCP servers** can now auto-install when an agent is created
- **Template default skills merging** improved so agent creation preserves template-defined skills alongside platform defaults
- **Template bootstrap metadata** added, including capability bullets and bootstrap content for richer cards and onboarding prompts

### Chat, Workspace & UX Improvements
- **Workspace switcher** added to agent chat and detail flows for faster context switching
- **Clawith-styled modal and toast system** replaces native browser dialogs in key frontend flows
- **Agent chat and workspace interactions** polished for smoother file and panel operations
- **Agent creation flow** improved with better structure and clearer template-driven setup
- **Company logo settings** added to the admin/company experience
- **Company region picker** added to enterprise settings
- **Agent detail, layout, enterprise settings, and admin company pages** received usability and visual refinements

### Localization & Marketplace Readiness
- **Locale-aware greeting behavior** added for hired agents
- **Chinese translations and template localization** expanded across Talent Market and onboarding experiences
- **Hardcoded English copy** removed from key hire/onboarding paths to improve multilingual consistency

### Platform & Integration Enhancements
- **WeChat channel support** completed in the mainline release path
- **Webpage tools** enhanced for richer browsing and page interaction workflows
- **Smithery/MCP tool discovery and invocation** made more resilient with live schema override behavior and improved request headers

### Optimizations & Fixes
- **Onboarding performance optimization**: the greeting turn now skips the full tool list, significantly reducing prompt size on first contact
- **Onboarding stability fixes**: prevents ritual leakage into later sessions and avoids duplicate/late onboarding triggers
- **Model picker fixes**: better default syncing, improved dropdown positioning, and clipping fixes
- **Channel user identity reuse and outbound routing** fixed for more reliable cross-channel delivery
- **Agent creation fixes**: template skills and auto-installed MCP tools now attach more consistently
- **Migration graph fixes**: release migrations were stabilized and merged to avoid broken multi-head upgrade paths
- **UI polish fixes** across chat panels, dialogs, agent cards, and company branding

---

## v1.9.1 — Upgrade Guide

> **Database migration required.** Run `alembic upgrade heads` before restarting application services.

This release introduces new schema changes in the `v1.9.0..main` range, including:
- `tenants.default_model_id`
- `agent_user_onboardings`
- `agent_templates.capability_bullets`
- `agent_templates.bootstrap_content`
- `agent_templates.default_mcp_servers`
- release-head merge migration cleanup

### Docker Deployment (Recommended)

```bash
git pull origin main

# Run database migrations
docker exec clawith-backend-1 alembic upgrade heads

# Rebuild and restart services
docker compose down && docker compose up -d --build
```

### Source Deployment

```bash
git pull origin main

# Run database migrations
cd backend && alembic upgrade heads
cd ..

# Rebuild frontend
cd frontend && npm install && npm run build
cd ..

# Restart backend / frontend services
```

### Kubernetes (Helm)

```bash
helm upgrade clawith helm/clawith/ -f values.yaml
# Run migration job / command: alembic upgrade heads
```

### Notes
- Existing user-agent pairs are automatically backfilled into `agent_user_onboardings`, so established conversations should not be re-onboarded after upgrade.
- If your deployment provisions agents from templates, review any template metadata that now uses `bootstrap_content`, `capability_bullets`, or `default_mcp_servers`.
- If you rely on tenant-scoped model management, validate the new default model selection in Company / Enterprise settings after migration.
- New template-driven MCP auto-install flows require a valid Smithery/system MCP configuration in environments that use those templates.

# v1.8.3-beta.2 — A2A Async Communication, Image Context & Search Tools

## What's New

### Agent-to-Agent (A2A) Async Communication — Beta
- **Three communication modes** for `send_message_to_agent`:
  - `notify` — fire-and-forget, one-way announcement
  - `task_delegate` — delegate work and get results back asynchronously via `on_message` trigger
  - `consult` — synchronous question-reply (original behaviour)
- **Feature flag**: controlled at the tenant level via Company Settings → Company Info → A2A Async toggle (default: **OFF**)
- When disabled, the `msg_type` parameter is **hidden from the LLM** so agents only see synchronous consult mode
- Security: chain depth protection (max 3 hops), regex filtering of internal terms, SQL injection prevention
- Performance: async wake sessions use the agent's own `max_tool_rounds` setting (default 50)

### Multimodal Image Context
- Base64 image markers are now persisted to the database at write time
- Chat UI correctly strips `[image_data:]` markers and renders thumbnails
- Fixed chat page vertical scrolling (flexbox `min-height: 0` constraint)
- Removed deprecated `/agents/:id/chat` route

### Search Engine Tools
- New `Exa Search` tool — AI-powered semantic search with category filtering
- New standalone search engine tools: DuckDuckGo, Tavily, Google, Bing (each as own tool)

### UI Improvements
- Drag-and-drop file upload across the application
- Chat sidebar polish: segment control, session items styling
- Agent-to-agent sessions now visible in the admin "Other Users" tab

### Bug Fixes
- DingTalk org sync rate limiting to prevent API throttling
- Tool seeder: `parameters_schema` now correctly included in new tool INSERT
- Unified `msg_type` enum references across codebase
- Docker access port corrected to 3008

---

## v1.8.3-beta.2 — Bug Fixes

### A2A Chat History Fixes
- **A2A session now shows both sides of the conversation**: when a target agent is woken via `notify` or `task_delegate`, its reply is now mirrored into the shared A2A chat session so the full conversation is visible in the admin **Other Users** tab
- **Removed hardcoded 2-round tool call limit** for A2A wake invocations: agents were hitting the limit before completing basic tasks; they now use their own configurable `max_tool_rounds` setting (default 50)
- **Fixed message loading order**: sessions with many messages (e.g. long-running A2A threads) were only showing the oldest 500 messages; now correctly loads the most recent 500

## Upgrade Guide

> **Database migration required.** Run `alembic upgrade heads` to add the `a2a_async_enabled` column.

### Docker Deployment (Recommended)

```bash
git pull origin main

# Run database migration
docker exec clawith-backend-1 alembic upgrade heads

# Rebuild and restart
docker compose down && docker compose up -d --build
```

### Source Deployment

```bash
git pull origin main

# Run database migration
alembic upgrade heads

# Rebuild frontend
cd frontend && npm install && npm run build
cd ..

# Restart services
```

### Kubernetes (Helm)

```bash
helm upgrade clawith helm/clawith/ -f values.yaml
# Run migration job for a2a_async_enabled column
```

### Notes
- The A2A Async feature is **disabled by default**. No behaviour changes until explicitly enabled.
- The `a2a_async_enabled` column defaults to `FALSE`, so existing tenants are unaffected.
