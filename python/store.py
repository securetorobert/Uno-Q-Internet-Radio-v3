# UNO Q Internet Radio v3 - SQLite storage (Arduino "Database - SQL" brick)
#
# Tables
#   stations     - the station list, in play order
#   settings     - key/value pairs: current station id, volume, mute, AI switches
#   history      - "now playing" texts, raw and tidied (artist / title / kind)
#   title_cache  - raw text -> tidied result, so Gemma never cleans the same text twice
#   sessions     - listening sessions (station, start, end) for the daily summary
#   summaries    - one Gemma-written summary per day, with the facts it was given
#
# A v2 database is upgraded in place: missing tables are created and the new
# history columns are added with ALTER TABLE.
#
# The database lives at /app/data/radio.db inside the App folder, so it
# survives restarts and App Lab updates. All queries use bound parameters.
#
# SPDX-License-Identifier: MIT

import json
import os
import time

from arduino.app_bricks.dbstorage_sqlstore import SQLStore

HISTORY_LIMIT = 200

STATION_COLUMNS = {
    "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "name": "TEXT NOT NULL",
    "url": "TEXT DEFAULT ''",             # direct stream URL (optional)
    "rb_search": "TEXT DEFAULT ''",       # Radio Browser fallback search
    "rb_countrycode": "TEXT DEFAULT ''",
    "resolved_url": "TEXT DEFAULT ''",    # last URL that actually played
    "genre": "TEXT DEFAULT ''",
    "country": "TEXT DEFAULT ''",
    "logo": "TEXT DEFAULT ''",            # favicon / logo URL
    "homepage": "TEXT DEFAULT ''",
    "position": "INTEGER DEFAULT 0",
    "enabled": "INTEGER DEFAULT 1",
    "play_count": "INTEGER DEFAULT 0",
    "last_played": "REAL DEFAULT 0",
    "created_at": "REAL DEFAULT 0",
}

EDITABLE = ("name", "url", "rb_search", "rb_countrycode", "genre", "country",
            "logo", "homepage", "enabled")


