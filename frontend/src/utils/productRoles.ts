type AgentIdentity = {
    id: string;
    product_role?: 'personal_assistant' | 'legacy_personal_assistant' | 'agent_employee';
};

export type AgentRolePartition<T extends AgentIdentity> = {
    personalAssistant: T | null;
    legacyPersonalAssistants: T[];
    employees: T[];
};

/**
 * Keep product roles tied to the server-owned onboarding relation. Names,
 * templates, access_mode and free-form role descriptions are not identities.
 */
export function partitionAgentRoles<T extends AgentIdentity>(
    agents: T[],
    personalAssistantAgentId: string | null | undefined,
): AgentRolePartition<T> {
    const linkedPersonalAssistant = personalAssistantAgentId
        ? agents.find((agent) => agent.id === personalAssistantAgentId) || null
        : null;
    const serverMarkedPersonalAssistants = agents.filter(
        (agent) => agent.product_role === 'personal_assistant',
    );
    // The onboarding relation wins when it is present in the visible roster.
    // During independent query loading or stale cache recovery, the unique
    // server-derived role prevents the assistant from flashing as an employee.
    // Multiple server markers fail closed instead of guessing.
    const personalAssistant = linkedPersonalAssistant
        || (serverMarkedPersonalAssistants.length === 1
            ? serverMarkedPersonalAssistants[0]
            : null);
    const legacyPersonalAssistants: T[] = [];
    const employees: T[] = [];
    for (const agent of agents) {
        if (agent.id === personalAssistant?.id) {
            continue;
        }
        if (agent.product_role === 'legacy_personal_assistant') {
            legacyPersonalAssistants.push(agent);
        } else {
            employees.push(agent);
        }
    }
    return { personalAssistant, legacyPersonalAssistants, employees };
}
