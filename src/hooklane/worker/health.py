"""Role-specific Kubernetes probe commands for the worker container."""

from __future__ import annotations

import argparse
import asyncio

from redis.asyncio import Redis
from redis.exceptions import RedisError

from hooklane.runtime_config import redis_config_from_environment

GROUP_NAME = "hooklane-workers"
STREAM_NAME = "hooklane:events"


async def check(mode: str) -> bool:
    if mode == "live":
        return True
    client = Redis.from_url(
        redis_config_from_environment().value,
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=1,
    )
    try:
        if not await client.ping():
            return False
        if mode == "startup":
            return True
        groups = await client.xinfo_groups(STREAM_NAME)
        return any(group.get("name") == GROUP_NAME for group in groups)
    except RedisError:
        return False
    finally:
        await client.aclose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("startup", "ready", "live"))
    return parser.parse_args()


def main() -> int:
    return 0 if asyncio.run(check(parse_args().mode)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
