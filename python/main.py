# UNO Q Internet Radio v3 - Linux side (Qualcomm QRB2210, Arduino App Lab)
#
# New in v3 (Gemma 3 1B on the board, via the Large Language Model brick)
#   * "Ask the radio": plain-language commands ("play something chill",
#     "turn it down a bit"). Simple ones are understood instantly by rules.py;
#     the rest go to Gemma, whose answer is checked against the station list.
#   * tidy song titles: messy "now playing" text becomes artist + title
#   * daily listening summary: Python counts, Gemma writes 2-3 sentences
#
# From v2: SQLite station database, web UI on port 7000, ICY song titles.
# From v1: the MCU sketch, Modulino Buttons control and LED matrix (unchanged).
#
# SPDX-License-Identifier: MIT

import datetime as dt
import json
import os
import queue
import threading
import time

from fastapi import HTTPException
from arduino.app_utils import App, Bridge
from arduino.app_bricks.web_ui import WebUI

import insights
import radiobrowser
import rules
from ai import Gemma
from matrix import render
from player import Player, make_output, IDLE, CONNECTING, PLAYING, ERROR
from store import RadioStore

HERE = os.path.dirname(os.path.abspath(__file__))

# Button event codes - must match the #defines in sketch.ino
EV_PREV, EV_PLAY, EV_NEXT, EV_VOL_DOWN, EV_VOL_UP, EV_MUTE = 1, 2, 3, 4, 5, 6

# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------
store = RadioStore(os.path.join(HERE, "stations_seed.json"))
cfg = store.seed_cfg

output = make_output(cfg.get("audio_device", "auto"), int(cfg.get("sample_rate", 48000)))

commands = queue.Queue()       # everything that changes playback goes through here
status_lock = threading.Lock()
now = {                         # "now playing" information shown in the web UI
    "title": "",
    "title_since": 0.0,
    "connected_url": "",
    "started_at": 0.0,
    "song": None,               # tidied: {kind, artist, title, by}
}
session_id = None               # open listening session (for the daily summary)
stations = []                   # enabled stations, in play order (cache)
current_id = store.get_setting("current_id")
playing_wanted = bool(store.get_setting("playing", True))

AI_DEFAULTS = {"ai_enabled": True, "ai_commands": True, "ai_titles": True,
               "ai_summary_auto": True, "ai_summary_time": "21:00"}
ai_cfg = {k: store.get_setting(k, v) for k, v in AI_DEFAULTS.items()}


def refresh_stations():
    global stations
    stations = store.list_stations(only_enabled=True)


def current_station():
    for st in stations:
        if st["id"] == current_id:
            return st
    return None


def current_index():
    for i, st in enumerate(stations):
        if st["id"] == current_id:
            return i
    return -1


# --------------------------------------------------------------------------
# Player callbacks (run on the player thread)
# --------------------------------------------------------------------------
def close_session():
    global session_id
    if session_id is not None:
        store.touch_session(session_id)
        session_id = None


def on_state(state):
    if state != PLAYING:
        close_session()
    push_status()


def on_title(title):
    with status_lock:
        if title == now["title"]:
            return
        now["title"] = title
        now["title_since"] = time.time()
        now["song"] = None
    st = current_station()
    if title and st:
        print(f"[now playing] {st['name']}: {title}")
        row_id = store.add_history(st, title)
        tidy(title, st, row_id)
        ui.send_message("history", store.recent_history())
    push_status()


def tidy(raw, st, row_id):
    """Rules first; Gemma only for text the rules aren't sure about. Results are cached."""
    cached = store.cached_title(raw)
    if cached:
        clean, by = cached
        apply_clean(raw, row_id, clean, by)
        return
    clean = rules.clean_title(raw, st["name"])
    confident = clean.pop("confident")
    apply_clean(raw, row_id, clean, "rules")
    if confident:
        store.cache_title(raw, clean, "rules")
        return
    if not (ai_cfg["ai_titles"] and gemma.available):
        return

    def done(result, ms):
        if result is None:
            store.cache_title(raw, clean, "rules")     # Gemma failed: keep the rule result
            return
        print(f"[gemma] tidied in {ms} ms: {raw!r} -> {result}")
        store.cache_title(raw, result, "gemma")
        apply_clean(raw, row_id, result, "gemma")
        ui.send_message("history", store.recent_history())
        push_status()

    gemma.tidy_title(raw, st["name"], done)


