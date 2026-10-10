#!/usr/bin/env python3
"""
FBI-BOT - headless Mumble recorder bot.

- Joins a Mumble (Murmur) server as "FBI-BOT"
- Idles in / patrols configured channel(s)
- Starts recording when >= 2 real users are in the same channel as the bot
- Records to 128kbps OGG (Vorbis) via ffmpeg
- Headless, systemd-friendly, configured via INI .conf file

Ubuntu target: Python 3.10+, libopus0, ffmpeg.
"""
import argparse
import configparser
import logging
import os
import queue
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime

# ---------------------------------------------------------------- compat import
# pip package "pymumble" (azlux, 1.6.x):  import pymumble_py3
# newer codeberg "pymumble" (2.x):        from mumble import Mumble
try:
    import pymumble_py3
    from pymumble_py3.callbacks import (
        PYMUMBLE_CLBK_SOUNDRECEIVED,
        PYMUMBLE_CLBK_USERCREATED,
        PYMUMBLE_CLBK_USERREMOVED,
        PYMUMBLE_CLBK_USERUPDATED,
        PYMUMBLE_CLBK_DISCONNECTED,
        PYMUMBLE_CLBK_CONNECTED,
    )
    PYMUMBLE_VARIANT = "azlux"
except ImportError:
    pymumble_py3 = None
    try:
        from mumble import Mumble as _NewMumble  # type: ignore
        PYMUMBLE_VARIANT = "new"
    except ImportError:
        _NewMumble = None
        PYMUMBLE_VARIANT = "missing"

SAMPLE_RATE = 48000
CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit

log = logging.getLogger("fbi-bot")

# ------------------------------------------------------------------ config

DEFAULTS = {
    "server": "127.0.0.1",
    "port": "64738",
    "username": "FBI-BOT",
    "password": "",
    "tokens": "",
    "channels": "",          # comma- or newline-separated list, e.g. "Lobby, Ops / Room 1"
    "channel": "",           # legacy single-channel alias
    "channel_idle": "",      # where to wait when nothing is active (defaults to channels[0])
    "mode": "fixed",         # fixed = sit in 'channels'; roam = follow busiest; swarm = cover all busy
    "ignore_channels": "",   # roam/swarm never auto-join these (e.g. "AFK")
    "swarm_idle_timeout": "60",  # swarm retires a worker this long after its channel goes quiet
    "output_dir": "./recordings",
    "min_users": "2",        # start recording when >= this many REAL users (bot excluded)
    "stop_delay": "10",      # seconds to wait after dropping below min_users before stopping
    "poll_interval": "1.0",
    "bitrate": "128k",
    "ogg_format": "vorbis",  # vorbis | opus
    "min_duration": "3",     # discard recordings shorter than this (seconds)
    "retention_days": "7",   # auto-delete recordings older than this (0 = keep forever)
    "comment": "FBI-BOT | auto-records when 2+ users are present",
    "reconnect_delay": "30",
    "certfile": "",
    "keyfile": "",
}


def load_config(path):
    cp = configparser.ConfigParser()
    # allow --config to not exist yet with a clear error
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Config file not found: {path}")
    cp.read(path, encoding="utf-8")
    cfg = dict(DEFAULTS)
    if cp.has_section("mumble"):
        for k in ("server", "port", "username", "password", "tokens",
                  "channels", "channel", "channel_idle", "certfile", "keyfile",
                  "mode", "ignore_channels", "swarm_idle_timeout"):
            if cp.has_option("mumble", k):
                cfg[k] = cp.get("mumble", k)
    if cp.has_section("recording"):
        for k in ("output_dir", "min_users", "stop_delay", "poll_interval",
                  "bitrate", "ogg_format", "min_duration", "retention_days"):
            if cp.has_option("recording", k):
                cfg[k] = cp.get("recording", k)
    if cp.has_section("general"):
        for k in ("comment", "reconnect_delay"):
            if cp.has_option("general", k):
                cfg[k] = cp.get("general", k)
    # Anchor relative paths (output_dir, certfile, keyfile) to the config
    # file's directory, not the shell's CWD — so `sudo -u mumblebot ...`
    # works from anywhere (e.g. /home/bob) and the systemd unit and CLI agree.
    base = os.path.dirname(os.path.abspath(path))
    for k in ("output_dir", "certfile", "keyfile"):
        v = (cfg.get(k, "") or "").strip()
        if v and not os.path.isabs(v):
            cfg[k] = os.path.normpath(os.path.join(base, v))
    # also accept flat file without sections? be lenient:
    return cfg


def parse_channel_list(cfg):
    raw = cfg.get("channels", "").strip() or cfg.get("channel", "").strip()
    if not raw:
        return []
    parts = re.split(r"[,\n]+", raw)
    return [p.strip().strip("/") for p in parts if p.strip()]


