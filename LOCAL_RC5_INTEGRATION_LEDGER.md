# v1.10.12 RC5 Local Integration Ledger

This file is the local handoff and integration contract for the RC5 candidate.
It does not claim deployment or production verification.

## Repository boundary

- Canonical repository worktree: `/Users/sun/Documents/PythonProject/Clawith`
- Isolated release worktree: `/Users/sun/Documents/PythonProject/Clawith-release-1.10.12`
- Candidate branch: `codex/v1.10.12-production-closure`
- Candidate base/tag: `v1.10.12-rc4`
- Base commit: `c3a5e74897025b8045ec62f1ef2ba9d121717025`
- Remote publication: prohibited for this task
- Production deployment or mutation: prohibited without a new explicit approval

The two directories are Git worktrees backed by the same local repository.
Uncommitted files in one worktree are not visible in another worktree. The
release worktree must therefore be committed, reviewed, and integrated by Git;
its directory must never be copied over the canonical worktree.

## Known local branch integration

The previously reported MiniMax M3 multimodal task is already represented in
the RC4 ancestry:

- Original branch: `codex/minimax-m3-understanding`
- Original commit: `59dc910d4aebe9778a47c20da4f0bea059739d27`
- Integrated equivalent: `3c27eca` (`feat(saas): add safe MiniMax M3 understanding routes`)
- Follow-up correction: `74f11c2` (`fix(minimax): align M3 routing with shared Token Plan`)

That work covers the centrally managed `MiniMax-M3` Lite/Pro/Ultra
`text`/`image`/`video` understanding routes, structured multimodal input, the
frontend attachment route, and the SaaS understanding-model administration
surface. RC5 must preserve these changes and must not reintroduce model-object
authorization.

The registered `codex/production-privacy-v1.10.12` worktree is not a separate
repository and is not a branch to copy over this candidate. Patch-equivalence
inspection found its logging change already integrated and its Nginx map and
full-container-ID contracts present in the candidate. Its strict canonical
release-path validation was not fully represented, so that behavior and its
Unicode-control regression cases were semantically integrated into the RC5
working tree before freeze. The old branch remains registered until the final
candidate is committed and all worktrees are cleaned through Git.

At the time this ledger was created, every registered secondary worktree was
clean. The canonical worktree had only the unrelated untracked
`CUSTOMER_PRODUCT_BUSINESS_VALUATION.md`; it must remain untouched. No local
branch contained a committed change newer than RC4 that needed to be merged
into this candidate.

## Candidate closure scope

The accumulated RC5 branch spans the following reviewed domains. The table
records implementation scope only. The current tree remains uncommitted and
must not be described as `local_rc_candidate` or `business_flow_proven` until
all named gates pass again on one frozen local Git commit.

| Domain | Local closure | Gate result |
| --- | --- | --- |
| Tenant/auth/chat privacy | Tenant and session authorization, SSO/local-password separation and non-destructive legacy remediation, same-browser OAuth binding, Agent credential ownership, privacy-safe logs and realtime delivery | Implemented; final SHA gates pending |
| Credits and provider acceptance | Reservation ownership, provider-acceptance fencing, exact settlement and idempotent compensation for every tenant | Prior proof retained; final SHA gates pending |
| MiniMax media | Brand-safe image/video output, artifact validation, and durable image/audio/music/video recovery | Prior proof retained; final SHA gates pending |
| Automation | CEO/OKR/Heartbeat and automatic trigger execution paused without erasing desired state | Implemented; intentionally unavailable |
| AgentBay | Owner-scoped credentials, durable session binding, per-access Take Control revalidation and cleanup | Prior proof retained; final SHA gates pending |
| Code and MCP | Production Code-off contract, approval binding, tenant MCP isolation and network policy | Implemented; Code intentionally unavailable |
| Agent deletion | Active media/AgentBay/seat lifecycle fences and immediate frontend refresh | Prior proof retained; final SHA gates pending |
| SaaS/model routing | Shared-pool MiniMax M3 Lite/Pro/Ultra `text`/`image`/`video` understanding routes | Prior proof retained; final SHA gates pending |
| Deployment | Clean-tree packaging, release identity, durable deferred drain, incident JWT rotation, secret-envelope continuity, rollback, Nginx runtime, Code-off and egress preflight | Implemented; final SHA gates pending |
| Release communication | Automation, Code, legacy credential and deployment boundaries plus exact local evidence | Updated; final evidence pending |

