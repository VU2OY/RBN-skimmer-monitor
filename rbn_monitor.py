#!/usr/bin/env python3
"""RBN skimmer watchdog.

Watches the Reverse Beacon Network telnet feeds (CW/RTTY on 7000, FT8/FT4 on
7001) and wspr.live (WSPR) for spots from your own skimmer, and sends a
Telegram alert when a mode has been silent for longer than its threshold.
Send /status to the bot at any time for a summary.

Standard library only.  Usage:
    rbn_monitor.py --config config.ini            run the monitor
    rbn_monitor.py --config config.ini --test     send a test Telegram message
    rbn_monitor.py --config config.ini --dry-run  print alerts instead of sending
"""
import argparse
import asyncio
import configparser
import fnmatch
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("rbn_monitor")

SPOT_RE = re.compile(r"^DX de (\S+?):\s+([\d.]+)\s+(\S+)\s+(\S+)")
MODE_GROUP = {"CW": "CW", "RTTY": "RTTY", "FT8": "FT8", "FT4": "FT8"}
FEED_PORT = {"CW": 7000, "RTTY": 7000, "FT8": 7001}
WSPR_POLL_SECS = 300
FEED_WARN_SECS = 600  # warn if we ourselves can't reach a feed for this long


def hhmm(ts):
    if not ts:
        return "never"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d-%b %H:%M UTC")


def mins_ago(ts, now):
    return "never" if not ts else f"{int((now - ts) // 60)} min ago"