def apply_clean(raw, row_id, clean, by):
    if row_id:
        store.set_history_clean(row_id, clean, by)
    with status_lock:
        if now["title"] == raw:
            now["song"] = dict(clean, by=by)


def on_connected(url, info):
    global session_id
    with status_lock:
        now["connected_url"] = url
        now["started_at"] = time.time()
    st = current_station()
    if st:
        close_session()
        session_id = store.open_session(st)
        store.mark_played(st["id"])
        if url != st.get("resolved_url"):
            store.remember_resolved(st["id"], url)
    push_status()


player = Player(output, on_state=on_state, on_title=on_title, on_connected=on_connected)
player.volume = int(store.get_setting("volume", cfg.get("default_volume", 60)))
player.muted = bool(store.get_setting("muted", False))


def url_source_for(station):
    """Callable used by the player: URLs to try, in order, for one station."""
    def gen():
        seen = set()
        for u in (station.get("url"), station.get("resolved_url")):
            if u and u not in seen:
                seen.add(u)
                yield u
        if station.get("rb_search"):
            hit = radiobrowser.lookup(station["rb_search"], station.get("rb_countrycode") or "")
            if hit and hit["url"] not in seen:
                store.remember_resolved(station["id"], hit["url"], hit)
                yield hit["url"]
    return gen


# --------------------------------------------------------------------------
# Playback control (only ever called from the main loop thread)
# --------------------------------------------------------------------------
def tune(station_id=None, step=0):
    global current_id
    if not stations:
        player.stop()
        current_id = None
        return
    idx = current_index()
    if station_id is not None:
        ids = [s["id"] for s in stations]
        idx = ids.index(station_id) if station_id in ids else 0
    else:
        idx = (max(idx, 0) + step) % len(stations)
    st = stations[idx]
    current_id = st["id"]
    store.set_setting("current_id", current_id)
    with status_lock:
        now.update(title="", title_since=0.0, connected_url="", started_at=0.0, song=None)
    print(f"[radio] station {idx + 1}: {st['name']}")
    if playing_wanted:
        player.play(url_source_for(st))
    push_status()


def set_playing(wanted):
    global playing_wanted
    playing_wanted = wanted
    store.set_setting("playing", wanted)
    if wanted and current_station():
        player.play(url_source_for(current_station()))
    else:
        player.stop()
        with status_lock:
            now.update(title="", connected_url="", song=None)
    push_status()


def set_volume(v):
    player.volume = max(0, min(100, int(v)))
    if player.volume > 0:
        player.muted = False
    store.set_setting("volume", player.volume)
    store.set_setting("muted", player.muted)
    push_status()


def set_mute(m):
    player.muted = bool(m)
    store.set_setting("muted", player.muted)
    push_status()


def handle(cmd, arg=None):
    global playing_wanted
    if cmd == "button":
        if arg == EV_NEXT:
            tune(step=1)
        elif arg == EV_PREV:
            tune(step=-1)
        elif arg == EV_PLAY:
            set_playing(not playing_wanted)
        elif arg == EV_VOL_UP:
            set_volume(player.volume + 5)
        elif arg == EV_VOL_DOWN:
            set_volume(player.volume - 5)
        elif arg == EV_MUTE:
            set_mute(not player.muted)
    elif cmd == "next":
        tune(step=1)
    elif cmd == "prev":
        tune(step=-1)
    elif cmd == "select":
        tune(station_id=arg)                     # keeps the current play/pause state
    elif cmd == "play_id":
        playing_wanted = True
        store.set_setting("playing", True)
        tune(station_id=arg)
    elif cmd == "toggle":
        set_playing(not playing_wanted)
    elif cmd == "pause":
        set_playing(False)
    elif cmd == "resume":
        set_playing(True)
    elif cmd == "volume":
        set_volume(arg)
    elif cmd == "volume_rel":
        set_volume(player.volume + arg)
    elif cmd == "mute":
        set_mute(not player.muted if arg is None else arg)
    elif cmd == "reload":
        # station list changed in the database
        old = current_station()
        refresh_stations()
        new = current_station()
        if new is None:
            tune(station_id=stations[0]["id"] if stations else None)
        elif arg == current_id and old != new:
            tune(station_id=current_id)          # current station was edited
        ui.send_message("stations", stations_payload())
        push_status()


# --------------------------------------------------------------------------
# Status for the web UI
# --------------------------------------------------------------------------
_last_pushed = None


