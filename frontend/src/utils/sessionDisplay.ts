export function displaySessionTitle(title: string, createdAt: string, locale: string, timeZone?: string): string {
    if (!/^Session \d{2}-\d{2} \d{2}:\d{2}$/.test(title)) return title;
    const created = new Date(createdAt);
    if (Number.isNaN(created.getTime())) return title;
    const parts = new Intl.DateTimeFormat(locale, {
        month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false, timeZone,
    }).formatToParts(created);
    const value = (type: Intl.DateTimeFormatPartTypes) => parts.find(part => part.type === type)?.value || '';
    return `Session ${value('month')}-${value('day')} ${value('hour')}:${value('minute')}`;
}
