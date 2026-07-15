# v1.10.12 — Safe MiniMax M3 Routing and Bounded Automation

## Model Routing and Multimodal Understanding

- Lite, Pro, and Ultra understanding routes are seeded through the centrally managed `MiniMax-M3` pool for `text`, `image`, and `video` inputs. Chat uploads select the concrete attachment route, and the OpenAI-compatible caller converts image/video markers into structured content parts instead of sending them as plain text.
- Attachment-driven `image`/`video` understanding is request-scoped. It no longer overwrites the session's persistent modality, so a later text-only turn or page refresh cannot silently keep using the previous attachment route; the user's Lite/Pro/Ultra tier remains persistent.
- `audio` and `music` remain generation-tool capabilities rather than chat-understanding routes. Image, speech, music, and video generation continue through the explicit media tools, plan entitlements, reservation, and exactly-once Credits settlement paths.
- Routed M3 models retain a legacy-compatible primary `text` modality plus explicit `text/image/video` capabilities. The selector separates request capability from provider quota scope, so both blue/green slots can use the healthy platform pool during migration.
- Media capability discovery uses the same centrally funded platform-pool boundary as runtime credential selection. A tenant-private MiniMax credential cannot make a shared SaaS media capability appear available to another company.
- The model selector remains a centrally funded shared-pool policy. This release does **not** add tenant-level or LLM-model-object-level authorization.

## External Channel Reliability

- WeChat, WeCom, DingTalk, Discord webhook/gateway, Slack, Teams, and WhatsApp now forward the resolved Lite/Pro/Ultra route metadata through the unified LLM caller. The former positional-argument mismatch could fail a channel request before provider execution even though browser chat still worked.
- WeChat history now uses the shared tool-call-aware message converter, preventing persisted tool calls and tool results from being flattened into invalid provider messages.
- WeCom ordinary application replies and proactive delivery now share one bounded positive ASCII-numeric `wecom_agent_id` contract. Invalid and oversized legacy values fail before session creation or LLM/Credits work, and every ordinary outbound message serializes `agentid` as an integer instead of silently sending application ID `0`. Customer Service-only Webhook configuration may omit AgentID because that reply path uses the Customer Service send API instead. Explicit `connection_mode` now governs edits, so hidden credentials from the previous mode cannot override a WebSocket/Webhook switch; inactive-mode secrets are cleared on save.
- WeCom media messages remain an explicitly unclaimed capability in v1.10.12. The production connector is text-only until inbound download/storage controls, outbound media upload, MIME/size/malware policy, idempotency, retention, and Credits behavior have their own reviewed implementation and provider proof.
- The Agent runtime capability prompt, live tool schema, database tool seed, and channel callbacks now match that boundary: named-recipient file delivery is advertised only for the implemented Feishu and Slack paths. A current-conversation callback must explicitly confirm provider attachment delivery before the tool reports success; Feishu HTTP success is accepted only when the provider business code also confirms token, upload, and final-send success. The existing DingTalk media-upload path is preserved only when both upload and media send succeed, while text-only or failed paths fall back to an authenticated workspace download link instead of fabricating attachment delivery. File delivery rejects absolute paths and traversal before storage materialization, rejects LocalStorage symlinks before materialization, then rechecks resolved containment so cross-Agent, external-target, and prefix-collision escapes cannot expose files outside the Agent workspace. A shared strict Agent-scoped storage-key contract also rejects traversal before A2A file reads, email attachment reads, and both new and legacy public-page rendering.

## Production Privacy and Cutover Safety

