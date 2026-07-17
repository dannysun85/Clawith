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

At the time this ledger was created, every registered secondary worktree was
clean. The canonical worktree had only the unrelated untracked
`CUSTOMER_PRODUCT_BUSINESS_VALUATION.md`; it must remain untouched. No local
branch contained a committed change newer than RC4 that needed to be merged
into this candidate.

## Candidate closure scope

The accumulated RC5 branch spans the following reviewed domains. `Closed locally`
means implementation, regression coverage, and the named local release gates
passed. It does not mean a provider or production deployment was tested.

| Domain | Local closure | Gate result |
| --- | --- | --- |
| Tenant/auth/chat privacy | Tenant and session authorization, Agent credential ownership, fail-closed signup verification policy, privacy-safe logs and realtime delivery | Closed locally |
| Credits and provider acceptance | Reservation ownership, provider-acceptance fencing, exact settlement and idempotent compensation for every tenant | Closed locally |
| MiniMax media | Brand-safe image/video output, artifact validation, and durable image/audio/music/video recovery | Closed locally |
| Automation | CEO/OKR/Heartbeat and automatic trigger execution paused without erasing desired state | Closed locally; intentionally unavailable |
| AgentBay | Owner-scoped credentials, durable session binding, per-access Take Control revalidation and cleanup | Closed locally |
| Code and MCP | Production Code-off contract, approval binding, tenant MCP isolation and network policy | Closed locally; Code intentionally unavailable |
| Agent deletion | Active media/AgentBay/seat lifecycle fences and immediate frontend refresh | Closed locally |
| SaaS/model routing | Shared-pool MiniMax M3 Lite/Pro/Ultra `text`/`image`/`video` understanding routes | Closed locally |
| Deployment | Clean-tree packaging, release identity, rollback, Nginx runtime, Code-off and egress preflight | Closed locally |
| Release communication | Automation, Code, legacy credential and deployment boundaries plus exact local evidence | Closed locally |

Because the initial RC5 checkpoint accumulated edits from multiple sessions,
Git cannot infer author-level provenance for each line. The release branch keeps
that checkpoint and every subsequent correction as local Git commits. Review
and validation apply to the complete diff from RC4, not to a copied directory
or an uncommitted temporary tree.

## Closed release blockers

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
5. Password and SSO registration plus unverified login resolve one strict SMTP
   policy snapshot. Lookup failure returns `503` and cannot be mistaken for a
   no-SMTP installation that permits automatic verification.
6. Release notes and this ledger distinguish local proof from production proof
   and retain the intentional Code-off and automation-paused boundaries.

Independent architect and code-reviewer passes found no remaining local P0/P1
release blocker after these corrections. Any later code change invalidates that
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
