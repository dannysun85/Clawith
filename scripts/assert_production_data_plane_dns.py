#!/usr/bin/env python3
"""Fail closed unless production postgres/redis DNS is unique and shared.

Read-only: docker network inspect, ps, inspect, and optional getent via docker exec.
Does not start, stop, or compose-up anything, and does not print secrets.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
import subprocess
import sys


ALIASES = ("postgres", "redis")
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_SERVICE_LABEL = "com.docker.compose.service"
IPV4 = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
SLOT_SUFFIXES = ("-app-a", "-app-b")


class DataPlaneError(RuntimeError):
    """Raised when the production data plane is missing, split, or not shared."""


def _run_docker(docker_bin: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [docker_bin, *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _require_safe_name(value: str, label: str) -> str:
    if not value or not SAFE_NAME.fullmatch(value):
        raise DataPlaneError(f"invalid {label}")
    return value


def _strip_env_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def read_docker_network(app_root: Path) -> str:
    env_path = app_root / "current" / ".env"
    if env_path.is_symlink() or not env_path.is_file():
        raise DataPlaneError("current release environment is missing or unsafe")
    network = ""
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() != "DOCKER_NETWORK":
            continue
        network = _strip_env_value(value)
    if not network:
        raise DataPlaneError("current release environment must define DOCKER_NETWORK")
    return _require_safe_name(network, "DOCKER_NETWORK")


def _load_json(payload: str, error: str) -> object:
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise DataPlaneError(error) from exc


def _inspect_network(docker_bin: str, network: str) -> tuple[str, str]:
    result = _run_docker(docker_bin, "network", "inspect", network)
    if result.returncode != 0:
        raise DataPlaneError("cannot inspect production app network")
    inspected = _load_json(result.stdout, "invalid docker network inspect payload")
    if not isinstance(inspected, list) or len(inspected) != 1 or not isinstance(inspected[0], dict):
        raise DataPlaneError("production app network inspect must return exactly one network")
    network_id = str(inspected[0].get("Id") or "")
    network_name = str(inspected[0].get("Name") or "")
    if not network_id or not network_name:
        raise DataPlaneError("production app network identity is incomplete")
    return network_name, network_id


def _ps_ids(docker_bin: str, *filters: str) -> list[str]:
    args = ["ps", "--no-trunc", "-q"]
    for item in filters:
        args.extend(["--filter", item])
    result = _run_docker(docker_bin, *args)
    if result.returncode != 0:
        raise DataPlaneError("cannot list production containers")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _inspect_containers(docker_bin: str, container_ids: list[str]) -> list[dict]:
    if not container_ids:
        return []
    result = _run_docker(docker_bin, "inspect", *container_ids)
    if result.returncode != 0:
        raise DataPlaneError("cannot inspect production containers")
    inspected = _load_json(result.stdout, "invalid docker inspect payload")
    if not isinstance(inspected, list):
        raise DataPlaneError("docker inspect payload must be a list")
    containers = []
    for item in inspected:
        if not isinstance(item, dict):
            raise DataPlaneError("docker inspect payload is malformed")
        containers.append(item)
    return containers


def _labels(container: dict) -> dict[str, str]:
    labels = (container.get("Config") or {}).get("Labels") or {}
    if not isinstance(labels, dict):
        return {}
    return {str(key): str(value) for key, value in labels.items()}


def _container_name(container: dict) -> str:
    name = str(container.get("Name") or "").lstrip("/")
    if name:
        return name
    container_id = str(container.get("Id") or "")
    return container_id[:12] or "unknown"


def _endpoint_for_network(container: dict, network_name: str, network_id: str) -> dict | None:
    networks = (container.get("NetworkSettings") or {}).get("Networks") or {}
    if not isinstance(networks, dict):
        return None
    direct = networks.get(network_name)
    if isinstance(direct, dict):
        return direct
    for endpoint in networks.values():
        if not isinstance(endpoint, dict):
            continue
        endpoint_id = str(endpoint.get("NetworkID") or "")
        if endpoint_id == network_id or (network_id and endpoint_id.startswith(network_id)):
            return endpoint
    return None


def _alias_matches(container: dict, network_name: str, network_id: str, alias: str) -> dict | None:
    endpoint = _endpoint_for_network(container, network_name, network_id)
    if endpoint is None:
        return None
    aliases = endpoint.get("Aliases") or []
    if not isinstance(aliases, list) or alias not in aliases:
        return None
    ip_address = str(endpoint.get("IPAddress") or "")
    if not IPV4.fullmatch(ip_address):
        raise DataPlaneError(f"{alias} endpoint is missing a usable IPv4 address")
    labels = _labels(container)
    return {
        "name": _container_name(container),
        "ip": ip_address,
        "project": labels.get(COMPOSE_PROJECT_LABEL, ""),
        "service": labels.get(COMPOSE_SERVICE_LABEL, ""),
        "id": str(container.get("Id") or ""),
    }


def _ipv4_addresses(payload: str) -> list[str]:
    found: list[str] = []
    for line in payload.splitlines():
        parts = line.split()
        if parts and IPV4.fullmatch(parts[0]) and parts[0] not in found:
            found.append(parts[0])
    return found


def _getent_ips(docker_bin: str, container_id: str, alias: str) -> list[str] | None:
    result = _run_docker(docker_bin, "exec", container_id, "getent", "hosts", alias)
    combined = f"{result.stdout}\n{result.stderr}"
    if result.returncode == 127 or "not found" in combined.lower() or "executable file not found" in combined.lower():
        return None
    if result.returncode != 0:
        raise DataPlaneError(f"live backend cannot resolve {alias!r} on the production app network")
    addresses = _ipv4_addresses(result.stdout)
    if not addresses:
        raise DataPlaneError(f"live backend resolved 0 IPv4 addresses for {alias!r}")
    return addresses


def _slot_projects(compose_project: str) -> set[str]:
    return {f"{compose_project}{suffix}" for suffix in SLOT_SUFFIXES}


def assert_unique_shared_data_plane(
    *,
    docker_bin: str,
    compose_project: str,
    network: str,
    aliases: tuple[str, ...] = ALIASES,
) -> None:
    compose_project = _require_safe_name(compose_project, "compose project")
    network = _require_safe_name(network, "Docker network")
    network_name, network_id = _inspect_network(docker_bin, network)
    attached_ids = _ps_ids(docker_bin, f"network={network}")
    if not attached_ids:
        raise DataPlaneError("production app network has no running containers")
    attached = _inspect_containers(docker_bin, attached_ids)
    backends = [
        container
        for container in attached
        if _labels(container).get(COMPOSE_SERVICE_LABEL) == "backend"
        and _endpoint_for_network(container, network_name, network_id) is not None
    ]
    if not backends:
        raise DataPlaneError("production app network has no live backend from which to resolve data-plane DNS")

    slot_projects = _slot_projects(compose_project)
    for alias in aliases:
        service_ids = _ps_ids(docker_bin, f"label={COMPOSE_SERVICE_LABEL}={alias}")
        for container in _inspect_containers(docker_bin, service_ids):
            project = _labels(container).get(COMPOSE_PROJECT_LABEL, "")
            name = _container_name(container)
            if project != compose_project:
                if project in slot_projects:
                    raise DataPlaneError(
                        f"slot compose must not publish {alias} DNS ({name}); "
                        f"shared data plane belongs to {compose_project}"
                    )
                raise DataPlaneError(
                    f"shared {alias} must belong to compose project {compose_project}, "
                    f"not {project or 'unknown'} ({name})"
                )

        matches = []
        for container in attached:
            match = _alias_matches(container, network_name, network_id, alias)
            if match is not None:
                matches.append(match)
        names = ", ".join(item["name"] for item in matches) or "none"
        if len(matches) != 1:
            raise DataPlaneError(
                f"production data plane must have exactly one {alias!r} alias on {network_name}; "
                f"found {len(matches)} ({names})"
            )
        ips = {item["ip"] for item in matches}
        if len(ips) != 1:
            raise DataPlaneError(
                f"production data plane {alias!r} resolves to {len(ips)} addresses on {network_name}"
            )
        match = matches[0]
        if match["project"] != compose_project:
            raise DataPlaneError(
                f"shared {alias} must belong to compose project {compose_project}, "
                f"not {match['project'] or 'unknown'}"
            )
        if match["service"] != alias:
            raise DataPlaneError(
                f"shared {alias} service label must be {alias}, not {match['service'] or 'unknown'}"
            )
        expected_ip = match["ip"]
        for backend in backends:
            backend_id = str(backend.get("Id") or "")
            if not backend_id:
                raise DataPlaneError("live backend identity is incomplete")
            getent_ips = _getent_ips(docker_bin, backend_id, alias)
            if getent_ips is None:
                continue
            unique_ips = list(dict.fromkeys(getent_ips))
            if len(unique_ips) != 1:
                raise DataPlaneError(
                    f"live backend resolved {len(unique_ips)} addresses for {alias!r} on {network_name}"
                )
            if unique_ips[0] != expected_ip:
                raise DataPlaneError(
                    f"live backend {alias!r} address does not match the unique container endpoint"
                )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fail a production release unless postgres/redis DNS is unique and shared."
    )
    parser.add_argument("--app-root")
    parser.add_argument("--compose-project", required=True)
    parser.add_argument("--network")
    parser.add_argument("--expected-network")
    parser.add_argument("--docker", default="docker")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.network:
            network = _require_safe_name(args.network, "Docker network")
        elif args.app_root:
            network = read_docker_network(Path(args.app_root))
        else:
            raise DataPlaneError("either --app-root or --network is required")
        if args.expected_network and args.expected_network != network:
            raise DataPlaneError("DOCKER_NETWORK does not match the expected production app network")
        if args.app_root and args.network:
            env_network = read_docker_network(Path(args.app_root))
            if env_network != args.network:
                raise DataPlaneError("DOCKER_NETWORK does not match the expected production app network")
        assert_unique_shared_data_plane(
            docker_bin=args.docker,
            compose_project=args.compose_project,
            network=network,
        )
    except DataPlaneError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
