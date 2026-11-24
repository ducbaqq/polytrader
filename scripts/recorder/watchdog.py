#!/usr/bin/env python3
"""Recorder watchdog: checks the recorder every minute and alerts on Telegram.

Run by polytrader-watchdog.timer (every minute, as the polytrader user). Standard library only;
Postgres via `psql`.

  - Only actionable problems alert: a check must fail continuously for its threshold (in minutes,
    one run per minute) before it pings. Self-healing blips (a dropped connection that reconnects, a
    short upstream outage, a single restart) never ping; they appear in the daily summary.
  - An alert is repeated every REMIND_MIN while it lasts and reported again when it clears.
  - Restarts alert only as a crash loop (>= 3 within an hour).
  - A daily summary at DAILY_HOUR_UTC proves the alerting itself works.
  - Send /status to the bot for an on-demand report (answered within a minute).
  - Optional HEALTHCHECK_URL (e.g. healthchecks.io, which can notify Telegram) is pinged every run:
    it alerts if this watchdog or the whole droplet goes silent.

Env (from /opt/polytrader/.env): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DATABASE_URL, DATABASE_CA_CERT,
optional HEALTHCHECK_URL, RECORDER_DATA_DIR.

Usage:
  watchdog.py              run all checks once, send alerts (what the timer does)
  watchdog.py --dry-run    run checks, print what would be sent, don't send or save state
  watchdog.py --status     print the status report
  watchdog.py --chat-ids   list chats that messaged the bot (to find TELEGRAM_CHAT_ID)
  watchdog.py --test       send a test message
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

STATE_FILE = os.environ.get("WATCHDOG_STATE", "/opt/polytrader/watchdog-state.json")
DATA_DIR = os.environ.get("RECORDER_DATA_DIR", "/opt/polytrader/data")
SERVICE = "polytrader-recorder"
REMIND_MIN = 180
DAILY_HOUR_UTC = 7  # 08:00 in Prague (winter), 09:00 summer

# Streams that must have rows every minute. Minimum rows per minute for rate checks
# (normal rates: bn_fut_mark 480 = 8 symbols x 60, rtds_price ~840 = 14 symbols x 60).
FLOWING = ["bn_spot_trade", "bn_spot_bbo", "bn_spot_depth", "bn_fut_trade", "bn_fut_bbo", "bn_fut_depth",
           "bn_fut_mark", "pm_price_change", "pm_best_bid_ask", "rtds_price"]
MIN_RATE = {"bn_fut_mark": 400, "rtds_price": 600}


# ---------------------------------------------------------------------------------------------
# helpers

def sh(*cmd: str, timeout: int = 20) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()


def sql(query: str) -> list[list[str]]:
    url = os.environ["DATABASE_URL"]
    ca = os.environ.get("DATABASE_CA_CERT")
    if ca:
        url += ("&" if "?" in url else "?") + "sslrootcert=" + ca
    r = subprocess.run(["psql", url, "-X", "-At", "-F", "\t", "-v", "ON_ERROR_STOP=1", "-c", query],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip().splitlines()[-1] if r.stderr.strip() else f"psql exit {r.returncode}")
    return [line.split("\t") for line in r.stdout.splitlines() if line]


def telegram(method: str, **params) -> dict:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}", data=data, timeout=20) as r:
        return json.loads(r.read())


def send(text: str) -> None:
    for i in range(0, len(text), 4000):  # Telegram limit is 4096 chars
        telegram("sendMessage", chat_id=os.environ["TELEGRAM_CHAT_ID"], text=text[i:i + 4000],
                 disable_web_page_preview="true")


def fmt_dur(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m // 60}h {m % 60}m" if m >= 60 else f"{m}m"


# ---------------------------------------------------------------------------------------------
# checks: each returns {key: (message, runs_needed)} for every failing condition

def check_service(state: dict, events: list[str]) -> dict:
    out = {}
    props = dict(l.split("=", 1) for l in sh("systemctl", "show", SERVICE, "-p", "ActiveState", "-p",
                                               "SubState", "-p", "NRestarts", "-p", "ExecMainStartTimestamp",
                                               "-p", "ExecMainStatus", "-p", "MemoryCurrent").splitlines() if "=" in l)
    if props.get("ActiveState") != "active":
        status = props.get("ExecMainStatus", "?")
        why = " (stopped itself: disk almost full)" if status == "75" else f" (exit status {status})"
        out["service"] = (f"Recorder is NOT running: {props.get('ActiveState')}/{props.get('SubState')}{why}", 2)
    started = props.get("ExecMainStartTimestamp", "")
    if state.get("started") and started and started != state["started"]:
        state.setdefault("restarts", []).append(time.time())
    state["started"] = started
    state["restarts"] = [t for t in state.get("restarts", []) if t > time.time() - 3600]
    if len(state["restarts"]) >= 3:
        out["crashloop"] = (f"Recorder restarted {len(state['restarts'])}x in the last hour (crash loop?)", 1)
    mem = props.get("MemoryCurrent", "")
    if mem.isdigit() and int(mem) > 900 * 2**20:
        out["memory"] = (f"Recorder memory high: {int(mem) / 2**20:.0f} MB", 15)
    return out


def check_host() -> dict:
    out = {}
    free_gb = shutil.disk_usage(DATA_DIR).free / 1e9
    if free_gb < 10:
        out["disk"] = (f"Disk space low: {free_gb:.1f} GB free (recorder stops below 3 GB)", 1)
    if sh("timedatectl", "show", "-p", "NTPSynchronized", "--value") != "yes":
        out["clock"] = ("System clock is not NTP-synchronised (recv_ms timestamps may drift)", 30)
    return out


def check_db() -> dict:
    out = {}
    # Per-stream rows over the last 3 complete minutes (stats are flushed at each minute boundary).
    rows = sql("""SELECT stream, extract(epoch FROM minute)::bigint, rows FROM rec_stats
                  WHERE minute >= date_trunc('minute', now()) - interval '4 minutes'
                    AND minute <  date_trunc('minute', now()) - interval '1 minute'""")
    by_stream: dict[str, list[int]] = {}
    for stream, _, n in rows:
        by_stream.setdefault(stream, []).append(int(n))
    if not rows:
        out["stats"] = ("Recorder has written no stats for 5+ minutes (stalled, or can't reach Postgres)", 3)
    else:
        dead = [s for s in FLOWING if sum(by_stream.get(s, [])) == 0]
        if dead:
            out["streams"] = (f"No data for 5+ min: {', '.join(dead)}", 3)
        for s, minimum in MIN_RATE.items():
            vals = by_stream.get(s, [])
            if vals and max(vals) < minimum:
                out[f"rate:{s}"] = (f"{s} degraded: {max(vals)}/min (normal >= {minimum}) for 15+ min", 13)

    # Connections whose latest event is a close/stale/error more than 90 s ago (= not reconnected),
    # and connections that keep dropping.
    for source, kind, age, drops in sql("""
            WITH e AS (SELECT source, kind, ts, row_number() OVER (PARTITION BY source ORDER BY ts DESC) rn
                       FROM rec_events WHERE ts > now() - interval '30 minutes' AND kind LIKE 'ws_%')
            SELECT source, kind, extract(epoch FROM now() - ts)::int,
                   (SELECT count(*) FROM rec_events r WHERE r.source = e.source AND r.kind IN ('ws_close', 'ws_stale')
                      AND r.ts > now() - interval '15 minutes')
            FROM e WHERE rn = 1"""):
        # Reconnect backoff is at most 30 s, so 5 minutes without a reconnect means it is stuck.
        if kind in ("ws_close", "ws_stale", "ws_error") and int(age) > 300:
            out[f"conn:{source}"] = (f"Connection {source} down for {fmt_dur(int(age))}, not reconnecting", 1)

    # Binance rate limiting is actionable (it escalates to IP bans): 418 at once, repeated 429s.
    # Polymarket 429s are not alerted directly: the recorder pauses that API and backs off, and any
    # lasting effect shows up in the coverage / unresolved / trades checks below.
    for code, n, sample in sql("""SELECT substring(detail FROM '\\m(429|418)\\M'), count(*), max(left(source || ': ' || detail, 200))
                                  FROM rec_events WHERE ts > now() - interval '10 minutes'
                                    AND kind = 'rest_error' AND source LIKE 'bn_%' AND detail ~ '\\m(429|418)\\M'
                                  GROUP BY 1"""):
        if code == "418":
            out["ipban"] = (f"BINANCE IP BAN (HTTP 418, {n}x in 10 min): {sample}", 1)
        elif int(n) >= 5:
            out["ratelimit"] = (f"Binance rate limiting ({n}x in 10 min): {sample}", 3)

    # Format changes (Polymarket or Binance changed something) and recorder bugs.
    thresholds = {"parse_error": 20, "unhandled_rejection": 5, "pm_unexpected_outcomes": 1}
    for kind, n, sample in sql(f"""SELECT kind, count(*), max(left(source || ': ' || coalesce(detail, ''), 200))
                                  FROM rec_events WHERE ts > now() - interval '10 minutes'
                                    AND kind IN ({", ".join(f"'{k}'" for k in thresholds)})
                                  GROUP BY 1"""):
        if int(n) >= thresholds[kind]:
            label = {"parse_error": "unparseable messages (format change?)"}.get(kind, kind)
            out[f"errors:{kind}"] = (f"{n}x {label} in 10 min, e.g. {sample}", 3)

    # Every series must have its current market subscribed.
    missing = sql("""SELECT s.series_slug FROM (SELECT DISTINCT series_slug FROM rec_markets
                                               WHERE window_end > now() - interval '1 day') s
                     WHERE NOT EXISTS (SELECT 1 FROM rec_markets m WHERE m.series_slug = s.series_slug
                                       AND m.window_start <= now() AND m.window_end > now()
                                       AND m.subscribed_at IS NOT NULL)""")
    if missing:
        out["coverage"] = (f"Not recording the current market of: {', '.join(r[0] for r in missing)}", 5)

    # Resolutions and trade downloads (p99 resolution is ~75 s after the window; trades after 2 min).
    (unresolved, no_trades), = sql("""SELECT count(*) FILTER (WHERE outcome IS NULL),
                                             count(*) FILTER (WHERE outcome IS NOT NULL AND api_trades IS NULL)
                                      FROM rec_markets WHERE window_end < now() - interval '60 minutes'
                                        AND window_end > now() - interval '1 day'""")
    if int(unresolved) > 0:
        out["unresolved"] = (f"{unresolved} markets ended >1h ago without a recorded outcome", 30)
    if int(no_trades) > 0:
        out["trades"] = (f"{no_trades} resolved markets without Data API trades (>1h after end)", 30)

    # Archive to Spaces.
    (age_min, failures), = sql("""SELECT coalesce(extract(epoch FROM now() - max(ts) FILTER (WHERE kind = 'archive_ok')) / 60, 9999)::int,
                                         0
                                  FROM rec_events WHERE source = 'archive' AND ts > now() - interval '1 day'""")
    if int(age_min) > 135:
        out["archive"] = (f"No successful archive upload for {fmt_dur(int(age_min) * 60)}", 1)
    return out


def run_checks(state: dict, events: list[str]) -> dict:
    failing = {}
    for name, fn in (("service", lambda: check_service(state, events)), ("host", check_host), ("db", check_db)):
        try:
            failing.update(fn())
        except Exception as e:  # a broken check is itself a problem worth hearing about
            failing[f"check:{name}"] = (f"Watchdog check '{name}' failed: {str(e)[:300]}", 5)
    return failing


# ---------------------------------------------------------------------------------------------
# status report

def status_report() -> str:
    lines = [f"📊 Recorder status {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC"]
    props = dict(l.split("=", 1) for l in sh("systemctl", "show", SERVICE, "-p", "ActiveState",
                                               "-p", "ExecMainStartTimestamp", "-p", "NRestarts").splitlines() if "=" in l)
    lines.append(f"Service: {props.get('ActiveState')} since {props.get('ExecMainStartTimestamp')} (restarts: {props.get('NRestarts')})")
    lines.append(f"Disk free: {shutil.disk_usage(DATA_DIR).free / 1e9:.1f} GB")
    try:
        (rows, mb), = sql("""SELECT coalesce(sum(rows), 0), round(coalesce(sum(bytes), 0) / 1e6)
                             FROM rec_stats WHERE minute > now() - interval '24 hours'""")
        lines.append(f"Last 24h: {int(rows) / 1e6:.0f}M rows ({int(float(mb)) / 1e3:.0f} GB before compression)")
        (n, resolved, trades), = sql("""SELECT count(*), count(outcome), coalesce(sum(api_trades), 0)
                                        FROM rec_markets WHERE window_end > now() - interval '24 hours'
                                          AND window_end < now() - interval '20 minutes'""")
        lines.append(f"Markets ended in 24h: {n}, resolved {resolved}, Data API trades {int(trades):,}")
        # code=1000 = Polymarket closing a single-market connection after the market resolved (expected)
        (drops, rest, gaps, restarts, limited), = sql("""SELECT
                count(*) FILTER (WHERE kind IN ('ws_close', 'ws_stale') AND coalesce(detail, '') NOT LIKE 'code=1000%'),
                count(*) FILTER (WHERE kind = 'rest_error'),
                count(*) FILTER (WHERE kind = 'seq_gap'),
                count(*) FILTER (WHERE kind = 'start'),
                count(*) FILTER (WHERE kind = 'rate_limited')
            FROM rec_events WHERE ts > now() - interval '24 hours'""")
        lines.append(f"Self-healed in 24h: {drops} connection drops, {rest} REST errors, {limited} Polymarket "
                     f"rate-limit pauses, {gaps} depth resyncs, {restarts} restarts")
        arch = sql("""SELECT to_char(ts, 'HH24:MI'), detail FROM rec_events WHERE kind = 'archive_ok'
                      ORDER BY ts DESC LIMIT 1""")
        if arch:
            lines.append(f"Last archive {arch[0][0]} UTC: {arch[0][1]}")
    except Exception as e:
        lines.append(f"Postgres query failed: {e}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# main

def load_env() -> None:
    path = os.environ.get("ENV_FILE", "/opt/polytrader/.env")
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def handle_commands(state: dict, dry: bool) -> None:
    """Answer /status sent to the bot from the configured chat."""
    r = telegram("getUpdates", offset=state.get("update_offset", 0), timeout=0)
    for u in r.get("result", []):
        state["update_offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != os.environ["TELEGRAM_CHAT_ID"]:
            continue  # ignore everyone else
        if (msg.get("text") or "").strip().split("@")[0] in ("/status", "/start"):
            report = status_report() + "\n\n" + ("Open problems:\n" + "\n".join(
                f"🔴 {p['msg']}" for p in state.get("problems", {}).values() if p.get("alerted")) if any(
                p.get("alerted") for p in state.get("problems", {}).values()) else "No open problems ✅")
            print(report) if dry else send(report)


def main() -> None:
    load_env()
    args = set(sys.argv[1:])
    if "--chat-ids" in args:
        for u in telegram("getUpdates").get("result", []):
            c = (u.get("message") or {}).get("chat", {})
            print(c.get("id"), c.get("type"), c.get("username") or c.get("title"))
        return
    if "--test" in args:
        send("✅ polytrader watchdog: test message. Alerts will arrive here.")
        return
    if "--status" in args:
        print(status_report())
        return

    dry = "--dry-run" in args
    try:
        state = json.load(open(STATE_FILE))
    except (OSError, ValueError):
        state = {}
    problems: dict = state.setdefault("problems", {})
    now = time.time()
    events: list[str] = []
    failing = run_checks(state, events)

    messages = list(events)
    for key, (msg, needed) in failing.items():
        p = problems.setdefault(key, {"since": now, "runs": 0, "alerted": False, "last_sent": 0})
        p["runs"] += 1
        p["msg"] = msg
        if not p["alerted"] and p["runs"] >= needed:
            messages.append(f"🔴 PROBLEM: {msg}")
            p["alerted"], p["last_sent"] = True, now
        elif p["alerted"] and now - p["last_sent"] >= REMIND_MIN * 60:
            messages.append(f"🔴 STILL FAILING ({fmt_dur(now - p['since'])}): {msg}")
            p["last_sent"] = now
    for key in [k for k in problems if k not in failing]:
        p = problems.pop(key)
        if p["alerted"]:
            messages.append(f"✅ RESOLVED after {fmt_dur(now - p['since'])}: {p['msg']}")

    hour_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if datetime.now(timezone.utc).hour == DAILY_HOUR_UTC and state.get("daily") != hour_key:
        state["daily"] = hour_key
        open_ = [p["msg"] for p in problems.values() if p["alerted"]]
        messages.append(status_report() + ("\n\nOpen problems:\n" + "\n".join("🔴 " + m for m in open_)
                                           if open_ else "\n\nNo open problems ✅"))

    if dry:
        print("\n\n".join(messages) if messages else "(nothing to send)")
        print("\nfailing checks:", json.dumps({k: v[0] for k, v in failing.items()}, indent=1))
        return

    if messages:
        try:
            send("\n\n".join(messages))
        except Exception as e:  # keep state so problems are re-sent next run
            print(f"telegram send failed: {e}", file=sys.stderr)
            for key in failing:
                if key in problems and problems[key]["last_sent"] == now:
                    problems[key]["alerted"] = False
                    problems[key]["last_sent"] = 0
    try:
        handle_commands(state, dry)
    except Exception as e:
        print(f"telegram getUpdates failed: {e}", file=sys.stderr)

    tmp = STATE_FILE + ".tmp"
    json.dump(state, open(tmp, "w"))
    os.replace(tmp, STATE_FILE)

    hc = os.environ.get("HEALTHCHECK_URL")
    if hc:
        try:
            urllib.request.urlopen(hc, timeout=10).read()
        except Exception as e:
            print(f"healthcheck ping failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
