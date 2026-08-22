/** Group chat types — mirror of backend/app/api/groups.py schemas. */

export interface Group {
    id: string;
    tenant_id: string;
    name: string;
    description: string | null;
    created_by_participant_id: string;
    created_at: string;
    updated_at: string;
}

export type ParticipantType = 'user' | 'agent';
export type GroupRole = 'manager' | 'member';

export interface GroupMember {
    id: string;
    participant_id: string;
    participant_type: ParticipantType;
    participant_ref_id: string;
    display_name: string;
    avatar_url: string | null;
    role: GroupRole;
    role_description: string | null;
    title: string | null;
    is_deleted: boolean;
    joined_at: string;
}

export interface GroupMemberCandidate {
    participant_id: string;
    participant_type: ParticipantType;
    participant_ref_id: string;
    display_name: string;
    avatar_url: string | null;
    role_description: string | null;
    title: string | null;
}

export interface GroupSession {
    id: string;
    group_id: string;
    title: string;
    is_primary: boolean;
    unread_count: number;
    created_by_participant_id: string | null;
    created_at: string;
    updated_at: string;
    last_message_at: string | null;
}

export interface GroupMention {
    participant_id: string;
    participant_type?: ParticipantType;
    display_name?: string;
}

export interface GroupMessage {
    id: string;
    role: 'user' | 'assistant' | 'system';
    content: string;
    participant_id: string | null;
    sender_name: string | null;
    mentions: GroupMention[];
    created_at: string;
    /** Message Position `<created_at ISO>|<id>` — the shared (created_at, id) ordering contract. */
    cursor: string;
}

/** `none` = no agent mentioned, `single` = one agent, `planning` = multi-agent task planning. */
export type DispatchKind = 'none' | 'single' | 'planning';

export interface GroupError {
    code: string;
    message: string;
    trace_id: string;
    run_id: string | null;
    agent_id: string | null;
    stage: 'planning' | 'execution' | 'delivery' | null;
    details: unknown;
    retryable: boolean | null;
}

export interface GroupMessageIntake {
    message: GroupMessage;
    dispatch_kind: DispatchKind;
    run_ids: string[];
    created: boolean;
    error_code: string | null;
    error?: GroupError | null;
}

export interface GroupPlanningReadiness {
    available: boolean;
    code: 'ready' | 'planning_model_unavailable';
    message: string;
    remediation: 'contact_platform_operator_or_mention_one_agent' | null;
}

export interface GroupRunState {
    run_id: string;
    status: string;
    can_cancel: boolean;
    agent_id: string | null;
    system_role: string | null;
}

export interface GroupTextFile {
    path: string;
    content: string;
    exists: boolean;
    version_token: string | null;
    modified_at: string | null;
    revision_id: string | null;
}

export interface GroupWorkspaceEntry {
    path: string;
    name: string;
    is_dir: boolean;
    size: number;
    modified_at: string;
    version_token: string | null;
}

export interface GroupSessionSummary {
    version: number;
    summary: string;
    requirements: unknown[];
    decisions: unknown[];
    open_items: unknown[];
    evidence_refs: unknown[];
    workspace_refs: unknown[];
    covered_through_message_id: string | null;
}

export interface GroupTaskSummary {
    task_id: string;
    title: string;
    intent: string;
    task_status: string;
    user_stage: string;
    status_axes: {
        execution: string;
        artifact: string;
        quality: string;
        runtime_approval: string;
        delivery_approval: string;
        delivery: string;
    };
    primary_owner_agent_id: string;
    primary_owner_agent_name: string;
    participants: Array<{
        agent_id: string;
        agent_name: string;
        responsibility: 'primary_owner' | 'collaborator';
    }>;
    runs: Array<{
        id: string;
        agent_id: string | null;
        agent_name: string | null;
        parent_run_id: string | null;
        root_run_id: string | null;
        run_kind: string;
        latest_event: string | null;
        delivery_status: string;
        created_at: string;
        updated_at: string;
    }>;
    group_id: string;
    group_session_id: string | null;
    source_message_id: string | null;
    source_message_cursor: string | null;
    latest_update: string | null;
    latest_update_at: string | null;
    next_actions: Array<{
        id: string;
        kind: 'quality_review' | 'runtime_approval' | 'delivery_approval' | 'task_recovery' | 'delivery_recovery';
        title: string;
        reason_code: string;
        action_url: string;
    }>;
    work_link: string;
    group_link: string | null;
    created_by: string;
    created_at: string;
    updated_at: string;
}
