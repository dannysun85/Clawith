import { useEffect, useState } from 'react';
import { toDataURL } from 'qrcode';

import './MfaQrCode.css';

interface MfaQrCodeProps {
    provisioningUri?: string;
    secret?: string;
    isChinese: boolean;
}

export function MfaQrCode({ provisioningUri, secret, isChinese }: MfaQrCodeProps) {
    const [qrDataUrl, setQrDataUrl] = useState('');
    const [qrFailed, setQrFailed] = useState(false);

    useEffect(() => {
        let cancelled = false;
        setQrDataUrl('');
        setQrFailed(false);

        if (!provisioningUri) return () => { cancelled = true; };

        void toDataURL(provisioningUri, {
            width: 220,
            margin: 2,
            color: { dark: '#16151d', light: '#ffffff' },
        }).then((dataUrl) => {
            if (!cancelled) setQrDataUrl(dataUrl);
        }).catch(() => {
            if (!cancelled) setQrFailed(true);
        });

        return () => { cancelled = true; };
    }, [provisioningUri]);

    if (!provisioningUri && !secret) return null;

    return (
        <div className="mfa-enrollment" data-testid="mfa-enrollment">
            <div className="mfa-enrollment__primary">
                <div className="mfa-enrollment__qr" aria-live="polite">
                    {qrDataUrl ? (
                        <img
                            src={qrDataUrl}
                            width={220}
                            height={220}
                            alt={isChinese ? '多因素验证器绑定二维码' : 'Authenticator enrollment QR code'}
                        />
                    ) : (
                        <span>
                            {qrFailed
                                ? (isChinese ? '二维码生成失败，请使用手工密钥。' : 'QR generation failed. Use the manual key.')
                                : (isChinese ? '正在生成二维码…' : 'Generating QR code…')}
                        </span>
                    )}
                </div>
                <div className="mfa-enrollment__instructions">
                    <strong>{isChinese ? '用验证器扫描' : 'Scan with an authenticator'}</strong>
                    <p>
                        {isChinese
                            ? '使用任意兼容 TOTP 的验证器应用扫描。二维码和密钥只保留在当前页面内存中，不会写入浏览器存储。'
                            : 'Scan with any TOTP-compatible authenticator. The QR code and key stay only in this page memory and are not written to browser storage.'}
                    </p>
                    {provisioningUri && (
                        <a href={provisioningUri}>
                            {isChinese ? '在验证器应用中打开' : 'Open in authenticator app'}
                        </a>
                    )}
                </div>
            </div>
            {secret && (
                <details className="mfa-enrollment__manual">
                    <summary>{isChinese ? '无法扫码？显示手工密钥' : 'Cannot scan? Show manual key'}</summary>
                    <code>{secret}</code>
                    <p>{isChinese ? '请勿通过聊天、邮件或截图分享此密钥。' : 'Do not share this key through chat, email, or screenshots.'}</p>
                </details>
            )}
        </div>
    );
}
