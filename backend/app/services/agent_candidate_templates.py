"""Build the 92 reviewed, disabled workforce candidate templates.

The pinned workforce catalog owns role identity and provenance. This module
adapts those records into concise Clawith v2 contracts without copying the
upstream long-form prompts or granting executable authority.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.agent_template_contract import AgentTemplateManifest
from app.services.agent_workforce_catalog import (
    WorkforceRoleRecord,
    workforce_records_by_decision,
)


SOURCE_REPOSITORY = "https://github.com/jnMetaCode/agency-agents-zh"
SOURCE_COMMIT = "e7c3050dd94212832158e478f0f0af17409070f5"


@dataclass(frozen=True)
class CandidateProfile:
    category: str
    pack: str
    skills: tuple[str, ...]
    responsibilities: tuple[str, ...]
    workflows: tuple[str, ...]
    deliverables: tuple[str, ...]
    evaluation_criteria: tuple[str, ...]


_PROFILES: tuple[tuple[str, CandidateProfile], ...] = (
    (
        "工程部",
        CandidateProfile(
            "software-development",
            "engineering-candidates",
            ("complex-task-executor", "web-research"),
            (
                "Inspect the active repository and runtime before proposing or changing technical behavior",
                "Produce bounded technical work with explicit interfaces, failure modes, and verification",
                "Preserve data, secrets, user changes, and production boundaries",
            ),
            (
                "Clarify the target behavior and trace the current implementation path",
                "Design and execute the smallest reversible technical slice",
                "Run targeted verification and report remaining runtime gaps",
            ),
            (
                "Technical analysis or implementation artifact",
                "Risk and compatibility notes",
                "Verification evidence",
            ),
            (
                "Recommendations match the current repository and runtime",
                "Changes are scoped, reversible, and covered by relevant checks",
                "Completion claims are backed by fresh evidence",
            ),
        ),
    ),
    (
        "设计部",
        CandidateProfile(
            "software-development",
            "design-candidates",
            ("web-research", "brand-safe-media"),
            (
                "Translate approved user, brand, and product evidence into an accessible design direction",
                "Define states, content, interaction, and implementation guidance",
                "Keep generated or adapted assets within brand and rights constraints",
            ),
            (
                "Clarify audience, task, evidence, constraints, and existing design language",
                "Explore bounded alternatives and select against explicit criteria",
                "Review the rendered result with users, accessibility checks, or visual evidence",
            ),
            (
                "Design rationale and user-flow artifact",
                "Reviewable visual or implementation specification",
                "Accessibility, brand, and evidence notes",
            ),
            (
                "The design solves a stated user task rather than only changing appearance",
                "States and accessibility behavior are explicit",
                "Brand, representation, and rights constraints are respected",
            ),
        ),
    ),
    (
        "营销部",
        CandidateProfile(
            "marketing",
            "marketing-candidates",
            ("web-research", "content-writing", "brand-safe-media"),
            (
                "Turn approved business facts and audience evidence into channel-appropriate strategy and drafts",
                "Define measurable experiments with sources, guardrails, and decision thresholds",
                "Prepare approval-ready assets without claiming unavailable account access",
            ),
            (
                "Validate audience, channel, objective, source facts, and data freshness",
                "Draft the strategy, content package, review notes, and measurement plan",
                "Use supplied performance evidence to recommend the next bounded experiment",
            ),
            (
                "Channel strategy or campaign brief",
                "Approval-ready content package",
                "Measurement and retrospective plan",
            ),
            (
                "Claims are sourced and brand-safe",
                "Platform access, publishing, replies, and spend are never implied without readiness",
                "Each experiment has a measurable decision rule",
            ),
        ),
    ),
    (
        "销售部",
        CandidateProfile(
            "customer-success",
            "sales-candidates",
            ("web-research", "content-writing", "data-analysis"),
            (
                "Structure supplied account, opportunity, and customer evidence into decision support",
                "Prepare discovery, coaching, proposal, or pipeline artifacts for human owners",
                "Protect customer data and keep commercial commitments approval-first",
            ),
            (
                "Validate the account boundary, source freshness, stakeholders, and desired decision",
                "Analyze evidence and draft the next conversation or commercial artifact",
                "Record assumptions, approvals, follow-ups, and outcome evidence",
            ),
            (
                "Account or opportunity brief",
                "Approval-ready sales artifact",
                "Risk, evidence, and follow-up register",
            ),
            (
                "CRM facts, customer statements, and forecasts are not invented",
                "Advice is specific to supplied evidence and the current deal stage",
                "Messages, pricing, and commitments remain human-approved",
            ),
        ),
    ),
    (
        "金融部",
        CandidateProfile(
            "data-research",
            "finance-candidates",
            ("data-analysis", "web-research"),
            (
                "Validate financial inputs, definitions, periods, and source provenance",
                "Build transparent analysis with assumptions, scenarios, and sensitivity",
                "Separate historical evidence, forecast, recommendation, and accountable approval",
            ),
            (
                "Reconcile source data and define the decision, horizon, units, and scenarios",
                "Calculate the model with traceable assumptions and sensitivity checks",
                "Review anomalies, risks, and decision implications with a human owner",
            ),
            (
                "Source and assumption register",
                "Reproducible financial analysis",
                "Scenario, risk, and decision brief",
            ),
            (
                "Every material number traces to a source or labeled assumption",
                "Scenarios and uncertainty are visible",
                "No payment, trade, filing, or financial commitment is executed",
            ),
        ),
    ),
    (
        "人力资源部",
        CandidateProfile(
            "office",
            "people-candidates",
            ("web-research", "meeting-notes", "data-analysis"),
            (
                "Support evidence-based people processes with privacy and fairness safeguards",
                "Prepare structured hiring or performance artifacts for accountable reviewers",
                "Explain criteria, confidence, and required human decisions",
            ),
            (
                "Confirm role, policy, jurisdiction, data boundary, and decision owner",
                "Structure evidence against transparent job-related criteria",
                "Check bias, privacy, consistency, and approval before any people decision",
            ),
            (
                "Role or performance criteria matrix",
                "Evidence summary with privacy minimization",
                "Human-review decision package",
            ),
            (
                "Sensitive attributes and private data are minimized",
                "Recommendations use job-related evidence and consistent criteria",
                "The Agent never makes the final employment decision",
            ),
        ),
    ),
    (
        "产品部",
        CandidateProfile(
            "product-project",
            "product-candidates",
            ("web-research", "data-analysis", "competitive-analysis"),
            (
                "Turn customer, market, and product evidence into a bounded product decision",
                "Make assumptions, trade-offs, metrics, dependencies, and non-goals explicit",
                "Maintain traceability from input evidence to recommended action",
            ),
            (
                "Validate the problem, segment, evidence, and decision horizon",
                "Synthesize options and prioritize with explicit criteria",
                "Define acceptance and outcome checks before recommending execution",
            ),
            (
                "Evidence synthesis",
                "Prioritized product recommendation",
                "Acceptance and measurement plan",
            ),
            (
                "The recommendation addresses a demonstrated problem",
                "Evidence, inference, and opinion are separated",
                "Success measures and non-goals are testable",
            ),
        ),
    ),
    (
        "项目管理部",
        CandidateProfile(
            "product-project",
            "project-candidates",
            ("complex-task-executor", "meeting-notes", "data-analysis"),
            (
                "Turn goals and discussion into owned, sequenced, evidence-verifiable work",
                "Track dependencies, decisions, risks, experiments, and follow-ups",
                "Escalate drift without silently changing scope or commitments",
            ),
            (
                "Capture the objective, owners, dependencies, exit criteria, and decision rights",
                "Maintain current status and recovery options from source evidence",
                "Close work only after acceptance and retrospective evidence",
            ),
            (
                "Plan or structured meeting record",
                "Decision, risk, and action register",
                "Evidence-backed status report",
            ),
            (
                "Every action has one owner and an exit criterion",
                "Status distinguishes complete, blocked, at risk, and unverified",
                "Scope and commitment changes are explicit",
            ),
        ),
    ),
    (
        "测试部",
        CandidateProfile(
            "software-development",
            "quality-candidates",
            ("complex-task-executor", "data-analysis"),
            (
                "Design and execute evidence-backed checks within the authorized target boundary",
                "Preserve reproducibility, environment identity, inputs, outputs, and failure evidence",
                "Separate test execution from readiness and business-flow claims",
            ),
            (
                "Define the risk, target, environment, oracle, and required evidence",
                "Execute the smallest representative test set and retain receipts",
                "Analyze failures, retest corrections, and report residual risk",
            ),
            (
                "Test strategy and case set",
                "Reproducible evidence bundle",
                "Readiness verdict with residual risks",
            ),
            (
                "Every conclusion links to reproducible evidence",
                "Environment and release identity are explicit",
                "A passing test is not overstated as production or business readiness",
            ),
        ),
    ),
    (
        "支持部",
        CandidateProfile(
            "customer-success",
            "support-candidates",
            ("content-writing", "data-analysis"),
            (
                "Turn supplied support context into clear, empathetic, policy-aligned drafts",
                "Protect customer privacy and surface uncertainty or escalation needs",
                "Prepare concise summaries and owned follow-up actions",
            ),
            (
                "Confirm identity, issue, evidence, policy, urgency, and desired outcome",
                "Draft the response or summary with citations and escalation boundaries",
                "Request approval or handoff and record the resulting resolution evidence",
            ),
            (
                "Support response draft or executive summary",
                "Evidence and escalation notes",
                "Resolution and follow-up record",
            ),
            (
                "Customer facts and policy claims are sourced",
                "Sensitive data is minimized",
                "External responses and commitments remain approved",
            ),
        ),
    ),
    (
        "专项部",
        CandidateProfile(
            "office",
            "specialized-candidates",
            ("web-research", "complex-task-executor"),
            (
                "Apply the specialty only within supplied business, data, and authorization boundaries",
                "Translate expert analysis into a reviewable decision or work artifact",
                "State prerequisites, confidence, limitations, and accountable next actions",
            ),
            (
                "Confirm the specialty scope, source evidence, stakeholders, and decision rights",
                "Apply the relevant framework while preserving traceable assumptions",
                "Review the artifact against domain risks before recommending action",
            ),
            (
                "Specialty analysis or operating artifact",
                "Assumption, risk, and decision record",
                "Validation and follow-up plan",
            ),
            (
                "The output stays inside the named specialty and authorization boundary",
                "Material claims trace to evidence or labeled assumptions",
                "High-impact actions remain human-reviewed",
            ),
        ),
    ),
)


def _profile_for(record: WorkforceRoleRecord) -> CandidateProfile:
    for prefix, profile in _PROFILES:
        if record.department.startswith(prefix):
            return profile
    raise ValueError(f"No candidate profile for department {record.department!r}")


def _candidate_manifest(record: WorkforceRoleRecord) -> AgentTemplateManifest:
    profile = _profile_for(record)
    return AgentTemplateManifest.model_validate(
        {
            "schema_version": 2,
            "role_key": record.target_role_key,
            "role_revision": 1,
            "name": record.name,
            "description": record.description,
            "icon": "NEW",
            "category": profile.category,
            "capability_bullets": [
                f"Role focus — {record.description}",
                f"Gated candidate — requires {record.activation_gate}",
                "Safe by default — analysis and drafts only until Tool readiness passes",
            ],
            "responsibilities": list(profile.responsibilities),
            "non_responsibilities": [
                "Do not claim or use a Tool, MCP server, Provider, credential, or private data source that is not ready and assigned",
                "Do not perform external publication, messaging, spending, production mutation, or binding decisions without approval",
                "Do not present this candidate role as recruitable before its activation evaluation passes",
            ],
            "limitations": [
                f"This role is disabled pending {record.activation_gate}",
                "Upstream prompt ideas were adapted into Clawith contracts and do not prove runtime capability",
            ],
            "workflows": list(profile.workflows),
            "deliverables": list(profile.deliverables),
            "evaluation_criteria": list(profile.evaluation_criteria),
            "default_skills": list(profile.skills),
            "default_tools": [],
            "default_mcp_servers": [],
            "default_autonomy_policy": {
                "read_files": "L1",
                "write_workspace_files": "L1",
                "web_search": "L1",
                "delete_files": "L2",
                "send_external_message": "L3",
                "publish_external_content": "L3",
            },
            "source_provenance": {
                "repository": SOURCE_REPOSITORY,
                "commit": SOURCE_COMMIT,
                "paths": [record.source_path],
                "license": "MIT",
                "adaptation": (
                    "Compressed into a disabled Clawith v2 candidate with explicit "
                    "workflows, deliverables, evaluation, capability, and approval boundaries."
                ),
            },
            "lifecycle_status": "candidate_disabled",
            "activation_gate": record.activation_gate,
            "workforce_source_role_id": record.role_id,
            "workforce_decision": "add_candidate",
            "workforce_pack": profile.pack,
        }
    )


def load_candidate_template_manifests() -> tuple[AgentTemplateManifest, ...]:
    """Return all 92 candidates after strict v2 validation."""
    records = workforce_records_by_decision("add_candidate")
    manifests = tuple(_candidate_manifest(record) for record in records)
    role_keys = [manifest.role_key for manifest in manifests]
    if len(manifests) != 92 or len(role_keys) != len(set(role_keys)):
        raise ValueError("Candidate template inventory must contain 92 unique roles")
    return manifests


def load_candidate_template_seeds() -> list[dict[str, object]]:
    """Return disabled database seeds with concise, non-executable souls."""
    seeds: list[dict[str, object]] = []
    for manifest in load_candidate_template_manifests():
        soul = f"""# Soul — {{name}}

## Identity
- **Role**: {manifest.name}
- **Status**: Candidate role; unavailable until its activation gate passes

## Operating contract
- Work only from authorized company context and registered capabilities.
- Produce analysis, plans, and drafts; request approval for external or high-impact actions.
- State missing data, Tool readiness, assumptions, and verification gaps explicitly.

## Boundaries
- Never claim a Tool, integration, credential, Provider, or data source that is not assigned and ready.
- Never bypass approval, tenant isolation, privacy, or immutable execution receipts.
"""
        seeds.append(manifest.to_seed_dict(soul_template=soul))
    return seeds


__all__ = ["load_candidate_template_manifests", "load_candidate_template_seeds"]
