"""Local verdict cache + the pre-flight that provokes sharing hazards early."""

from __future__ import annotations

import json
from typing import Any

import pytest

from kiro_crew.mcp_discovery import McpServerInfo
from kiro_crew.mcp_gateway import preflight as pf
from kiro_crew.mcp_gateway import verdict_cache as vc


def _key(**over: Any) -> vc.CacheKey:
    base = {
        "server_name": "srv",
        "command_args_hash": "cmd1",
        "env_hash": "env1",
        "binary_version": "1.0.0",
    }
    base.update(over)
    return vc.CacheKey(**base)  # type: ignore[arg-type]


def _verdict(ran: bool = True, caller_sensitive: bool = False) -> vc.CachedPreflight:
    return vc.CachedPreflight(
        ran=ran,
        caller_sensitive=caller_sensitive,
        reasons=() if ran else (pf.REASON_PREFLIGHT_UNAVAILABLE,),
        evaluated_at=1.0,
    )


class TestCacheKeyInvalidation:
    """The key is the whole point: a stale hit is a wrong answer, not a slow one."""

    def test_round_trip(self, tmp_path) -> None:
        cache = vc.VerdictCache(vc.cache_path(tmp_path))
        cache.put(_key(), _verdict(ran=True, caller_sensitive=True))
        cache.flush()

        fresh = vc.load_cache(tmp_path)
        hit = fresh.get(_key())
        assert hit is not None and hit.caller_sensitive is True

    @pytest.mark.parametrize(
        "changed",
        [
            {"command_args_hash": "cmd2"},
            {"env_hash": "env2"},
            {"binary_version": "1.0.1"},
            {"server_name": "other"},
            {"schema": vc.SCHEMA + 1},
        ],
    )
    def test_any_identity_change_misses(self, tmp_path, changed: dict[str, Any]) -> None:
        """Upgrading the MCP, editing its env, or shipping a smarter pre-flight
        must all re-derive rather than inherit."""
        cache = vc.VerdictCache(vc.cache_path(tmp_path))
        cache.put(_key(), _verdict())
        assert cache.get(_key(**changed)) is None

    def test_prune_drops_removed_servers(self, tmp_path) -> None:
        """Without this the file grows once per config edit and never shrinks."""
        cache = vc.VerdictCache(vc.cache_path(tmp_path))
        cache.put(_key(), _verdict())
        cache.put(_key(server_name="gone"), _verdict())
        assert cache.prune_to({_key().as_str()}) == 1
        assert cache.get(_key()) is not None
        assert cache.get(_key(server_name="gone")) is None

    def test_prune_marks_dirty_so_the_removal_persists(self, tmp_path) -> None:
        cache = vc.VerdictCache(vc.cache_path(tmp_path))
        cache.put(_key(server_name="gone"), _verdict())
        cache.flush()
        cache.prune_to(set())
        cache.flush()
        assert len(vc.load_cache(tmp_path)) == 0


class TestCacheDegradesSafely:
    def test_absent_file_is_empty(self, tmp_path) -> None:
        assert len(vc.load_cache(tmp_path)) == 0

    def test_corrupt_file_is_empty(self, tmp_path) -> None:
        vc.cache_path(tmp_path).write_text("{{{", encoding="utf-8")
        assert len(vc.load_cache(tmp_path)) == 0

    def test_entry_that_cannot_say_whether_it_ran_is_dropped(self, tmp_path) -> None:
        vc.cache_path(tmp_path).write_text(
            json.dumps({"entries": {"k": {"reasons": ["x"]}}}), encoding="utf-8"
        )
        assert len(vc.load_cache(tmp_path)) == 0

    def test_flush_is_a_no_op_when_clean(self, tmp_path) -> None:
        vc.VerdictCache(vc.cache_path(tmp_path)).flush()
        assert not vc.cache_path(tmp_path).exists()


