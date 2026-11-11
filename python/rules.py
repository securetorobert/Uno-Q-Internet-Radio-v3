# UNO Q Internet Radio v3 - instant, rule-based understanding
#
# Gemma 3 1B is good but slow on the UNO Q's CPU, so the easy cases are handled
# here first, in microseconds:
#   * parse_command()  - "next", "turn it down a bit", "volume 30", "play cool fm"
#   * clean_title()    - "NOW PLAYING: BURNA BOY - LAST LAST (Clean) | coolfm.ng"
# Each returns confident=False when the text needs Gemma.
#
# SPDX-License-Identifier: MIT

import difflib
import re
import unicodedata

ACTIONS = ("play", "next", "previous", "pause", "resume", "volume_up", "volume_down",
           "set_volume", "mute", "unmute", "none")


def norm(text):
    """Lowercase, strip accents and punctuation, collapse spaces."""
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = re.sub(r"[^a-z0-9%]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
_FILLER = r"(?:please |pls |abeg |can you |could you |would you |hey radio |radio |just |now )*"
_SIMPLE = [
    (r"(?:next|skip|next station|skip (?:this|it)|change (?:the )?station|another station)", "next"),
    (r"(?:previous|prev|back|go back|last station|previous station)", "previous"),
    (r"(?:pause|stop|stop (?:it|the music|playing)|be quiet|silence)", "pause"),
    (r"(?:play|resume|unpause|start|continue|start playing|play music|turn (?:it|the radio) on)", "resume"),
    (r"(?:mute|mute (?:it|the radio|sound))", "mute"),
    (r"(?:unmute|un mute|sound on|unmute (?:it|the radio))", "unmute"),
]
_UP = r"(?:louder|turn (?:it )?up|volume up|increase (?:the )?volume|raise (?:the )?volume|more volume|up)"
_DOWN = (r"(?:quieter|softer|lower|turn (?:it )?down|volume down|decrease (?:the )?volume|"
         r"reduce (?:the )?volume|less volume|too loud|it s too loud|down)")
_SMALL = r"(?: a (?:bit|little|touch|little bit))"
_BIG = r"(?: a lot| much more| loads)"


def match_station(query, stations, threshold=0.78):
    """Best station for a free-text name, or (None, score)."""
    q = norm(query)
    if not q:
        return None, 0.0
    best, best_score = None, 0.0
    for st in stations:
        name = norm(st["name"])
        score = difflib.SequenceMatcher(None, q, name).ratio()
        # "cool fm" should match "cool fm 96 9 lagos": reward whole-word prefix/containment
        if f" {q} " in f" {name} ":
            score = max(score, 0.85 + 0.15 * len(q) / max(len(name), 1))
        q_words, n_words = set(q.split()), set(name.split())
        if q_words and q_words <= n_words:
            score = max(score, 0.8 + 0.2 * len(q_words) / len(n_words))
        if score > best_score:
            best, best_score = st, score
    return (best, best_score) if best_score >= threshold else (None, best_score)


def match_genre(query, stations):
    q = norm(query)
    hits = [s for s in stations if s.get("genre") and norm(s["genre"]) == q]
    return hits[0] if len(hits) == 1 else None


def parse_command(text, stations):
    """Return (command_dict, confident). command keys: action, station_id, volume, delta."""
    t = norm(text)
    t = re.sub(r"^" + _FILLER, "", t).strip()
    if not t:
        return {"action": "none"}, True

    for pattern, action in _SIMPLE:
        if re.fullmatch(pattern, t):
            return {"action": action}, True

    m = re.fullmatch(r"(?:set )?(?:the )?(?:volume|vol)(?: to| at)? (\d{1,3}) ?%?|(\d{1,3}) ?%|volume (\d{1,3}) percent", t)
    if m:
        v = int(next(g for g in m.groups() if g))
        return {"action": "set_volume", "volume": max(0, min(100, v))}, True
    if re.fullmatch(r"(?:max(?:imum)?|full) volume|volume (?:max|full)", t):
        return {"action": "set_volume", "volume": 100}, True

    for base, action in ((_UP, "volume_up"), (_DOWN, "volume_down")):
        m = re.fullmatch(base + r"(" + _SMALL + "|" + _BIG + r")?", t)
        if m:
            size = m.group(1) or ""
            delta = 5 if re.search(_SMALL, size) else 20 if re.search(_BIG, size) else 10
            return {"action": action, "delta": delta}, True

    m = re.fullmatch(r"(?:play|put on|switch to|change to|tune to|tune in to|go to|listen to|i want) (?:the )?(.+?)(?: station| radio| please)?", t)
    name = m.group(1) if m else t
    st, _ = match_station(name, stations)
    if st:
        return {"action": "play", "station_id": st["id"]}, True
    st = match_genre(name, stations)
    if st:
        return {"action": "play", "station_id": st["id"]}, True
    return {"action": "none"}, False


def validate_command(cmd, stations):
    """Check a command (e.g. from Gemma) against the real station list. Returns a clean dict or None."""
    if not isinstance(cmd, dict):
        return None
    action = str(cmd.get("action", "")).strip().lower().replace(" ", "_")
    action = {"prev": "previous", "stop": "pause", "louder": "volume_up", "quieter": "volume_down",
              "volume": "set_volume", "play_station": "play"}.get(action, action)
    if action not in ACTIONS:
        return None
    out = {"action": action}
    if action == "play":
        ids = {s["id"] for s in stations}
        try:
            sid = int(cmd.get("station_id"))
        except (TypeError, ValueError):
            return None
        if sid not in ids:
            return None
        out["station_id"] = sid
    if action == "set_volume":
        try:
            out["volume"] = max(0, min(100, int(round(float(cmd.get("volume"))))))
        except (TypeError, ValueError):
            return None
    if action in ("volume_up", "volume_down"):
        try:
            out["delta"] = max(1, min(50, int(cmd.get("delta", 10))))
        except (TypeError, ValueError):
            out["delta"] = 10
    return out


# --------------------------------------------------------------------------
# Song titles
# --------------------------------------------------------------------------
_PREFIX = re.compile(r"^\s*(?:now playing|now on air|on air|playing now|playing|now|np|current song|song)\s*[:\-–|>]+\s*", re.I)
_URL = re.compile(r"(?:https?://)?(?:www\.)?[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:com|ng|net|org|fm|radio|info|co\.uk|uk|live|online|io|app)(?:/\S*)?", re.I)
_JUNK_TAGS = re.compile(r"\s*[\(\[](?:clean|explicit|dirty|radio edit|radio version|official (?:audio|video|music video)|"
                        r"lyrics?|lyric video|audio|hq|hd|visuali[sz]er|remastered(?: \d{4})?|\d{4} remaster)[\)\]]", re.I)
_SEP = re.compile(r"\s+[-\u2013\u2014~/]\s+|\s+\*{2}\s+")
_SPLIT = re.compile(r"\s+[|\u2022\u00b7]\s+|\s{3,}")
_FEAT = re.compile(r"\s*\b(?:ft\.?|feat\.?|featuring)\s+", re.I)
_ACRONYMS = {"dj", "mc", "uk", "usa", "fm", "am", "tv", "ii", "iii", "iv", "nyc", "la", "abba", "r&b", "ac/dc", "mo", "ok", "bbc", "npr"}
_AD_WORDS = re.compile(r"\b(?:advert(?:isement)?|commercial|sponsored|jingle|promo|ad break|station id)\b", re.I)
_SHOW_WORDS = re.compile(r"\b(?:news|show|live|programme|program|with|hour|breakfast|drive ?time|traffic|"
                         r"weather|talk|sports?|update|bulletin|headlines|interview|phone[- ]in|morning|evening)\b", re.I)


def _fix_case(s):
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return s
    upper = sum(c.isupper() for c in letters) / len(letters)
    lower = sum(c.islower() for c in letters) / len(letters)
    if upper < 0.8 and lower < 0.95:
        return s                       # already mixed case: trust it

    def word(w):
        if w.lower() in _ACRONYMS:
            return w.upper() if w.lower() not in ("mo", "ok") else w.capitalize()
        parts = re.split(r"(['’])", w.lower())
        out = parts[0][:1].upper() + parts[0][1:]
        return out + "".join(parts[1:])     # "don't" stays "Don't", not "Don'T"
    return " ".join(word(w) for w in s.split(" "))


def _is_station(text, station_n):
    """True if `text` is (part of) the station's own name, e.g. "COOLFM 96.9" for "Cool FM 96.9 Lagos"."""
    t = norm(text)
    if not t or not station_n:
        return False
    squashed_t, squashed_s = t.replace(" ", ""), station_n.replace(" ", "")
    if t == station_n or (len(squashed_t) >= 4 and squashed_t in squashed_s):
        return True
    return difflib.SequenceMatcher(None, squashed_t, squashed_s).ratio() > 0.75


def clean_title(raw, station_name=""):
    """Rule-based tidy. Returns dict(kind, artist, title, confident)."""
    s = (raw or "").strip().strip("\"'")
    s = _PREFIX.sub("", s)
    station_n = norm(station_name)
    # "Wazobia FM: Ayra Starr / Rush" - drop a leading "<station>:" label
    m = re.match(r"^([^:]{2,40}):\s+(.+)$", s)
    if m and station_n and _is_station(m.group(1), station_n):
        s = m.group(2)
    s = _URL.sub("", s)
    s = _JUNK_TAGS.sub("", s)
    parts = [p.strip(" -|:*") for p in _SPLIT.split(s)]
    parts = [p for p in parts if p and not _is_station(p, station_n)]
    junk_left = len(parts) > 1
    s = parts[0] if parts else ""
    s = re.sub(r"\s+", " ", s).strip(" -|:")
    s = _FEAT.sub(" feat. ", s)
    s = _fix_case(s)
    if not s:
        # the text was only the station's own name or a website (a "station ID")
        return {"kind": "other", "artist": "", "title": "", "confident": True}

    if _AD_WORDS.search(s):
        title = re.sub(r"^(?:advert(?:isement)?|commercial|sponsored(?: by)?|ad break)\s*[:\-]?\s*", "", s, flags=re.I)
        return {"kind": "ad", "artist": "", "title": title or s, "confident": True}

    pieces = [p.strip(" *") for p in _SEP.split(s)]
    pieces = [p for p in pieces if p and not _is_station(p, station_n)]
    if len(pieces) == 2 and all(p.strip() for p in pieces):
        artist, title = pieces[0].strip(), pieces[1].strip()
        # "Morning Crossfire with Ayo - Nigeria Info" is a show, not a song
        if _SHOW_WORDS.search(artist) and (station_n and difflib.SequenceMatcher(None, norm(title), station_n).ratio() > 0.6):
            return {"kind": "show", "artist": "", "title": artist, "confident": True}
        return {"kind": "song", "artist": artist, "title": title, "confident": not junk_left}
    rest = pieces[0] if len(pieces) == 1 else s
    if len(pieces) == 1 and _SHOW_WORDS.search(rest):
        return {"kind": "show", "artist": "", "title": rest, "confident": True}
    if not pieces:
        return {"kind": "other", "artist": "", "title": "", "confident": True}
    return {"kind": "other", "artist": "", "title": rest, "confident": False}


def grounded(text, raw):
    """True if every word of `text` appears in `raw` - stops Gemma inventing names."""
    raw_words = set(norm(raw).split())
    words = [w for w in norm(text).split() if w not in ("feat", "the", "a", "and", "of")]
    return all(w in raw_words for w in words)
