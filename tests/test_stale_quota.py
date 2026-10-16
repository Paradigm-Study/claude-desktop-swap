"""Offline regressions against a temporary patched upstream module.

Install claude-swap==0.26.0 in an isolated environment to enable these tests.
No installed source, credentials, API, or running application is changed.
"""
import importlib.metadata
import importlib.util
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import cswap_quota_patch as patcher

try:
    distribution = importlib.metadata.distribution("claude-swap")
except importlib.metadata.PackageNotFoundError:
    raise unittest.SkipTest("Optional claude-swap==0.26.0 is not installed")
if distribution.version != patcher.SUPPORTED_VERSION:
    raise unittest.SkipTest("Optional regressions require claude-swap==0.26.0")
source_path = Path(distribution.locate_file("claude_swap/autoswitch.py"))
source_bytes = source_path.read_bytes()
if patcher.sha256(source_bytes) == patcher.PATCHED_SHA256:
    patched_bytes = source_bytes
else:
    patched_bytes = patcher.patch_bytes(source_bytes, patcher.DEFAULT_PATCH.read_bytes())
# Import only a temporary copy. Dependencies still come from the test environment.
with tempfile.TemporaryDirectory() as module_root:
    module_path = Path(module_root) / "autoswitch.py"
    module_path.write_bytes(patched_bytes)
    spec = importlib.util.spec_from_file_location("claude_swap.autoswitch", module_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
from claude_swap.settings import AutoSwitchSettings
from claude_swap.usage_store import UsageEntry, _row_eligible

NOW = 2_000_000_000.0


def entry(pct=20, age=0, error=None, sentinel=None):
    return UsageEntry(
        last_good={"five_hour": {"pct": pct, "resets_at": datetime.fromtimestamp(
            NOW + 3600, timezone.utc).isoformat()}, "seven_day": {"pct": 10}},
        fetched_at=NOW-age, age_s=age, sentinel=sentinel,
        last_error=error, consecutive_failures=1 if error else 0,
        backoff_until=NOW+3600 if error else None,
        next_poll_at=NOW+180, poll_interval_s=180, trust_extended=True,
    )


class FakeSwitcher:
    def __init__(self, root, rows, clock):
        self.backup_dir, self.rows, self.clock = root, rows, clock
        self.active, self.switches, self.collections = "1", [], []
        self.on_collect = None

    def set_poll_policy_inputs(self, *_): pass
    def current_account_number(self): return self.active
    def account_email(self, n): return f"account-{n}@example.test"
    def account_kind_for(self, n): return "oauth"
    def switchable_account_numbers(self): return list(self.rows)

    def usage_entries_by_account(self, fetch=None, scheduled=False):
        self.collections.append((set(fetch or ()), scheduled))
        if self.on_collect: self.on_collect(len(self.collections))
        return {n: replace(e, age_s=self.clock()-e.fetched_at)
                for n, e in self.rows.items()}

    def switch_to(self, n, **_):
        old, self.active = self.active, n
        self.switches.append((old, n))
        return {"switched": True, "from": {"number": int(old)},
                "to": {"number": int(n)}}


class OfflineEngine(mod.AutoSwitchEngine):
    def _freshen_target(self, number, email):
        # This fixture must never read or refresh real credentials.
        if getattr(self, "freshen_action", None): self.freshen_action(number)
        return "ok"


class StaleQuotaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now, self.events = [NOW], []

    def engine(self, rows, **settings):
        switcher = FakeSwitcher(Path(self.temp.name), rows, lambda: self.now[0])
        engine = OfflineEngine(switcher, AutoSwitchSettings(**settings),
                               self.events.append, clock=lambda: self.now[0])
        return engine, switcher

    def test_stale_active_fails_over_after_three_ticks(self):
        e, s = self.engine({"1": entry(34, 301, "http-429"), "2": entry(20)})
        self.assertEqual([e.tick(), e.tick(), e.tick()],
                         [mod.TickOutcome.NO_ACTION]*2 + [mod.TickOutcome.SWITCHED])
        self.assertEqual(s.switches, [("1", "2")])
        self.assertEqual(e._read_state()["leftTrigger"], "failover")

    def test_fresh_429_no_early_failover(self):
        for age in (0, 299, 300):
            e, s = self.engine({"1": entry(34, age, "http-429"), "2": entry(20)})
            self.assertTrue(all(e.tick() == mod.TickOutcome.NO_ACTION for _ in range(4)))
            self.assertEqual(s.switches, [])

    def test_all_stale_does_not_rotate(self):
        e, s = self.engine({"1": entry(34, 301, "http-429"),
                            "2": entry(0, 400, "http-429")})
        results = [e.tick() for _ in range(12)]
        self.assertEqual(results[2:], [mod.TickOutcome.BLOCKED]*10)
        self.assertEqual(s.switches, [])

    def test_stale_best_candidate_does_not_hide_fresh_peer(self):
        e, s = self.engine({"1": entry(34, 301, "http-429"),
                            "2": entry(0, 181, "http-429"), "3": entry(30)},
                           unhealthy_ticks=1)
        self.assertEqual(e.tick(), mod.TickOutcome.SWITCHED)
        self.assertEqual(s.active, "3")

    def test_phase_b_recovery_cancels_failover(self):
        e, s = self.engine({"1": entry(34, 301, "http-429"), "2": entry(20)},
                           unhealthy_ticks=1)
        def recover(call):
            if call == 3: s.rows["1"] = entry(40)
        s.on_collect = recover
        self.assertEqual(e.tick(), mod.TickOutcome.NO_ACTION)
        self.assertEqual(len(s.collections), 3)
        self.assertEqual(e._unhealthy_ticks, 0)
        self.assertEqual(s.switches, [])

    def test_normal_below_threshold_and_threshold_switch(self):
        for pct, expected in ((34, mod.TickOutcome.NO_ACTION),
                              (90, mod.TickOutcome.SWITCHED),
                              (100, mod.TickOutcome.SWITCHED)):
            e, s = self.engine({"1": entry(pct), "2": entry(20)}, cooldown_seconds=0)
            self.assertEqual(e.tick(), expected)

    def test_expired_token_keeps_existing_idle_hold(self):
        e, s = self.engine({"1": entry(34, 301, "http-429", mod.USAGE_TOKEN_EXPIRED),
                            "2": entry(20)}, unhealthy_ticks=1)
        self.assertEqual(e.tick(), mod.TickOutcome.NO_ACTION)
        self.assertEqual(e._unhealthy_ticks, 0)
        self.assertEqual(s.switches, [])

    def test_backoff_and_display_data_are_unchanged(self):
        active = entry(34, 301, "http-429")
        e, s = self.engine({"1": active, "2": entry(20)}, unhealthy_ticks=1)
        self.assertEqual(e.tick(), mod.TickOutcome.SWITCHED)
        self.assertEqual(s.rows["1"], active)
        self.assertEqual(active.decision_value()["five_hour"]["pct"], 34)
        row = {"fetchedAt": active.fetched_at, "backoffUntil": active.backoff_until}
        self.assertFalse(_row_eligible(row, NOW, respect_plans=False))
        self.assertFalse(_row_eligible(row, NOW, respect_plans=True))

    def test_candidate_aging_during_auth_freshening_is_skipped(self):
        e, s = self.engine({"1": entry(34, 301, "http-429"), "2": entry(20, 179)},
                           unhealthy_ticks=1)
        e.freshen_action = lambda _: self.now.__setitem__(0, NOW+2)
        self.assertEqual(e.tick(), mod.TickOutcome.BLOCKED)
        self.assertEqual(s.switches, [])

    def test_clock_age_not_old_snapshot_age_controls_trust(self):
        e, s = self.engine({"1": entry(34, 299, "http-429"), "2": entry(20)},
                           unhealthy_ticks=1)
        self.now[0] += 2
        self.assertEqual(e.tick(), mod.TickOutcome.SWITCHED)

    def test_non_429_stale_behavior_is_unchanged(self):
        e, s = self.engine({"1": entry(34, 301, "timeout"), "2": entry(20)},
                           unhealthy_ticks=1)
        self.assertEqual(e.tick(), mod.TickOutcome.NO_ACTION)

    def test_consume_first_rechecks_phase_two(self):
        active = entry(34, 299, "http-429")
        peer = entry(20)
        active.last_good["seven_day"]["resets_at"] = datetime.fromtimestamp(
            NOW+7200, timezone.utc).isoformat()
        peer.last_good["seven_day"]["resets_at"] = datetime.fromtimestamp(
            NOW+3600, timezone.utc).isoformat()
        e, s = self.engine({"1": active, "2": peer}, strategy="consume-first")
        def age_at_refetch(call):
            if call == 3: self.now[0] += 2
        s.on_collect = age_at_refetch
        self.assertEqual(e.tick(), mod.TickOutcome.NO_ACTION)
        self.assertEqual(s.switches, [])
        self.assertTrue(any(getattr(x, "reason", "") == "reset-unknown"
                            for x in self.events))


if __name__ == "__main__":
    unittest.main(verbosity=2)
