"""Telegram alerts and commands (CLAUDE.md Hard Rule 7, LAUNCH_CHECKLIST):
only TELEGRAM_CHAT_ID may command the bot; everything else is ignored.
/kill acts immediately; /resetramp needs a "YES" reply from the same chat
within the confirmation window.
"""

import asyncio
import logging

import pytest

from predcup.alerts import (
    CommandRouter,
    TelegramAlerter,
    TelegramConfig,
    load_telegram_config,
    load_telegram_env,
)
from predcup.store import EventStore

OWNER = "123456789"
STRANGER = "987654321"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Actions:
    def __init__(self):
        self.kills = []
        self.resets = []

    async def kill(self, reason: str) -> None:
        self.kills.append(reason)

    def reset_ramp(self, reason: str) -> None:
        self.resets.append(reason)


def make_router(clock=None, store=None):
    actions = Actions()
    router = CommandRouter(
        allowed_chat_id=OWNER,
        on_kill=actions.kill,
        on_reset_ramp=actions.reset_ramp,
        confirm_timeout_seconds=60,
        event_store=store or EventStore(":memory:"),
        clock=clock or Clock(),
    )
    return router, actions


def send(router, chat_id, text):
    return asyncio.run(router.handle(chat_id, text))


# --- chat-id restriction -----------------------------------------------------


def test_kill_from_other_chat_is_ignored():
    router, actions = make_router()
    assert send(router, STRANGER, "/kill") is None
    assert actions.kills == []


def test_resetramp_and_yes_from_other_chat_are_ignored():
    router, actions = make_router()
    assert send(router, STRANGER, "/resetramp") is None
    assert send(router, STRANGER, "YES") is None
    assert actions.resets == []


def test_stranger_yes_cannot_confirm_owner_pending_reset():
    router, actions = make_router()
    send(router, OWNER, "/resetramp")
    assert send(router, STRANGER, "YES") is None
    assert actions.resets == []
    # The owner's pending confirmation is untouched by the stranger.
    send(router, OWNER, "YES")
    assert len(actions.resets) == 1


def test_integer_chat_id_matches_string_config():
    router, actions = make_router()
    send(router, int(OWNER), "/kill")
    assert len(actions.kills) == 1


def test_ignored_commands_are_logged_without_text_echoed_back():
    store = EventStore(":memory:")
    router, _ = make_router(store=store)
    send(router, STRANGER, "/kill")
    events = store.all_events(event_type="telegram_ignored")
    assert events[-1]["payload"]["chat_id"] == STRANGER


# --- /kill -------------------------------------------------------------------


def test_kill_acts_immediately_without_confirmation():
    router, actions = make_router()
    reply = send(router, OWNER, "/kill")
    assert actions.kills == ["telegram /kill"]
    assert "kill" in reply.lower()


def test_kill_with_bot_suffix_and_whitespace():
    router, actions = make_router()
    send(router, OWNER, "  /kill@predcup_bot  ")
    assert len(actions.kills) == 1


def test_kill_failure_is_reported_back():
    async def boom(reason):
        raise RuntimeError("venue down")

    router = CommandRouter(
        allowed_chat_id=OWNER,
        on_kill=boom,
        on_reset_ramp=lambda r: None,
        confirm_timeout_seconds=60,
        event_store=EventStore(":memory:"),
    )
    reply = send(router, OWNER, "/kill")
    assert "fail" in reply.lower()
    assert "touch KILL" in reply


def test_kill_cancels_pending_resetramp():
    router, actions = make_router()
    send(router, OWNER, "/resetramp")
    send(router, OWNER, "/kill")
    send(router, OWNER, "YES")
    assert actions.resets == []


# --- /resetramp + YES ----------------------------------------------------------


def test_resetramp_asks_for_confirmation_and_does_nothing_yet():
    router, actions = make_router()
    reply = send(router, OWNER, "/resetramp")
    assert "YES" in reply
    assert actions.resets == []


def test_resetramp_then_yes_resets():
    router, actions = make_router()
    send(router, OWNER, "/resetramp")
    reply = send(router, OWNER, "YES")
    assert len(actions.resets) == 1
    assert "reset" in reply.lower()


def test_yes_is_single_use():
    router, actions = make_router()
    send(router, OWNER, "/resetramp")
    send(router, OWNER, "YES")
    send(router, OWNER, "YES")
    assert len(actions.resets) == 1


def test_yes_without_pending_does_nothing():
    router, actions = make_router()
    reply = send(router, OWNER, "YES")
    assert actions.resets == []
    assert "nothing" in reply.lower()


@pytest.mark.parametrize("answer", ["yes", "Yes", "y", "YES!", "ok"])
def test_only_exact_uppercase_yes_confirms(answer):
    router, actions = make_router()
    send(router, OWNER, "/resetramp")
    send(router, OWNER, answer)
    assert actions.resets == []


def test_any_other_reply_cancels_pending():
    router, actions = make_router()
    send(router, OWNER, "/resetramp")
    send(router, OWNER, "no")
    send(router, OWNER, "YES")
    assert actions.resets == []


