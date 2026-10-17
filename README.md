# RBN Skimmer Monitor

Get a Telegram message when your [Reverse Beacon Network](https://www.reversebeacon.net/)
skimmer stops reporting spots — and another when it comes back.

It runs on any always-on Linux box (a Raspberry Pi is ideal) **separate from the
skimmer PC**, so it also catches the skimmer PC freezing, losing power or losing
its internet connection.

## What it watches

| Mode | Source | Default alert after |
|---|---|---|
| CW | RBN telnet `telnet.reversebeacon.net:7000` | 60 min |
| RTTY | RBN telnet port 7000 | 24 h |
| FT8 / FT4 | RBN telnet port 7001 | 60 min |
| WSPR | [wspr.live](https://wspr.live/) database | 24 h |

- 🔴 alert when a mode has had no spots from your call for longer than its limit
- 🟢 message when spots resume
- ⚠️ warning (instead of false alarms) if the monitor itself can't reach RBN or wspr.live
- Send `/status` to your bot for last-spot times per mode

Python 3 standard library only — nothing to `pip install`.

## Setup

### 1. Create a Telegram bot
1. In Telegram, open **@BotFather**, send `/newbot`, follow the prompts and copy the **token**.
2. Send your new bot any message (e.g. `hi`).
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy the
   number from `"chat":{"id": ...}`.

### 2. Install
```bash
git clone https://github.com/VU2OY/rbn-skimmer-monitor.git
mkdir -p ~/rbn-monitor
cp rbn-skimmer-monitor/rbn_monitor.py ~/rbn-monitor/
cp rbn-skimmer-monitor/config.example.ini ~/rbn-monitor/config.ini
chmod 600 ~/rbn-monitor/config.ini
nano ~/rbn-monitor/config.ini
```
In `config.ini` set `bot_token`, `chat_id`, your `callsign_pattern`
(e.g. `VU2OY*` matches `VU2OY-#`, `VU2OY-9-#` and `VU2OY`) and `login_call`.

Test that Telegram works:
```bash
python3 ~/rbn-monitor/rbn_monitor.py --config ~/rbn-monitor/config.ini --test
```

### 3. Run it as a service
```bash
sed "s/YOURUSER/$USER/g" rbn-skimmer-monitor/rbn-monitor.service | sudo tee /etc/systemd/system/rbn-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now rbn-monitor
```

Check it:
```bash
systemctl status rbn-monitor
journalctl -u rbn-monitor -f
```
After changing `config.ini`, run `sudo systemctl restart rbn-monitor`.

## Options
```
rbn_monitor.py --config config.ini            run the monitor
rbn_monitor.py --config config.ini --test     send a test Telegram message
rbn_monitor.py --config config.ini --dry-run  log alerts instead of sending them
rbn_monitor.py --config config.ini -v         also log every matching spot
```

## Notes
- RTTY spots are rare outside contests, so keep its limit long or remove the line.
- wspr.live lags a few minutes behind real time.
- Never commit your real `config.ini` — it holds your bot token (it's in `.gitignore`).

## Licence
GPL-3.0 — see [LICENSE](LICENSE).

73 de VU2OY
