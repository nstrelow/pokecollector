#!/usr/bin/env python3
"""Fork-only: fail when docker-compose.coolify.yml drifts from docker-compose.yml.

Coolify deploys docker-compose.coolify.yml, a single-file copy of the upstream
stack. An upstream change to a service's environment, volumes or published
ports would otherwise be silently missing from the deployed stack.
"""
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
# Set only in the Coolify file (timezone pin) or deliberately different there.
FORK_ONLY_ENV = {"TZ"}
# Upstream's lorcana-prices source is a host-dir variable; Coolify gets a plain bind.
VOLUME_ALIASES = {
    "${LORCANA_PRICES_HOST_DIR:-./data/lorcana-prices}:/app/data/lorcana-prices:ro":
        "./data/lorcana-prices:/app/data/lorcana-prices:ro",
}


def load(name):
    return yaml.safe_load((ROOT / name).read_text())["services"]


def env(service):
    value = service.get("environment") or {}
    if isinstance(value, list):
        value = dict(item.split("=", 1) for item in value)
    return {k: str(v) for k, v in value.items() if k not in FORK_ONLY_ENV}


def main():
    upstream, coolify = load("docker-compose.yml"), load("docker-compose.coolify.yml")
    problems = []
    if set(upstream) != set(coolify):
        problems.append(f"services: upstream {sorted(upstream)} vs coolify {sorted(coolify)}")
    for name in sorted(set(upstream) & set(coolify)):
        up, co = upstream[name], coolify[name]
        up_env, co_env = env(up), env(co)
        if name == "postgres":
            # The Coolify stack requires a real password; no "changeme" fallback.
            up_env.pop("POSTGRES_PASSWORD", None)
            co_env.pop("POSTGRES_PASSWORD", None)
        if name == "backend":
            up_env.pop("DATABASE_URL", None)
            co_env.pop("DATABASE_URL", None)
        for key in sorted(set(up_env) | set(co_env)):
            if up_env.get(key) != co_env.get(key):
                problems.append(f"{name}.environment.{key}: upstream {up_env.get(key)!r} vs coolify {co_env.get(key)!r}")
        up_vols = [VOLUME_ALIASES.get(v, v) for v in up.get("volumes", [])]
        if up_vols != co.get("volumes", []):
            problems.append(f"{name}.volumes: upstream {up_vols} vs coolify {co.get('volumes', [])}")
        if up.get("ports", []) != co.get("ports", []):
            problems.append(f"{name}.ports: upstream {up.get('ports')} vs coolify {co.get('ports')}")
    if problems:
        print("docker-compose.coolify.yml is out of step with docker-compose.yml:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print("docker-compose.coolify.yml matches docker-compose.yml")
    return 0


if __name__ == "__main__":
    sys.exit(main())
