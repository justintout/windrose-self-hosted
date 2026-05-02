#!/usr/bin/env python3
"""
Unit tests for the game-version detection + auto-update-when-empty path.
Stdlib only.

Coverage:
  1. _parse_acf_flat extracts buildid from a sample appmanifest.
  2. _parse_app_info_branch_buildid returns the public branch's buildid
     (not beta's) given a multi-branch fixture.
  3. read_installed_buildid surfaces "manifest-missing" when no path exists.
  4. read_installed_buildid reads from APPMANIFEST_PATH when present.
  5. query_latest_buildid invokes the right argv via subprocess.run.
  6. query_latest_buildid surfaces "steamcmd-unavailable" when the binary
     isn't on disk.
  7. UpdateAvailabilityChecker.check_now() fires game.update.available
     exactly once across two successive calls when latest > installed
     (idempotency).
  8. Idempotency reset: when installed catches up to latest, a *next*
     newer latest re-fires.
  9. game.update.available is in _WEBHOOK_COLORS and build_discord_payload
     produces a non-empty embed with the buildIds.
 10. UpdateAutomationScheduler fires game.update.scheduled + calls
     request_restart when policy is on and idle threshold is met.
 11. UpdateAutomationScheduler does NOT fire when policy is off.
 12. UpdateAutomationScheduler does NOT fire when players are still
     connected.
 13. _save_update_policy + _load_update_policy round-trip with floor
     applied to graceMinutes.

We monkey-patch at the server module level to avoid needing real
steamcmd, real Steam, or a real game container.
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402


SAMPLE_APPMANIFEST = '''"AppState"
{
\t"appid"\t\t"4129620"
\t"buildid"\t\t"12345678"
\t"name"\t\t"Windrose Dedicated Server"
\t"InstalledDepots"
\t{
\t\t"4129621"
\t\t{
\t\t\t"manifest"\t\t"99999999999999"
\t\t}
\t}
}
'''

SAMPLE_APP_INFO_PRINT = '''AppID : 4129620, change number : 35398323/35398323, last change : Wed Apr 30
"4129620"
{
\t"common"
\t{
\t\t"name"\t\t"Windrose Dedicated Server"
\t}
\t"depots"
\t{
\t\t"branches"
\t\t{
\t\t\t"public"
\t\t\t{
\t\t\t\t"buildid"\t\t"22222222"
\t\t\t\t"timeupdated"\t\t"1745000000"
\t\t\t}
\t\t\t"beta"
\t\t\t{
\t\t\t\t"buildid"\t\t"33333333"
\t\t\t\t"description"\t\t"unstable"
\t\t\t}
\t\t}
\t}
}
'''


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _run(case, fn):
    try:
        fn()
    except AssertionError as e:
        print(f"  FAIL  {case}: {e}")
        raise
    print(f"  PASS  {case}")


# --- 1 -----------------------------------------------------------------
def case_parse_acf_flat():
    flat = server._parse_acf_flat(SAMPLE_APPMANIFEST)
    assert flat.get("buildid") == "12345678", flat
    assert flat.get("appid") == "4129620"


# --- 2 -----------------------------------------------------------------
def case_parse_app_info_public_branch():
    bid = server._parse_app_info_branch_buildid(SAMPLE_APP_INFO_PRINT, branch="public")
    assert bid == "22222222", f"got {bid!r}"
    # Confirms the state-machine doesn't return the beta branch's buildid.
    bid_beta = server._parse_app_info_branch_buildid(SAMPLE_APP_INFO_PRINT, branch="beta")
    assert bid_beta == "33333333", f"got {bid_beta!r}"


# --- 3 + 4 -------------------------------------------------------------
def case_read_installed_buildid_missing(monkey_paths):
    # Both candidate paths point inside an empty tmpdir.
    bid, err = server.read_installed_buildid()
    assert bid is None
    assert err == "manifest-missing", err


def case_read_installed_buildid_present(tmp_root):
    server.APPMANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    server.APPMANIFEST_PATH.write_text(SAMPLE_APPMANIFEST)
    try:
        bid, err = server.read_installed_buildid()
        assert err is None, err
        assert bid == "12345678", bid
    finally:
        server.APPMANIFEST_PATH.unlink(missing_ok=True)


# --- 5 + 6 -------------------------------------------------------------
def case_query_latest_buildid_invokes_steamcmd(tmp_root):
    cmd = server.STEAMCMD_PATH / "steamcmd.sh"
    cmd.parent.mkdir(parents=True, exist_ok=True)
    cmd.write_text("#!/bin/sh\n")
    cmd.chmod(0o755)
    captured = {}
    def fake_run(args, **kw):
        captured["args"] = list(args)
        captured["kw"] = kw
        return _FakeCompleted(returncode=0, stdout=SAMPLE_APP_INFO_PRINT)
    orig_run = server.subprocess.run
    server.subprocess.run = fake_run  # type: ignore[assignment]
    try:
        bid, err = server.query_latest_buildid(timeout=5.0)
    finally:
        server.subprocess.run = orig_run  # type: ignore[assignment]
    assert err is None, err
    assert bid == "22222222", bid
    assert captured["args"][0] == str(cmd)
    assert "+app_info_print" in captured["args"]
    assert server.STEAM_APP_ID in captured["args"]
    assert "+login" in captured["args"]
    assert "anonymous" in captured["args"]


def case_query_latest_buildid_no_steamcmd(tmp_root):
    # Make sure there's no leftover steamcmd.sh from prior cases.
    cmd = server.STEAMCMD_PATH / "steamcmd.sh"
    cmd.unlink(missing_ok=True)
    bid, err = server.query_latest_buildid(timeout=1.0)
    assert bid is None
    assert err == "steamcmd-unavailable", err


# --- 7 + 8 -------------------------------------------------------------
def case_update_checker_idempotency(tmp_root):
    server.APPMANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    server.APPMANIFEST_PATH.write_text(SAMPLE_APPMANIFEST)  # installed = 12345678
    cmd = server.STEAMCMD_PATH / "steamcmd.sh"
    cmd.parent.mkdir(parents=True, exist_ok=True)
    cmd.write_text("#!/bin/sh\n"); cmd.chmod(0o755)
    fired: list[tuple[str, dict]] = []
    orig_fire = server.fire_event
    def capture(name, **fields):
        fired.append((name, fields))
    server.fire_event = capture  # type: ignore[assignment]
    orig_run = server.subprocess.run
    server.subprocess.run = lambda *a, **kw: _FakeCompleted(0, SAMPLE_APP_INFO_PRINT)  # type: ignore[assignment]
    try:
        # Reset the one-shot guard between tests.
        server._LAST_FIRED_AVAILABLE_BUILDID = None
        c = server.UpdateAvailabilityChecker()
        c.check_now()
        c.check_now()
        avail = [(n, f) for (n, f) in fired if n == "game.update.available"]
        assert len(avail) == 1, f"expected 1 fire, got {len(avail)}: {avail}"
        ev = avail[0][1]
        assert ev.get("installedBuildId") == "12345678"
        assert ev.get("latestBuildId")    == "22222222"
        # Now flip installed forward (operator restarted, manifest updated)
        # and then bump latest higher again — re-fire is required.
        server.APPMANIFEST_PATH.write_text(SAMPLE_APPMANIFEST.replace("12345678", "22222222"))
        c.check_now()  # installed catches up to latest → no fire, resets guard
        # Newer latest now:
        newer = SAMPLE_APP_INFO_PRINT.replace("22222222", "44444444")
        server.subprocess.run = lambda *a, **kw: _FakeCompleted(0, newer)  # type: ignore[assignment]
        c.check_now()
        avail = [(n, f) for (n, f) in fired if n == "game.update.available"]
        assert len(avail) == 2, f"expected re-fire after gap; got {avail}"
        assert avail[1][1].get("latestBuildId") == "44444444"
    finally:
        server.fire_event = orig_fire  # type: ignore[assignment]
        server.subprocess.run = orig_run  # type: ignore[assignment]
        server.APPMANIFEST_PATH.unlink(missing_ok=True)


# --- 9 -----------------------------------------------------------------
def case_discord_payload_includes_update_event():
    assert "game.update.available" in server._WEBHOOK_COLORS
    assert "game.update.scheduled" in server._WEBHOOK_COLORS
    embed = server.build_discord_payload({
        "event": "game.update.available",
        "installedBuildId": "11",
        "latestBuildId": "22",
        "timestamp": "2026-05-02T00:00:00Z",
        "serverName": "test",
    })
    desc = embed["embeds"][0]["description"]
    assert "11" in desc and "22" in desc, desc
    embed2 = server.build_discord_payload({
        "event": "game.update.scheduled",
        "installedBuildId": "11", "latestBuildId": "22",
        "idleSeconds": 600, "graceMinutes": 5,
        "timestamp": "2026-05-02T00:00:00Z", "serverName": "test",
    })
    desc2 = embed2["embeds"][0]["description"]
    assert "10 min" in desc2, desc2


# --- 10 + 11 + 12 ------------------------------------------------------
def case_automation_fires_when_idle(tmp_root):
    # Force policy to on with a tiny grace window.
    server.UPDATE_POLICY_PATH.write_text(json.dumps({
        "autoUpdateWhenEmpty": True, "graceMinutes": 1,
    }))
    # Cached status reports an update.
    with server._UPDATE_STATE_LOCK:
        server._UPDATE_STATUS_CACHE.update({
            "installedBuildId": "11", "latestBuildId": "22",
            "updateAvailable":  True, "lastCheckedAt": "now",
        })
    fired: list[tuple[str, dict]] = []
    restart_called = []
    orig_fire = server.fire_event
    orig_restart = server.request_restart
    orig_players = server.parse_active_players
    server.fire_event = lambda n, **f: fired.append((n, f))  # type: ignore[assignment]
    server.request_restart = lambda: restart_called.append(True)  # type: ignore[assignment]
    server.parse_active_players = lambda: []  # type: ignore[assignment]
    try:
        s = server.UpdateAutomationScheduler()
        # First tick: empty + clock starts (no fire yet).
        s._tick()
        assert s._players_zero_since is not None
        assert not fired
        # Push the clock past the threshold (1 min) and re-tick.
        s._players_zero_since = time.time() - 120.0
        # Block the auto-backup-coordination check by ensuring no recent
        # auto-backup landed during this synthetic window.
        with server._auto_state_lock:
            server._auto_state["lastAutoBackupAt"] = None
        s._tick()
        assert any(n == "game.update.scheduled" for (n, _) in fired), fired
        assert restart_called, "request_restart not invoked"
    finally:
        server.fire_event = orig_fire  # type: ignore[assignment]
        server.request_restart = orig_restart  # type: ignore[assignment]
        server.parse_active_players = orig_players  # type: ignore[assignment]
        server.UPDATE_POLICY_PATH.unlink(missing_ok=True)


def case_automation_skips_when_policy_off(tmp_root):
    server.UPDATE_POLICY_PATH.write_text(json.dumps({
        "autoUpdateWhenEmpty": False, "graceMinutes": 1,
    }))
    with server._UPDATE_STATE_LOCK:
        server._UPDATE_STATUS_CACHE.update({"updateAvailable": True,
            "installedBuildId": "11", "latestBuildId": "22"})
    fired = []
    orig_fire = server.fire_event
    orig_players = server.parse_active_players
    server.fire_event = lambda n, **f: fired.append((n, f))  # type: ignore[assignment]
    server.parse_active_players = lambda: []  # type: ignore[assignment]
    try:
        s = server.UpdateAutomationScheduler()
        s._players_zero_since = time.time() - 600.0
        s._tick()
        assert not fired, f"should not fire while policy off: {fired}"
    finally:
        server.fire_event = orig_fire  # type: ignore[assignment]
        server.parse_active_players = orig_players  # type: ignore[assignment]
        server.UPDATE_POLICY_PATH.unlink(missing_ok=True)


def case_automation_skips_when_players_present(tmp_root):
    server.UPDATE_POLICY_PATH.write_text(json.dumps({
        "autoUpdateWhenEmpty": True, "graceMinutes": 1,
    }))
    with server._UPDATE_STATE_LOCK:
        server._UPDATE_STATUS_CACHE.update({"updateAvailable": True,
            "installedBuildId": "11", "latestBuildId": "22"})
    fired = []
    orig_fire = server.fire_event
    orig_players = server.parse_active_players
    server.fire_event = lambda n, **f: fired.append((n, f))  # type: ignore[assignment]
    server.parse_active_players = lambda: [{"accountId": "abc", "name": "tester"}]  # type: ignore[assignment]
    try:
        s = server.UpdateAutomationScheduler()
        s._players_zero_since = time.time() - 600.0  # would trigger if empty
        s._tick()
        assert s._players_zero_since is None, "clock should reset on player presence"
        assert not fired
    finally:
        server.fire_event = orig_fire  # type: ignore[assignment]
        server.parse_active_players = orig_players  # type: ignore[assignment]
        server.UPDATE_POLICY_PATH.unlink(missing_ok=True)


# --- 13 ----------------------------------------------------------------
def case_update_policy_roundtrip(tmp_root):
    merged = server._save_update_policy({"autoUpdateWhenEmpty": True, "graceMinutes": 0.4})
    # Floor applied: 0.4 → 1.0
    assert merged["graceMinutes"] == 1.0, merged
    assert merged["autoUpdateWhenEmpty"] is True
    loaded = server._load_update_policy()
    assert loaded == merged
    server.UPDATE_POLICY_PATH.unlink(missing_ok=True)


def main():
    print("Running game-version tests…")
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        # Repoint the module-level paths to a sandbox.
        orig_appmanifest      = server.APPMANIFEST_PATH
        orig_appmanifest_fb   = server.APPMANIFEST_FALLBACK
        orig_steamcmd_path    = server.STEAMCMD_PATH
        orig_policy_path      = server.UPDATE_POLICY_PATH
        server.APPMANIFEST_PATH    = td_path / "WindowsServer" / "steamapps" / "appmanifest_4129620.acf"
        server.APPMANIFEST_FALLBACK = td_path / "steamcmd-fb" / "steamapps" / "appmanifest_4129620.acf"
        server.STEAMCMD_PATH        = td_path / "steamcmd"
        server.UPDATE_POLICY_PATH   = td_path / ".update-policy.json"
        try:
            _run("parse_acf_flat extracts buildid", case_parse_acf_flat)
            _run("parse_app_info returns public buildid", case_parse_app_info_public_branch)
            _run("read_installed surfaces missing", lambda: case_read_installed_buildid_missing(td_path))
            _run("read_installed reads manifest", lambda: case_read_installed_buildid_present(td_path))
            _run("query_latest invokes steamcmd", lambda: case_query_latest_buildid_invokes_steamcmd(td_path))
            _run("query_latest surfaces missing steamcmd", lambda: case_query_latest_buildid_no_steamcmd(td_path))
            _run("checker fires once per latest, re-fires after catchup", lambda: case_update_checker_idempotency(td_path))
            _run("discord payload includes update events", case_discord_payload_includes_update_event)
            _run("automation fires when idle + policy on", lambda: case_automation_fires_when_idle(td_path))
            _run("automation skips when policy off", lambda: case_automation_skips_when_policy_off(td_path))
            _run("automation skips when players present", lambda: case_automation_skips_when_players_present(td_path))
            _run("policy round-trip applies floor", lambda: case_update_policy_roundtrip(td_path))
        finally:
            server.APPMANIFEST_PATH    = orig_appmanifest
            server.APPMANIFEST_FALLBACK = orig_appmanifest_fb
            server.STEAMCMD_PATH        = orig_steamcmd_path
            server.UPDATE_POLICY_PATH   = orig_policy_path
    print("\nall game-version tests passed")


if __name__ == "__main__":
    main()
