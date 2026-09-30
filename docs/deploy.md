# Deploy: systemd on the VM

The bot runs as the systemd service `predcup`, from `deploy/predcup.service`.
It restarts on a crash after 10 s; after 5 starts in 10 minutes systemd stops
trying and the bot stays down (resting quotes expire on their own
`expirationDate`).

## Install (once)

Assumes Ubuntu/Debian with Python 3.12 and the repo at `/opt/predcup`.

```bash
sudo useradd --system --create-home --home-dir /var/lib/predcup --shell /usr/sbin/nologin predcup
sudo git clone <repo-url> /opt/predcup
cd /opt/predcup
sudo python3.12 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo cp .env.example .env
sudo nano .env                      # SIG_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
sudo mkdir -p data
sudo chown -R predcup:predcup /opt/predcup
sudo chmod 600 .env

sudo cp deploy/predcup.service /etc/systemd/system/predcup.service
sudo systemctl daemon-reload
sudo systemctl enable --now predcup
```

If the checkout is not at `/opt/predcup` or the user is not `predcup`, edit
`WorkingDirectory`, `ExecStart`, `ReadWritePaths`, `User` and `Group` in
the installed unit first.

## Watchdog

The unit is `Type=notify` with `WatchdogSec=60s`. `predcup.watchdog` sends
`READY=1` once the loops are running, then `WATCHDOG=1` every 20 s, but
only while each loop in `watchdog.loops` (kalshi_poll, quoter,
reconciliation) has started an iteration within
`watchdog.max_silence_seconds` (180 s). A hung loop alerts once on Telegram
and stops the pings; a blocked event loop stops them too. After 60 s
without a ping systemd kills the bot and restarts it. The new process
cancels all Cup orders (live mode) before its loops start; if any remain,
it halts and alerts.

`main.py` must run `Watchdog.run` (via `App.add_task`). Without it the bot
never sends `READY=1`, so systemd treats the start as failed after
`TimeoutStartSec` (120 s) and restarts it in a loop.

`journalctl -u predcup | grep -i watchdog` shows watchdog kills.

## Day to day

```bash
systemctl status predcup            # running? last restart?
journalctl -u predcup -f            # live logs
sudo systemctl restart predcup      # after a config change or git pull
sudo systemctl stop predcup         # SIGINT: bot cancels all Cup orders (live mode) in a finally block, 30 s grace
```

Update: `cd /opt/predcup && sudo -u predcup git pull && sudo -u predcup .venv/bin/pip install -r requirements.txt && sudo systemctl restart predcup`.
If the unit file changed, copy it again and run `sudo systemctl daemon-reload`.

After a crash loop hits the limit: fix the cause, then
`sudo systemctl reset-failed predcup && sudo systemctl start predcup`.

## Kill switch

Either of these cancels all orders and halts quoting until the next restart:

- Telegram `/kill` from `TELEGRAM_CHAT_ID` (messages from any other chat are ignored).
- `sudo -u predcup touch /opt/predcup/KILL` (checked every `kill_switch.poll_interval_seconds`).

A `KILL` file present at startup kills again immediately, so a restart with
the file still there stays halted. To resume: `sudo rm /opt/predcup/KILL`,
then `sudo systemctl restart predcup`.

## Restart after a kill

A kill (Telegram `/kill`, the `KILL` file, a reconciliation mismatch, a
whole-batch rejection) latches: the bot stays up but halted, and every
order is refused until the process restarts. To resume:

```bash
cd /opt/predcup
sudo rm -f KILL                                   # 1. delete KILL if present (else it kills again at start)
sudo systemctl restart predcup                    # 2. restart (the size ramp resumes one step lower)
sleep 75                                          # 3. let the first reconciliation run (every 60 s)
sudo -u predcup .venv/bin/python -m scripts.bot_status   # 4. confirm
```

`scripts.bot_status` is read-only. It must print `No open Cup orders.` and
a `clean` latest reconciliation dated after the restart, then `OK` (exit
code 0). If it shows open orders, a mismatch, or no reconciliation yet,
don't let it quote: `touch KILL` again and find out why first.

A kill caused by a reconciliation mismatch also dropped the size ramp one
step; a restart drops it one more. Use `/resetramp` only if you want launch
size again, not to raise it.

`/resetramp` resets the size ramp to launch size; the bot asks you to reply
`YES` within `telegram.confirm_timeout_seconds`. With the bot stopped, use
`sudo -u predcup .venv/bin/python -m scripts.reset_ramp` instead.
