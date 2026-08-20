"""Print a secret-free billing readiness report and optionally fail closed."""

from __future__ import annotations

import argparse
import json

from app.services.billing_provider import billing_provider_readiness


def readiness_payload() -> dict[str, object]:
    readiness = billing_provider_readiness()
    return {
        "provider": readiness.provider,
        "status": readiness.status,
        "checkout_enabled": readiness.checkout_enabled,
        "native_payment_enabled": readiness.native_payment_enabled,
        "webhook_ready": readiness.webhook_ready,
        "missing_config": list(readiness.missing_config),
        "issues": list(readiness.issues),
        "next_action": readiness.next_action,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--require-native-ready",
        action="store_true",
        help="exit non-zero unless a native payment provider is fully ready",
    )
    args = parser.parse_args()
    payload = readiness_payload()
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    if args.require_native_ready and not payload["native_payment_enabled"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