class RadioStore:
    def __init__(self, seed_file):
        self.db = SQLStore("radio.db")
        self.db.create_table("stations", STATION_COLUMNS)
        self.db.create_table("settings", {"key": "TEXT PRIMARY KEY", "value": "TEXT"})
        self.db.create_table("history", {
            "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
            "station_id": "INTEGER",
            "station_name": "TEXT",
            "title": "TEXT",
            "played_at": "REAL",
        })
        self._add_columns("history", {"artist": "TEXT DEFAULT ''", "song": "TEXT DEFAULT ''",
                                      "kind": "TEXT DEFAULT ''", "cleaned_by": "TEXT DEFAULT ''"})
        self.db.create_table("title_cache", {"raw": "TEXT PRIMARY KEY", "result": "TEXT", "cleaned_by": "TEXT"})
        self.db.create_table("sessions", {
            "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
            "station_id": "INTEGER",
            "station_name": "TEXT",
            "started_at": "REAL",
            "ended_at": "REAL",
        })
        self.db.create_table("summaries", {
            "day": "TEXT PRIMARY KEY",          # YYYY-MM-DD, board local time
            "text": "TEXT",
            "facts": "TEXT",                    # JSON of the numbers Gemma was given
            "created_at": "REAL",
            "ms": "INTEGER",
            "written_by": "TEXT",               # "gemma" or "template"
        })
        self.seed_cfg = {}
        if os.path.exists(seed_file):
            with open(seed_file, "r", encoding="utf-8") as f:
                self.seed_cfg = json.load(f)
        if self.count_stations() == 0:
            self._seed(self.seed_cfg.get("stations", []))

    # ------------------------------------------------------------ helpers
    def _q(self, sql, args=()):
        return self.db.execute_sql(sql, tuple(args)) or []

    def _add_columns(self, table, columns):
        have = {r["name"] for r in self._q(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in have:
                self._q(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                print(f"[db] upgraded {table}: added column {name}")

    def _seed(self, stations):
        for i, st in enumerate(stations):
            self.add_station(st, position=i)
        print(f"[db] seeded {len(stations)} stations from stations_seed.json")

    # ------------------------------------------------------------ stations
    def count_stations(self):
        return self._q("SELECT COUNT(*) AS n FROM stations")[0]["n"]

    def list_stations(self, only_enabled=False):
        sql = "SELECT * FROM stations"
        if only_enabled:
            sql += " WHERE enabled = 1"
        return self._q(sql + " ORDER BY position ASC, id ASC")

    def get_station(self, station_id):
        rows = self._q("SELECT * FROM stations WHERE id = ?", (int(station_id),))
        return rows[0] if rows else None

    def add_station(self, data, position=None):
        clean = self._clean(data)
        if not clean.get("name"):
            raise ValueError("Station name is required")
        if not clean.get("url") and not clean.get("rb_search"):
            raise ValueError("Give a stream URL or a Radio Browser search name")
        if position is None:
            row = self._q("SELECT COALESCE(MAX(position), -1) + 1 AS p FROM stations")[0]
            position = row["p"]
        clean.setdefault("enabled", 1)
        clean["position"] = int(position)
        clean["created_at"] = time.time()
        cols = ", ".join(clean.keys())
        marks = ", ".join("?" for _ in clean)
        return self._q(f"INSERT INTO stations ({cols}) VALUES ({marks}) RETURNING *",
                       clean.values())[0]

    def update_station(self, station_id, data):
        clean = self._clean(data)
        if "name" in clean and not clean["name"]:
            raise ValueError("Station name is required")
        if not clean:
            return self.get_station(station_id)
        if "url" in clean or "rb_search" in clean:
            clean["resolved_url"] = ""          # force a fresh lookup
        sets = ", ".join(f"{k} = ?" for k in clean)
        self._q(f"UPDATE stations SET {sets} WHERE id = ?",
                list(clean.values()) + [int(station_id)])
        return self.get_station(station_id)

    def delete_station(self, station_id):
        self._q("DELETE FROM stations WHERE id = ?", (int(station_id),))
        self._renumber()

    def move_station(self, station_id, direction):
        rows = self.list_stations()
        ids = [r["id"] for r in rows]
        if int(station_id) not in ids:
            return
        i = ids.index(int(station_id))
        j = i + (1 if direction > 0 else -1)
        if 0 <= j < len(ids):
            ids[i], ids[j] = ids[j], ids[i]
            self.reorder(ids)

    def reorder(self, ordered_ids):
        for pos, sid in enumerate(ordered_ids):
            self._q("UPDATE stations SET position = ? WHERE id = ?", (pos, int(sid)))

    def _renumber(self):
        self.reorder([r["id"] for r in self.list_stations()])

    def remember_resolved(self, station_id, url, extra=None):
        fields = {"resolved_url": url}
        for k in ("logo", "homepage", "genre", "country"):
            v = (extra or {}).get(k)
            if v:
                fields[k] = v
        st = self.get_station(station_id)
        if not st:
            return
        # never overwrite values the user typed in
        fields = {k: v for k, v in fields.items() if k == "resolved_url" or not st.get(k)}
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._q(f"UPDATE stations SET {sets} WHERE id = ?", list(fields.values()) + [int(station_id)])

    def mark_played(self, station_id):
        self._q("UPDATE stations SET play_count = play_count + 1, last_played = ? WHERE id = ?",
                (time.time(), int(station_id)))

    @staticmethod
    def _clean(data):
        out = {}
        for k in EDITABLE:
            if k not in data or data[k] is None:
                continue
            v = data[k]
            if k == "enabled":
                out[k] = 1 if v in (True, 1, "1", "true", "on") else 0
                continue
            v = str(v).strip()[:500]
            if k in ("url", "logo", "homepage") and v and not v.lower().startswith(("http://", "https://")):
                raise ValueError(f"{k} must start with http:// or https://")
            if k == "rb_countrycode":
                v = v.upper()[:2]
            out[k] = v
        return out

    # ------------------------------------------------------------ settings
    def get_setting(self, key, default=None):
        rows = self._q("SELECT value FROM settings WHERE key = ?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0]["value"])
        except ValueError:
            return default

    def set_setting(self, key, value):
        self._q("INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)))

    # ------------------------------------------------------------ history
    def add_history(self, station, raw):
        """Store a new raw title. Returns the row id, or None if it repeats the last one."""
        last = self._q("SELECT title, station_id FROM history ORDER BY id DESC LIMIT 1")
        if last and last[0]["title"] == raw and last[0]["station_id"] == station["id"]:
            return None
        row = self._q("INSERT INTO history (station_id, station_name, title, played_at) "
                      "VALUES (?, ?, ?, ?) RETURNING id",
                      (station["id"], station["name"], raw, time.time()))
        self._q("DELETE FROM history WHERE id NOT IN "
                "(SELECT id FROM history ORDER BY id DESC LIMIT ?)", (HISTORY_LIMIT,))
        return row[0]["id"] if row else None

    def set_history_clean(self, row_id, clean, cleaned_by):
        self._q("UPDATE history SET artist = ?, song = ?, kind = ?, cleaned_by = ? WHERE id = ?",
                (clean.get("artist", ""), clean.get("title", ""), clean.get("kind", ""), cleaned_by, int(row_id)))

    def recent_history(self, limit=25):
        return self._q("SELECT * FROM history ORDER BY id DESC LIMIT ?", (int(limit),))

    def clear_history(self):
        self._q("DELETE FROM history")

    # ------------------------------------------------------------ title cache
    def cached_title(self, raw):
        rows = self._q("SELECT result, cleaned_by FROM title_cache WHERE raw = ?", (raw,))
        if not rows:
            return None
        try:
            return json.loads(rows[0]["result"]), rows[0]["cleaned_by"]
        except ValueError:
            return None

    def cache_title(self, raw, clean, cleaned_by):
        self._q("INSERT INTO title_cache (raw, result, cleaned_by) VALUES (?, ?, ?) "
                "ON CONFLICT(raw) DO UPDATE SET result = excluded.result, cleaned_by = excluded.cleaned_by",
                (raw, json.dumps(clean), cleaned_by))

    # ------------------------------------------------------------ listening sessions
    def open_session(self, station):
        t = time.time()
        row = self._q("INSERT INTO sessions (station_id, station_name, started_at, ended_at) "
                      "VALUES (?, ?, ?, ?) RETURNING id", (station["id"], station["name"], t, t))
        return row[0]["id"]

    def touch_session(self, session_id):
        self._q("UPDATE sessions SET ended_at = ? WHERE id = ?", (time.time(), int(session_id)))

    def sessions_between(self, start, end):
        return self._q("SELECT * FROM sessions WHERE ended_at > ? AND started_at < ? ORDER BY started_at",
                       (start, end))

    def history_between(self, start, end):
        return self._q("SELECT * FROM history WHERE played_at >= ? AND played_at < ? ORDER BY played_at",
                       (start, end))

    # ------------------------------------------------------------ summaries
    def get_summary(self, day):
        rows = self._q("SELECT * FROM summaries WHERE day = ?", (day,))
        return rows[0] if rows else None

    def save_summary(self, day, text, facts, ms, written_by):
        self._q("INSERT INTO summaries (day, text, facts, created_at, ms, written_by) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(day) DO UPDATE SET text = excluded.text, facts = excluded.facts, "
                "created_at = excluded.created_at, ms = excluded.ms, written_by = excluded.written_by",
                (day, text, json.dumps(facts), time.time(), int(ms), written_by))
