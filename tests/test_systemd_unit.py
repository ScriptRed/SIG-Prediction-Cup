"""deploy/predcup.service: restarts on crash, runs the bot as an
unprivileged user from the repo checkout, and has a stop timeout long
enough for the shutdown cancel-all."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
UNIT = REPO_ROOT / "deploy" / "predcup.service"


def parse_unit(text: str) -> dict[str, dict[str, list[str]]]:
    sections: dict[str, dict[str, list[str]]] = {}
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
            continue
        key, _, value = line.partition("=")
        current.setdefault(key.strip(), []).append(value.strip())
    return sections


def unit():
    return parse_unit(UNIT.read_text())


def test_restarts_on_crash_with_delay():
    service = unit()["Service"]
    assert service["Restart"] == ["on-failure"]
    assert float(service["RestartSec"][0].rstrip("s")) >= 5


def test_crash_loop_is_rate_limited():
    u = unit()["Unit"]
    assert int(u["StartLimitBurst"][0]) >= 1
    assert "StartLimitIntervalSec" in u


def test_runs_trading_process_from_repo_checkout():
    service = unit()["Service"]
    assert service["ExecStart"][0].endswith("-m predcup.main")
    assert "WorkingDirectory" in service
    assert service["User"][0] not in ("", "root")


def test_graceful_stop_gives_time_for_cancel_all():
    service = unit()["Service"]
    assert service["KillSignal"] == ["SIGINT"]
    assert float(service["TimeoutStopSec"][0].rstrip("s")) >= 20


def test_starts_after_network_and_on_boot():
    sections = unit()
    assert "network-online.target" in sections["Unit"]["After"][0]
    assert sections["Install"]["WantedBy"] == ["multi-user.target"]


def test_no_secrets_in_unit_file():
    text = UNIT.read_text()
    for name in ("SIG_API_KEY=", "TELEGRAM_BOT_TOKEN="):
        assert name not in text


def test_deploy_guide_exists_and_references_unit():
    guide = (REPO_ROOT / "docs" / "deploy.md").read_text()
    assert "deploy/predcup.service" in guide
    assert "KILL" in guide


def test_watchdog_restarts_a_hung_bot():
    service = unit()["Service"]
    # Type=notify: pings (and READY=1) from predcup.watchdog are accepted
    # from the main process only.
    assert service["Type"] == ["notify"]
    assert service["NotifyAccess"] == ["main"]
    watchdog = float(service["WatchdogSec"][0].rstrip("s"))
    assert 30 <= watchdog <= 120
    # Restart=on-failure covers a watchdog timeout.
    assert service["Restart"] == ["on-failure"]


def test_startup_timeout_leaves_room_to_resolve_tournament_and_cancel():
    service = unit()["Service"]
    assert float(service["TimeoutStartSec"][0].rstrip("s")) >= 60
