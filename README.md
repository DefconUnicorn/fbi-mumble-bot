# FBI-BOT — headless Mumble recorder

Joins a Mumble server as **`FBI-BOT`**, waits in configured channel(s), and
records to **128 kbps OGG** whenever **2 or more real users** share its channel.

## Files

| File | Purpose |
|---|---|
| `bot.py` | The bot (headless, stdlib + pymumble only) |
| `bot.conf.example` | Copy to `bot.conf`, fill in server details |
| `requirements.txt` | Pinned Python deps |
| `install.sh` | Ubuntu installer (deps + venv + systemd) |
| `fbi-bot.service` | systemd unit (bare-metal Ubuntu) |
| `Dockerfile` | Container image (TrueNAS / Docker) |
| `docker-compose.yml` | TrueNAS Custom App / `docker compose up` |

## Quick start (Ubuntu 22.04 / 24.04)

Requires Python ≤ 3.11 (pymumble 1.6.1 uses `ssl.wrap_socket`, removed in
3.12 — `install.sh` picks `python3.11` automatically, via deadsnakes if needed).

```bash
git clone <this> /opt/fbi-mumble-bot   # or scp the folder up
cd /opt/fbi-mumble-bot
sudo bash install.sh
sudo nano /opt/fbi-mumble-bot/bot.conf   # <-- server, port, password, channels

# test the config: connects, prints exact channel names, exits
sudo -u mumblebot /opt/fbi-mumble-bot/venv/bin/python \
  /opt/fbi-mumble-bot/bot.py -c /opt/fbi-mumble-bot/bot.conf --list-channels

sudo systemctl start fbi-bot
journalctl -u fbi-bot -f
```

## TrueNAS SCALE (Custom App via compose)

No systemd needed — the container handles the 30s boot delay + 30s rejoin
cadence internally.

```bash
# on TrueNAS (dataset e.g. /mnt/tank/apps/fbi-mumble-bot):
git clone https://github.com/DefconUnicorn/fbi-mumble-bot .
cp bot.conf.example bot.conf && nano bot.conf
# in bot.conf set: server, channels, and output_dir = /app/recordings
mkdir -p recordings
docker compose up -d --build
docker logs -f fbi-bot
```

Or via UI: **Apps > Discover Apps > (⋮) Install via YAML**, paste
`docker-compose.yml`, adjust the two host paths (`./bot.conf`,
`./recordings`) to your dataset paths, Install. Runs as `568:568` by
default (`PUID`/`PGID` env override); recordings persist in the mounted
`recordings` dataset. `TZ` defaults to `Europe/London`.

Recordings land in `output_dir` (default `./recordings`) as:

```
2026-10-10_21-14-03_Ops_Room.ogg   # OGG Vorbis, 128k, 48 kHz mono
```

## Config (`bot.conf`)

```ini
[mumble]
server = mumble.example.com
port = 64738
username = FBI-BOT
password =
channels = Lobby, Ops Room     # idles in first, follows activity

[recording]
output_dir = ./recordings
min_users = 2                  # bot itself is never counted
stop_delay = 10                # grace period before closing the file
bitrate = 128k
ogg_format = vorbis
min_duration = 3               # drop sub-3s clips
```

Run `bot.py --help` for options. `bot.py -c bot.conf --list-channels` is the
recommended way to confirm channel spelling (names are case-insensitive but
must otherwise match; nested channels can be given as `Root/Sub`).

## How it works

1. One service, N channels: `channels = Lobby, Ops Room` opens one TCP
   connection per channel in threads (first as `FBI-BOT`, extras as
   `FBI-BOT-2`, … — required since one Mumble client can sit in only one
   channel). No second service needed. Single channel = single connection.
2. Each worker sits in its own channel and polls occupancy every
   `poll_interval` seconds.
3. When its channel reaches `min_users` (2 real users, bot excluded), it
   mixes all same-channel Opus-decoded PCM (48 kHz mono s16le) on a
   real-time wall-clock timeline (overlapping speech is summed + clipped).
4. When occupancy drops, it waits `stop_delay` seconds, then pipes raw PCM
   through `ffmpeg -c:a libvorbis -b:a 128k` to OGG (`chmod 600`). If
   `ffmpeg` is missing it saves a WAV fallback and logs an error.
5. On disconnect/failure each worker stops cleanly and retries its join every
   `reconnect_delay` (30s default) forever; systemd `RestartSec=30` is the
   backstop, and the unit waits 30s after boot (boot-only `ExecStartPre`)
   for network/Murmur.
6. Recordings older than `retention_days` (7 default) are auto-deleted at
   startup + hourly.

## Manual (non-service) run

```bash
sudo apt install -y python3-venv libopus0 libopus-dev ffmpeg
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp bot.conf.example bot.conf && nano bot.conf
./venv/bin/python bot.py -c bot.conf
```

## Notes / limits

- Mumble clients show workers as `FBI-BOT`, `FBI-BOT-2`, …; they never
  transmit audio (deafened-by-design: they only receive).
- One connection = one channel (Mumble protocol limit), so N channels = N
  parallel connections inside the one service. Very large channel lists
  (10+) may hit server connection limits — split into two services then.
- OGG Vorbis 128k mono is ~0.96 MB/min. Ensure disk space / logrotate for
  long sessions.
- Check your jurisdiction's consent / disclosure rules before recording
  voice. Consider a channel description such as "This channel is recorded
  by FBI-BOT when 2+ users are present."