class _FakeProbe:
    """Stands in for ``probe_server``, answering per clientInfo name.

    Mutates the passed server the way the real probe does, so the pre-flight is
    exercised through the same interface it uses in production.
    """

    def __init__(self, answers: dict[str, tuple[str, dict[str, Any] | None]]) -> None:
        self.answers = answers
        self.identities: list[str] = []

    async def __call__(
        self, server: McpServerInfo, *, client_info: dict[str, str] | None = None
    ) -> McpServerInfo:
        name = (client_info or {}).get("name", "default")
        self.identities.append(name)
        status, caps = self.answers[name]
        server.status = status
        server.capabilities = caps
        if status != "ok":
            server.error = "boom"
        return server


@pytest.fixture
def patch_probe(monkeypatch: pytest.MonkeyPatch):
    def _install(answers: dict[str, tuple[str, dict[str, Any] | None]]) -> _FakeProbe:
        fake = _FakeProbe(answers)
        # Patch the CONSUMER namespace: preflight imports probe_server at module
        # scope, so it holds its own reference and patching the source module
        # would leave the real prober in place — the test would pass while
        # spawning nothing, or spawn for real.
        import kiro_crew.mcp_gateway.preflight as pf_mod

        monkeypatch.setattr(pf_mod, "probe_server", fake)
        return fake

    return _install


def _server() -> McpServerInfo:
    return McpServerInfo(name="srv", command="/bin/true")


