# UNO Q Internet Radio v3 - Gemma 3 1B helper (Arduino "Large Language Model - Text" brick)
#
# The model runs on the UNO Q itself in App Lab's llama.cpp runner. It is set in
# app.yaml:   - arduino:llm:  model: llamacpp:gemma-3-1b-it-Q4_0
#
# Design rules for a 1B model on a small CPU
#   * one request at a time, from one worker thread, in priority order:
#       voice-style commands  >  daily summary  >  title tidying
#   * tiny outputs (a short JSON object or 2-3 sentences) and few-shot prompts
#   * the long, unchanging part of each prompt comes first so llama.cpp can reuse
#     its prompt cache; the changing part (the request) comes last
#   * every answer is checked: station ids must exist, names must appear in the
#     raw text, numbers come from Python, never from the model
#   * if the brick or model is missing, the radio keeps working without AI
#
# SPDX-License-Identifier: MIT

import itertools
import json
import queue
import re
import threading
import time

from rules import validate_command, grounded, _fix_case

MODEL_LABEL = "Gemma 3 1B"
RETRY_AFTER_S = 60

# ---------------------------------------------------------------- prompts
CMD_SYSTEM = ("You control a home internet radio. You turn the listener's request into one JSON "
              "object. Reply with the JSON object only, no other words.")

CMD_TEMPLATE = """Stations (id | name | genre | country):
{stations}

Actions: play (needs "station_id" from the list), next, previous, pause, resume, volume_up, volume_down, set_volume (needs "volume" 0-100), mute, unmute, none.
Pick the station whose name, genre or country best fits the request. Use "none" if the request is not about the radio.

Examples:
Request: put on something relaxing
{{"action":"play","station_id":{ex_chill}}}
Request: I want news
{{"action":"play","station_id":{ex_news}}}
Request: it is a bit loud
{{"action":"volume_down"}}
Request: make it 25 percent
{{"action":"set_volume","volume":25}}
Request: tell me a joke
{{"action":"none"}}

Now playing: {current}.
Request: {request}
"""

TITLE_SYSTEM = ("You clean up the 'now playing' text that radio stations send. Reply with one JSON "
                "object only, no other words.")

TITLE_TEMPLATE = """Return {{"kind": "song" or "show" or "ad" or "other", "artist": "...", "title": "..."}}.
Rules: use only words that appear in the raw text. Remove station names, websites, and tags like (Clean) or [Radio Edit]. Change ALL CAPS to normal capitalisation. For a show, ad or other, put its name in "title" and leave "artist" empty.

Examples:
Station: Cool FM
Raw: NOW PLAYING: BURNA BOY - LAST LAST (Clean) | coolfm.ng
{{"kind":"song","artist":"Burna Boy","title":"Last Last"}}
Station: Nigeria Info
Raw: Morning Crossfire with Ayo | Nigeria Info 99.3
{{"kind":"show","artist":"","title":"Morning Crossfire with Ayo"}}
Station: Smooth FM
Raw: SMOOTH FM 98.1 ** Sade ** Smooth Operator
{{"kind":"song","artist":"Sade","title":"Smooth Operator"}}
Station: Wazobia FM
Raw: Advert - Indomie Noodles
{{"kind":"ad","artist":"","title":"Indomie Noodles"}}

Station: {station}
Raw: {raw}
"""

SUMMARY_SYSTEM = ("You write short, friendly listening summaries for a home radio. Use only the facts "
                  "given. Never invent stations, artists, numbers or times.")

SUMMARY_TEMPLATE = """{facts}

Write 2 or 3 short sentences summarising this day of listening, speaking to the listener as "you". Plain text only: no lists, no headings, no emojis."""

_CHILL = ("ambient", "chill", "jazz", "classical", "lounge", "smooth", "relax")
_NEWS = ("news", "talk", "info")


def extract_json(text):
    """First JSON object in a model reply (handles ```json fences and chatter)."""
    if not text:
        return None
    start = text.find("{")
    while start != -1:
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[start:])
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
        start = text.find("{", start + 1)
    return None


