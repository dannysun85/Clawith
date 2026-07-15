#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-verify}"
NETWORK="${2:-astra_network}"
SOURCE_CONTRACT="${3:-}"
CHAIN="ASTRA_MCP_EGRESS_V1"
JUMP_COMMENT="astra-mcp-egress-v1"
REPAIR_COMMENT="astra-mcp-egress-repair-v1"
INSTALL_SCRIPT="/usr/local/sbin/astra-mcp-egress-guard"
INSTALL_DIR="/etc/astra/security"
INSTALL_CONTRACT="$INSTALL_DIR/mcp-egress-v1.contract"
INSTALL_NETWORK="$INSTALL_DIR/mcp-egress-v1.network"
MARKER="$INSTALL_DIR/mcp-egress-v1.marker"

die() {
    echo "$1" >&2
    exit 1
}

require_root() {
    [ "$(id -u)" = "0" ] || die "MCP egress guard requires root"
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

require_cmd docker
require_cmd iptables
require_cmd sha256sum
require_cmd python3
require_cmd diff
require_cmd flock

acquire_process_lock() {
    exec 9>/run/lock/astra-mcp-egress-guard.lock
    flock -w 30 9 || die "another MCP egress guard operation is running"
}

if [ "$ACTION" = "ensure" ]; then
    NETWORK="$(tr -d '[:space:]' < "$INSTALL_NETWORK")"
    SOURCE_CONTRACT="$INSTALL_CONTRACT"
elif [ -z "$SOURCE_CONTRACT" ]; then
    SOURCE_CONTRACT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/deploy/security-contracts/mcp-egress-v1"
fi

[ -f "$SOURCE_CONTRACT" ] && [ ! -L "$SOURCE_CONTRACT" ] || \
    die "MCP egress contract must be a regular file"
case "$NETWORK" in
    ''|*[!A-Za-z0-9_.-]*) die "invalid Docker network name" ;;
esac

network_contract() {
    docker network inspect "$NETWORK" | python3 -c '
import ipaddress
import json
import sys

payload = json.load(sys.stdin)
if len(payload) != 1:
    raise SystemExit("Docker network lookup must return exactly one network")
network = payload[0]
if bool(network.get("EnableIPv6")):
    raise SystemExit("Docker IPv6 must remain disabled for MCP egress contract v1")
subnets = []
for item in (network.get("IPAM") or {}).get("Config") or []:
    raw = str(item.get("Subnet") or "").strip()
    if raw:
        parsed = ipaddress.ip_network(raw, strict=True)
        if parsed.version == 4:
            subnets.append(str(parsed))
if len(subnets) != 1:
    raise SystemExit("Docker application network must have exactly one IPv4 subnet")
print(subnets[0])
'
}

CONTRACT_HASH="$(sha256sum "$SOURCE_CONTRACT" | awk '{print $1}')"
SUBNET="$(network_contract)"

blocked_cidrs=(
    0.0.0.0/8
    10.0.0.0/8
    100.64.0.0/10
    127.0.0.0/8
    169.254.0.0/16
    172.16.0.0/12
    192.0.0.0/24
    192.0.2.0/24
    192.168.0.0/16
    198.18.0.0/15
    198.51.100.0/24
    203.0.113.0/24
    224.0.0.0/4
    240.0.0.0/4
)

write_expected_chain() {
    local path="$1"
    {
        printf '%s\n' "-N $CHAIN"
        printf '%s\n' "-A $CHAIN -d $SUBNET -m comment --comment astra-mcp-egress-internal-v1 -j RETURN"
        local cidr
        for cidr in "${blocked_cidrs[@]}"; do
            printf '%s\n' "-A $CHAIN -d $cidr -m comment --comment astra-mcp-egress-block-v1 -j REJECT --reject-with icmp-port-unreachable"
        done
        printf '%s\n' "-A $CHAIN -m comment --comment astra-mcp-egress-public-v1 -j RETURN"
    } > "$path"
}