class TestEvaluateOnlyWhatChanged:
    """The orchestration policy: pay for a measurement once, per identity."""

    @pytest.mark.asyncio
    async def test_cached_identity_is_not_re_provoked(self, patch_probe, tmp_path) -> None:
        from kiro_crew.mcp_gateway import evaluate as ev

        fake = patch_probe({"kirocrew-preflight-a": ("ok", {}), "mcp-inspector": ("ok", {})})
        server = _server()

        first = await ev.evaluate_new_servers([server], tmp_path)
        assert set(first) == {"srv"}
        spawns_after_first = len(fake.identities)
        assert spawns_after_first == 2, "a fresh server costs exactly two spawns"

        second = await ev.evaluate_new_servers([_server()], tmp_path)
        assert set(second) == {"srv"}
        assert len(fake.identities) == spawns_after_first, "cache hit must not spawn"

    @pytest.mark.asyncio
    async def test_changed_command_is_re_provoked(self, patch_probe, tmp_path) -> None:
        from kiro_crew.mcp_gateway import evaluate as ev

        fake = patch_probe({"kirocrew-preflight-a": ("ok", {}), "mcp-inspector": ("ok", {})})
        await ev.evaluate_new_servers([_server()], tmp_path)
        before = len(fake.identities)

        upgraded = McpServerInfo(name="srv", command="/bin/true", args=["--v2"])
        await ev.evaluate_new_servers([upgraded], tmp_path)
        assert len(fake.identities) > before, "an upgraded MCP must be re-measured"

    @pytest.mark.asyncio
    async def test_an_unavailable_server_is_re_provoked_next_pass(
        self, patch_probe, tmp_path
    ) -> None:
        """A pre-flight that could not run says nothing about the server.

        A missing credential, an unreachable tunnel, a binary mid-install: none
        of those change the execution identity, so caching the failure against it
        would freeze the server at ``unknown`` for good. It must cost the spawns
        again rather than become permanently unevaluated.
        """
        from kiro_crew.mcp_gateway import evaluate as ev

        fake = patch_probe(
            {"kirocrew-preflight-a": ("error", None), "mcp-inspector": ("error", None)}
        )

        first = await ev.evaluate_new_servers([_server()], tmp_path)
        assert first["srv"].ran is False, "the unavailable verdict is still reported"
        spawns = len(fake.identities)
        assert spawns > 0

        await ev.evaluate_new_servers([_server()], tmp_path)

        assert len(fake.identities) > spawns, "an unavailable result must not be cached"

    @pytest.mark.asyncio
    async def test_a_successful_verdict_is_still_cached(self, patch_probe, tmp_path) -> None:
        """The other side of the rule: only failure is exempt from caching."""
        from kiro_crew.mcp_gateway import evaluate as ev

        fake = patch_probe({"kirocrew-preflight-a": ("ok", {}), "mcp-inspector": ("ok", {})})
        await ev.evaluate_new_servers([_server()], tmp_path)
        spawns = len(fake.identities)

        await ev.evaluate_new_servers([_server()], tmp_path)

        assert len(fake.identities) == spawns

    @pytest.mark.asyncio
    async def test_disabled_server_is_never_spawned(self, patch_probe, tmp_path) -> None:
        """Probing IS the act consent gates; a disabled row must not be provoked."""
        from kiro_crew.mcp_gateway import evaluate as ev

        fake = patch_probe({"kirocrew-preflight-a": ("ok", {}), "mcp-inspector": ("ok", {})})
        server = _server()
        server.disabled = True
        known = await ev.evaluate_new_servers([server], tmp_path)
        assert known == {}
        assert fake.identities == []

    @pytest.mark.asyncio
    async def test_pass_budget_is_respected(self, patch_probe, tmp_path) -> None:
        """Twenty newly added MCPs must not cost forty spawns in one request."""
        from kiro_crew.mcp_gateway import evaluate as ev

        fake = patch_probe({"kirocrew-preflight-a": ("ok", {}), "mcp-inspector": ("ok", {})})
        servers = [McpServerInfo(name=f"s{i}", command="/bin/true") for i in range(20)]
        known = await ev.evaluate_new_servers(servers, tmp_path)
        assert len(known) == ev.MAX_EVALUATIONS_PER_PASS
        assert len(fake.identities) == 2 * ev.MAX_EVALUATIONS_PER_PASS

    @pytest.mark.asyncio
    async def test_removed_server_is_pruned_from_the_cache(self, patch_probe, tmp_path) -> None:
        from kiro_crew.mcp_gateway import evaluate as ev

        patch_probe({"kirocrew-preflight-a": ("ok", {}), "mcp-inspector": ("ok", {})})
        await ev.evaluate_new_servers([_server()], tmp_path)
        assert len(vc.load_cache(tmp_path)) == 1

        await ev.evaluate_new_servers([], tmp_path)
        assert len(vc.load_cache(tmp_path)) == 0

    def test_an_in_place_binary_upgrade_is_re_measured(self, tmp_path) -> None:
        """Same path, same args, new bytes — the measurement must not be reused.

        Without a binary fingerprint the key hits and the pre-flight never re-runs,
        so a binary that BECAME caller-sensitive would hand its first caller's
        ``initialize`` result to every co-tenant. The hazard ledger only fires
        after a session has already lost its tools.

        Synchronous on purpose: ``cache_key_for`` refuses to run on the event
        loop, and production reaches it through ``asyncio.to_thread``.
        """
        from kiro_crew.mcp_gateway.evaluate import cache_key_for

        exe = tmp_path / "server-bin"
        exe.write_text("#!/bin/sh\necho v1\n", encoding="utf-8")
        exe.chmod(0o755)
        srv = McpServerInfo(name="s", command=str(exe))
        before = cache_key_for(srv).as_str()

        exe.write_text("#!/bin/sh\necho v2-different-bytes\n", encoding="utf-8")
        after = cache_key_for(srv).as_str()

        assert before != after, "an in-place upgrade reused the old measurement"

    def test_env_is_hashed_by_the_same_helper_the_pool_uses(self, tmp_path) -> None:
        """So a rotating credential does not look like a different server here."""
        from kiro_crew.mcp_gateway.evaluate import cache_key_for

        a = McpServerInfo(name="s", command="/bin/true", env={"AWS_SECRET_ACCESS_KEY": "one"})
        b = McpServerInfo(name="s", command="/bin/true", env={"AWS_SECRET_ACCESS_KEY": "two"})
        assert cache_key_for(a).as_str() == cache_key_for(b).as_str()

        c = McpServerInfo(name="s", command="/bin/true", env={"REGION": "us-west-2"})
        assert cache_key_for(a).as_str() != cache_key_for(c).as_str()