def safe_filename(s):
    s = re.sub(r"[^\w\-. ]+", "_", s).strip()
    return re.sub(r"\s+", "_", s)[:80] or "channel"


# ------------------------------------------------------------ PCM mixer
# Mixes interleaved per-user Opus-decoded PCM (48kHz mono s16le) into one
# timeline in real time. Overlapping speech is summed + clipped.

class RealtimeMixer:
    def __init__(self):
        self.lock = threading.Lock()
        self.samples = []  # list of ints, mono s16
        self.t0 = None

    def start(self):
        with self.lock:
            self.samples = []
            self.t0 = time.time()

    def add_pcm(self, pcm_bytes):
        if not pcm_bytes:
            return
        n = len(pcm_bytes) // 2
        fmt = "<" + "h" * n
        try:
            incoming = struct.unpack(fmt, pcm_bytes[: n * 2])
        except struct.error:
            return
        now = time.time()
        with self.lock:
            if self.t0 is None:
                self.t0 = now
            expected_pos = int((now - self.t0) * SAMPLE_RATE)
            offset = expected_pos - n
            if offset < 0:
                # Session just started (expected_pos < chunk len): mix at 0
                # and run slightly ahead of wall-clock; later chunks catch up.
                offset = 0
            # pad silence gap
            if len(self.samples) < offset:
                self.samples.extend([0] * (offset - len(self.samples)))
            # ensure capacity
            need = offset + len(incoming) - len(self.samples)
            if need > 0:
                self.samples.extend([0] * need)
            # sum + clip
            s = self.samples
            for i, v in enumerate(incoming):
                mixed = s[offset + i] + v
                if mixed > 32767:
                    mixed = 32767
                elif mixed < -32768:
                    mixed = -32768
                s[offset + i] = mixed

    def duration(self):
        with self.lock:
            return len(self.samples) / float(SAMPLE_RATE)

    def to_bytes(self):
        with self.lock:
            return struct.pack("<" + "h" * len(self.samples), *self.samples) if self.samples else b""

    def reset(self):
        with self.lock:
            self.samples = []
            self.t0 = None


# ------------------------------------------------------------ recorder

class SessionRecorder:
    """Buffers mixed PCM during a session, encodes to 128k OGG on stop."""

    def __init__(self, output_dir, bitrate="128k", ogg_format="vorbis", min_duration=3):
        self.output_dir = os.path.abspath(output_dir)
        self.bitrate = bitrate
        self.ogg_format = ogg_format.lower().strip()
        self.min_duration = float(min_duration)
        self.mixer = RealtimeMixer()
        self.active = False
        self.channel_name = ""
        self.start_dt = None
        os.makedirs(self.output_dir, exist_ok=True)

    def start(self, channel_name):
        self.mixer.start()
        self.active = True
        self.channel_name = channel_name
        self.start_dt = datetime.now()
        log.warning("REC START  channel='%s'", channel_name)

    def feed(self, pcm_bytes):
        if self.active:
            self.mixer.add_pcm(pcm_bytes)

    def stop(self):
        """Encode and return output path, or None if discarded."""
        if not self.active:
            return None
        self.active = False
        dur = self.mixer.duration()
        if dur < self.min_duration:
            log.warning("REC DISCARD channel='%s' duration=%.1fs (< min %.0fs)",
                        self.channel_name, dur, self.min_duration)
            self.mixer.reset()
            return None
        pcm = self.mixer.to_bytes()
        self.mixer.reset()
        if not pcm:
            return None
        stamp = self.start_dt.strftime("%Y-%m-%d_%H-%M-%S") if self.start_dt else datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        base = f"{stamp}_{safe_filename(self.channel_name)}"
        out_path = os.path.join(self.output_dir, base + ".ogg")
        # de-dup
        i = 2
        while os.path.exists(out_path):
            out_path = os.path.join(self.output_dir, f"{base}_{i}.ogg")
            i += 1
        ok = self._encode_ogg(pcm, out_path)
        if ok:
            log.warning("REC STOP  channel='%s' duration=%.1fs -> %s", self.channel_name, dur, out_path)
            return out_path
        log.error("Encoding failed, wrote nothing")
        return None

    def _encode_ogg(self, pcm_bytes, out_path):
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            # fallback: plain wav so nothing is lost
            wav_path = os.path.splitext(out_path)[0] + ".wav"
            import wave
            with wave.open(wav_path, "wb") as w:
                w.setnchannels(CHANNELS)
                w.setsampwidth(SAMPLE_WIDTH)
                w.setframerate(SAMPLE_RATE)
                w.writeframes(pcm_bytes)
            try:
                os.chmod(wav_path, 0o600)
            except Exception:  # noqa: BLE001
                pass
            log.error("ffmpeg not found - saved WAV fallback: %s (install ffmpeg for OGG)", wav_path)
            return False
        if self.ogg_format == "opus":
            codec = "libopus"
        else:
            codec = "libvorbis"  # OGG Vorbis @ 128k
        cmd = [ffmpeg, "-y", "-v", "error",
               "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
               "-i", "pipe:0",
               "-c:a", codec, "-b:a", self.bitrate,
               out_path]
        try:
            p = subprocess.run(cmd, input=pcm_bytes, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=300)
            if p.returncode != 0 or not os.path.isfile(out_path):
                log.error("ffmpeg failed: %s", p.stderr.decode(errors='replace')[:500])
                return False
            try:
                os.chmod(out_path, 0o600)
            except Exception:  # noqa: BLE001
                pass
            return True
        except Exception as e:  # noqa: BLE001
            log.error("ffmpeg error: %s", e)
            return False


