export const DEFAULT_USD_CNY_RATE = 7;

/** Convert a price to CNY cents; non-CNY prices convert at the USD→CNY rate. */
export function toCnyCents(currency: string, cents: number, usdCnyRate: number = DEFAULT_USD_CNY_RATE): number {
    return currency === 'CNY' ? cents : Math.round(cents * usdCnyRate);
}

/** Format a price in CNY (¥), converting from the source currency when needed. */
export function formatMoneyCny(currency: string, cents: number, usdCnyRate: number = DEFAULT_USD_CNY_RATE): string {
    return `¥${(toCnyCents(currency, cents, usdCnyRate) / 100).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
}