- Audited production runtime paths no longer include chat prompts, assistant response previews, tool arguments/results, channel message text, OAuth/provider response bodies, credential prefixes, external sender/message identifiers, or user-controlled file paths in operational logs. Diagnostics use server-generated Trace IDs plus code-owned operation/type, status/error code, content length, and aggregate counts.
- Central exception formatting no longer renders exception values or diagnostic local variables. It retains the exception type and a bounded function/line trace shape for investigation without exposing customer or credential data. Standard-library records retain only safe diagnostic shape (`source`, `level`, message length, argument shape, bounded HTTP status, and exception type), while a source-level contract rejects direct logging of sensitive values in application and startup-seeder paths.
- HTTP request contexts always use a server-generated 12-hex Trace ID; successful and handled-error responses expose it through `X-Trace-Id`. Client-supplied `X-Trace-Id` content is ignored so correlation headers cannot inject customer or credential data into operational logs.
- After a successful cutover, newly written Clawith production Nginx access-log entries retain only an Nginx-generated request ID, status, response size, and timing. They omit client IP, method, URI/path, query, Referer, User-Agent, and other request-controlled values in every effective Clawith HTTP and HTTPS `server` block; deployment gates audit the expanded target-site configuration and reject target-site `include` directives or unsafe location overrides. Automatic rollback changes the upstream while preserving the privacy-safe format, and a cutover is not declared complete until pre-reload Nginx workers have exited, the public release identity is exact, and the worker is both healthy and running the intended release image. Any nonterminal cutover journal is recovered to its declared slot/release before inactive-slot cleanup, while invalid journals preserve both slots and stop the deployment. Historical access logs remain unchanged until a separately authorized operator retention action is approved.
- Production deployment is serialized by a host lock and records a durable canonical slot/release state before updating compatibility mirrors. Recovery cross-checks that state against `current`, the terminal cutover journal, the live Nginx upstream, and exact release identity. Worker handoff requires exactly one healthy worker with the candidate image, release ID, and worker process role; critical background-task failure makes dedicated worker health fail. Deferred connection drain is resumed only for an exact managed inactive release and blocks slot reuse while live connections remain.
- Formal deployment now refuses dirty worktrees and cannot skip the full local backend, frontend, PostgreSQL migration, Ruff, diff, build, or effective-Compose gates. The package is generated directly from the reviewed Git commit, its embedded commit identity is checked locally, and its SHA-256 is checked remotely before extraction. Real-account remote smoke is on by default; an emergency skip requires a non-empty, version-and-full-commit-bound approval with an issued time, a maximum four-hour window, and a unique nonce. Duplicate fields are rejected. The nonce is consumed only when one complete root-owned record containing the original approval artifact, its file and nonce SHA-256 values, release ID, version, and full commit has been fsynced and atomically published; interruption before publication remains retryable, while any failure after publication requires a new approval.
- The same privacy contract now covers WebSocket chat, LLM/tool execution, Heartbeat and scheduled work, AgentBay control, OAuth, and Feishu, DingTalk, WeCom, Teams, Slack, and Discord channel paths.
- Feishu, Heartbeat, one-shot, and supervision failures now log only stable error types and show generic operator guidance. Provider exception bodies can no longer be copied into operational logs, OAuth responses, task replies, or one-shot notifications.
- MiniMax `2056` media-plan exhaustion remains a recorded production issue and still isolates only the affected modality, but it is logged as an expected provider-capacity warning rather than a platform `ERROR`. Unknown, authentication, transport, persistence, and code failures remain errors.

## Tool Authorization and Code Isolation

