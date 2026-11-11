# UNO Q Internet Radio v3 - daily listening facts
#
# All numbers are computed here in Python from the sessions and history tables.
# Gemma only turns these facts into a few friendly sentences; the web UI shows
# the same numbers next to the summary, so nothing depends on the model's maths.
#
# SPDX-License-Identifier: MIT

import datetime as dt
from collections import Counter, defaultdict

PARTS = (("morning", 5, 12), ("afternoon", 12, 17), ("evening", 17, 22), ("night", 22, 29))


def day_bounds(day):
    """(start, end) epoch seconds for a YYYY-MM-DD in the board's local time."""
    d = dt.date.fromisoformat(day)
    start = dt.datetime.combine(d, dt.time()).timestamp()
    end = dt.datetime.combine(d + dt.timedelta(days=1), dt.time()).timestamp()
    return start, end


def part_of_day(ts):
    h = dt.datetime.fromtimestamp(ts).hour
    h = h + 24 if h < 5 else h
    for name, a, b in PARTS:
        if a <= h < b:
            return name
    return "night"


def fmt_minutes(m):
    m = int(round(m))
    if m < 60:
        return f"{m} min"
    h, r = divmod(m, 60)
    return f"{h} h {r} min" if r else f"{h} h"


def compute_facts(store, day):
    start, end = day_bounds(day)
    per_station = defaultdict(float)
    per_station_part = defaultdict(lambda: defaultdict(float))
    per_part = defaultdict(float)
    names = {st["id"]: st["name"] for st in store.list_stations()}   # current names, even after a rename
    for s in store.sessions_between(start, end):
        name = names.get(s["station_id"], s["station_name"])
        a, b = max(s["started_at"], start), min(s["ended_at"], end)
        t = a
        while t < b:                                 # walk in 1-minute steps
            step = min(60.0, b - t)
            part = part_of_day(t)
            per_station[name] += step / 60
            per_station_part[name][part] += step / 60
            per_part[part] += step / 60
            t += step

    songs = [h for h in store.history_between(start, end) if h.get("kind") == "song" and h.get("artist")]
    artists = Counter(h["artist"] for h in songs)
    distinct = {(h["artist"].lower(), (h.get("song") or "").lower()) for h in songs}

    stations = sorted(per_station.items(), key=lambda kv: -kv[1])
    total = sum(per_station.values())
    return {
        "day": day,
        "weekday": dt.date.fromisoformat(day).strftime("%A %d %B").replace(" 0", " "),
        "total_minutes": round(total, 1),
        "stations": [{"name": n, "minutes": round(m, 1),
                      "mostly": max(per_station_part[n].items(), key=lambda kv: kv[1])[0]}
                     for n, m in stations if m >= 1],
        "parts": {name: round(per_part.get(name, 0.0), 1) for name, _, _ in PARTS},
        "busiest_part": max(per_part.items(), key=lambda kv: kv[1])[0] if per_part else "",
        "songs": len(distinct),
        "top_artists": [{"name": a, "plays": c} for a, c in artists.most_common(5)],
    }


def facts_as_text(f):
    """The exact text Gemma is given. Plain sentences work better than JSON for a 1B model."""
    lines = [f"Facts for {f['weekday']}:",
             f"- Total listening: {fmt_minutes(f['total_minutes'])} across {len(f['stations'])} "
             f"station{'s' if len(f['stations']) != 1 else ''}."]
    if f["stations"]:
        tops = "; ".join(f"{s['name']} {fmt_minutes(s['minutes'])} (mostly {s['mostly']})" for s in f["stations"][:4])
        lines.append(f"- Top stations: {tops}.")
    if f["busiest_part"]:
        lines.append(f"- Busiest time of day: {f['busiest_part']}.")
    if f["songs"]:
        arts = ", ".join(f"{a['name']} ({a['plays']})" for a in f["top_artists"][:3])
        lines.append(f"- Songs heard: {f['songs']}." + (f" Most played artists: {arts}." if arts else ""))
    else:
        lines.append("- No song titles were recorded (talk or news stations, or stations without song info).")
    return "\n".join(lines)


def fallback_summary(f):
    """Used when Gemma is off or unavailable - same facts, plain template."""
    if not f["stations"]:
        return f"No listening recorded on {f['weekday']}."
    top = f["stations"][0]
    text = (f"You listened for {fmt_minutes(f['total_minutes'])} on {f['weekday']}, "
            f"mostly {top['name']} ({fmt_minutes(top['minutes'])}).")
    if f["busiest_part"]:
        text += f" Your busiest listening was in the {f['busiest_part']}."
    if f["top_artists"]:
        text += f" Most played artist: {f['top_artists'][0]['name']}."
    return text
