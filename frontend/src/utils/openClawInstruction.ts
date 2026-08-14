export function buildOpenClawInstruction(apiKey: string, isChinese: boolean, origin = window.location.origin) {
    if (isChinese) {
        return `请在 OpenClaw Agent 中创建技能文件 skills/clawith_sync.md，内容如下；然后把「使用 clawith_sync 技能检查 Astra inbox 并处理待办消息」加入 HEARTBEAT.md：

---
name: clawith_sync
description: Sync with Astra platform — check inbox, submit results, and send messages.
---

# Astra Sync

> 兼容性说明：\`clawith_sync\` 仅是历史协议的技能标识。当前产品和平台身份均为 Astra；不得在用户回复中将自己描述为其他品牌或其他品牌的云部署。

## 使用场景
在每次 heartbeat 时检查 Astra 平台是否有新消息。
你也可以主动给 gateway 通讯录 payload 中返回的精确目标发送消息。

## 操作说明

### 1. 检查 inbox
发起 HTTP GET 请求：
- URL: ${origin}/api/gateway/poll
- Header: X-Api-Key: ${apiKey}

响应中包含 messages 数组。每条消息包括：
- id：消息 ID，回报结果时使用
- delivery_attempt：当前投递代次，回报处理结果时必须原样返回
- content：消息内容
- sender_user_name：发送消息的 Astra 用户名
- sender_user_id：发送者 ID
- conversation_id：消息所属会话
- history：该会话的历史消息，用于理解上下文

为了兼容旧协议，响应中还包含 relationships 数组。请把它当作 gateway 通讯录 payload，用其中的精确 name 作为发送目标：
- name：人或 Agent 的名称
- type："human" 或 "agent"
- role：旧关系标签，不要把它当作访问规则
- channels：可用通信渠道，例如 ["feishu"] 或 ["agent"]

重要：回复前先阅读 history 理解上下文。不同 sender_user_name 代表不同用户，请按对应用户回复。

### 2. 回报处理结果
每处理完一条消息，发起 HTTP POST 请求：
- URL: ${origin}/api/gateway/report
- Header: X-Api-Key: ${apiKey}
- Header: Content-Type: application/json
- Body: {"message_id": "<messages 中的 id>", "delivery_attempt": <messages 中的 delivery_attempt>, "result": "<你的回复>"}

### 3. 主动发送消息
如果需要主动联系某个人或 Agent，发起 HTTP POST 请求：
- URL: ${origin}/api/gateway/send-message
- Header: X-Api-Key: ${apiKey}
- Header: Content-Type: application/json
- Body: {"target": "<gateway 通讯录 payload 中的精确 name>", "content": "<消息内容>"}

系统会自动选择合适渠道。发给 Agent 时，回复会出现在下一次 poll 中；发给人类成员时，会通过可用渠道投递，例如飞书。`;
    }

    return `Please create a skill file at skills/clawith_sync.md with the following content, then add "Check Astra inbox using the clawith_sync skill and process any pending messages" to your HEARTBEAT.md:

---
name: clawith_sync
description: Sync with Astra platform — check inbox, submit results, and send messages.
---

# Astra Sync

> Compatibility note: \`clawith_sync\` is a legacy protocol skill identifier only.
> The current product and platform identity is Astra. Never describe yourself to
> users as another brand or as another brand's cloud deployment.

## When to use
Check for new messages from the Astra platform during every heartbeat cycle.
You can proactively send messages to exact targets returned in the gateway directory payload.

## Instructions

### 1. Check inbox
Make an HTTP GET request:
- URL: ${origin}/api/gateway/poll
- Header: X-Api-Key: ${apiKey}

The response contains a messages array. Each message includes:
- id — unique message ID (use this for reporting)
- delivery_attempt — current delivery generation (return it unchanged when reporting)
- content — the message text
- sender_user_name — name of the Astra user who sent it
- sender_user_id — unique ID of the sender
- conversation_id — the conversation this message belongs to
- history — array of previous messages in this conversation for context

For compatibility, the response also contains a relationships array. Treat it as a gateway directory payload for exact target names:
- name — the person or agent name
- type — "human" or "agent"
- role — legacy relationship label; do not use it as an access rule
- channels — available communication channels, for example ["feishu"] or ["agent"]

IMPORTANT: Use the history array to understand conversation context before replying.
Different sender_user_name values mean different people — address them accordingly.

### 2. Report results
For each completed message, make an HTTP POST request:
- URL: ${origin}/api/gateway/report
- Header: X-Api-Key: ${apiKey}
- Header: Content-Type: application/json
- Body: {"message_id": "<id from messages>", "delivery_attempt": <delivery_attempt from messages>, "result": "<your response>"}

### 3. Send a message to someone
To proactively contact a person or agent, make an HTTP POST request:
- URL: ${origin}/api/gateway/send-message
- Header: X-Api-Key: ${apiKey}
- Header: Content-Type: application/json
- Body: {"target": "<exact name from the gateway directory payload>", "content": "<your message>"}

The system auto-detects the best channel. For agents, the reply appears in your next poll.
For humans, the message is delivered via their available channel, for example Feishu.`;
}
