# UNO Q Internet Radio v3 - stream player with ICY "now playing" metadata
#
# Plain HTTP streams (Icecast / Shoutcast 2) are opened with urllib and the
# header "Icy-MetaData: 1". The server then interleaves small metadata blocks
# (StreamTitle='Artist - Song';) into the audio every `icy-metaint` bytes.
# IcyReader strips those blocks out, reports the titles, and hands clean audio
# bytes to PyAV for decoding. HLS (.m3u8) streams and servers urllib cannot
# talk to (old "ICY 200 OK" Shoutcast) are opened by PyAV/FFmpeg directly;
# they still play, only without song titles.
#
# SPDX-License-Identifier: MIT

import re
import threading
import time
import urllib.error
import urllib.request

import av
import numpy as np

USER_AGENT = "UNOQ-InternetRadio/3.0"
IDLE, CONNECTING, PLAYING, ERROR = "idle", "connecting", "playing", "error"

PLAYLIST_TYPES = ("audio/x-scpls", "audio/scpls", "audio/x-mpegurl", "audio/mpegurl",
                  "application/x-mpegurl", "application/vnd.apple.mpegurl",
                  "application/pls+xml", "text/")
FORMAT_BY_TYPE = {
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/mpeg3": "mp3",
    "audio/aac": "aac", "audio/aacp": "aac", "audio/x-aac": "aac",
    "audio/ogg": "ogg", "application/ogg": "ogg", "audio/opus": "ogg",
}
_TITLE_RE = re.compile(r"StreamTitle='(.*?)';(?=\s*[A-Za-z]+=|\s*$)", re.S)


def parse_icy_title(raw: bytes):
    """Return the StreamTitle from an ICY metadata block, or None."""
    text = raw.rstrip(b"\x00")
    try:
        text = text.decode("utf-8")
    except UnicodeDecodeError:
        text = text.decode("latin-1")
    m = _TITLE_RE.search(text)
    return m.group(1).strip() if m else None


def parse_playlist(body: str):
    """First stream URL from a .pls or .m3u playlist, or None."""
    for line in body.splitlines():
        line = line.strip()
        if line.lower().startswith("file") and "=" in line:
            return line.split("=", 1)[1].strip()
        if line.startswith(("http://", "https://")):
            return line
    return None


class IcyReader:
    """File-like object for PyAV: yields audio bytes, strips ICY metadata."""

    def __init__(self, resp, metaint, on_title, stop_evt):
        self.resp = resp
        self.metaint = metaint
        self.remaining = metaint
        self.on_title = on_title
        self.stop_evt = stop_evt
        self._read1 = getattr(resp, "read1", resp.read)

    def _exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.resp.read(n - len(buf))
            if not chunk:
                break
            buf += chunk
        return buf

    def read(self, n=-1):
        if n is None or n < 0:
            n = 16384
        if self.stop_evt.is_set():
            return b""
        if self.metaint:
            if self.remaining == 0:
                length = self._exact(1)
                if not length:
                    return b""
                meta = self._exact(length[0] * 16)
                self.remaining = self.metaint
                if meta:
                    title = parse_icy_title(meta)
                    if title is not None:
                        self.on_title(title)
            n = min(n, self.remaining)
        chunk = self._read1(n)
        if self.metaint:
            self.remaining -= len(chunk)
        return chunk


# --------------------------------------------------------------------------
# Audio outputs
# --------------------------------------------------------------------------
class AlsaOutput:
    PERIOD = 1024  # frames per write

    def __init__(self, device, rate):
        import alsaaudio
        self.rate = rate
        if device == "auto":
            device = self.find_usb_device(alsaaudio)
        self.device = device
        args = dict(type=alsaaudio.PCM_PLAYBACK, mode=alsaaudio.PCM_NORMAL, device=device,
                    channels=2, rate=rate, format=alsaaudio.PCM_FORMAT_S16_LE, periodsize=self.PERIOD)
        try:
            # v3: a deeper ALSA buffer (8 x 1024 frames, about 170 ms) rides out the
            # moments when Gemma is busy on all four CPU cores
            self.pcm = alsaaudio.PCM(periods=8, **args)
        except TypeError:                     # older pyalsaaudio without `periods`
            self.pcm = alsaaudio.PCM(**args)
        self.period_bytes = self.PERIOD * 4
        self.buf = bytearray()

    @staticmethod
    def find_usb_device(alsaaudio):
        for idx in alsaaudio.card_indexes():
            short, long_name = alsaaudio.card_name(idx)
            if "usb" in long_name.lower() or "usb" in short.lower():
                print(f"[audio] using card {idx}: {long_name}")
                return f"plughw:{idx},0"
        print("[audio] no USB card found, falling back to 'default'")
        return "default"

    def write(self, data):
        self.buf += data
        while len(self.buf) >= self.period_bytes:
            self.pcm.write(bytes(self.buf[:self.period_bytes]))
            del self.buf[:self.period_bytes]

    def flush(self):
        self.buf.clear()


class NullOutput:
    """Used when ALSA is unavailable (e.g. testing on a PC). Paces like real audio."""

    def __init__(self, rate):
        self.rate = rate
        self.device = "null"
        self._t = None

    def write(self, data):
        frames = len(data) // 4
        now = time.monotonic()
        self._t = max(self._t or now, now) + frames / self.rate
        delay = self._t - now - 0.2          # keep ~200 ms "buffered"
        if delay > 0:
            time.sleep(delay)

    def flush(self):
        self._t = None


def make_output(device, rate):
    try:
        return AlsaOutput(device, rate)
    except ImportError:
        print("[audio] pyalsaaudio not available - using silent NullOutput")
        return NullOutput(rate)


