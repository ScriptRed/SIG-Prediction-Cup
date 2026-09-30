"""Telegram alerts and commands.

Wiring for main.py (all inside the running event loop):

    token, chat_id = load_telegram_env()
    tg = load_telegram_config(config)
    bot = TelegramBot(token)
    alerter = TelegramAlerter(chat_id, bot.send_message,
                              tg.min_send_interval_seconds, tg.max_send_attempts,
                              event_store=store)
    ... build RiskManager(alerter=alerter, ...) ...
    router = CommandRouter(chat_id, on_kill=risk.kill,
                           on_reset_ramp=risk.reset_size_ramp,
                           confirm_timeout_seconds=tg.confirm_timeout_seconds,
                           event_store=store,
                           status_provider=AppStatusProvider(app))  # predcup/status.py
    asyncio.create_task(alerter.run())
    await bot.start(router)          # polling; await bot.stop() on shutdown

Only TELEGRAM_CHAT_ID can command the bot. Messages from any other chat
are dropped without a reply (and logged as `telegram_ignored`). /kill acts
at once; a kill switch that waits for a confirmation is one you might not
get to use. /resetramp raises the bot's size, so it needs a literal "YES"
reply from the same chat within `confirm_timeout_seconds`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from predcup.status import StatusProvider, format_status
from predcup.store import EventStore

logger = logging.getLogger(__name__)

# httpx logs each request URL at INFO and the Bot API puts the token in the
# URL. Never let it reach a log (CLAUDE.md Hard Rule 6).
logging.getLogger("httpx").setLevel(logging.WARNING)

TELEGRAM_MAX_MESSAGE_LENGTH = 4096
CONFIRM_WORD = "YES"
HELP_TEXT = (
    "Commands:\n"
    "/kill - cancel all orders and stop quoting (immediate)\n"
    "/resetramp - reset the size ramp to launch size (asks for YES)\n"
    "/status - ramp, halt, open orders, positions, P&L, reconciliation, loop lag"
)


@dataclass(frozen=True)
class TelegramConfig:
    confirm_timeout_seconds: float
    min_send_interval_seconds: float
    max_send_attempts: int

    def __post_init__(self) -> None:
        if self.confirm_timeout_seconds <= 0:
            raise ValueError("telegram.confirm_timeout_seconds must be > 0")
        if self.min_send_interval_seconds < 0:
            raise ValueError("telegram.min_send_interval_seconds must be >= 0")
        if self.max_send_attempts < 1:
            raise ValueError("telegram.max_send_attempts must be >= 1")


def load_telegram_config(config: dict) -> TelegramConfig:
    section = config["telegram"]
    return TelegramConfig(
        confirm_timeout_seconds=section["confirm_timeout_seconds"],
        min_send_interval_seconds=section["min_send_interval_seconds"],
        max_send_attempts=section["max_send_attempts"],
    )


def load_telegram_env() -> tuple[str, str]:
    """(bot token, chat id) from the environment. Raises naming only the
    missing variable, never a value."""
    values = {}
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        value = os.environ.get(name, "").strip()
        if not value:
            raise RuntimeError(f"{name} is not set (see .env.example)")
        values[name] = value
    return values["TELEGRAM_BOT_TOKEN"], values["TELEGRAM_CHAT_ID"]


def _command_word(text: str) -> str:
    """'/kill@predcup_bot now' -> '/kill'."""
    first = text.strip().split(maxsplit=1)[0] if text.strip() else ""
    return first.split("@", 1)[0].lower()


class CommandRouter:
    """Pure command logic, no Telegram library: handle() takes a chat id and
    message text and returns the reply, or None to stay silent."""

    def __init__(
        self,
        allowed_chat_id: str | int,
        on_kill: Callable[[str], Awaitable[object]],
        on_reset_ramp: Callable[[str], None],
        confirm_timeout_seconds: float,
        event_store: EventStore,
        clock: Callable[[], float] = time.monotonic,
        status_provider: StatusProvider | None = None,
    ) -> None:
        self._status_provider = status_provider
        self._allowed = str(allowed_chat_id).strip()
        if not self._allowed:
            raise ValueError("allowed_chat_id is required")
        self._on_kill = on_kill
        self._on_reset_ramp = on_reset_ramp
        self._confirm_timeout = confirm_timeout_seconds
        self._event_store = event_store
        self._clock = clock
        self._resetramp_requested_at: float | None = None

    async def handle(self, chat_id: str | int, text: str | None) -> str | None:
        chat = str(chat_id).strip()
        if chat != self._allowed:
            self._event_store.log(
                "telegram_ignored", {"chat_id": chat, "text": (text or "")[:64]}
            )
            return None

        text = (text or "").strip()
        pending_at = self._resetramp_requested_at
        # Any message consumes a pending confirmation: YES is only valid as
        # the very next message.
        self._resetramp_requested_at = None

        if text == CONFIRM_WORD:
            return self._confirm_resetramp(pending_at)

        command = _command_word(text)
        if command == "/kill":
            return await self._kill()
        if command == "/status":
            return await self._status()
        if command == "/resetramp":
            self._resetramp_requested_at = self._clock()
            self._log("resetramp_requested")
            return (
                f"Reset the size ramp to launch size? Reply {CONFIRM_WORD} within "
                f"{self._confirm_timeout:g}s to confirm; anything else cancels."
            )
        if pending_at is not None:
            self._log("resetramp_cancelled")
            return "Size ramp reset cancelled.\n" + HELP_TEXT
        return HELP_TEXT

    async def _kill(self) -> str:
        self._log("kill")
        try:
            result = await self._on_kill("telegram /kill")
        except Exception as exc:
            logger.exception("telegram /kill failed")
            return (
                f"KILL FAILED: {exc!r}. Quoting may still be live. "
                "SSH in and `touch KILL` in the repo root, or stop the service."
            )
        success = getattr(result, "success", True)
        if not success:
            remaining = getattr(result, "remaining_order_ids", [])
            return (
                f"Kill engaged, quoting halted, but {len(remaining)} order(s) still "
                f"open after retries: {remaining}. Check the venue by hand."
            )
        return "Kill engaged: quoting halted, all orders cancelled. Restart the service to resume."

    async def _status(self) -> str:
        if self._status_provider is None:
            return "Status not available: no status provider wired in main.py."
        try:
            return format_status(await self._status_provider.snapshot())
        except Exception as exc:
            logger.exception("telegram /status failed")
            return f"Status failed: {exc!r}"[:500]

    def _confirm_resetramp(self, pending_at: float | None) -> str:
        if pending_at is None:
            return "Nothing to confirm. Send /resetramp first."
        if self._clock() - pending_at > self._confirm_timeout:
            self._log("resetramp_expired")
            return "Confirmation expired. Send /resetramp again."
        self._on_reset_ramp("manual via Telegram /resetramp")
        self._log("resetramp_confirmed")
        return "Size ramp reset to launch size."

    def _log(self, action: str) -> None:
        self._event_store.log("telegram_command", {"action": action, "chat_id": self._allowed})


class TelegramAlerter:
    """Implements the Alerter protocol (`send(message)`, synchronous) used by
    RiskManager, SizeRamp and LoopLagMonitor. send() only queues, so it never
    blocks the trading loop; run() is the background task that delivers,
    spaced by min_send_interval_seconds (Telegram allows about one message
    per second per chat) and retried with backoff up to max_send_attempts.
    """

    def __init__(
        self,
        chat_id: str | int,
        send_fn: Callable[[str, str], Awaitable[object]],
        min_send_interval_seconds: float,
        max_send_attempts: int,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        event_store: EventStore | None = None,
    ) -> None:
        self._event_store = event_store
        self._chat_id = str(chat_id).strip()
        self._send_fn = send_fn
        self._interval = min_send_interval_seconds
        self._max_attempts = max_send_attempts
        self._sleep = sleep
        self._queue: deque[str] = deque()
        self._wakeup: asyncio.Event | None = None

    def send(self, message: str) -> None:
        logger.warning("alert: %s", message)
        if self._event_store is not None:
            # events_log "alert", as LogAlerter does: the daily summary counts these.
            self._event_store.log("alert", {"message": message})
        if len(message) > TELEGRAM_MAX_MESSAGE_LENGTH:
            message = message[: TELEGRAM_MAX_MESSAGE_LENGTH - 3] + "..."
        self._queue.append(message)
        if self._wakeup is not None:
            self._wakeup.set()

    async def flush(self) -> None:
        """Deliver everything queued, then return."""
        first = True
        while self._queue:
            if not first:
                await self._sleep(self._interval)
            first = False
            await self._deliver(self._queue.popleft())

    async def run(self) -> None:
        self._wakeup = asyncio.Event()
        while True:
            await self.flush()
            self._wakeup.clear()
            if not self._queue:
                await self._wakeup.wait()
            await self._sleep(self._interval)

    async def _deliver(self, message: str) -> None:
        for attempt in range(1, self._max_attempts + 1):
            try:
                await self._send_fn(self._chat_id, message)
                return
            except Exception as exc:
                # Log the exception type only: some client errors embed the
                # request URL, which contains the bot token.
                logger.warning(
                    "telegram send failed (attempt %d/%d): %s",
                    attempt, self._max_attempts, type(exc).__name__,
                )
                if attempt < self._max_attempts:
                    await self._sleep(min(30.0, 2.0 ** attempt))
        logger.error("telegram alert dropped after %d attempts: %s", self._max_attempts, message)


class TelegramBot:
    """Thin python-telegram-bot glue: long-polls for messages and passes
    each one to a CommandRouter. Every text message goes to the router,
    which does the chat-id check, so ignored attempts get logged."""

    def __init__(self, token: str) -> None:
        from telegram.ext import Application

        self._app = Application.builder().token(token).build()

    async def send_message(self, chat_id: str, text: str) -> None:
        await self._app.bot.send_message(chat_id=chat_id, text=text)

    async def start(self, router: CommandRouter) -> None:
        from telegram.ext import MessageHandler, filters

        async def on_message(update, _context) -> None:
            message = update.effective_message
            chat = update.effective_chat
            if message is None or chat is None:
                return
            reply = await router.handle(chat.id, message.text)
            if reply is not None:
                await message.reply_text(reply)

        self._app.add_handler(MessageHandler(filters.TEXT, on_message))
        await self._app.initialize()
        await self._app.start()
        # Keep updates queued while the bot was down: a /kill sent then
        # should still kill. A stale YES is harmless (nothing is pending
        # in a fresh router).
        await self._app.updater.start_polling()

    async def stop(self) -> None:
        if self._app.updater.running:
            await self._app.updater.stop()
        if self._app.running:
            await self._app.stop()
        await self._app.shutdown()