- Tool-management APIs now reuse the canonical Agent access policy, require manage access for mutations, reject cross-tenant targets, and reserve shared Tool-row mutations for platform administrators. A malicious or stale assignment can no longer expose another company's admin tool.
- Company and Agent tool secrets are masked on every response. Masked, blank, or omitted password fields preserve the encrypted stored value during ordinary edits; MCP URL userinfo, token-like query values, and fragments are masked and safely preserved on round-trip.
- MCP execution now resolves only inside the calling Agent's explicit enabled assignments and rechecks the Tool's tenant boundary. A fabricated or bare MCP tool name can no longer select another company's URL or credential. The literal `/tools/mcp-server` credential route is no longer captured by the UUID update route, failed credential persistence is surfaced by the UI, and tenant-owned imports receive a deterministic internal tenant namespace while the rollback-compatible global database uniqueness contract remains intact.
- MCP discovery can no longer report success with an empty catalog or retain a legacy Agent-owned generic placeholder as an executable tool. Empty/failed discovery quarantines that generic row and its assignments without deleting another Agent's configuration; successful discovery migrates the current Agent's encrypted configuration only onto concrete named tools. Authentication-pending Smithery connections preserve existing named tools without fabricating new capabilities.
- MCP transport validates every resolved address, rejects mixed public/private results, pins the validated numeric public peer for the connection, and rechecks the connected peer. Redirects, URL credentials, fragments, non-HTTPS production endpoints, private/link-local/reserved addresses, and DNS-rebinding attempts fail closed. The deployment quarantine snapshots legacy MCP rows before migration, restores only rows whose single-company ownership is proven, and keeps shared/orphan/admin rows disabled without copying credentials across tenants.
- Production adds a separately authorized host-level MCP egress contract: a root-owned `DOCKER-USER` chain and systemd watchdog reject private, loopback, link-local, metadata, benchmark, documentation, multicast, and reserved ranges while retaining the shared application network's required public SMTP, HTTP, HTTPS/WSS, and reviewed proxy flows. MCP keeps its independent HTTPS and resolved/connected-peer checks. Rule repair installs a temporary source-subnet REJECT fence first and removes it only after the exact chain and unique first jump are verified, preventing fail-open or half-built windows. Ordinary releases verify the contract hash, subnet, marker, and exact rule order before backup or migration and never silently install or weaken host firewall policy.
- Agent Tool assignments now have a database uniqueness contract plus conflict-aware writes. Same-company imports of the same MCP server are serialized with a PostgreSQL transaction-scoped tenant/server lock, so two Agents reuse one Tool row and receive separate assignments instead of racing on the global Tool name.
- Code execution is a separately authorized high-risk capability, not a model permission. It is disabled by default and requires the platform kill switch plus exact tenant, tool name, provider/endpoint, network, and Agent-assignment grants. Wildcards are rejected and every gate is rechecked immediately before execution; Code provider credentials, endpoints, resource limits, and egress controls are platform-admin-only.
- Code execution is forced to L3 approval by default. The complete immutable tool payload is encrypted at rest and bound to the Agent/tool with a hash and HMAC; truncated or legacy approval payloads fail closed and must be requested again. AgentBay approvals are also bound to the originating session and recheck human Take Control before execution. Approval completion is owned by the durable worker, and the model is explicitly told not to retry a queued side effect.
- Production Code accepts only explicitly approved external isolation backends. Local `subprocess`/`docker`, unsafe bubblewrap fallback, implicit host fallback, arbitrary custom endpoints, and Agent-level provider rerouting are rejected. Stateful AgentBay Code remains production-blocked because its egress and hard-timeout controls are not yet proven. Code configuration bypasses the general 60-second cache so revocation is immediate, and stored provider keys are decrypted exactly once before runtime use. The production API and worker no longer mount the Docker socket or run with `privileged`, `SYS_ADMIN`, or unconfined seccomp/AppArmor settings.
- Migration `095_secure_code_execution_defaults.py` changes every Code tool to non-default, creates any missing Agent Code assignment as disabled (preventing an old application seeder from resurrecting grants after rollback), disables historical assignments, and resets provider endpoint/network/unsafe-fallback values at Tool, company, and Agent override levels. It also formalizes the previously runtime-created `tenant_settings` table. Startup seeding no longer recreates enabled Code file-helper grants from an unrelated or historical AgentBay file-transfer assignment.
- Every ordinary production deployment now actively rewrites the Code platform switch to false and empties the tenant, tool, provider, and endpoint allowlists instead of inheriting a prior release's `.env`. **Code activation is not part of v1.10.12**: the new preview and crash-safe approval controls are necessary but not sufficient. Activation remains blocked until a separately authorized production change names the exact tenant, Agent assignment, tool, sandbox provider and endpoint, and proves provider dependency/contract, egress, timeout/output/concurrency, Credits settlement, metrics/alerts, and independent security-review gates.
- Nginx cutover parsing now treats directive-shaped keys such as `access_log`, `proxy_pass`, and `log_format` as data while inside a valid `map` block, without counting them as live directives or upstreams. Includes and nested/invalid map contexts remain rejected.

## Durable Approvals and Integration State

- Migration `096_durable_approval_execution.py` adds one-attempt claim tokens, compare-and-swap terminal writes, stale-claim fencing, encrypted immutable payload checks, active-request fingerprinting, and a tenant-fair bounded queue. An approved side effect is executed once by the worker; an indeterminate crash is marked ambiguous rather than replayed.
- Approval records and in-app notifications commit before optional Feishu delivery, so a failed database transaction cannot send a card containing a phantom approval ID. Approval previews and terminal UI labels expose bounded, code-owned status and error codes without rendering secrets or raw provider bodies.
- Atlassian configuration has one canonical synchronization path. Both the dedicated channel page and the Tools category entry persist `syncing`, await the same sync operation, persist `ready`/`failed`, and return a failure instead of reporting success before background work has completed.
- Douyin approval outcomes distinguish a user publish package, provider acceptance/review, unverified publication, user confirmation awaiting verification, and confirmed success. Only the final verified state is described as published; blocked, permission, authentication, and ambiguous provider outcomes remain explicit failures or pending verification. Once an external write may have started, timeouts, network failures, invalid responses, provider 5xx responses, and unknown exceptions become `verification_required` with `retry_safe=false`; approval recovery projects that state without overwriting stronger confirmed/pending-provider states, preventing blind duplicate posts or replies.