def test_yes_after_timeout_is_rejected():
    clock = Clock()
    router, actions = make_router(clock=clock)
    send(router, OWNER, "/resetramp")
    clock.now += 61
    reply = send(router, OWNER, "YES")
    assert actions.resets == []
    assert "expired" in reply.lower()


def test_yes_just_inside_timeout_is_accepted():
    clock = Clock()
    router, actions = make_router(clock=clock)
    send(router, OWNER, "/resetramp")
    clock.now += 59
    send(router, OWNER, "YES")
    assert len(actions.resets) == 1


def test_commands_are_logged():
    store = EventStore(":memory:")
    router, _ = make_router(store=store)
    send(router, OWNER, "/resetramp")
    send(router, OWNER, "YES")
    actions = [e["payload"]["action"] for e in store.all_events(event_type="telegram_command")]
    assert actions == ["resetramp_requested", "resetramp_confirmed"]


def test_unknown_command_gets_help():
    router, actions = make_router()
    reply = send(router, OWNER, "/foo")
    assert "/kill" in reply and "/resetramp" in reply
    assert actions.kills == [] and actions.resets == []


# --- TelegramAlerter ----------------------------------------------------------


class FakeSender:
    def __init__(self, fail_times=0):
        self.sent = []
        self.fail_times = fail_times

    async def __call__(self, chat_id: str, text: str) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("telegram unreachable")
        self.sent.append((chat_id, text))


async def no_sleep(_s):
    return None


def make_alerter(sender, **overrides):
    kwargs = dict(
        chat_id=OWNER,
        send_fn=sender,
        min_send_interval_seconds=1.0,
        max_send_attempts=3,
        sleep=no_sleep,
    )
    kwargs.update(overrides)
    return TelegramAlerter(**kwargs)


def test_alerter_send_is_sync_and_nonblocking_then_flushes():
    sender = FakeSender()
    alerter = make_alerter(sender)
    alerter.send("one")
    alerter.send("two")
    assert sender.sent == []
    asyncio.run(alerter.flush())
    assert sender.sent == [(OWNER, "one"), (OWNER, "two")]


def test_alerter_spaces_messages_for_rate_limit():
    sleeps = []

    async def rec_sleep(s):
        sleeps.append(s)

    sender = FakeSender()
    alerter = make_alerter(sender, sleep=rec_sleep, min_send_interval_seconds=1.5)
    for m in ("a", "b", "c"):
        alerter.send(m)
    asyncio.run(alerter.flush())
    assert sleeps.count(1.5) >= 2


def test_alerter_retries_transient_failure():
    sender = FakeSender(fail_times=2)
    alerter = make_alerter(sender)
    alerter.send("hello")
    asyncio.run(alerter.flush())
    assert sender.sent == [(OWNER, "hello")]


def test_alerter_drops_after_max_attempts_and_continues():
    sender = FakeSender(fail_times=3)
    alerter = make_alerter(sender)
    alerter.send("lost")
    alerter.send("next")
    asyncio.run(alerter.flush())
    assert sender.sent == [(OWNER, "next")]


def test_alerter_truncates_to_telegram_limit():
    sender = FakeSender()
    alerter = make_alerter(sender)
    alerter.send("x" * 5000)
    asyncio.run(alerter.flush())
    assert len(sender.sent[0][1]) <= 4096


# --- config / env / secrets -------------------------------------------------------


def test_load_telegram_env_requires_both(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with pytest.raises(RuntimeError, match="TELEGRAM_CHAT_ID"):
        load_telegram_env()
    monkeypatch.setenv("TELEGRAM_CHAT_ID", " 42 ")
    assert load_telegram_env() == ("123:abc", "42")


def test_missing_env_error_does_not_contain_token(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:supersecret")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with pytest.raises(RuntimeError) as err:
        load_telegram_env()
    assert "supersecret" not in str(err.value)


def test_repo_settings_have_telegram_config():
    import yaml

    from predcup.killfile import REPO_ROOT

    with open(REPO_ROOT / "config" / "settings.yaml") as f:
        loaded = load_telegram_config(yaml.safe_load(f))
    assert isinstance(loaded, TelegramConfig)
    assert loaded.confirm_timeout_seconds > 0


def test_http_client_loggers_are_quiet_so_token_urls_are_not_logged():
    # httpx logs every request URL at INFO, and Telegram puts the bot token
    # in the URL (Hard Rule 6: never log keys).
    import predcup.alerts  # noqa: F401

    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


def test_alerter_run_delivers_messages_sent_while_running():
    sender = FakeSender()
    alerter = make_alerter(sender, sleep=asyncio.sleep, min_send_interval_seconds=0)

    async def scenario():
        task = asyncio.create_task(alerter.run())
        await asyncio.sleep(0)
        alerter.send("later")
        for _ in range(10):
            await asyncio.sleep(0)
        task.cancel()

    asyncio.run(scenario())
    assert sender.sent == [(OWNER, "later")]


def test_telegram_bot_constructs_without_network():
    from predcup.alerts import TelegramBot

    TelegramBot("123456:TEST-TOKEN")
