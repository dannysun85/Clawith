type AgentIdentity = {
    id: string;
};

export type AgentRolePartition<T extends AgentIdentity> = {
    personalAssistant: T | null;
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
    if (!personalAssistantAgentId) {
        return { personalAssistant: null, employees: [...agents] };
    }

    let personalAssistant: T | null = null;
    const employees: T[] = [];
    for (const agent of agents) {
        if (agent.id === personalAssistantAgentId && personalAssistant === null) {
            personalAssistant = agent;
        } else {
            employees.push(agent);
        }
    }
    return { personalAssistant, employees };
}