## MiniMax Token Plan Correctness

- MiniMax Token Plan capacity is treated as one provider `plan` circuit shared by text, image, audio, music, and video, matching the current provider contract. An exhausted shared plan can no longer leave another capability incorrectly routable.
- Provider-specific media allowances are tracked with an exact model scope when MiniMax reports a separate or legacy model cap. Exhausting one concrete video model therefore does not automatically poison unrelated media models.
- Removed the local `window_5h_limit` raw-token gate. MiniMax applies resource-weighted, cross-modal five-hour and weekly allowances, so a local raw-token counter could falsely reject a valid Code Plan before the provider had exhausted it. The stored field remains for schema compatibility but is no longer enforced or editable in the SaaS console.
- The account-pool monitor periodically reads MiniMax `/v1/token_plan/remains` evidence, including official status and count fields (`1=limited`, `2=exhausted`, `3=unlimited`). Unknown status values and authentication/probe failures never masquerade as quota depletion or recovery; ordinary request success cannot race and clear a newer provider quota circuit.
- Read-only credential verification no longer clears every scoped quota state. Replacing an API key resets the old state, while relabeling a credential or changing its endpoint cannot silently re-enable exhausted capacity.
- MiniMax-M3 `priority` delivery is charged at the provider's documented 1.5x Standard PAYG rate in both the pre-call reservation and exact settlement; Ultra can no longer use Priority latency while being costed as Standard.

## Automation and Production Safety

- Platform-seeded OKR/CEO automation is disabled by default through migration `094_disable_system_okr_automation.py` and `OKR_AUTOMATION_ENABLED=false`. Explicit user-triggered work, user-managed schedules, durable A2A delivery, and media reconciliation remain available.
- The OKR safety switch now checks `is_system` as well as the reserved trigger name. A user-created trigger that happens to use an OKR-like name is no longer suppressed or given system-only execution instructions.
- Trigger claiming and execution are bounded by `TRIGGER_MAX_CONCURRENCY` and `TRIGGER_CLAIM_BATCH_SIZE`, preventing a backlog from starting an unbounded number of Agent runs at once.
- Trigger routing metadata is now server-reserved: REST responses omit internal `_...` fields, Agent-visible trigger lists and prompts omit internal routing plus webhook tokens/secrets, and both REST and Agent tools reject attempts to modify reserved fields. External webhook JSON remains bounded message context and cannot overwrite execution leases or delivery routes. Ordinary triggers with different destination principals run in separate batches, while A2A delivery accepts only the isolated system wake trigger, validates participation and tenant/access boundaries before model work, and revalidates the target immediately before persistence. Automatic notification attribution therefore follows the validated session owner instead of the Agent creator, and legacy ordinary `_a2a_session_id` data is never treated as an A2A route.
- Production deployment quiesces the previous worker before the automation-state migration. The blue/green cutover uses a serialized host lock, durable slot/release and cutover journals, exact public/worker identity checks, bounded Nginx drain, and signal-safe rollback. The release migrations are backward-compatible and remain applied during application rollback, preserving the OKR safety switch and operational evidence instead of attempting a risky online schema downgrade.
- Every trigger claim gets a unique generation fence, long executions renew their lease, and completion/failure requires both the `processing` state and the exact current fence. A late coroutine from an expired/reclaimed worker cannot overwrite the new owner, a migration, or an operator-forced terminal state.
- Durable A2A delivery now treats an unexpired `processing` execution as the Agent's active queue head. A later message cannot be claimed by the next daemon tick until that head completes or becomes reclaimable, preventing concurrent same-Agent replies and overlapping tool side effects.
- Production issue ingestion has a bounded fallback queue, and authenticated browser telemetry has a server-side rolling Redis limit, so a faulty or hostile client cannot create unbounded database growth. Migration `097_durable_production_issue_alert_delivery.py` adds a per-issue-epoch/per-sink outbox with privacy-safe payload snapshots, stable idempotency keys, short `SKIP LOCKED` claims, stale-claim recovery, bounded retries, and compare-and-swap completion. Claim and finalization transactions use the same parent-Issue-before-delivery lock order, notifications render the claimed epoch snapshot rather than mutable live fields, and obsolete resolved/reopened epochs terminate without sending a stale notification. Each webhook has a 10-second timeout and bounded concurrency; its parent Issue lock fences status/epoch changes across the external call, so a claim resolved or reopened before send cannot emit a stale alert. The aggregate is marked alerted only after every required sink is durable. Database-loop freshness is exposed through dedicated-worker health, and repeated database failures emit a content-free `PRODUCTION_MONITOR_FATAL` signal before the worker exits for container restart; webhook failure alone does not make the worker unhealthy.