class TestPreflight:
    @pytest.mark.asyncio
    async def test_identical_capabilities_pass(self, patch_probe) -> None:
        caps = {"tools": {"listChanged": True}}
        fake = patch_probe({"kirocrew-preflight-a": ("ok", caps), "mcp-inspector": ("ok", caps)})
        result = await pf.preflight(_server())
        assert result.ran and not result.caller_sensitive
        assert result.reasons == ()
        # Two DIFFERENT identities, or the check proves nothing.
        assert fake.identities == ["kirocrew-preflight-a", "mcp-inspector"]

    @pytest.mark.asyncio
    async def test_divergent_capabilities_are_caught(self, patch_probe) -> None:
        patch_probe(
            {
                "kirocrew-preflight-a": ("ok", {"tools": {}}),
                "mcp-inspector": ("ok", {"tools": {}, "resources": {"subscribe": True}}),
            }
        )
        result = await pf.preflight(_server())
        assert result.ran and result.caller_sensitive
        assert result.reasons == (pf.REASON_CALLER_SENSITIVE_INIT,)

    @pytest.mark.asyncio
    async def test_free_form_values_do_not_count_as_divergence(self, patch_probe) -> None:
        """A build id or session token in ``experimental`` is not caller sensitivity.

        Comparing raw dicts would flag every such server and make the check
        useless, so only the SHAPE is compared.
        """
        patch_probe(
            {
                "kirocrew-preflight-a": ("ok", {"experimental": {"buildId": "abc"}}),
                "mcp-inspector": ("ok", {"experimental": {"buildId": "zzz"}}),
            }
        )
        result = await pf.preflight(_server())
        assert result.ran and not result.caller_sensitive

    @pytest.mark.asyncio
    async def test_a_flipped_boolean_flag_does_count(self, patch_probe) -> None:
        """Flags ARE part of the contract a pooled backend must keep identical."""
        patch_probe(
            {
                "kirocrew-preflight-a": ("ok", {"resources": {"subscribe": True}}),
                "mcp-inspector": ("ok", {"resources": {"subscribe": False}}),
            }
        )
        result = await pf.preflight(_server())
        assert result.caller_sensitive

    @pytest.mark.asyncio
    async def test_unstartable_server_is_not_a_failure(self, patch_probe) -> None:
        """"Could not ask" must never collapse into "answered no".

        A server needing a credential this host lacks would otherwise be marked
        unshareable for ever.
        """
        patch_probe({"kirocrew-preflight-a": ("error", None), "mcp-inspector": ("ok", {})})
        result = await pf.preflight(_server())
        assert result.ran is False
        assert result.caller_sensitive is False
        assert result.reasons == (pf.REASON_PREFLIGHT_UNAVAILABLE,)

    @pytest.mark.asyncio
    async def test_answering_once_but_not_twice_is_also_unavailable(self, patch_probe) -> None:
        patch_probe({"kirocrew-preflight-a": ("ok", {}), "mcp-inspector": ("error", None)})
        result = await pf.preflight(_server())
        assert result.ran is False

    @pytest.mark.asyncio
    async def test_the_caller_s_server_object_is_never_mutated(self, patch_probe) -> None:
        """The dashboard is showing this object; a pre-flight must not touch it."""
        patch_probe({"kirocrew-preflight-a": ("ok", {}), "mcp-inspector": ("ok", {})})
        server = _server()
        server.status = "unknown"
        server.tools = ["kept"]
        await pf.preflight(server)
        assert server.status == "unknown"
        assert server.tools == ["kept"]
        assert server.capabilities is None
