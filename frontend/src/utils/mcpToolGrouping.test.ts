import { describe, expect, it } from 'vitest';

import {
    bulkEligibleToolIds,
    defaultMcpServerName,
    mcpToolGroupKey,
    normalizedMcpEndpointForGrouping,
} from './mcpToolGrouping';

describe('tool bulk eligibility', () => {
    it('excludes tools blocked by platform authorization', () => {
        expect(bulkEligibleToolIds([
            { id: 'allowed', available: true },
            { id: 'blocked', available: false },
            { id: 'legacy-without-flag' },
        ])).toEqual(['allowed', 'legacy-without-flag']);
    });
});

describe('MCP tool grouping', () => {
    it('separates same-name servers with different endpoints', () => {
        const first = mcpToolGroupKey({
            type: 'mcp',
            mcp_server_name: 'Search',
            mcp_server_url: 'https://one.example/mcp',
        });
        const second = mcpToolGroupKey({
            type: 'mcp',
            mcp_server_name: 'Search',
            mcp_server_url: 'https://two.example/mcp',
        });

        expect(first).not.toBe(second);
    });

    it('does not place query credentials in a grouping key', () => {
        const grouped = normalizedMcpEndpointForGrouping(
            'https://MCP.EXAMPLE/mcp?workspace=one&tavilyApiKey=secret',
        );

        expect(grouped).toBe('https://mcp.example/mcp?workspace=one');
        expect(grouped).not.toContain('secret');
    });

    it('derives a credential-free display name from a URL', () => {
        expect(defaultMcpServerName('https://mcp.example/mcp?token=secret')).toBe('mcp.example');
    });
});