def status():
    st = current_station()
    with status_lock:
        info = dict(player.info)
        return {
            "state": player.state,
            "playing_wanted": playing_wanted,
            "volume": player.volume,
            "muted": player.muted,
            "station": st,
            "index": current_index() + 1,
            "count": len(stations),
            "title": now["title"],
            "title_since": now["title_since"],
            "started_at": now["started_at"],
            "url": now["connected_url"],
            "stream": {
                "codec": info.get("codec", ""),
                "bitrate": info.get("bitrate") or info.get("icy_bitrate") or "",
                "sample_rate": info.get("sample_rate", 0),
                "icy_name": info.get("icy_name", ""),
                "icy_genre": info.get("icy_genre", ""),
                "metadata": info.get("metadata", False),
            },
            "song": now["song"],
            "audio_device": getattr(output, "device", ""),
            "ai": dict(gemma.status(), **{k: ai_cfg[k] for k in ("ai_commands", "ai_titles")}),
            "server_time": time.time(),
        }


def push_status(force=False):
    global _last_pushed
    s = status()
    key = json.dumps({k: v for k, v in s.items() if k != "server_time"}, sort_keys=True, default=str)
    if force or key != _last_pushed:
        _last_pushed = key
        ui.send_message("status", s)


def stations_payload():
    return {"stations": store.list_stations(), "current_id": current_id}


# --------------------------------------------------------------------------
# Web UI: REST API (the page itself is served from /app/assets)
# --------------------------------------------------------------------------
ui = WebUI()


def _bad(e):
    raise HTTPException(status_code=400, detail=str(e))


def api_status():
    return status()


def api_list_stations():
    return stations_payload()


def api_add_station(payload: dict):
    try:
        st = store.add_station(payload)
    except ValueError as e:
        _bad(e)
    commands.put(("reload", None))
    return st


def api_update_station(station_id: int, payload: dict):
    if not store.get_station(station_id):
        raise HTTPException(status_code=404, detail="No such station")
    try:
        st = store.update_station(station_id, payload)
    except ValueError as e:
        _bad(e)
    commands.put(("reload", station_id))
    return st


def api_delete_station(station_id: int):
    store.delete_station(station_id)
    commands.put(("reload", station_id))
    return {"ok": True}


def api_move_station(station_id: int, payload: dict):
    store.move_station(station_id, int(payload.get("direction", 1)))
    commands.put(("reload", None))
    return {"ok": True}


def api_play(payload: dict):
    sid = payload.get("station_id")
    if sid is None:
        commands.put(("toggle", None))
    else:
        commands.put(("play_id", int(sid)))
    return {"ok": True}


def api_control(payload: dict):
    action = payload.get("action")
    if action not in ("next", "prev", "toggle", "mute"):
        _bad(f"unknown action {action!r}")
    commands.put((action, None))
    return {"ok": True}


def api_volume(payload: dict):
    try:
        v = int(payload["volume"])
    except (KeyError, TypeError, ValueError):
        _bad("volume must be a number 0-100")
    commands.put(("volume", v))
    return {"ok": True}


def api_history(limit: int = 25):
    return store.recent_history(min(max(limit, 1), 200))


def api_clear_history():
    store.clear_history()
    ui.send_message("history", [])
    return {"ok": True}


def api_search(q: str, country: str = ""):
    if len(q.strip()) < 2:
        _bad("Type at least 2 characters")
    try:
        return radiobrowser.search(q.strip(), country.strip(), limit=25)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))


# --------------------------------------------------------------------------
# v3: Ask the radio
# --------------------------------------------------------------------------
def describe(cmd):
    """Short confirmation shown under the ask box. Written by code, not by Gemma."""
    a = cmd["action"]
    if a == "play":
        st = next((s for s in stations if s["id"] == cmd["station_id"]), None)
        return f"Playing {st['name']}." if st else "Playing."
    return {"next": "Next station.", "previous": "Previous station.", "pause": "Paused.",
            "resume": "Playing.", "mute": "Muted.", "unmute": "Sound on.",
            "volume_up": f"Volume up to {min(100, player.volume + cmd.get('delta', 10))}.",
            "volume_down": f"Volume down to {max(0, player.volume - cmd.get('delta', 10))}.",
            "set_volume": f"Volume set to {cmd.get('volume')}."}.get(a, "")


def execute(cmd):
    a = cmd["action"]
    if a == "play":
        commands.put(("play_id", cmd["station_id"]))
    elif a in ("next", "pause", "resume"):
        commands.put((a, None))
    elif a == "previous":
        commands.put(("prev", None))
    elif a == "mute":
        commands.put(("mute", True))
    elif a == "unmute":
        commands.put(("mute", False))
    elif a == "volume_up":
        commands.put(("volume_rel", cmd.get("delta", 10)))
    elif a == "volume_down":
        commands.put(("volume_rel", -cmd.get("delta", 10)))
    elif a == "set_volume":
        commands.put(("volume", cmd["volume"]))


