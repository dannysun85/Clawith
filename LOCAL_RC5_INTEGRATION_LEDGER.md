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

## Current candidate scope

The accumulated RC5 working tree spans the following reviewed domains. This is
a scope inventory, not a completion claim:

| Domain | Intended closure | Current gate |
| --- | --- | --- |
| Tenant/auth/chat privacy | Tenant and session authorization, Agent credential ownership, privacy-safe logs and realtime delivery | Full backend and browser gates pending |
| Credits and provider acceptance | Reservation ownership, provider acceptance fencing, exact settlement and compensation | Sync media recovery P0 open |
| MiniMax media | Brand-safe image/video output, media validation, durable video lifecycle | Image/audio/music durable recovery P0 open |
| Automation | CEO/OKR/Heartbeat and automatic trigger execution paused without erasing user desired state | PostgreSQL migration smoke pending |
| AgentBay | Owner-scoped credentials, durable session binding, Take Control authorization and cleanup | Cached control authority P0 open |
| Code and MCP | Production Code-off contract, approval binding, tenant MCP isolation and network policy | Deploy contract and legacy config migration pending |
| Agent deletion | Active media/AgentBay/seat lifecycle fences and frontend refresh | Full regression pending |
| SaaS/model routing | M3 route integrity and shared Token Plan semantics | Migration and full route tests pending |
| Deployment | Clean-tree packaging, release identity, rollback, Code-off and egress preflight | Full deploy contract pending |
| Release communication | Truthful automation, Code, legacy credential and deployment boundaries | Final evidence rewrite pending |

Because these edits accumulated before the first shared checkpoint commit, Git
cannot attribute every line in that checkpoint to a particular development
session. No author-level provenance is inferred. The checkpoint is deliberately
non-release and may contain open blockers. The final review is performed on the
complete diff from RC4, and no completion statement is valid until the diff is
frozen and all gates run on that exact state.

## Current release blockers

1. Changing any credential-bound media endpoint/header/path must require a
   fresh, unmasked key and complete destination bundle in the same request.
   Legacy partial or credential-in-destination configurations need a measured,
   fail-closed migration and administrator remediation notice.
2. MiniMax image, audio, and music provider success must create a durable,
   restart-safe recovery task. Credits may finalize only after the final asset
   is durably stored. Unrecoverable accepted work must settle provider debt and
   grant an idempotent customer compensation.
3. AgentBay Take Control must revalidate the durable session ledger on every
   access; an in-process cache is not reuse authority. Closed, expired,
   mismatched, or cleanup-required sessions must fail closed.
4. Release notes contain provisional RC5 evidence and outdated automation
   availability statements. They must be rewritten from the final gate output.

## Required local integration sequence

1. Close every blocker above and add adversarial regressions.
2. Verify exactly one Alembic head and run fresh upgrade plus
   downgrade/re-upgrade PostgreSQL smoke tests.
3. Run the complete backend suite, frontend suite and production build.
4. Run the Ruff Git-baseline gate, `git diff --check`, shell/deployment contract,
   effective Compose and packaging identity checks.
5. Freeze the exact diff and obtain independent architect and code-reviewer
   approval on that frozen tree.
6. Create the local RC5 candidate commit.
7. Rescan every local worktree and branch. If a newer local commit exists,
   integrate it explicitly and repeat steps 2 through 5.
8. Merge the reviewed candidate into the designated local release branch and
   rerun the release gates on the post-merge commit before creating an RC5 tag.
9. Remove this worktree only after the candidate is integrated, the worktree is
   clean, and the user approves cleanup. Removing the worktree does not remove
   its committed branch or tag.

## Proof-state vocabulary

- `code_exists`: implementation is present in this worktree.
- `tests_pass`: named local tests passed on a stated commit or working tree.
- `local_rc_candidate`: all local release gates and independent reviews passed.
- `business_flow_proven`: a real browser user flow passed on the candidate.
- `production_verified`: the exact candidate was deployed and verified against
  production data and providers.

RC5 is currently below `local_rc_candidate`. It is not deployed and must not be
described as production verified.
