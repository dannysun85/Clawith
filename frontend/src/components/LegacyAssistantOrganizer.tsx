import { IconMessage, IconRefresh, IconUsersGroup } from '@tabler/icons-react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router';

import type { Agent } from '../types';

export type LegacyAssistantAction = 'archive' | 'convert_to_employee' | 'restore_history';

interface LegacyAssistantOrganizerProps {
    agents: Agent[];
    busyAgentId: string | null;
    onAction: (agent: Agent, action: LegacyAssistantAction) => void;
}

function dispositionLabel(agent: Agent, isChinese: boolean): string {
    if (agent.legacy_assistant_disposition === 'archived') {
        return isChinese ? '已归档' : 'Archived';
    }
    if (agent.legacy_assistant_disposition === 'converted') {
        return isChinese ? '已转为员工' : 'Converted employee';
    }
    return isChinese ? '历史可用' : 'History available';
}

export default function LegacyAssistantOrganizer({
    agents,
    busyAgentId,
    onAction,
}: LegacyAssistantOrganizerProps) {
    const { i18n } = useTranslation();
    const navigate = useNavigate();
    const isChinese = i18n.language?.startsWith('zh') ?? false;

    if (agents.length === 0) return null;

    return (
        <section className="legacy-assistant-organizer" aria-labelledby="legacy-assistant-title">
            <header>
                <div>
                    <span className="legacy-assistant-organizer__eyebrow">
                        {isChinese ? '兼容数据整理' : 'Compatibility cleanup'}
                    </span>
                    <h2 id="legacy-assistant-title">{isChinese ? '历史助理整理' : 'Previous assistant cleanup'}</h2>
                    <p>
                        {isChinese
                            ? '这些对象来自旧版本。只有原创建者能整理；旧对话、文件、Workspace、Agent ID 和深链始终保留。'
                            : 'These objects come from earlier versions. Only the original creator can organize them; conversations, files, Workspace, Agent ID, and deep links are preserved.'}
                    </p>
                </div>
                <span className="legacy-assistant-organizer__count">{agents.length}</span>
            </header>
            <div className="legacy-assistant-organizer__list">
                {agents.map((agent) => {
                    const disposition = agent.legacy_assistant_disposition || 'active';
                    const busy = busyAgentId === agent.id;
                    return (
                        <article key={agent.id} className="legacy-assistant-organizer__item">
                            <div className="legacy-assistant-organizer__identity">
                                <span className="employee-directory__avatar" aria-hidden="true">
                                    {agent.avatar_url
                                        ? <img src={agent.avatar_url} alt="" />
                                        : Array.from(agent.name.trim())[0]?.toUpperCase() || 'A'}
                                </span>
                                <span>
                                    <strong>{agent.name}</strong>
                                    <small>{dispositionLabel(agent, isChinese)}</small>
                                </span>
                            </div>
                            <p>
                                {disposition === 'converted'
                                    ? (isChinese ? '当前计入数字员工名额；可撤回到仅自己可见的历史记录。' : 'Currently reserves an employee seat. It can be returned to private history.')
                                    : disposition === 'archived'
                                        ? (isChinese ? '已停止执行并从侧栏隐藏，不占数字员工名额。' : 'Execution is stopped and it is hidden from the sidebar without using an employee seat.')
                                        : (isChinese ? '可继续查看旧内容，不属于当前“我的助理”，也不占数字员工名额。' : 'Old content remains available. This is neither the current assistant nor an employee seat.')}
                            </p>
                            <div className="legacy-assistant-organizer__actions">
                                <button type="button" className="btn btn-secondary" onClick={() => navigate(`/agents/${agent.id}/chat`)}>
                                    <IconMessage size={15} />
                                    {isChinese ? '查看历史' : 'View history'}
                                </button>
                                {disposition === 'active' && (
                                    <button type="button" className="btn btn-ghost" disabled={busy} onClick={() => onAction(agent, 'archive')}>
                                        {isChinese ? '归档' : 'Archive'}
                                    </button>
                                )}
                                {disposition !== 'converted' && (
                                    <button type="button" className="btn btn-ghost" disabled={busy} onClick={() => onAction(agent, 'convert_to_employee')}>
                                        <IconUsersGroup size={15} />
                                        {isChinese ? '转为员工' : 'Convert to employee'}
                                    </button>
                                )}
                                {disposition !== 'active' && (
                                    <button type="button" className="btn btn-ghost" disabled={busy} onClick={() => onAction(agent, 'restore_history')}>
                                        <IconRefresh size={15} />
                                        {disposition === 'converted'
                                            ? (isChinese ? '撤回为历史助理' : 'Return to history')
                                            : (isChinese ? '恢复历史入口' : 'Restore history entry')}
                                    </button>
                                )}
                            </div>
                        </article>
                    );
                })}
            </div>
        </section>
    );
}
