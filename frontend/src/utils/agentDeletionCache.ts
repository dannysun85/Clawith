import type { QueryClient } from '@tanstack/react-query';

export function refreshAgentQueriesAfterDelete(queryClient: QueryClient, agentId: string): void {
    queryClient.removeQueries({ queryKey: ['agent', agentId], exact: true });
    void queryClient.invalidateQueries({ queryKey: ['agents'] });
    void queryClient.invalidateQueries({ queryKey: ['subscription-seats'] });
}
