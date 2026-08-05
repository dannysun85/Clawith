import { describe, expect, it } from 'vitest';

import { parseHttpError } from './apiError';

describe('parseHttpError', () => {
    it('surfaces safe field validation details instead of a generic message', () => {
        const error = parseHttpError({
            status: 422,
            bodyText: JSON.stringify({
                detail: [
                    {
                        type: 'value_error',
                        loc: ['body', 'username'],
                        msg: 'Value error, Username cannot be an email address or phone number',
                    },
                ],
                error: {
                    code: 'validation_error',
                    message: 'Request validation failed',
                    details: [
                        {
                            type: 'value_error',
                            loc: ['body', 'username'],
                            msg: 'Value error, Username cannot be an email address or phone number',
                        },
                    ],
                },
            }),
        });

        expect(error.message).toBe(
            '用户名: Value error, Username cannot be an email address or phone number',
        );
        expect(error.code).toBe('validation_error');
    });
});
