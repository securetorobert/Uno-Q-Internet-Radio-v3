# UNO Q Internet Radio v3 - Radio Browser directory client (api.radio-browser.info)
# SPDX-License-Identifier: MIT

import json
import urllib.parse
import urllib.request

USER_AGENT = "UNOQ-InternetRadio/3.0"
SERVERS = [
    "https://de1.api.radio-browser.info",
    "https://nl1.api.radio-browser.info",
    "https://at1.api.radio-browser.info",
]
PLAYABLE = ("MP3", "AAC", "AAC+", "OGG", "UNKNOWN", "")


def _get(path, params):
    query = urllib.parse.urlencode(params)
    last_err = None
    for server in SERVERS:
        try:
            req = urllib.request.Request(f"{server}{path}?{query}",
                                         headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=8) as r:
                return json.load(r)
        except Exception as e:
            last_err = e
            print(f"[rb] {server} failed: {e}")
    raise RuntimeError(f"Radio Browser unreachable: {last_err}")


def _summary(st):
    return {
        "name": (st.get("name") or "").strip(),
        "url": st.get("url_resolved") or st.get("url") or "",
        "codec": st.get("codec") or "",
        "bitrate": st.get("bitrate") or 0,
        "country": st.get("countrycode") or "",
        "genre": ", ".join([t for t in (st.get("tags") or "").split(",") if t][:2]),
        "logo": st.get("favicon") or "",
        "homepage": st.get("homepage") or "",
        "votes": st.get("votes") or 0,
    }


def search(name, countrycode="", limit=20):
    params = {"name": name, "hidebroken": "true", "order": "clickcount",
              "reverse": "true", "limit": str(limit)}
    if countrycode:
        params["countrycode"] = countrycode.upper()
    results = _get("/json/stations/search", params)
    return [_summary(s) for s in results
            if (s.get("codec") or "").upper() in PLAYABLE and (s.get("url_resolved") or s.get("url"))]


_cache = {}


def lookup(name, countrycode=""):
    """Best match for a station name, or None. Cached for the life of the app."""
    key = (name.lower(), countrycode.upper())
    if key not in _cache:
        try:
            hits = search(name, countrycode, limit=10)
        except RuntimeError:
            return None
        _cache[key] = hits[0] if hits else None
        if hits:
            print(f"[rb] '{name}' -> {hits[0]['name']} ({hits[0]['codec']}) {hits[0]['url']}")
    return _cache[key]
