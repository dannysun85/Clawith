export type AgentChannelEndpoint =
    | 'channel'
    | 'slack-channel'
    | 'discord-channel'
    | 'teams-channel'
    | 'wecom-channel'
    | 'dingtalk-channel'
    | 'atlassian-channel';

export interface AgentChannelSetup {
    channel: string;
    endpoint: AgentChannelEndpoint;
    payload: Record<string, unknown>;
}

interface ChannelCompletenessRule {
    channel: string;
    present: string[];
    required: string[];
}

const value = (values: Record<string, string>, key: string) =>
    String(values[key] || '').trim();

const anyValue = (values: Record<string, string>, keys: string[]) =>
    keys.some((key) => Boolean(value(values, key)));

/** Return optional channel forms that are partially filled and must not be skipped. */
export function findIncompleteAgentChannels(
    values: Record<string, string>,
): string[] {
    const discordMode = value(values, 'discord_connection_mode') || 'websocket';
    const wecomMode = value(values, 'wecom_connection_mode') || 'websocket';
    const rules: ChannelCompletenessRule[] = [
        {
            channel: 'Feishu',
            present: ['feishu_app_id', 'feishu_app_secret', 'feishu_encrypt_key'],
            required: ['feishu_app_id', 'feishu_app_secret'],
        },
        {
            channel: 'Slack',
            present: ['slack_bot_token', 'slack_signing_secret'],
            required: ['slack_bot_token', 'slack_signing_secret'],
        },
        {
            channel: 'Discord',
            present: ['discord_bot_token', 'discord_application_id', 'discord_public_key'],
            required: discordMode === 'websocket'
                ? ['discord_bot_token']
                : ['discord_bot_token', 'discord_application_id', 'discord_public_key'],
        },
        {
            channel: 'Microsoft Teams',
            present: ['teams_app_id', 'teams_app_secret', 'teams_tenant_id'],
            required: ['teams_app_id', 'teams_app_secret'],
        },
        {
            channel: 'WeCom',
            present: [
                'wecom_bot_id',
                'wecom_bot_secret',
                'wecom_corp_id',
                'wecom_wecom_agent_id',
                'wecom_secret',
                'wecom_token',
                'wecom_encoding_aes_key',
            ],
            required: wecomMode === 'websocket'
                ? ['wecom_bot_id', 'wecom_bot_secret']
                : ['wecom_corp_id', 'wecom_secret', 'wecom_token', 'wecom_encoding_aes_key'],
        },
        {
            channel: 'DingTalk',
            present: ['dingtalk_app_key', 'dingtalk_app_secret', 'dingtalk_agent_id'],
            required: ['dingtalk_app_key', 'dingtalk_app_secret'],
        },
        {
            channel: 'Atlassian',
            present: ['atlassian_api_key', 'atlassian_cloud_id'],
            required: ['atlassian_api_key'],
        },
    ];

    return rules
        .filter((rule) => (
            anyValue(values, rule.present)
            && !rule.required.every((key) => Boolean(value(values, key)))
        ))
        .map((rule) => rule.channel);
}

/**
 * Convert the optional Agent-create channel form into provider-specific API
 * requests. The generic `/channel` endpoint belongs to Feishu only; every
 * other provider must use its own endpoint and native field contract.
 */
