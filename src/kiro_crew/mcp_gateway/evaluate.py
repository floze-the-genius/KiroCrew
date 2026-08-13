"""Run the pre-flight for servers whose verdict is missing or stale, and cache it.

This is the orchestration the layers below deliberately do not do: ``preflight``
knows how to provoke one server, ``verdict_cache`` knows how to remember, and
``shareability`` knows how to judge. This module decides WHICH servers are worth
paying for, which is a policy question and belongs in one place.

The policy: evaluate only what changed. A server whose execution identity
already has a cached verdict is skipped, so the steady-state cost of the whole
feature is a file read. A newly installed or upgraded MCP costs two spawns,
once.

Never called while rendering anything. The pre-flight spawns processes, so this
runs from the explicit probe action the operator already triggers — the same
action that already spawns every configured server.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from kiro_crew.mcp_gateway.hashing import hash_command, hash_effective_env
from kiro_crew.mcp_gateway.preflight import preflight
from kiro_crew.mcp_gateway.stub import binary_fingerprint
from kiro_crew.mcp_gateway.verdict_cache import (
    CachedPreflight,
    CacheKey,
    VerdictCache,
    load_cache,
    now,
)

logger = logging.getLogger(__name__)

#: Ceiling on how many servers one pass will provoke. A machine that just had
#: twenty MCPs added should not spend forty spawns inside one request; the rest
#: are evaluated on the next pass, and until then they read as ``unknown``,
#: which is the honest answer.
MAX_EVALUATIONS_PER_PASS = 8


def _assert_off_loop(what: str) -> None:
    """Raise if called from the event loop thread.

    The blocking work below is reached from a request handler, so an
    accidentally synchronous call does not fail — it stalls every chat sharing
    that loop for as long as the disk takes, which is invisible in tests and
    surfaces only as a starvation failure under load. Raising converts that into
    an immediate, attributable error.

    Inside ``asyncio.to_thread`` there is no running loop in the worker thread,
    so the correct call path passes, as does any synchronous caller.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(f"{what} does blocking IO and must not run on the event loop")


def cache_key_for(server: Any) -> CacheKey:
    """Execution identity of *server*, using the hashes ``PoolKey`` is built from.

    Env is hashed by the same helper the pool uses, so the rotating-secret
    exclusions apply identically — a credential rotation must not look like a
    different server here either.

    ``binary_version`` is the pool's own fingerprint of the resolved binary, not
    a placeholder. An in-place upgrade keeps the command and env identical, so
    without it the cache would hit and the pre-flight would never re-run: a
    binary that BECAME caller-sensitive would ship its first caller's
    ``initialize`` result to everyone else. Deferring that to the hazard ledger
    was the wrong trade — the ledger only fires after a session has already lost
    its tools, which is the outcome this whole feature exists to prevent.

    BLOCKING IO: resolving the binary walks ``PATH``, stats it, and hashes
    bounded content, once per server. Call it from a worker thread.
    """
    _assert_off_loop("cache_key_for")
    args = list(server.args or [])
    env = getattr(server, "env", None)
    return CacheKey(
        server_name=server.name,
        command_args_hash=hash_command(server.command, args),
        env_hash=hash_effective_env(
            {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}
        ),
        binary_version=binary_fingerprint(server.command),
    )


def _load_and_key(
    servers: list[Any], runtime_dir: Path
) -> tuple[VerdictCache, dict[str, CacheKey]]:
    """Read the cache and derive every execution identity in one worker thread.

    These are one step, not two: the keys are what the cache is looked up by, and
    deriving them is the more expensive half — a ``PATH`` walk plus a bounded
    content hash per server, against a single file read. Splitting them across
    the loop boundary is what left the costlier half on the loop.
    """
    return load_cache(runtime_dir), {s.name: cache_key_for(s) for s in servers}


async def evaluate_new_servers(servers: list[Any], runtime_dir: Path) -> dict[str, CachedPreflight]:
    """Pre-flight the servers with no cached verdict; return every known verdict.

    *servers* are ``McpServerInfo`` objects. Returns name -> verdict for every
    server that has one, cached or freshly derived, so a caller can render
    without a second lookup.

    Every filesystem touch here is offloaded: this runs inside a request handler
    on the gateway's event loop, and a slow disk would otherwise stall every chat
    sharing that loop, not just this probe.
    """
    cache, keys = await asyncio.to_thread(_load_and_key, servers, runtime_dir)
    cache.prune_to({k.as_str() for k in keys.values()})

    known: dict[str, CachedPreflight] = {}
    budget = MAX_EVALUATIONS_PER_PASS
    for server in servers:
        key = keys[server.name]
        hit = cache.get(key)
        if hit is not None:
            known[server.name] = hit
            continue
        if getattr(server, "disabled", False) or not getattr(server, "command", ""):
            # A disabled server must not be spawned (probing is the act consent
            # gates), and a server with no command has no stdio pipe to stub.
            continue
        if budget <= 0:
            continue
        budget -= 1
        result = await preflight(server)
        verdict = CachedPreflight(
            ran=result.ran,
            caller_sensitive=result.caller_sensitive,
            reasons=result.reasons,
            evaluated_at=now(),
        )
        if result.ran:
            cache.put(key, verdict)
        else:
            # A pre-flight that could not run says nothing about the server, only
            # about the moment: a missing credential, an unreachable tunnel, a
            # binary mid-install. Caching that keys the failure to an execution
            # identity that has not changed, so the server would never be
            # re-evaluated once the condition clears. Report it for this pass and
            # pay the spawn again next time.
            logger.info("shareability: %s could not be evaluated yet", server.name)
        known[server.name] = verdict
        logger.info(
            "shareability: evaluated %s -> ran=%s caller_sensitive=%s",
            server.name, result.ran, result.caller_sensitive,
        )

    await asyncio.to_thread(cache.flush)
    return known
