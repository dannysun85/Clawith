type A2AMessageIdentity = {
    is_current_agent?: boolean;
    sender_agent_id?: string;
    sender_name?: string;
    role?: string;
};

/** Return true when an A2A history message belongs on the peer/left side. */
export function isA2AMessageLeft(
    message: A2AMessageIdentity,
    currentAgentId?: string,
    currentAgentName?: string,
): boolean {
    if (typeof message.is_current_agent === 'boolean') {
        return !message.is_current_agent;
    }
    if (message.sender_agent_id && currentAgentId) {
        return message.sender_agent_id !== currentAgentId;
    }
    if (message.sender_name && currentAgentName) {
        return message.sender_name !== currentAgentName;
    }
    return message.role === 'assistant';
}
