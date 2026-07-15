import { describe, expect, it } from 'vitest';

import {
    buildAgentChannelSetups,
    findIncompleteAgentChannels,
} from './agentChannelSetup';

describe('buildAgentChannelSetups', () => {
    it('routes every completed provider form to its dedicated endpoint', () => {
        const setups = buildAgentChannelSetups({
            feishu_app_id: 'cli_feishu',
            feishu_app_secret: 'feishu-secret',
            slack_bot_token: 'xoxb-token',
            slack_signing_secret: 'slack-secret',
            discord_connection_mode: 'webhook',
            discord_application_id: 'discord-app',
            discord_bot_token: 'discord-token',
            discord_public_key: 'discord-key',
            teams_app_id: 'teams-app',
            teams_app_secret: 'teams-secret',
            teams_tenant_id: 'teams-tenant',
            wecom_connection_mode: 'websocket',
            wecom_bot_id: 'wecom-bot',
            wecom_bot_secret: 'wecom-secret',
            dingtalk_app_key: 'dingtalk-key',
            dingtalk_app_secret: 'dingtalk-secret',
            dingtalk_agent_id: '123',
            atlassian_api_key: 'ATSTT-token',
            atlassian_cloud_id: 'cloud-id',
        });

        expect(setups.map(({ endpoint }) => endpoint)).toEqual([
            'channel',
            'slack-channel',
            'discord-channel',
            'teams-channel',
            'wecom-channel',
            'dingtalk-channel',
            'atlassian-channel',
        ]);
        expect(setups[1].payload).toEqual({
            bot_token: 'xoxb-token',
            signing_secret: 'slack-secret',
        });
        expect(setups[2].payload).toMatchObject({
            connection_mode: 'webhook',
            application_id: 'discord-app',
        });
        expect(setups[4].payload).toEqual({
            connection_mode: 'websocket',
            bot_id: 'wecom-bot',
            bot_secret: 'wecom-secret',
        });
    });

    it('keeps WeCom customer-service webhook AgentID optional', () => {
        const setups = buildAgentChannelSetups({
            wecom_connection_mode: 'webhook',
            wecom_corp_id: 'corp-id',
            wecom_secret: 'corp-secret',
            wecom_token: 'verification-token',
            wecom_encoding_aes_key: 'encoding-key',
        });

        expect(setups).toEqual([{
            channel: 'WeCom',
            endpoint: 'wecom-channel',
            payload: {
                connection_mode: 'webhook',
                corp_id: 'corp-id',
                wecom_agent_id: '',
                secret: 'corp-secret',
                token: 'verification-token',
                encoding_aes_key: 'encoding-key',
            },
        }]);
    });

    it('maps the Discord WebSocket UI mode to the provider gateway contract', () => {
        const setups = buildAgentChannelSetups({
            discord_connection_mode: 'websocket',
            discord_bot_token: 'discord-token',
        });

        expect(setups).toEqual([{
            channel: 'Discord',
            endpoint: 'discord-channel',
            payload: {
                connection_mode: 'gateway',
                bot_token: 'discord-token',
            },
        }]);
    });

    it('reports partial forms instead of silently dropping them', () => {
        expect(findIncompleteAgentChannels({
            slack_bot_token: 'xoxb-token',
            wecom_connection_mode: 'webhook',
            wecom_bot_id: 'stale-hidden-bot-id',
            teams_tenant_id: 'tenant-without-app',
        })).toEqual(['Slack', 'Microsoft Teams', 'WeCom']);

        expect(findIncompleteAgentChannels({
            discord_connection_mode: 'websocket',
        })).toEqual([]);
    });
});
