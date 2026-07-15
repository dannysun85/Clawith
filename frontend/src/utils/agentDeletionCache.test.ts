import { QueryClient } from '@tanstack/react-query';
import { describe, expect, it, vi } from 'vitest';

import { refreshAgentQueriesAfterDelete } from './agentDeletionCache';

describe('refreshAgentQueriesAfterDelete', () => {
    it('removes the deleted detail and refreshes Agent and seat queries', () => {
        const queryClient = new QueryClient();
        const removeQueries = vi.spyOn(queryClient, 'removeQueries');
        const invalidateQueries = vi.spyOn(queryClient, 'invalidateQueries');

        refreshAgentQueriesAfterDelete(queryClient, 'agent-123');

        expect(removeQueries).toHaveBeenCalledWith({
            queryKey: ['agent', 'agent-123'],
            exact: true,
        });
        expect(invalidateQueries).toHaveBeenNthCalledWith(1, { queryKey: ['agents'] });
        expect(invalidateQueries).toHaveBeenNthCalledWith(2, { queryKey: ['subscription-seats'] });
    });
});
