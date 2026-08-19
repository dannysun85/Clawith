import { describe, expect, it } from 'vitest';
import {
    buildPaymentDomainRedirectUrl,
    needsPaymentDomainRedirect,
    normalizeHostname,
} from './paymentCheckout';

describe('payment domain checkout redirect', () => {
    it('treats product aliases as off the payment host', () => {
        expect(normalizeHostname('opc.rama-server.com:443')).toBe('opc.rama-server.com');
        expect(needsPaymentDomainRedirect('opc.rama-server.com', 'opc.reeftotem.ai')).toBe(true);
        expect(needsPaymentDomainRedirect('opc.rama-server.com', 'opc.rama-server.com')).toBe(false);
        expect(needsPaymentDomainRedirect(null, 'opc.reeftotem.ai')).toBe(false);
    });

    it('moves the market page onto the payment origin with a session fragment', () => {
        const url = buildPaymentDomainRedirectUrl({
            paymentHost: 'opc.rama-server.com',
            currentHref: 'https://opc.reeftotem.ai/company-admin/market?tab=boost',
            sessionToken: 'header.payload.signature',
        });

        expect(url).toBe(
            'https://opc.rama-server.com/company-admin/market?tab=boost#session_token=header.payload.signature',
        );
    });

    it('keeps loopback protocol and port for local development', () => {
        const url = buildPaymentDomainRedirectUrl({
            paymentHost: 'localhost',
            currentHref: 'http://127.0.0.1:3008/company-admin/market',
            sessionToken: 'dev.jwt.token',
        });

        expect(url).toBe('http://localhost:3008/company-admin/market#session_token=dev.jwt.token');
    });

    it('forces https when leaving loopback for the public payment host', () => {
        const url = buildPaymentDomainRedirectUrl({
            paymentHost: 'opc.rama-server.com',
            currentHref: 'http://127.0.0.1:3008/company-admin/market',
            sessionToken: 'dev.jwt.token',
        });

        expect(url).toBe('https://opc.rama-server.com/company-admin/market#session_token=dev.jwt.token');
    });
});