Because the initial RC5 checkpoint accumulated edits from multiple sessions,
Git cannot infer author-level provenance for each line. The release branch keeps
that checkpoint and every subsequent correction as local Git commits. Review
and validation apply to the complete diff from RC4, not to a copied directory
or an uncommitted temporary tree.

## Implemented blocker closures (final validation pending)

1. Credential-bound media destination changes require a fresh, unmasked key and
   a complete endpoint/header/path bundle; unsafe legacy partial configurations
   fail closed and surface administrator remediation.
2. Provider-accepted image, audio, music, and video work is represented by a
   durable recovery task before settlement. Unrecoverable accepted work records
   provider debt and grants one idempotent customer compensation.
3. AgentBay Take Control revalidates the durable session ledger on every access;
   cache state is not reuse authority.
4. Both Nginx templates quote the bounded webhook regular expression, and the
   built frontend container passes a real `nginx -t` plus isolated browser boot.
5. Password registration resolves one strict SMTP policy snapshot and fails
   with `503` when lookup or delivery is unavailable. No-SMTP production
   registration cannot auto-verify; only the explicit non-production override
   permits local use. SSO callbacks are deliberately independent of SMTP.
6. SSO-only identities never receive a provider-derived local password.
   Migration `106_secure_sso_password_login.py` preserves every historical hash
   but enables local login only for a pure, unlinked Web identity; mixed and
   provider-linked identities remain disabled until a verified reset. The
   first production deployment rotates JWT signing under a new incident marker.
7. SSO provider callback, status and one-time POST consumption require the same
   per-session HttpOnly browser proof. A relay URL opened in another browser is
   rejected before provider exchange or JIT account creation; this is not a
   cross-device QR flow. Login-page provider discovery allocates no relay,
   relay creation occurs only after a concrete provider selection, and one
   atomic client/tenant/global Redis decision prevents a rejected client from
   exhausting broader SSO availability. Signed Google login state is resolved
   before Redis, while administrator sync state is browser-bound before atomic
   consumption. Global Google/GitHub OAuth validates the browser nonce hash,
   provider and exact redirect URI before compare-deleting state, so an invalid
   callback cannot consume the legitimate browser's one-time login.
8. Complete `IdentityProvider.config` objects are encrypted/authenticated at
   rest and recursively redacted on every response. Production preflight blocks
   non-object legacy JSON before maintenance, deploy requires `SECRET_KEY`
   continuity, and half-built Generic OAuth2 create/update paths are disabled.
   Authentication providers receive request-local config snapshots, so URL
   generation cannot mark the encrypted ORM object dirty or overwrite a
   concurrent administrator credential update.
9. Deferred drain ownership is persisted before Nginx traffic mutation and is
   removed only after zero connections plus a successful stop, or an exact
   successful rollback. A later release cannot reuse a live inactive slot.
10. Dedicated OAuth callbacks return fixed error text rather than provider
   exception bodies.
11. Release notes and this ledger distinguish local proof from production proof
   and retain the intentional Code-off and automation-paused boundaries.
12. Sensitive self-service identity changes require current-password proof;
    password and recovery mutations increment Identity `auth_version`, revoking
    older HTTP, file and WebSocket credentials. Platform-admin identity edits
    use the same normalized cross-column namespace lock, and production
    preflight blocks historical username/email or username/phone ambiguity.
13. Public social OAuth is sign-in-only in this release. The incomplete
    `/auth/register/sso` surface fails with `410` before external or persistent
    side effects, while tenant-managed SSO provisioning remains available.