def purge_old_recordings(output_dir, retention_days):
    """Delete *.ogg/*.wav older than retention_days. Returns count deleted."""
    try:
        days = float(retention_days)
    except (TypeError, ValueError):
        return 0
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    deleted = 0
    try:
        for name in os.listdir(output_dir):
            if not name.lower().endswith((".ogg", ".wav")):
                continue
            path = os.path.join(output_dir, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    deleted += 1
                    log.warning("RETENTION deleted %s (older than %sg)", name, retention_days)
            except Exception as e:  # noqa: BLE001
                log.error("retention delete failed %s: %s", path, e)
    except FileNotFoundError:
        pass
    return deleted


# ------------------------------------------------------------ bot

class FbiBot:
    def __init__(self, cfg, username=None, channels=None, label=None,
                 record_enabled=True, peers=None):
        self.cfg = cfg
        self.label = label or (username or cfg.get("username", "FBI-BOT"))
        self.record_enabled = record_enabled
        # peers = (set_of_bot_sessions, lock) shared by a swarm so workers
        # never count each other as "real users".
        self._peers, self._peer_lock = peers if peers else (set(), threading.Lock())
        if username is not None:
            # per-worker copy so parallel channels get unique visible names
            cfg = dict(cfg)
            cfg["username"] = username
            self.cfg = cfg
        self.watch_channels = list(channels) if channels is not None else parse_channel_list(cfg)
        if channels is not None:
            # Parallel worker: pin to its own channel, ignore global channel_idle.
            self.idle_channel = self.watch_channels[0] if self.watch_channels else ""
        else:
            self.idle_channel = (cfg.get("channel_idle", "") or "").strip().strip("/") or (
                self.watch_channels[0] if self.watch_channels else "")
        self.min_users = int(cfg.get("min_users", 2))
        self.stop_delay = float(cfg.get("stop_delay", 10))
        self.poll = float(cfg.get("poll_interval", 1.0))
        self.reconnect_delay = float(cfg.get("reconnect_delay", 30))
        self.retention_days = cfg.get("retention_days", "7")
        self.mode = (cfg.get("mode", "fixed") or "fixed").strip().lower()
        self._ignore_names = set(
            p.strip().lower()
            for p in re.split(r"[,\n]+", cfg.get("ignore_channels", ""))
            if p.strip()
        )
        self._move_blacklist = {}  # channel_id -> unix time; skips failed joins briefly
        self.output_dir = os.path.abspath(cfg.get("output_dir", "./recordings"))
        self._last_purge = 0.0
        self._purge_interval = 3600.0  # re-scan at most hourly
        self.mumble = None
        self.recorder = SessionRecorder(
            cfg.get("output_dir", "./recordings"),
            bitrate=cfg.get("bitrate", "128k"),
            ogg_format=cfg.get("ogg_format", "vorbis"),
            min_duration=cfg.get("min_duration", 3),
        )
        self._stop = threading.Event()
        self._below_since = None
        self._my_session = None

    # -- mumble plumbing ------------------------------------------------
    def _create_mumble(self):
        server = self.cfg["server"]
        port = int(self.cfg["port"])
        user = self.cfg.get("username", "FBI-BOT") or "FBI-BOT"
        pwd = self.cfg.get("password", "")
        tokens = [t.strip() for t in self.cfg.get("tokens", "").split(",") if t.strip()]
        cert = self.cfg.get("certfile", "").strip() or None
        key = self.cfg.get("keyfile", "").strip() or None
        if cert and not os.path.isfile(cert):
            log.error("certfile not found: %s (ignoring)", cert)
            cert = None
        if key and not os.path.isfile(key):
            log.error("keyfile not found: %s (ignoring)", key)
            key = None

        if PYMUMBLE_VARIANT == "azlux":
            m = pymumble_py3.Mumble(server, user, password=pwd, port=port,
                                   tokens=tokens, certfile=cert, keyfile=key,
                                   reconnect=False, debug=False)
            m.set_receive_sound(1)
            try:
                m.set_bandwidth(800000)
            except Exception:  # noqa: BLE001
                pass
            m.callbacks.set_callback(PYMUMBLE_CLBK_SOUNDRECEIVED, self.on_sound)
            m.callbacks.set_callback(PYMUMBLE_CLBK_USERCREATED, lambda *a: self._poke())
            m.callbacks.set_callback(PYMUMBLE_CLBK_USERREMOVED, lambda *a: self._poke())
            m.callbacks.set_callback(PYMUMBLE_CLBK_USERUPDATED, lambda *a: self._poke())
            m.callbacks.set_callback(PYMUMBLE_CLBK_CONNECTED, lambda *a: self._on_connected())
            m.callbacks.set_callback(PYMUMBLE_CLBK_DISCONNECTED, lambda *a: self._on_disconnected())
            return m
        elif PYMUMBLE_VARIANT == "new":
            m = _NewMumble(server, user, port=port, password=pwd, tokens=tokens,
                           certfile=cert, keyfile=key)
            try:
                m.callbacks.sound_received.set_handler(self.on_sound)
            except Exception:  # noqa: BLE001
                pass
            return m
        else:
            raise RuntimeError("pymumble not installed. Run: pip install -r requirements.txt")

    def _on_connected(self):
        log.warning("Connected to %s:%s as %s", self.cfg["server"], self.cfg["port"],
                    self.cfg.get("username", "FBI-BOT"))
        try:
            me = self.mumble.users.myself
            self._my_session = me.get("session") if isinstance(me, dict) else getattr(me, "session", None)
        except Exception:  # noqa: BLE001
            pass
        # register so swarm peers never count us as a "real user"
        try:
            if self._my_session is not None:
                with self._peer_lock:
                    self._peers.add(self._my_session)
        except Exception:  # noqa: BLE001
            pass
        # set comment so clients see what the bot does
        try:
            self.mumble.users.myself.comment(self.cfg.get("comment", ""))
        except Exception:  # noqa: BLE001
            pass
        self._join_initial()

    def _on_disconnected(self):
        log.error("Disconnected from server")

    def _poke(self):
        pass  # main loop polls; callbacks just wake it. Keep handler short (pymumble thread).

    # -- channel helpers ------------------------------------------------
    def _find_channel(self, name):
        """Find by exact name, then by path 'Root/Sub', then case-insensitive."""
        if not name:
            return None
        chs = self.mumble.channels
        # exact
        try:
            return chs.find_by_name(name)
        except Exception:  # noqa: BLE001
            pass
        # path walk: "A / B / C"
        parts = [p.strip() for p in name.split("/") if p.strip()]
        if len(parts) > 1:
            try:
                return chs.find_by_tree(parts)
            except Exception:  # noqa: BLE001
                pass
        # case-insensitive scan
        lname = name.lower()
        try:
            for _cid, ch in dict(chs).items():
                try:
                    n = ch.get_property("name") if hasattr(ch, "get_property") else ch["name"]
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(n, str) and n.lower() == lname:
                    return ch
        except Exception:  # noqa: BLE001
            pass
        return None

    def _my_channel_obj(self):
        try:
            return self.mumble.my_channel()
        except Exception:  # noqa: BLE001
            return None

    def _my_channel_id(self):
        try:
            me = self.mumble.users.myself
            if isinstance(me, dict):
                return me.get("channel_id")
            return getattr(me, "channel_id", None)
        except Exception:  # noqa: BLE001
            return None

    def _channel_id_of(self, ch):
        try:
            if hasattr(ch, "get_id"):
                return ch.get_id()
            if isinstance(ch, dict):
                return ch.get("channel_id")
            return ch["channel_id"]
        except Exception:  # noqa: BLE001
            return None

    def _channel_name_of(self, ch):
        try:
            if hasattr(ch, "get_property"):
                return ch.get_property("name")
            if isinstance(ch, dict):
                return ch.get("name", "?")
        except Exception:  # noqa: BLE001
            pass
        return "?"

    def _users_in_channel(self, channel_id):
        """Return list of User objects in channel (including bot)."""
        try:
            ch = self.mumble.channels[channel_id]
            if hasattr(ch, "get_users"):
                u = ch.get_users()
                if isinstance(u, dict):
                    return list(u.values())
                return list(u)
        except Exception:  # noqa: BLE001
            pass
        # fallback: scan users
        out = []
        try:
            for _sess, u in dict(self.mumble.users).items():
                try:
                    cid = u.get("channel_id") if isinstance(u, dict) else getattr(u, "channel_id", None)
                except Exception:  # noqa: BLE001
                    continue
                if cid == channel_id:
                    out.append(u)
        except Exception:  # noqa: BLE001
            pass
        return out

    def _real_user_count(self, channel_id):
        users = self._users_in_channel(channel_id)
        try:
            peers = set(self._peers) if self._peers else set()
        except Exception:  # noqa: BLE001
            peers = set()
        n = 0
        for u in users:
            try:
                sess = u.get("session") if isinstance(u, dict) else getattr(u, "session", None)
            except Exception:  # noqa: BLE001
                sess = None
            if sess == self._my_session or sess in peers:
                continue  # skip self and fellow swarm workers
            n += 1
        return n

    def _user_channel_id(self, user):
        try:
            if isinstance(user, dict):
                return user.get("channel_id")
            return getattr(user, "channel_id", None)
        except Exception:  # noqa: BLE001
            return None

    # -- audio callback (runs in pymumble thread - keep short!) ---------
    def on_sound(self, user, soundchunk):
        try:
            if not self.recorder.active:
                return
            if self._user_channel_id(user) != self._my_channel_id():
                return  # only record our own channel
            pcm = getattr(soundchunk, "pcm", None)
            if pcm:
                self.recorder.feed(pcm)
        except Exception:  # noqa: BLE001
            pass

    # -- join / patrol --------------------------------------------------
    def _join_initial(self):
        target = self.idle_channel or (self.watch_channels[0] if self.watch_channels else "")
        if not target:
            log.warning("No channels configured - staying in Root")
            return
        ch = self._find_channel(target)
        if ch is None:
            log.error("Channel not found: '%s'. Staying where server put us. "
                      "Check 'channels' in .conf (exact channel name).", target)
            self._list_channels()
            return
        try:
            ch.move_in()
            log.warning("Waiting in channel '%s'", target)
        except Exception as e:  # noqa: BLE001
            log.error("move_in('%s') failed: %s", target, e)

    def _list_channels(self):
        try:
            names = []
            for _cid, ch in dict(self.mumble.channels).items():
                try:
                    names.append(ch.get_property("name"))
                except Exception:  # noqa: BLE001
                    pass
            log.warning("Server channels: %s", ", ".join(sorted(set(names)))[:500])
        except Exception:  # noqa: BLE001
            pass

    def _most_active_watched(self):
        """Return (channel_obj, name, real_count) with highest count among watched."""
        best = (None, "", -1)
        for name in self.watch_channels:
            ch = self._find_channel(name)
            if ch is None:
                continue
            cid = self._channel_id_of(ch)
            if cid is None:
                continue
            n = self._real_user_count(cid)
            if n > best[2]:
                best = (ch, name, n)
        return best

    # -- main -----------------------------------------------------------
    def run_forever(self):
        while not self._stop.is_set():
            try:
                self._run_once()
            except Exception as e:  # noqa: BLE001
                log.error("Session error: %s", e)
            if not self._stop.is_set():
                log.warning("Reconnecting in %.0fs ...", self.reconnect_delay)
                time.sleep(self.reconnect_delay)

    def _run_once(self):
        self.mumble = self._create_mumble()
        self.mumble.start()
        self._wait_ready(timeout=30)
        # refresh own session id
        try:
            me = self.mumble.users.myself
            self._my_session = me.get("session") if isinstance(me, dict) else getattr(me, "session", None)
        except Exception:  # noqa: BLE001
            pass

        log.warning("[%s] Bot ready (mode=%s). Watching: %s | min_users=%d stop_delay=%.0fs",
                    self.label, self.mode,
                    self.watch_channels or ["(whole server)" if self.mode == "roam" else "(current channel only)"],
                    self.min_users, self.stop_delay)
        log.warning("Retention: %s days in %s (0=keep forever)",
                    self.retention_days, self.output_dir)
        try:
            purge_old_recordings(self.output_dir, self.retention_days)
            self._last_purge = time.time()
        except Exception:  # noqa: BLE001
            pass

        try:
            while not self._stop.is_set():
                try:
                    if not getattr(self.mumble, "connected", True):
                        raise ConnectionError("mumble.connected is False")
                    self._tick()
                except ConnectionError:
                    raise
                except Exception as e:  # noqa: BLE001
                    log.error("tick error: %s", e)
                time.sleep(self.poll)
        finally:
            self._shutdown_session()

    def _wait_ready(self, timeout=30):
        """Wait for connection with a hard timeout so retries stay on 30s cadence,
        regardless of pymumble version (newer supports timeout=, older blocks)."""
        err = []

        def _blocking():
            try:
                try:
                    self.mumble.is_ready(timeout=timeout)
                except TypeError:
                    self.mumble.is_ready()  # older signature: blocks
            except Exception as e:  # noqa: BLE001
                err.append(e)

        t = threading.Thread(target=_blocking, daemon=True)
        t.start()
        t.join(timeout=timeout + 5)
        if t.is_alive():
            raise TimeoutError(f"connection not ready after {timeout}s")
        if err:
            raise err[0]

    def _tick(self):
        # Hourly retention sweep: delete *.ogg/*.wav older than retention_days.
        now = time.time()
        if now - self._last_purge >= self._purge_interval:
            self._last_purge = now
            try:
                purge_old_recordings(self.output_dir, self.retention_days)
            except Exception:  # noqa: BLE001
                pass
        # Determine where we are and how many real users are with us
        my_ch = self._my_channel_obj()
        my_cid = self._my_channel_id()
        my_name = self._channel_name_of(my_ch) if my_ch is not None else "?"
        here = self._real_user_count(my_cid) if my_cid is not None else 0

        # If a watch-list is configured and another watched channel is more
        # active than where we sit, go there (and record there).
        if self.watch_channels:
            best_ch, best_name, best_n = self._most_active_watched()
            if best_ch is not None and best_n >= self.min_users and best_n > here:
                # move (also handles the idle->active transition)
                try:
                    best_ch.move_in()
                    log.warning("Moving to active channel '%s' (%d users)", best_name, best_n)
                except Exception as e:  # noqa: BLE001
                    log.error("move to '%s' failed: %s", best_name, e)
                my_name = best_name
                here = best_n

        # Roam mode: scan the whole server and auto-join the busiest channel
        # with min_users+ real occupants (skips ignore_channels).
        if self.mode == "roam":
            my_cid, my_name, here = self._tick_roam(my_cid, my_name, here)

        if self.record_enabled and here >= self.min_users:
            self._below_since = None
            if not self.recorder.active:
                self.recorder.start(my_name)
        else:
            if self.recorder.active:
                if self._below_since is None:
                    self._below_since = time.time()
                elif time.time() - self._below_since >= self.stop_delay:
                    self.recorder.stop()
                    self._below_since = None
                    # drift back to idle channel if configured
                    if self.idle_channel:
                        cur = self._my_channel_obj()
                        cur_name = self._channel_name_of(cur) if cur else ""
                        if cur_name != self.idle_channel:
                            idle = self._find_channel(self.idle_channel)
                            if idle is not None:
                                try:
                                    idle.move_in()
                                    log.warning("Back to idle channel '%s'", self.idle_channel)
                                except Exception:  # noqa: BLE001
                                    pass

    def _is_ignored(self, name):
        return isinstance(name, str) and name.strip().lower() in self._ignore_names

    def _try_move(self, ch, name, n):
        """move_in with a short blacklist on failure (e.g. locked channel)."""
        try:
            ch.move_in()
            log.warning("[%s] Auto-join '%s' (%d users)", self.label, name, n)
            return True
        except Exception as e:  # noqa: BLE001
            log.error("[%s] move to '%s' failed: %s", self.label, name, e)
            try:
                cid = self._channel_id_of(ch)
                if cid is not None:
                    self._move_blacklist[cid] = time.time() + 300
            except Exception:  # noqa: BLE001
                pass
            return False

    def _tick_roam(self, my_cid, my_name, here):
        """Find the busiest non-ignored channel and join it if worthwhile.
        Returns possibly-updated (my_cid, my_name, here)."""
        best_ch, best_name, best_n = None, "", -1
        try:
            for cid, ch in dict(self.mumble.channels).items():
                try:
                    nm = ch.get_property("name") if hasattr(ch, "get_property") else ch["name"]
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(nm, str):
                    continue
                nm = nm.strip()
                if self._is_ignored(nm):
                    continue
                if cid in self._move_blacklist and time.time() < self._move_blacklist[cid]:
                    continue
                n = self._real_user_count(cid)
                if n > best_n:
                    best_n = n
                    best_ch, best_name = ch, nm
        except Exception:  # noqa: BLE001
            pass
        if best_ch is None or best_n < self.min_users:
            return my_cid, my_name, here  # nothing worth joining
        best_cid = self._channel_id_of(best_ch)
        if best_cid == my_cid:
            return my_cid, my_name, here  # already there
        # Switch only if strictly busier (no flapping on ties), unless we're
        # sitting in an ignored channel (e.g. AFK) — then leave if anything
        # better qualifies.
        if best_n > here or self._is_ignored(my_name):
            if self._try_move(best_ch, best_name, best_n):
                return best_cid, best_name, best_n
        return my_cid, my_name, here

    def _shutdown_session(self):
        try:
            if self.recorder.active:
                self.recorder.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            # free our session id so peers stop excluding it (ids get reused)
            if self._my_session is not None:
                with self._peer_lock:
                    self._peers.discard(self._my_session)
        except Exception:  # noqa: BLE001
            pass
        self._my_session = None
        try:
            if self.mumble is not None:
                self.mumble.stop()
        except Exception:  # noqa: BLE001
            pass

    def stop(self):
        self._stop.set()


# ------------------------------------------------------------ swarm
# mode=swarm: join ALL channels with min_users+ occupants simultaneously.
# One Mumble connection sits in one channel, so a lightweight scout watches
# the server and the supervisor runs one FbiBot worker per busy channel,
# retiring workers whose channel stays quiet past swarm_idle_timeout.

class Swarm:
    def __init__(self, cfg, base_user=None, worker_factory=None, snapshot_fn=None):
        self.cfg = dict(cfg)
        self.base_user = (base_user or cfg.get("username", "FBI-BOT") or "FBI-BOT").strip()
        self.min_users = int(cfg.get("min_users", 2))
        self.poll = float(cfg.get("poll_interval", 1.0))
        self.idle_timeout = float(cfg.get("swarm_idle_timeout", 60))
        self._ignore_names = set(
            p.strip().lower()
            for p in re.split(r"[,\n]+", cfg.get("ignore_channels", ""))
            if p.strip()
        )
        self.sessions = set()  # every bot connection's session id (scout + workers)
        self.lock = threading.Lock()
        self.workers = {}  # cid -> {worker, thread, username, name, last_active}
        self.used_names = set()
        self.scout = None
        self._stop = threading.Event()
        self._worker_factory = worker_factory or self._real_worker
        self._snapshot_fn = snapshot_fn

    # -- plumbing -----------------------------------------------------
    def _real_worker(self, channel_name, username):
        w = FbiBot(self.cfg, username=username, channels=[channel_name],
                   label=f"{username}:{channel_name}",
                   peers=(self.sessions, self.lock))
        t = threading.Thread(target=w.run_forever, daemon=True,
                             name=f"swarm-{username}")
        return w, t

    def _pick_username(self):
        i = 0
        while True:
            nm = self.base_user if i == 0 else f"{self.base_user}-{i + 1}"
            if nm not in self.used_names:
                return nm
            i += 1

    def _snapshot(self):
        """{cid: (name, real_count)} for every non-ignored channel."""
        m = self.scout.mumble if self.scout is not None else None
        if m is None:
            return {}
        out = {}
        try:
            for cid, ch in dict(m.channels).items():
                try:
                    nm = ch.get_property("name") if hasattr(ch, "get_property") else ch["name"]
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(nm, str):
                    continue
                nm = nm.strip()
                out[cid] = (nm, self.scout._real_user_count(cid))
        except Exception:  # noqa: BLE001
            pass
        return out

    # -- supervision --------------------------------------------------
    def _tick(self):
        snap = self._snapshot_fn() if self._snapshot_fn else self._snapshot()
        now = time.time()
        for cid, (nm, n) in snap.items():
            if nm.lower() in self._ignore_names:
                continue  # e.g. AFK lobby
            if n < self.min_users:
                continue
            if cid in self.workers:
                self.workers[cid]["last_active"] = now
                continue
            username = self._pick_username()
            worker, thread = self._worker_factory(nm, username)
            thread.start()
            with self.lock:
                self.used_names.add(username)
            self.workers[cid] = {"worker": worker, "thread": thread,
                                 "username": username, "name": nm,
                                 "last_active": now}
            log.warning("[swarm] covering '%s' (%d users) as %s", nm, n, username)
        for cid in list(self.workers):
            info = self.workers[cid]
            n = snap.get(cid, (info["name"], 0))[1] if cid in snap else 0
            if n >= self.min_users:
                continue
            if now - info["last_active"] >= self.idle_timeout:
                log.warning("[swarm] retiring %s from '%s' (quiet %.0fs)",
                            info["username"], info["name"], now - info["last_active"])
                try:
                    info["worker"].stop()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    info["thread"].join(timeout=5)
                except Exception:  # noqa: BLE001
                    pass
                with self.lock:
                    self.used_names.discard(info["username"])
                del self.workers[cid]

    def start(self):
        self.scout = FbiBot(self.cfg, username=f"{self.base_user}-scout",
                            channels=[],
                            label=f"{self.base_user}-scout",
                            record_enabled=False,
                            peers=(self.sessions, self.lock))
        with self.lock:
            self.used_names.add(f"{self.base_user}-scout")
        st = threading.Thread(target=self.scout.run_forever, daemon=True,
                              name="swarm-scout")
        st.start()
        log.warning("[swarm] watching whole server (min_users=%d, idle_timeout=%.0fs, ignore=%s)",
                    self.min_users, self.idle_timeout,
                    sorted(self._ignore_names) or "none")
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001
                log.error("[swarm] tick error: %s", e)
            time.sleep(self.poll)
        self.shutdown()

    def shutdown(self):
        for cid in list(self.workers):
            try:
                self.workers[cid]["worker"].stop()
            except Exception:  # noqa: BLE001
                pass
        if self.scout is not None:
            try:
                self.scout.stop()
            except Exception:  # noqa: BLE001
                pass

    def stop(self):
        self._stop.set()


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description="FBI-BOT headless Mumble recorder")
    ap.add_argument("-c", "--config", default="bot.conf", help="path to .conf file (default: bot.conf)")
    ap.add_argument("--list-channels", action="store_true",
                    help="connect, print channel list, and exit (for filling in .conf)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")

    try:
        cfg = load_config(args.config)
    except FileNotFoundError as e:
        print(f"ERROR: {e}\nCopy bot.conf.example -> {args.config} and edit server details.", file=sys.stderr)
        sys.exit(2)

    if args.list_channels:
        bot = FbiBot(cfg)
        m = bot._create_mumble()
        bot.mumble = m  # so the CONNECTED callback (_on_connected) can use it
        m.start()
        try:
            m.is_ready(timeout=30)
        except TypeError:
            m.is_ready()
        time.sleep(2)
        print("Channels on server:")
        try:
            for cid, ch in sorted(dict(m.channels).items()):
                try:
                    print(f"  [{cid}] {ch.get_property('name')}")
                except Exception:  # noqa: BLE001
                    pass
        finally:
            try:
                m.stop()
            except Exception:  # noqa: BLE001
                pass
        return

    # sanity: channel config present?
    configured = parse_channel_list(cfg)
    mode = (cfg.get("mode", "fixed") or "fixed").strip().lower()
    roam = mode == "roam"
    swarm_mode = mode == "swarm"
    if not configured and not roam and not swarm_mode:
        print("WARNING: no channels configured - bot will record its starting channel only.", file=sys.stderr)

    base_user = (cfg.get("username", "FBI-BOT") or "FBI-BOT").strip()

    if swarm_mode:
        # Swarm mode: scout + one worker per channel with min_users+ occupants.
        sw = Swarm(cfg, base_user)

        def _sig_sw(signum, _frame):
            log.warning("Signal %s - shutting down swarm...", signum)
            sw.stop()

        signal.signal(signal.SIGINT, _sig_sw)
        signal.signal(signal.SIGTERM, _sig_sw)
        log.warning("FBI-BOT swarm starting (config: %s)", args.config)
        sw.start()
        return

    def _worker_name(i):
        # First worker keeps the exact name ("FBI-BOT" visible as requested);
        # extras get -2, -3... since one Mumble connection = one channel.
        return base_user if i == 0 else f"{base_user}-{i + 1}"

    bots = []
    if roam:
        # Roam mode: one worker scans the whole server and auto-joins the
        # busiest min_users+ channel. 'channels'/'channel_idle' are optional:
        # if set, the first entry is the idle fallback between recordings.
        bots = [FbiBot(cfg)]
    elif len(configured) <= 1:
        bots = [FbiBot(cfg)]
    else:
        # One connection per channel, all in a single service process.
        # Each worker idles+records ONLY its own channel simultaneously.
        for i, ch in enumerate(configured):
            bots.append(FbiBot(cfg, username=_worker_name(i), channels=[ch],
                               label=f"{_worker_name(i)}:{ch}"))

    def _sig(signum, _frame):
        log.warning("Signal %s - shutting down %d worker(s)...", signum, len(bots))
        for b in bots:
            b.stop()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    if len(bots) == 1:
        log.warning("FBI-BOT starting (config: %s)", args.config)
        bots[0].run_forever()
    else:
        log.warning("FBI-BOT starting %d workers (config: %s): %s",
                    len(bots), args.config,
                    ", ".join(f"{b.label}" for b in bots))
        threads = []
        for b in bots:
            t = threading.Thread(target=b.run_forever, daemon=True,
                                 name=f"fbi-{b.label}")
            t.start()
            threads.append(t)
            time.sleep(1)  # stagger joins to avoid hammering the server
        try:
            while any(t.is_alive() for t in threads):
                time.sleep(1)
        except KeyboardInterrupt:
            for b in bots:
                b.stop()


if __name__ == "__main__":
    main()