HELP = "Try 'play Cool FM', 'something relaxing', 'turn it down a bit' or 'next'."


def api_ask(payload: dict):
    text = str(payload.get("text", "")).strip()[:200]
    if not text:
        _bad("Type a request")
    t0 = time.monotonic()
    cmd, confident = rules.parse_command(text, stations)
    source, ms, err = "rules", int((time.monotonic() - t0) * 1000), None
    if not confident:
        if ai_cfg["ai_commands"] and gemma.available:
            st = current_station()
            current = (f"{st['name']} (id {st['id']}), {'playing' if playing_wanted else 'paused'}, "
                       f"volume {player.volume}") if st else "nothing"
            cmd, ms, err = gemma.command(text, stations, current)
            source = "gemma"
        else:
            why = "Gemma is off" if not (gemma.enabled and ai_cfg["ai_commands"]) else "Gemma isn't available right now"
            cmd, err = None, f"{why}, so I only understand simple requests"
    if not cmd or cmd["action"] == "none":
        reply = ("Sorry, I couldn't do that. " + HELP) if not err else f"{err.rstrip('.')}. {HELP}"
        if cmd and cmd["action"] == "none" and not err:
            reply = "That doesn't sound like a radio request. " + HELP
        return {"ok": False, "reply": reply, "source": source, "ms": ms}
    reply = describe(cmd)
    execute(cmd)
    print(f"[ask] {text!r} -> {cmd} via {source} in {ms} ms")
    return {"ok": True, "reply": reply, "action": cmd, "source": source, "ms": ms}


# --------------------------------------------------------------------------
# v3: Insights (daily listening summary)
# --------------------------------------------------------------------------
summary_pending = set()


def today():
    return dt.date.today().isoformat()


def summary_payload(day):
    row = store.get_summary(day)
    return {"day": day, "facts": insights.compute_facts(store, day),
            "summary": None if not row else {"text": row["text"], "created_at": row["created_at"],
                                             "ms": row["ms"], "written_by": row["written_by"]},
            "pending": day in summary_pending}


def request_summary(day):
    if day in summary_pending:
        return True
    facts = insights.compute_facts(store, day)
    if not facts["stations"] or not (ai_cfg["ai_enabled"] and gemma.available):
        store.save_summary(day, insights.fallback_summary(facts), facts, 0, "template")
        ui.send_message("insights", summary_payload(day))
        return False

    def done(text, ms, err):
        summary_pending.discard(day)
        if text:
            store.save_summary(day, text, facts, ms, "gemma")
            print(f"[gemma] summary for {day} in {ms} ms")
        else:
            store.save_summary(day, insights.fallback_summary(facts), facts, 0, "template")
        ui.send_message("insights", summary_payload(day))

    summary_pending.add(day)
    if not gemma.summarize(insights.facts_as_text(facts), done):
        summary_pending.discard(day)
        store.save_summary(day, insights.fallback_summary(facts), facts, 0, "template")
    ui.send_message("insights", summary_payload(day))
    return True


def api_insights(day: str = ""):
    day = day or today()
    try:
        dt.date.fromisoformat(day)
    except ValueError:
        _bad("day must be YYYY-MM-DD")
    days = []
    for i in range(7):
        d = (dt.date.today() - dt.timedelta(days=i)).isoformat()
        f = insights.compute_facts(store, d)
        days.append({"day": d, "minutes": f["total_minutes"], "has_summary": store.get_summary(d) is not None})
    return dict(summary_payload(day), days=days)


def api_make_summary(payload: dict):
    day = str(payload.get("day") or today())
    try:
        dt.date.fromisoformat(day)
    except ValueError:
        _bad("day must be YYYY-MM-DD")
    return {"queued": request_summary(day)}


def api_ai_settings():
    return dict(ai_cfg, status=gemma.status())


def api_set_ai_settings(payload: dict):
    for k in ("ai_enabled", "ai_commands", "ai_titles", "ai_summary_auto"):
        if k in payload:
            ai_cfg[k] = bool(payload[k])
            store.set_setting(k, ai_cfg[k])
    if "ai_summary_time" in payload:
        v = str(payload["ai_summary_time"])
        try:
            dt.datetime.strptime(v, "%H:%M")
        except ValueError:
            _bad("time must be HH:MM")
        ai_cfg["ai_summary_time"] = v
        store.set_setting("ai_summary_time", v)
    gemma.enabled = ai_cfg["ai_enabled"]
    push_status()
    return api_ai_settings()