export function buildAgentChannelSetups(
    values: Record<string, string>,
): AgentChannelSetup[] {
    const setups: AgentChannelSetup[] = [];

    const feishuAppId = value(values, 'feishu_app_id');
    const feishuAppSecret = value(values, 'feishu_app_secret');
    if (feishuAppId && feishuAppSecret) {
        setups.push({
            channel: 'Feishu',
            endpoint: 'channel',
            payload: {
                channel_type: 'feishu',
                app_id: feishuAppId,
                app_secret: feishuAppSecret,
                encrypt_key: value(values, 'feishu_encrypt_key') || undefined,
                extra_config: {
                    connection_mode: value(values, 'feishu_connection_mode') || 'websocket',
                    activation_mode: value(values, 'feishu_activation_mode') || 'mention',
                },
            },
        });
    }

    const slackBotToken = value(values, 'slack_bot_token');
    const slackSigningSecret = value(values, 'slack_signing_secret');
    if (slackBotToken && slackSigningSecret) {
        setups.push({
            channel: 'Slack',
            endpoint: 'slack-channel',
            payload: {
                bot_token: slackBotToken,
                signing_secret: slackSigningSecret,
            },
        });
    }

    const discordMode = value(values, 'discord_connection_mode') || 'websocket';
    const discordBotToken = value(values, 'discord_bot_token');
    const discordApplicationId = value(values, 'discord_application_id');
    const discordPublicKey = value(values, 'discord_public_key');
    if (discordMode === 'websocket' && discordBotToken) {
        setups.push({
            channel: 'Discord',
            endpoint: 'discord-channel',
            payload: {
                connection_mode: 'gateway',
                bot_token: discordBotToken,
            },
        });
    } else if (
        discordMode === 'webhook'
        && discordBotToken
        && discordApplicationId
        && discordPublicKey
    ) {
        setups.push({
            channel: 'Discord',
            endpoint: 'discord-channel',
            payload: {
                connection_mode: 'webhook',
                application_id: discordApplicationId,
                bot_token: discordBotToken,
                public_key: discordPublicKey,
            },
        });
    }

    const teamsAppId = value(values, 'teams_app_id');
    const teamsAppSecret = value(values, 'teams_app_secret');
    if (teamsAppId && teamsAppSecret) {
        setups.push({
            channel: 'Microsoft Teams',
            endpoint: 'teams-channel',
            payload: {
                app_id: teamsAppId,
                app_secret: teamsAppSecret,
                tenant_id: value(values, 'teams_tenant_id'),
            },
        });
    }

    const wecomMode = value(values, 'wecom_connection_mode') || 'websocket';
    const wecomBotId = value(values, 'wecom_bot_id');
    const wecomBotSecret = value(values, 'wecom_bot_secret');
    const wecomCorpId = value(values, 'wecom_corp_id');
    const wecomSecret = value(values, 'wecom_secret');
    const wecomToken = value(values, 'wecom_token');
    const wecomEncodingKey = value(values, 'wecom_encoding_aes_key');
    if (wecomMode === 'websocket' && wecomBotId && wecomBotSecret) {
        setups.push({
            channel: 'WeCom',
            endpoint: 'wecom-channel',
            payload: {
                connection_mode: 'websocket',
                bot_id: wecomBotId,
                bot_secret: wecomBotSecret,
            },
        });
    } else if (
        wecomMode === 'webhook'
        && wecomCorpId
        && wecomSecret
        && wecomToken
        && wecomEncodingKey
    ) {
        setups.push({
            channel: 'WeCom',
            endpoint: 'wecom-channel',
            payload: {
                connection_mode: 'webhook',
                corp_id: wecomCorpId,
                wecom_agent_id: value(values, 'wecom_wecom_agent_id'),
                secret: wecomSecret,
                token: wecomToken,
                encoding_aes_key: wecomEncodingKey,
            },
        });
    }

    const dingtalkAppKey = value(values, 'dingtalk_app_key');
    const dingtalkAppSecret = value(values, 'dingtalk_app_secret');
    if (dingtalkAppKey && dingtalkAppSecret) {
        setups.push({
            channel: 'DingTalk',
            endpoint: 'dingtalk-channel',
            payload: {
                app_key: dingtalkAppKey,
                app_secret: dingtalkAppSecret,
                extra_config: {
                    connection_mode: 'websocket',
                    agent_id: value(values, 'dingtalk_agent_id'),
                },
            },
        });
    }

    const atlassianApiKey = value(values, 'atlassian_api_key');
    if (atlassianApiKey) {
        setups.push({
            channel: 'Atlassian',
            endpoint: 'atlassian-channel',
            payload: {
                api_key: atlassianApiKey,
                cloud_id: value(values, 'atlassian_cloud_id'),
            },
        });
    }

    return setups;
}
