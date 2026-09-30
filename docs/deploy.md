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

## Day to day

```bash
systemctl status predcup            # running? last restart?
journalctl -u predcup -f            # live logs
sudo systemctl restart predcup      # after a config change or git pull
sudo systemctl stop predcup         # SIGINT: bot cancels orders, 30 s grace
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

`/resetramp` resets the size ramp to launch size; the bot asks you to reply
`YES` within `telegram.confirm_timeout_seconds`. With the bot stopped, use
`sudo -u predcup .venv/bin/python -m scripts.reset_ramp` instead.