verify_chain_body() {
    local expected
    local actual
    expected="$(mktemp)"
    actual="$(mktemp)"
    write_expected_chain "$expected"
    if ! iptables --wait 10 -S "$CHAIN" > "$actual" 2>/dev/null; then
        rm -f "$expected" "$actual"
        die "MCP egress chain is not installed"
    fi
    if ! diff -u "$expected" "$actual" >/dev/null; then
        rm -f "$expected" "$actual"
        die "MCP egress chain differs from the reviewed contract"
    fi
    rm -f "$expected" "$actual"
}

verify_jump_rules() {
    local expected_jump
    local first_jump
    local chain_jump_count
    local managed_jump_count
    expected_jump="-A DOCKER-USER -s $SUBNET -m comment --comment $JUMP_COMMENT -j $CHAIN"
    first_jump="$(iptables --wait 10 -S DOCKER-USER | awk '/^-A DOCKER-USER / {print; exit}')"
    [ "$first_jump" = "$expected_jump" ] || \
        die "MCP egress jump is missing or is not the first DOCKER-USER rule"
    chain_jump_count="$(
        iptables --wait 10 -S DOCKER-USER | awk -v target="$CHAIN" '
            $1 == "-A" && $2 == "DOCKER-USER" {
                for (index = 3; index <= NF; index += 1) {
                    if ($index == "-j" && $(index + 1) == target) count += 1
                }
            }
            END { print count + 0 }
        '
    )"
    [ "$chain_jump_count" = "1" ] || \
        die "MCP egress chain must have exactly one DOCKER-USER jump"
    managed_jump_count="$(
        iptables --wait 10 -S DOCKER-USER | \
            grep -Fc -- "--comment $JUMP_COMMENT" || true
    )"
    [ "$managed_jump_count" = "1" ] || \
        die "MCP egress managed jump must be unique"
}

verify_rules() {
    local repair_count
    verify_chain_body
    verify_jump_rules
    repair_count="$(
        iptables --wait 10 -S DOCKER-USER | \
            grep -Fc -- "--comment $REPAIR_COMMENT" || true
    )"
    [ "$repair_count" = "0" ] || \
        die "MCP egress repair fence was not removed"
}

verify_marker() {
    [ -f "$MARKER" ] && [ ! -L "$MARKER" ] || \
        die "MCP egress marker is missing"
    grep -Fxq "contract_sha256=$CONTRACT_HASH" "$MARKER" || \
        die "MCP egress marker contract hash does not match"
    grep -Fxq "network=$NETWORK" "$MARKER" || \
        die "MCP egress marker network does not match"
    grep -Fxq "subnet=$SUBNET" "$MARKER" || \
        die "MCP egress marker subnet does not match"
    [ "$(wc -l < "$MARKER" | tr -d '[:space:]')" = "3" ] || \
        die "MCP egress marker contains unexpected fields"
}