# --------------------------------------------------------------------------
# Player
# --------------------------------------------------------------------------
class Player:
    """Plays one station at a time in a background thread.

    Callbacks (all called from the player thread):
      on_state(state)                       - IDLE / CONNECTING / PLAYING / ERROR
      on_title(title)                       - new ICY StreamTitle ("" if none)
      on_connected(url, info: dict)         - a URL started producing audio
    """

    def __init__(self, output, on_state=None, on_title=None, on_connected=None):
        self.out = output
        self.volume = 60
        self.muted = False
        self.state = IDLE
        self.info = {}
        self._on_state = on_state or (lambda s: None)
        self._on_title = on_title or (lambda t: None)
        self._on_connected = on_connected or (lambda u, i: None)
        self._thread = None
        self._stop = threading.Event()
        self._resp = None
        self._lock = threading.Lock()

    # -- public ------------------------------------------------------------
    def gain(self):
        if self.muted:
            return 0.0
        v = self.volume / 100.0
        return v * v  # perceptual curve

    def play(self, url_source):
        """url_source: a callable returning an iterable of URLs to try in order."""
        self.stop()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(url_source, self._stop), daemon=True)
        self._thread.start()

    def stop(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                self._stop.set()
                resp = self._resp
                if resp is not None:
                    try:
                        resp.close()        # unblock a pending network read
                    except Exception:
                        pass
                self._thread.join(timeout=5)
            self._thread = None
            self.out.flush()
            self.info = {}
            self._set_state(IDLE)

    # -- internals ---------------------------------------------------------
    def _set_state(self, s):
        if s != self.state:
            self.state = s
            self._on_state(s)

    def _run(self, url_source, stop_evt):
        failures = 0
        while not stop_evt.is_set() and failures < 3:
            self._set_state(CONNECTING)
            self._on_title("")
            started = time.monotonic()
            played = False
            for url in url_source():
                if stop_evt.is_set():
                    return
                try:
                    played = self._stream(url, stop_evt)
                except Exception as e:
                    print(f"[player] {url} failed: {e}")
                if played or stop_evt.is_set():
                    break
            if stop_evt.is_set():
                return
            # A stream that played for a while and then dropped gets fresh retries
            failures = 0 if played and time.monotonic() - started > 30 else failures + 1
            if failures < 3:
                print(f"[player] reconnecting ({failures}/3)...")
                stop_evt.wait(2)
        if not stop_evt.is_set():
            self._set_state(ERROR)

    def _open(self, url, stop_evt, depth=0):
        """Return (container, info). info carries icy headers when available."""
        info = {"url": url, "icy_name": "", "icy_genre": "", "icy_bitrate": "", "metadata": False}
        if ".m3u8" in url.lower():
            return av.open(url, options={"user_agent": USER_AGENT}, timeout=(10.0, 15.0)), info
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Icy-MetaData": "1"})
        try:
            resp = urllib.request.urlopen(req, timeout=15)
        except urllib.error.HTTPError:
            raise                                       # 404, 403...: a real failure
        except Exception as e:
            # e.g. BadStatusLine from a legacy "ICY 200 OK" server: let FFmpeg try
            print(f"[player] urllib could not open ({e}); trying FFmpeg directly")
            return av.open(url, options={"user_agent": USER_AGENT}, timeout=(10.0, 15.0)), info
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype.startswith(PLAYLIST_TYPES) and depth < 2:
            body = resp.read(65536).decode("utf-8", "replace")
            final = resp.geturl()
            resp.close()
            if "#EXT-X-" in body:                       # HLS behind a non-.m3u8 URL
                return av.open(final, options={"user_agent": USER_AGENT}, timeout=(10.0, 15.0)), info
            inner = parse_playlist(body)
            if not inner:
                raise RuntimeError(f"playlist at {url} has no stream URL")
            print(f"[player] playlist -> {inner}")
            return self._open(inner, stop_evt, depth + 1)
        h = resp.headers
        metaint = int(h.get("icy-metaint") or 0)
        info.update(icy_name=(h.get("icy-name") or "").strip(),
                    icy_genre=(h.get("icy-genre") or "").strip(),
                    icy_bitrate=(h.get("icy-br") or "").split(",")[0].strip(),
                    metadata=bool(metaint))
        self._resp = resp
        reader = IcyReader(resp, metaint, self._on_title, stop_evt)
        return av.open(reader, mode="r", format=FORMAT_BY_TYPE.get(ctype)), info

    def _stream(self, url, stop_evt):
        print(f"[player] opening {url}")
        container, info = self._open(url, stop_evt)
        played = False
        try:
            stream = container.streams.audio[0]
            cc = stream.codec_context
            codec = (cc.name or "").lower().replace("float", "").replace("_fixed", "").replace("_latm", "")
            info.update(codec=codec.upper(), sample_rate=cc.sample_rate or 0,
                        channels=getattr(cc, "channels", 0) or 0,
                        bitrate=int((cc.bit_rate or container.bit_rate or 0) / 1000))
            if not info.get("icy_name"):
                info["icy_name"] = (container.metadata.get("icy-name") or "").strip()
            resampler = av.AudioResampler(format="s16", layout="stereo", rate=self.out.rate)
            for packet in container.demux(stream):
                if stop_evt.is_set():
                    break
                for frame in packet.decode():
                    for out_frame in resampler.resample(frame):
                        pcm = out_frame.to_ndarray().reshape(-1)
                        g = self.gain()
                        if g < 0.999:
                            pcm = (pcm.astype(np.float32) * g).astype(np.int16)
                        self.out.write(pcm.tobytes())
                        if not played:
                            played = True
                            self.info = info
                            self._on_connected(url, info)
                            self._set_state(PLAYING)
                if stop_evt.is_set():
                    break
        finally:
            self._resp = None
            try:
                container.close()
            except Exception:
                pass
        return played