def clean_summary(text):
    text = re.sub(r"[*#_`>]+", "", text or "").strip()
    text = re.sub(r"^\s*(?:summary|here is.*?:)\s*:?\s*", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text)
    if len(text) > 600:
        cut = text[:600].rsplit(". ", 1)[0]
        text = cut + "."
    return text


class _Job:
    def __init__(self, kind, prompt, on_done):
        self.kind = kind
        self.prompt = prompt
        self.on_done = on_done          # callback(result_text or None, ms, error)
        self.cancelled = False


class Gemma:
    """Owns the three LLM clients and the single worker thread."""

    PRIORITY = {"command": 0, "summary": 1, "title": 2}
    MAX_TITLE_BACKLOG = 6

    def __init__(self, on_status=None):
        self.enabled = True
        self.state = "starting"          # starting | ready | busy | unavailable | off
        self.detail = ""
        self.last_ms = 0
        self.jobs_done = 0
        self._on_status = on_status or (lambda s: None)
        self._q = queue.PriorityQueue()
        self._seq = itertools.count()
        self._clients = None
        self._next_try = 0.0
        self._failures = 0

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="gemma").start()
        return self

    # ------------------------------------------------------------ status
    def status(self):
        return {"model": MODEL_LABEL, "enabled": self.enabled,
                "state": "off" if not self.enabled else self.state,
                "detail": self.detail, "last_ms": self.last_ms, "queue": self._q.qsize(),
                "jobs_done": self.jobs_done}

    def _set(self, state, detail=None):
        self.state = state
        if detail is not None:
            self.detail = detail
        try:
            self._on_status(self.status())
        except Exception as e:            # a status display problem must never stop the worker
            print(f"[gemma] status callback failed: {e}")

    @property
    def available(self):
        return self.enabled and self.state in ("ready", "busy")

    # ------------------------------------------------------------ public API
    def command(self, request, stations, current, timeout=75):
        """Blocking. Returns (validated command dict or None, ms, error)."""
        if not stations:
            return None, 0, "no stations"
        chill = next((s for s in stations if any(k in (s.get("genre") or "").lower() for k in _CHILL)), stations[0])
        news = next((s for s in stations if any(k in (s.get("genre") or "").lower() or k in s["name"].lower()
                                                for k in _NEWS)), stations[-1])
        lines = "\n".join(f"{s['id']} | {s['name']} | {s.get('genre') or '-'} | {s.get('country') or '-'}"
                          for s in stations)
        prompt = CMD_TEMPLATE.format(stations=lines, ex_chill=chill["id"], ex_news=news["id"],
                                     current=current, request=request.strip()[:200])
        done = threading.Event()
        box = {}

        def finished(text, ms, err):
            box.update(text=text, ms=ms, err=err)
            done.set()

        job = self._submit("command", prompt, finished)
        if job is None:
            return None, 0, self.detail or "Gemma is off"
        if not done.wait(timeout):
            job.cancelled = True
            return None, timeout * 1000, "Gemma took too long - try a simpler request"
        if box["err"]:
            return None, box["ms"], box["err"]
        cmd = validate_command(extract_json(box["text"]), stations)
        if cmd is None:
            print(f"[gemma] unusable command reply: {box['text']!r}")
        return cmd, box["ms"], None if cmd else "I couldn't work out which station or action you meant"

    def tidy_title(self, raw, station_name, on_result):
        """Async. on_result(clean_dict or None, ms) - None means keep the rule-based result."""
        prompt = TITLE_TEMPLATE.format(station=station_name or "-", raw=raw[:200])

        def finished(text, ms, err):
            if err:
                return on_result(None, ms)
            obj = extract_json(text) or {}
            kind = str(obj.get("kind", "")).lower()
            artist = str(obj.get("artist") or "").strip()[:120]
            title = str(obj.get("title") or "").strip()[:160]
            if kind not in ("song", "show", "ad", "other") or not title:
                print(f"[gemma] unusable title reply: {text!r}")
                return on_result(None, ms)
            if not (grounded(artist, raw) and grounded(title, raw)):
                print(f"[gemma] rejected invented words: {obj} for {raw!r}")
                return on_result(None, ms)
            if kind != "song":
                artist = ""
            on_result({"kind": kind, "artist": _fix_case(artist), "title": _fix_case(title)}, ms)

        return self._submit("title", prompt, finished) is not None

    def summarize(self, facts_text, on_result):
        """Async. on_result(text or None, ms, error)."""
        def finished(text, ms, err):
            on_result(clean_summary(text) if text and not err else None, ms, err)
        return self._submit("summary", SUMMARY_TEMPLATE.format(facts=facts_text), finished) is not None

    # ------------------------------------------------------------ worker
    def _submit(self, kind, prompt, on_done):
        if not self.enabled or self.state == "unavailable":
            return None
        if kind == "title":
            backlog = [j for _, _, j in list(self._q.queue) if j.kind == "title" and not j.cancelled]
            for old in backlog[:max(0, len(backlog) - self.MAX_TITLE_BACKLOG + 1)]:
                old.cancelled = True     # stale titles: keep their rule-based tidy
        job = _Job(kind, prompt, on_done)
        self._q.put((self.PRIORITY[kind], next(self._seq), job))
        return job

    def _connect(self):
        from arduino.app_bricks.llm import LargeLanguageModel   # brick listed in app.yaml
        common = {"timeout": 180}
        self._clients = {
            "command": LargeLanguageModel(system_prompt=CMD_SYSTEM, temperature=0.1, max_tokens=60, **common),
            "title": LargeLanguageModel(system_prompt=TITLE_SYSTEM, temperature=0.0, max_tokens=60, **common),
            "summary": LargeLanguageModel(system_prompt=SUMMARY_SYSTEM, temperature=0.5, max_tokens=160, **common),
        }
        for c in self._clients.values():
            c.with_memory(max_messages=0)     # every request stands alone
        t0 = time.monotonic()
        self._clients["command"].init()       # loads the model into the runner
        print(f"[gemma] {MODEL_LABEL} loaded in {time.monotonic() - t0:.1f} s")

    def _run(self):
        while True:
            if self.enabled and self._clients is None and time.monotonic() >= self._next_try:
                self._set("starting", "Loading Gemma 3 1B (first start can take a minute)")
                try:
                    self._connect()
                    self._set("ready", "")
                except Exception as e:
                    self._clients = None
                    self._next_try = time.monotonic() + RETRY_AFTER_S
                    self._set("unavailable", f"{type(e).__name__}: {e}"[:300])
                    print(f"[gemma] not available, retrying in {RETRY_AFTER_S} s: {e}")
                    self._fail_pending()
            try:
                _, _, job = self._q.get(timeout=5)
            except queue.Empty:
                continue
            if job.cancelled:
                continue
            if not self.enabled or self._clients is None:
                job.on_done(None, 0, self.detail or "Gemma is not available")
                continue
            self._set("busy")
            t0 = time.monotonic()
            text, err = None, None
            try:
                text = self._clients[job.kind].chat(job.prompt)
                self._failures = 0
            except Exception as e:
                print(f"[gemma] {job.kind} failed: {e}")
                err = "Gemma isn't responding right now"
                self._failures += 1
                if self._failures >= 2:          # runner probably restarted: reconnect later
                    self._clients = None
                    self._next_try = time.monotonic() + RETRY_AFTER_S
                    self.detail = f"{type(e).__name__}: {e}"[:300]
            ms = int((time.monotonic() - t0) * 1000)
            self.last_ms = ms
            self.jobs_done += 1
            if not job.cancelled:
                try:
                    job.on_done(text, ms, err)
                except Exception as e:
                    print(f"[gemma] callback error: {e}")
            self._set("ready" if self._clients else "unavailable")

    def _fail_pending(self):
        while True:
            try:
                _, _, job = self._q.get_nowait()
            except queue.Empty:
                return
            if not job.cancelled:
                job.on_done(None, 0, self.detail)
