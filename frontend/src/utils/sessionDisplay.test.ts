import { describe, expect, it } from 'vitest';
import { displaySessionTitle } from './sessionDisplay';

describe('displaySessionTitle', () => {
    it('renders legacy UTC-generated titles in the requested local timezone', () => {
        expect(displaySessionTitle('Session 07-04 06:24', '2026-07-04T06:24:00Z', 'zh-CN', 'Asia/Shanghai'))
            .toBe('Session 07-04 14:24');
    });

    it('preserves user-defined titles', () => {
        expect(displaySessionTitle('客户跟进', '2026-07-04T06:24:00Z', 'zh-CN')).toBe('客户跟进');
    });

    it('preserves malformed timestamps', () => {
        expect(displaySessionTitle('Session 07-04 06:24', 'invalid', 'zh-CN')).toBe('Session 07-04 06:24');
    });
});