14. DingTalk webhook failures redact secret-bearing URLs, expose delivery
    failure through the production issue monitor, and never persist an
    assistant response that was not delivered.
15. Login claims a separate raw-identifier/client/global query budget before
    any Identity namespace lookup, then retains the resolved-Identity bcrypt
    budget. A rejected pre-lookup request performs no database query.
16. Every protected frontend transition awaits establishment of the HttpOnly
    browser-session cookie before exposing authenticated state. Same-origin
    tenant changes refresh the selected membership; cross-domain changes retain
    the server-issued one-time fragment until the destination consumes it.
17. The public email-existence oracle and incomplete social registration route
    both return `410` without lookup or provider side effects. Password and
    organization-managed SSO remain the supported registration contracts.
18. First-admin ownership and last-admin protection count only active
    administrators. Tenant membership creation is serialized, and migration
    106 refuses duplicate tenantless memberships before adding its partial
    unique index.
19. Production monitoring persists database rollups and in-product notification
    outbox delivery by default. Realtime external alerting is a separate
    production configuration gate: a secret webhook must be configured and its
    exact release canary must be delivered before that capability is claimed.
20. Deployment slot journals and `current` accept only complete ASCII-named
    direct children of the managed `releases/` directory. Nested paths, Unicode
    control characters, symlinked metadata and malformed journals fail closed
    before recovery or traffic mutation.

## Read-only production compatibility evidence

On 2026-07-18, read-only checks against `opc.reeftotem.ai` reported public
version `1.10.12` at commit `53b7cbd`, with the active database stamped at
`add_user_chat_tier_preference`. The production `identities` table has neither
`auth_version` nor `password_login_enabled`, and `identity_providers.config`
remains legacy JSON. Revision 106 has therefore not been applied to production;
the candidate may keep the current 106 instead of creating a post-applied 107.

The production issue monitor is enabled, but the active Worker reported no
configured `PRODUCTION_ISSUE_ALERT_WEBHOOK_URL`. Database aggregation and
in-product notification remain available; external realtime alerting is not
production-verified and is a deployment/operations gate, not a code claim.

Independent architect and code-reviewer passes are required on the exact frozen
candidate after these corrections. Any later code change invalidates their
conclusion and requires the full sequence below again.

## Required local integration sequence

1. Freeze the complete RC4-to-RC5 diff in this local branch.
2. Verify exactly one Alembic head and run fresh upgrade plus
   downgrade/re-upgrade PostgreSQL smoke tests.
3. Run the complete backend suite, frontend suite, browser assertions and
   production frontend build.
4. Run the Ruff Git-baseline gate, `git diff --check`, Python compilation,
   shell/deployment contracts and effective Compose rendering.
5. Build images from the exact Git state, then run credential-bound API and
   real-browser product flows on an isolated PostgreSQL/Redis/network topology.
6. Verify the Git archive embeds the exact candidate commit and record its
   deterministic package digest.
7. Obtain independent architect and code-reviewer approval on the frozen diff.
8. Rescan the canonical worktree and all local branches before integration. If
   a newer local change must be included, integrate it explicitly and repeat
   steps 2 through 7.
9. Merge into the user-designated local release branch and rerun the release
   gates on the post-merge commit before creating an RC5 tag.
10. Deploy only after separate explicit production approval, remote preflight,
    provider/credential checks, cutover verification, and observation gates.
11. Remove this worktree only after integration and explicit cleanup approval;
    removing it does not remove its committed branch or tag.

## Proof-state vocabulary

- `code_exists`: implementation is present in this worktree.
- `tests_pass`: named local tests passed on a stated commit or working tree.
- `local_rc_candidate`: all local release gates and independent reviews passed.
- `business_flow_proven`: a real browser user flow passed on the candidate.
- `production_verified`: the exact candidate was deployed and verified against
  production data and providers.

The frozen branch may be described as `local_rc_candidate` and
`business_flow_proven` only while every gate above remains green on its exact
commit. It is not merged, tagged, deployed, provider-verified, or
`production_verified`; those states require separate evidence and approval.