## Credits and Failure Isolation

- Provider failures continue to release media reservations without creating consumption transactions. Quota state is kept separate from credential authentication health, and unknown, transport, persistence, validation, and code failures do not falsely disable the shared account pool.
- MiniMax `2056` capacity failures remain recorded production issues but are treated as expected provider-capacity warnings. Exact media-task settlement and release remain idempotent under concurrent reconciliation.
- MiniMax `2062` high-traffic rejections are classified as retryable provider saturation rather than authentication or daily-quota exhaustion. The rejected credential enters a short Redis-backed cooldown so another independent healthy credential can be selected; a proven pre-generation `2062` rejection releases its `provider_inflight` Credits hold. A single Token Plan credential is still not production high availability: multi-tenant production requires PAYG/enterprise capacity and at least two independently verified credentials. The SaaS account form shows this capacity notice only for MiniMax credentials, with localized English and Chinese text.
- Asynchronous video reconciliation forwards the task's concrete provider model for correlation. A bare MiniMax `2056` still opens the shared plan circuit; only provider evidence naming a concrete model opens an exact-model circuit.
- Asynchronous video completion now persists one session-bound assistant message before attempting realtime delivery. WebSocket publication targets the exact session user, retries through a durable outbox, deduplicates by message ID in the browser, and opens the verified workspace video path instead of relying on an Agent to invent a downloadable link.
- Media notification deep links now wait for the requested chat session to finish initialization before restoring the workspace asset. A direct link or hard refresh therefore reopens the same video instead of allowing the Agent reset lifecycle to clear the preview path.
- First-run onboarding now decides whether a session is empty only after that exact session's message history has loaded. A missing platform credential can produce one actionable assistant error, but reloads no longer race history loading and append the same error repeatedly. History network and server failures receive three bounded attempts and still fail closed instead of being mistaken for an empty session. Explicit session changes also replace stale notification `session_id`, `message_id`, and `workspace_path` parameters so a later refresh cannot jump back to the notification's old session; authorized administrators can restore an exact `scope=all` session instead of being redirected to their own first conversation.
- Persisted WebSocket chat messages now carry their database message IDs. User optimistic messages use a server-validated client UUID as that same database ID, so REST history hydration and realtime delivery converge without introducing a new acknowledgement frame that could confuse an older open frontend during a blue/green cutover. Session-list responses are also fenced by both user identity and auth token, preventing a superseded login request from repopulating another account's session metadata.
- Provider-accepted video tasks keep polling and retain their Credits hold across transient errors or age thresholds; only a task proven not to have reached the provider can be failed and released automatically. Editable legacy workspace JSON cannot select a credential, call the provider, or authorize a refund. The backfill imports only an already-finalized reservation with an existing Agent-workspace asset; every uncertain legacy task becomes an operator-visible held reconciliation item.
- A completed LLM response is no longer discarded when secondary usage accounting or Credits settlement persistence fails. Credits settlement runs before the secondary Agent quota counter, failures emit a critical privacy-safe production issue, and each settlement stage remains independently observable.
- Every routed LLM provider round now atomically reserves a conservative maximum as `provider_inflight` before the request. Once the provider completes, the exact debt is persisted as a durable `settlement_ready` outbox before tools or results are released; an outbox failure keeps the hold instead of releasing already-incurred usage, records the reservation and exact intended settlement in the privacy-safe production monitor, and stale indeterminate holds are escalated for operator reconciliation. Invalid tool output, round limits, and cancellation after a provider response therefore remain billable, while a reservation database failure never calls or degrades the provider.
- Provider-call cancellation and transport failures now distinguish an explicit pre-connection or deterministic client-side HTTP rejection from an indeterminate request that may already have reached the provider. Only the proven rejection releases `provider_inflight`; cancellation, server failure, request/read timeout, and partial-stream failure keep the hold for operator reconciliation. OpenAI-compatible streams retry only connection-establishment failures and never replay a request after a successful response starts, preventing duplicate generations and double provider cost.
- Provider business failures returned inside HTTP 200 payloads, including MiniMax `base_resp`, Gemini, Anthropic, and Responses API errors, now release `provider_inflight` only for a proven deterministic pre-generation rejection with no output or usage evidence. MiniMax transient/internal codes such as `1000` and `1001` remain held for reconciliation and cannot trigger cross-model replay; quota, authentication, validation, policy, and explicit rate-limit rejections can still release safely.
- MiniMax interleaved-thinking responses preserve and replay the complete cumulative `reasoning_details` object across foreground, Heartbeat, one-shot, and background tool-call rounds, matching the provider's OpenAI-compatible multi-turn contract.
- Feishu uses the same guarded `call_llm_with_failover` state machine as WebSocket chat. Its channel safety timeout cancels an in-flight unified call exactly once and never starts a second fallback after an ambiguous provider cancellation.
- Final LLM Credits use the higher of the configured Lite/Pro/Ultra product price and MiniMax's token-derived cost, including the higher M3 long-context band above 512K input tokens. This preserves tier pricing without undercharging unusually expensive requests.
- Synchronous MiniMax image, speech, and music generation now reserve Credits before the provider call, finalize the reservation only after a usable workspace artifact exists, and release unfinished reservations on every failure path. Concurrent requests can no longer all pass a read-only balance check and overspend the same available Credits.
- Native and OpenClaw Agent deletion now clears the deleted detail cache and immediately triggers server-authoritative refreshes for both the Agent collection and subscription-seat usage without blocking navigation. Native deletion also disables its confirmation controls while the request is in flight. A released seat becomes available in the sidebar instead of remaining incorrectly blocked at the previous `used/total` value until reload or the 30-second poll.