ui.expose_api("POST", "/api/ask", api_ask)
ui.expose_api("GET", "/api/insights", api_insights)
ui.expose_api("POST", "/api/insights/summary", api_make_summary)
ui.expose_api("GET", "/api/ai", api_ai_settings)
ui.expose_api("POST", "/api/ai", api_set_ai_settings)
ui.expose_api("GET", "/api/status", api_status)
ui.expose_api("GET", "/api/stations", api_list_stations)
ui.expose_api("POST", "/api/stations", api_add_station)
ui.expose_api("PUT", "/api/stations/{station_id}", api_update_station)
ui.expose_api("DELETE", "/api/stations/{station_id}", api_delete_station)
ui.expose_api("POST", "/api/stations/{station_id}/move", api_move_station)
ui.expose_api("POST", "/api/play", api_play)
ui.expose_api("POST", "/api/control", api_control)
ui.expose_api("POST", "/api/volume", api_volume)
ui.expose_api("GET", "/api/history", api_history)
ui.expose_api("DELETE", "/api/history", api_clear_history)
ui.expose_api("GET", "/api/search", api_search)

# WebSocket: send a full snapshot to each browser as it connects, and accept
# a "volume" message so the slider can update without a REST round trip.
ui.on_connect(lambda sid: (ui.send_message("status", status()),
                           ui.send_message("stations", stations_payload()),
                           ui.send_message("history", store.recent_history())))
ui.on_message("volume", lambda sid, data: commands.put(("volume", int(data.get("volume", 0)))))

# --------------------------------------------------------------------------
# MCU bridge: buttons in, matrix frames and LEDs out
# --------------------------------------------------------------------------
def on_button(code):
    commands.put(("button", int(code)))


Bridge.provide("button_event", on_button)

last_frame = None
last_leds = None
tick = 0
last_tick_time = 0.0
last_heartbeat = 0.0
last_minute = 0.0


def update_outputs():
    global last_frame, last_leds
    frame = render(max(current_index(), 0), player.state, player.volume, player.muted, tick)
    leds = (0b010 if player.state == PLAYING else 0) | \
           (0b101 if player.state == CONNECTING and tick % 2 else 0)
    try:
        if frame != last_frame:
            Bridge.call("show_frame", frame)
            last_frame = frame
        if leds != last_leds:
            Bridge.call("set_leds", leds)
            last_leds = leds
    except Exception as e:
        print(f"[bridge] {e}")
        last_frame = last_leds = None
        time.sleep(0.5)


def daily_jobs():
    """Once a minute: keep the open session's end time fresh; write the evening summary."""
    if session_id is not None:
        store.touch_session(session_id)
    if not ai_cfg["ai_summary_auto"]:
        return
    day = today()
    if dt.datetime.now().strftime("%H:%M") >= ai_cfg["ai_summary_time"] and store.get_summary(day) is None \
            and day not in summary_pending:
        if insights.compute_facts(store, day)["total_minutes"] >= 10:
            print(f"[radio] writing the daily summary for {day}")
            request_summary(day)


def loop():
    global tick, last_tick_time, last_heartbeat, last_minute
    try:
        while True:
            cmd, arg = commands.get_nowait()
            try:
                handle(cmd, arg)
            except Exception as e:
                print(f"[radio] command {cmd} failed: {e}")
    except queue.Empty:
        pass
    t = time.monotonic()
    if t - last_tick_time > 0.4:
        tick += 1
        last_tick_time = t
    if t - last_heartbeat > 10:            # keeps elapsed time in browsers honest
        last_heartbeat = t
        push_status(force=True)
    if t - last_minute > 60:
        last_minute = t
        try:
            daily_jobs()
        except Exception as e:
            print(f"[radio] daily jobs failed: {e}")
    update_outputs()
    time.sleep(0.05)


# Gemma starts last, once the web UI exists to receive its status updates
gemma = Gemma(on_status=lambda s: push_status())
gemma.enabled = bool(ai_cfg["ai_enabled"])
gemma.start()

refresh_stations()
print(f"[radio] {len(stations)} enabled stations in database; web UI at {ui.url}")
if stations:
    commands.put(("select", current_id if current_station() else stations[0]["id"]))
App.run(user_loop=loop)