apply_rules() {
    require_root
    # The 30-second watchdog is verification-first. A healthy contract causes
    # no firewall writes or connection churn.
    if [ -f "$MARKER" ] && \
        (verify_marker && verify_rules) >/dev/null 2>&1; then
        return 0
    fi
    # Install a source-subnet REJECT before touching the active chain.  It
    # remains in place on every intermediate failure, so repair can interrupt
    # egress but can never create a permissive or half-built window.
    iptables --wait 10 -I DOCKER-USER 1 -s "$SUBNET" \
        -m comment --comment "$REPAIR_COMMENT" \
        -j REJECT --reject-with icmp-port-unreachable

    local rule_number
    while true; do
        rule_number="$(
            iptables --wait 10 -S DOCKER-USER | awk -v target="$CHAIN" '
                $1 == "-A" && $2 == "DOCKER-USER" { rule_number += 1 }
                $1 == "-A" && $2 == "DOCKER-USER" {
                    for (index = 3; index <= NF; index += 1) {
                        if ($index == "-j" && $(index + 1) == target) {
                            print rule_number
                            exit
                        }
                    }
                }
            '
        )"
        [ -n "$rule_number" ] || break
        iptables --wait 10 -D DOCKER-USER "$rule_number"
    done

    iptables --wait 10 -N "$CHAIN" 2>/dev/null || true
    iptables --wait 10 -F "$CHAIN"
    iptables --wait 10 -A "$CHAIN" -d "$SUBNET" \
        -m comment --comment astra-mcp-egress-internal-v1 -j RETURN
    local cidr
    for cidr in "${blocked_cidrs[@]}"; do
        iptables --wait 10 -A "$CHAIN" -d "$cidr" \
            -m comment --comment astra-mcp-egress-block-v1 \
            -j REJECT --reject-with icmp-port-unreachable
    done
    iptables --wait 10 -A "$CHAIN" \
        -m comment --comment astra-mcp-egress-public-v1 -j RETURN

    verify_chain_body
    iptables --wait 10 -I DOCKER-USER 1 -s "$SUBNET" \
        -m comment --comment "$JUMP_COMMENT" -j "$CHAIN"
    verify_jump_rules

    # The reviewed chain is now the first rule.  Keep the temporary fence
    # until the body and unique jump have both been verified, then remove all
    # repair fences left by this or an interrupted earlier attempt.
    while true; do
        rule_number="$(
            iptables --wait 10 -S DOCKER-USER | awk -v marker="$REPAIR_COMMENT" '
                $1 == "-A" && $2 == "DOCKER-USER" { rule_number += 1 }
                index($0, "--comment " marker) > 0 {
                    print rule_number
                    exit
                }
            '
        )"
        [ -n "$rule_number" ] || break
        iptables --wait 10 -D DOCKER-USER "$rule_number"
    done
    verify_rules

    install -d -m 0700 "$INSTALL_DIR"
    local temporary
    temporary="$(mktemp "$INSTALL_DIR/.mcp-egress-v1.marker.XXXXXX")"
    {
        printf 'contract_sha256=%s\n' "$CONTRACT_HASH"
        printf 'network=%s\n' "$NETWORK"
        printf 'subnet=%s\n' "$SUBNET"
    } > "$temporary"
    chmod 0600 "$temporary"
    mv -f "$temporary" "$MARKER"
    verify_marker
    verify_rules
}

install_watchdog() {
    require_root
    require_cmd install
    require_cmd systemctl
    install -d -m 0700 "$INSTALL_DIR"
    install -m 0755 "${BASH_SOURCE[0]}" "$INSTALL_SCRIPT"
    install -m 0600 "$SOURCE_CONTRACT" "$INSTALL_CONTRACT"
    printf '%s\n' "$NETWORK" > "$INSTALL_NETWORK"
    chmod 0600 "$INSTALL_NETWORK"
    cat > /etc/systemd/system/astra-mcp-egress-guard.service <<'UNIT'
[Unit]
Description=Astra MCP host egress guard
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/astra-mcp-egress-guard ensure
UNIT
    cat > /etc/systemd/system/astra-mcp-egress-guard.timer <<'TIMER'
[Unit]
Description=Continuously verify Astra MCP host egress guard

[Timer]
OnBootSec=30s
OnUnitActiveSec=30s
AccuracySec=5s
Unit=astra-mcp-egress-guard.service
Persistent=true

[Install]
WantedBy=timers.target
TIMER
    systemctl daemon-reload
    apply_rules
    systemctl enable --now astra-mcp-egress-guard.timer
}

case "$ACTION" in
    install)
        require_root
        acquire_process_lock
        install_watchdog
        ;;
    ensure|apply)
        require_root
        acquire_process_lock
        apply_rules
        ;;
    verify)
        require_root
        acquire_process_lock
        verify_marker
        verify_rules
        ;;
    *)
        die "usage: $0 {install|ensure|apply|verify} [docker-network] [contract-file]"
        ;;
esac
