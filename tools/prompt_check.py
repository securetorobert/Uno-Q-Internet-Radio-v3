#!/usr/bin/env python3
# UNO Q Internet Radio v3 - check the Gemma prompts against a real model
#
# Sends the exact prompts the radio uses (from python/ai.py) to any
# OpenAI-compatible chat server and checks the answers the same way the radio does.
#
#   On the UNO Q (App Lab shell, with the v3 App running):
#     python3 tools/prompt_check.py --url http://localhost:9999/v1 --model gemma-3-1b-it-Q4_0
#   On a computer with LM Studio or Ollama serving Gemma 3 1B:
#     python3 tools/prompt_check.py --url http://localhost:1234/v1 --model gemma-3-1b-it
#     python3 tools/prompt_check.py --url http://localhost:11434/v1 --model gemma3:1b
#
# Uses only the Python standard library.
# SPDX-License-Identifier: MIT

import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python"))
import ai          # noqa: E402  (prompts and parsers)
import rules       # noqa: E402

STATIONS = [
    {"id": 1, "name": "BBC World Service", "genre": "News", "country": "GB"},
    {"id": 3, "name": "Radio Paradise", "genre": "Eclectic", "country": "US"},
    {"id": 4, "name": "SomaFM Groove Salad", "genre": "Ambient", "country": "US"},
    {"id": 5, "name": "TSF Jazz", "genre": "Jazz", "country": "FR"},
    {"id": 6, "name": "Classic FM (UK)", "genre": "Classical", "country": "GB"},
    {"id": 7, "name": "Cool FM 96.9 Lagos", "genre": "Pop", "country": "NG"},
    {"id": 8, "name": "Wazobia FM Lagos", "genre": "Pidgin", "country": "NG"},
    {"id": 9, "name": "Nigeria Info Lagos", "genre": "Talk", "country": "NG"},
    {"id": 12, "name": "Brila FM", "genre": "Sport", "country": "NG"},
]

# (request, acceptable answers). Each answer is a dict the validated command must match.
COMMANDS = [
    ("something relaxing to study to", [{"action": "play", "station_id": 4}, {"action": "play", "station_id": 5},
                                        {"action": "play", "station_id": 6}]),
    ("I want to hear the football", [{"action": "play", "station_id": 12}]),
    ("some Nigerian talk radio please", [{"action": "play", "station_id": 9}]),
    ("put on something in pidgin", [{"action": "play", "station_id": 8}]),
    ("world news", [{"action": "play", "station_id": 1}]),
    ("it's way too loud", [{"action": "volume_down"}, {"action": "set_volume"}]),
    ("can you make it a little louder", [{"action": "volume_up"}]),
    ("halve the volume", [{"action": "set_volume"}, {"action": "volume_down"}]),
    ("I don't like this one", [{"action": "next"}]),
    ("what's the capital of France", [{"action": "none"}]),
]

TITLES = [
    ("Cool FM 96.9 Lagos", "COOLFM 96.9 ** WIZKID ** ESSENCE ** coolfm.ng", {"kind": "song", "artist": "Wizkid", "title": "Essence"}),
    ("Nigeria Info Lagos", "The Morning Crossfire | Nigeria Info 99.3FM", {"kind": "show", "title": "The Morning Crossfire"}),
    ("Wazobia FM Lagos", "Wazobia FM: Ayra Starr / Rush", {"kind": "song", "artist": "Ayra Starr", "title": "Rush"}),
    ("Smooth FM 98.1", "SADE  ~  NO ORDINARY LOVE  ~  SMOOTH 98.1", {"kind": "song", "artist": "Sade", "title": "No Ordinary Love"}),
    ("Cool FM 96.9 Lagos", "Sponsored by Indomie | Cool FM", {"kind": "ad"}),
]

FACTS = """Facts for Tuesday 10 November:
- Total listening: 3 h 20 min across 3 stations.
- Top stations: Cool FM 96.9 Lagos 1 h 40 min (mostly morning); Nigeria Info Lagos 55 min (mostly afternoon); TSF Jazz 45 min (mostly evening).
- Busiest time of day: morning.
- Songs heard: 31. Most played artists: Burna Boy (4), Asa (3), Wizkid (2)."""


def chat(url, model, system, user, temperature, max_tokens):
    body = json.dumps({"model": model, "temperature": temperature, "max_tokens": max_tokens,
                       "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
    req = urllib.request.Request(url.rstrip("/") + "/chat/completions", data=body.encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer x"})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.load(r)
    return data["choices"][0]["message"]["content"], time.monotonic() - t0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://localhost:9999/v1")
    ap.add_argument("--model", default="gemma-3-1b-it-Q4_0")
    args = ap.parse_args()
    passed = total = 0
    times = []

    print(f"Model {args.model} at {args.url}\n\nCOMMANDS")
    lines = "\n".join(f"{s['id']} | {s['name']} | {s['genre']} | {s['country']}" for s in STATIONS)
    for request, ok in COMMANDS:
        prompt = ai.CMD_TEMPLATE.format(stations=lines, ex_chill=4, ex_news=1,
                                        current="Cool FM 96.9 Lagos (id 7), playing, volume 60", request=request)
        reply, secs = chat(args.url, args.model, ai.CMD_SYSTEM, prompt, 0.1, 60)
        times.append(secs)
        cmd = rules.validate_command(ai.extract_json(reply), STATIONS)
        good = cmd is not None and any(all(cmd.get(k) == v for k, v in want.items()) for want in ok)
        passed += good; total += 1
        print(f"  [{'PASS' if good else 'FAIL'}] {secs:5.1f}s  {request!r:40} -> {cmd}   raw={reply.strip()[:80]!r}")

    print("\nTITLES")
    for station, raw, want in TITLES:
        prompt = ai.TITLE_TEMPLATE.format(station=station, raw=raw)
        reply, secs = chat(args.url, args.model, ai.TITLE_SYSTEM, prompt, 0.0, 60)
        times.append(secs)
        obj = ai.extract_json(reply) or {}
        safe = rules.grounded(str(obj.get("artist") or ""), raw) and rules.grounded(str(obj.get("title") or ""), raw)
        good = safe and all(rules.norm(str(obj.get(k, ""))) == rules.norm(v) for k, v in want.items())
        passed += good; total += 1
        print(f"  [{'PASS' if good else 'FAIL'}] {secs:5.1f}s  {raw!r:50} -> {obj}{'' if safe else '  (invented words: rejected)'}")

    print("\nSUMMARY")
    reply, secs = chat(args.url, args.model, ai.SUMMARY_SYSTEM, ai.SUMMARY_TEMPLATE.format(facts=FACTS), 0.5, 160)
    times.append(secs)
    text = ai.clean_summary(reply)
    numbers_ok = all(n in FACTS for n in __import__("re").findall(r"\d+", text))
    passed += numbers_ok; total += 1
    print(f"  [{'PASS' if numbers_ok else 'CHECK'}] {secs:5.1f}s  {text}")
    if not numbers_ok:
        print("         (contains a number that is not in the facts - read it before trusting the prompt)")

    print(f"\n{passed}/{total} passed. Median reply time {sorted(times)[len(times) // 2]:.1f} s.")


if __name__ == "__main__":
    main()