## Validation

- New source-level privacy contracts reject known payload/preview logging patterns, and unit tests verify that diagnostic shape summaries cannot contain values or mapping keys.
- RC3 local release gates passed with 993 backend tests, 81 frontend tests, a 6,999-module production frontend build, and the complete PostgreSQL migration/rollback/re-upgrade smoke covering Credits settlement, production issue aggregation and durable alert retry, media-generation exactly-once behavior and safe legacy recovery, A2A queue serialization, tenant-fair approval claim/CAS/crash handling, same-company cross-Agent MCP import concurrency, preference/queue concurrency contracts, and fail-closed Code/MCP migration. The focused deployment/security gates cover Nginx `map` parsing, full-length Worker container identity, tool tenant/secret/MCP runtime boundaries, deterministic tenant tool naming, Smithery live-empty fail-closed behavior, Douyin direct/H5/reply ambiguous-write handling, DNS/connected-peer validation, Code authorization and approval integrity, sandbox policy, and the exactly-one healthy Worker boundary. The Ruff Git-baseline gate found no new violations across 17 changed Python files; Bash syntax, effective production Compose rendering, Alembic single-head and revision-length verification, and `git diff --check` also passed.
- RC4 extends the same local release gates to 1,055 backend tests and 88 frontend tests with the 7,001-module production build. New adversarial coverage verifies `2062` deterministic rejection settlement and provider cooldown for both text and media calls, independent-credential cooldown selection, unknown error-text sanitization, Redis read/write interruption fallback, routed metadata forwarding across every external channel, WeChat tool-call history conversion, save-time/runtime/oversized WeCom application-ID enforcement, Customer Service-only configuration without AgentID, explicit WebSocket/Webhook mode switching, real LocalStorage cross-Agent/external-target/prefix-collision symlink rejection and temporary-workspace cleanup, strict A2A/email/public-page Agent namespace enforcement before any read or external send, Feishu HTTP-200 business failure handling, provider-specific Agent-create channel endpoints with partial-form rejection, channel-mode restoration after wizard remount, OpenClaw isolation from hidden Native channel drafts, DingTalk AgentID persistence on first configuration, and the text-only channel capability contract across the runtime prompt, live tool schema, database seed, callback registration, and provider-confirmed delivery result. The complete PostgreSQL fresh-install and production-era upgrade/downgrade/re-upgrade smoke, Alembic single head, Ruff Git-baseline gate, Bash syntax, effective production Compose rendering, and `git diff --check` pass.
- Authenticated browser proof on an isolated, freshly migrated PostgreSQL environment finalized a real H.264 video through the durable media-completion path, persisted and delivered exactly one completion message, restored the video after a direct-link hard refresh, and received the asset through HTTP `206 Partial Content`. A fresh no-credential onboarding session retained exactly one friendly error after repeated hard refreshes, explicit session switches retained the selected session in the URL, and an administrator's authorized other-user session survived a direct-link hard refresh without falling back to `scope=mine`. The Native five-step wizard, Discord Webhook mode restoration, Native-to-OpenClaw isolation, Native/OpenClaw creation, and both deletion paths were also exercised; each deletion issued immediate Agent and seat refetches and restored the create action without a page reload.
- This evidence makes the tree a **local Code-off RC4 candidate only**. Production cutover, production resolver/endpoint checks, production data-plane proof, and post-release observation remain separate gates and have not been claimed by this local validation.