class Monitor:
    def __init__(self, cfg, dry_run=False):
        self.token = cfg.get("telegram", "bot_token", fallback="").strip()
        self.chat_id = cfg.get("telegram", "chat_id", fallback="").strip()
        self.dry_run = dry_run or not (self.token and self.chat_id)
        self.pattern = cfg.get("skimmer", "callsign_pattern").strip().upper()
        self.login = cfg.get("skimmer", "login_call").strip().upper()
        self.host = cfg.get("rbn", "host", fallback="telnet.reversebeacon.net")
        self.thresholds = {m.upper(): int(v) * 60 for m, v in cfg.items("thresholds")}

        now = time.time()
        self.started = now
        self.last_seen = {m: None for m in self.thresholds}
        self.alerted = {m: False for m in self.thresholds}
        self.counts = {m: 0 for m in self.thresholds}
        self.spotters = set()
        ports = {FEED_PORT[m] for m in self.thresholds if m in FEED_PORT}
        self.feeds = {p: {"up_since": None, "down_since": now, "alerted": False} for p in ports}
        self.wspr = {"last_ok": None, "fail_since": None, "alerted": False}

    # ---- Telegram -------------------------------------------------------
    def _tg(self, method, params, timeout=20):
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        data = urllib.parse.urlencode(params).encode()
        with urllib.request.urlopen(urllib.request.Request(url, data), timeout=timeout) as r:
            return json.load(r)

    async def send(self, text):
        log.info("ALERT: %s", text.replace("\n", " | "))
        if self.dry_run:
            return
        for attempt in range(3):
            try:
                await asyncio.to_thread(self._tg, "sendMessage", {"chat_id": self.chat_id, "text": text})
                return
            except Exception as e:
                log.warning("Telegram send failed (%s), attempt %d", e, attempt + 1)
                await asyncio.sleep(10)

    async def telegram_commands(self):
        offset = None
        try:  # skip any backlog of old messages
            r = await asyncio.to_thread(self._tg, "getUpdates", {"timeout": 0})
            if r.get("result"):
                offset = r["result"][-1]["update_id"] + 1
        except Exception as e:
            log.warning("Telegram getUpdates failed: %s", e)
        while True:
            try:
                params = {"timeout": 50}
                if offset is not None:
                    params["offset"] = offset
                r = await asyncio.to_thread(self._tg, "getUpdates", params, 70)
                for u in r.get("result", []):
                    offset = u["update_id"] + 1
                    msg = u.get("message") or {}
                    if str(msg.get("chat", {}).get("id")) != self.chat_id:
                        continue
                    if msg.get("text", "").strip().lower().startswith("/status"):
                        await self.send(self.status_text())
            except Exception as e:
                log.warning("Telegram getUpdates failed: %s", e)
                await asyncio.sleep(30)

    def status_text(self):
        now = time.time()
        lines = [f"RBN skimmer status ({self.pattern})"]
        for m, thr in self.thresholds.items():
            icon = "🔴" if self.alerted[m] else "✅"
            lines.append(f"{icon} {m}: last spot {hhmm(self.last_seen[m])} "
                         f"({mins_ago(self.last_seen[m], now)}), limit {thr // 60} min")
        for p, f in self.feeds.items():
            lines.append(f"RBN feed :{p} {'connected' if f['up_since'] else 'DISCONNECTED'}")
        if "WSPR" in self.thresholds:
            lines.append(f"wspr.live last checked {mins_ago(self.wspr['last_ok'], now)}")
        if self.spotters:
            lines.append("Spotter calls seen: " + ", ".join(sorted(self.spotters)))
        return "\n".join(lines)

    # ---- spots ----------------------------------------------------------
    async def spot(self, mode, ts):
        prev = self.last_seen[mode]
        if prev and ts <= prev:
            return
        self.last_seen[mode] = ts
        self.counts[mode] += 1
        log.debug("%s spot at %s", mode, hhmm(ts))
        if self.alerted[mode]:
            self.alerted[mode] = False
            await self.send(f"🟢 {self.pattern} {mode} skimmer is spotting again "
                            f"(was silent since {hhmm(prev)})")

    async def rbn_feed(self, port):
        backoff = 5
        while True:
            writer = None
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_connection(self.host, port), 30)
                buf = b""
                while b"call" not in buf.lower():
                    chunk = await asyncio.wait_for(reader.read(1024), 30)
                    if not chunk:
                        raise ConnectionError("closed before login prompt")
                    buf += chunk
                writer.write(self.login.encode() + b"\r\n")
                await writer.drain()
                await self.feed_up(port)
                backoff = 5
                while True:
                    raw = await asyncio.wait_for(reader.readline(), 180)
                    if not raw:
                        raise ConnectionError("closed by server")
                    m = SPOT_RE.match(raw.decode("latin-1").strip())
                    if not m:
                        continue
                    spotter, mode = m.group(1).upper(), MODE_GROUP.get(m.group(4).upper())
                    if mode in self.thresholds and fnmatch.fnmatchcase(spotter, self.pattern):
                        self.spotters.add(spotter)
                        await self.spot(mode, time.time())
            except (OSError, asyncio.TimeoutError, ConnectionError) as e:
                log.warning("RBN feed :%d error: %s", port, e or type(e).__name__)
            finally:
                if writer:
                    writer.close()
                f = self.feeds[port]
                if f["up_since"]:
                    f["up_since"], f["down_since"] = None, time.time()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)

    async def feed_up(self, port):
        f = self.feeds[port]
        f["up_since"] = time.time()
        log.info("RBN feed :%d connected", port)
        if f["alerted"]:
            f["alerted"] = False
            await self.send(f"🟢 Monitor reconnected to RBN feed :{port}")

    def _wspr_query(self):
        like = self.pattern.replace("*", "%")
        if not re.fullmatch(r"[A-Z0-9/%_-]+", like):
            raise ValueError(f"unsupported callsign pattern for WSPR: {self.pattern}")
        sql = (f"SELECT toUnixTimestamp(max(time)) FROM wspr.rx WHERE rx_sign LIKE '{like}' "
               f"AND time > now() - INTERVAL 2 DAY FORMAT TabSeparated")
        url = "https://db1.wspr.live/?query=" + urllib.parse.quote(sql)
        with urllib.request.urlopen(url, timeout=30) as r:
            return int(r.read().decode().strip() or 0)

    async def wspr_poll(self):
        while True:
            try:
                ts = await asyncio.to_thread(self._wspr_query)
                self.wspr["last_ok"] = time.time()
                self.wspr["fail_since"] = None
                if self.wspr["alerted"]:
                    self.wspr["alerted"] = False
                    await self.send("🟢 Monitor can reach wspr.live again")
                if ts:
                    await self.spot("WSPR", ts)
            except Exception as e:
                log.warning("wspr.live query failed: %s", e)
                self.wspr["fail_since"] = self.wspr["fail_since"] or time.time()
            await asyncio.sleep(WSPR_POLL_SECS)

    # ---- periodic checks ------------------------------------------------
    async def checker(self):
        last_summary = time.time()
        while True:
            await asyncio.sleep(30)
            now = time.time()
            for mode, thr in self.thresholds.items():
                ref = self.last_seen[mode] or self.started
                if self.alerted[mode] or now - ref < thr:
                    continue
                if mode == "WSPR":
                    # wspr.live has a few minutes of lag; only judge while it answers
                    ok = self.wspr["last_ok"] and now - self.wspr["last_ok"] < 2 * WSPR_POLL_SECS
                else:
                    # only judge if we were connected for the whole window
                    up = self.feeds[FEED_PORT[mode]]["up_since"]
                    ok = up and up <= now - thr
                if ok:
                    self.alerted[mode] = True
                    await self.send(f"🔴 {self.pattern} {mode} skimmer: no spots for "
                                    f"{int((now - ref) // 60)} min (last spot {hhmm(self.last_seen[mode])})")

            for port, f in self.feeds.items():
                if not f["up_since"] and not f["alerted"] and now - f["down_since"] > FEED_WARN_SECS:
                    f["alerted"] = True
                    await self.send(f"⚠️ Monitor can't reach RBN feed :{port} — "
                                    f"skimmer status unknown (check the Pi's internet)")
            fs = self.wspr["fail_since"]
            if fs and not self.wspr["alerted"] and now - fs > 1800:
                self.wspr["alerted"] = True
                await self.send("⚠️ Monitor can't reach wspr.live — WSPR status unknown")

            if now - last_summary >= 900:
                last_summary = now
                log.info("Spots in last 15 min: %s", self.counts)
                self.counts = {m: 0 for m in self.thresholds}

    async def run(self):
        log.info("Watching %s for modes %s%s", self.pattern, list(self.thresholds),
                 " (dry run: alerts only logged)" if self.dry_run else "")
        tasks = [self.checker()]
        tasks += [self.rbn_feed(p) for p in self.feeds]
        if "WSPR" in self.thresholds:
            tasks.append(self.wspr_poll())
        if not self.dry_run:
            tasks.append(self.telegram_commands())
        await self.send(f"▶️ RBN monitor started for {self.pattern} "
                        f"({', '.join(f'{m} {t // 60}m' for m, t in self.thresholds.items())}). "
                        f"Send /status any time.")
        await asyncio.gather(*tasks)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.ini")))
    ap.add_argument("--dry-run", action="store_true", help="log alerts instead of sending them")
    ap.add_argument("--test", action="store_true", help="send a test Telegram message and exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    cfg = configparser.ConfigParser()
    if not cfg.read(args.config):
        raise SystemExit(f"Cannot read config file {args.config}")
    mon = Monitor(cfg, args.dry_run)
    if args.test:
        if mon.dry_run:
            raise SystemExit("Set bot_token and chat_id in the config first")
        asyncio.run(mon.send(f"✅ Test message from RBN monitor ({mon.pattern})"))
        return
    try:
        asyncio.run(mon.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