# v1.10.11 — Verified Public Blue/Green Cutover

## Release Safety

- Blue/green deployment now verifies the public health payload, release version, and exact commit after Nginx reload and before stopping the previous application slot.
- Public verification bypasses intermediary caches with a release-specific query and `Cache-Control: no-cache`, retries boundedly for reload propagation, and records the verified health/version evidence in the rollback backup directory.
- If the public endpoint does not expose the expected version and commit, deployment exits through the existing rollback trap: Nginx, the `current` symlink, and any stopped previous services are restored instead of accepting a partially switched release.
- Rollback records the exact pre-release Alembic revision and downgrades to it before returning traffic to the legacy slot. If database downgrade fails, rollback fails closed: it keeps the current traffic slot in place, leaves the legacy worker stopped, and points operators to the pre-migration database backup.

## Validation

- The deployment contract test fixes the ordering requirement: strict public identity verification must occur before the old worker or application is stopped.
- This release includes the complete v1.10.10 WebSocket monitoring signal-integrity fix. It does not change Credits, model routing, plan entitlements, credentials, media settlement, automation, or tenant authorization.

# v1.10.10 — WebSocket Monitoring Signal Integrity

## Bug Fixes

- Agent chat WebSockets now distinguish intentional browser lifecycle shutdowns from unexpected disconnects. Navigation, logout, session teardown, and component cleanup close normally and no longer create `close_1005` false-positive production incidents or reconnect loops.
- Genuine unexpected `1005` / `1006` disconnects remain observable and retain the existing bounded reconnect behavior. Application terminal codes `4002` / `4003` remain expected control flow rather than infrastructure errors.
- Client WebSocket reports now include Agent context for faster diagnosis. The backend validates that context through the existing Agent access policy; an unauthorized Agent ID is discarded while the privacy-safe operational report is still accepted.
- Browser-side incident deduplication now keeps separate Agent occurrences distinct without storing prompts, messages, request bodies, credentials, or tenant-supplied identity.

## Validation

- Focused monitoring regressions pass for intentional versus unexpected WebSocket closes, Agent-level deduplication, schema restrictions, product access-policy reuse, and unauthorized-context removal.
- Complete backend and frontend unit suites, changed-file Ruff checks, frontend production compilation, and `git diff --check` pass.
- Fresh PostgreSQL upgrade, downgrade/re-upgrade, Plan CAS, A2A durability, media exactly-once settlement, production-issue aggregation/alerting, and chat-tier CAS smoke tests pass.

## Upgrade Notes

- This release changes only WebSocket lifecycle signaling and production-monitoring attribution. It does not change Credits, reservations, subscriptions, Lite / Pro / Ultra routing, shared credential ownership, plan entitlements, provider calls, media settlement, or autonomous execution.
- The production observation clock restarts from the v1.10.10 cutover because runtime code changed after the v1.10.9 observation began.
- The centrally funded shared account pool remains unchanged. This release does **not** add tenant-level or model-object-level authorization.

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
