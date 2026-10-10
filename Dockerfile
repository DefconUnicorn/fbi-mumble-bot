# FBI-BOT — headless Mumble recorder (TrueNAS / Docker)
# Pinned to 3.11: pymumble 1.6.1 calls ssl.wrap_socket(), removed in 3.12.
FROM python:3.11-slim

# libopus0 = voice decode (pymumble), ffmpeg = 128k OGG encode
# build-essential = protobuf may need a source build on newer Pythons
RUN apt-get update \
    && apt-get install -y --no-install-recommends libopus0 ffmpeg build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# pymumble 1.6.1's sdist carries a stale protobuf==3.12.2 pin (upstream uses
# 3.20.3), so install it ignoring deps, then install our pinned deps on top.
RUN pip install --no-cache-dir --no-deps pymumble==1.6.1 \
    && pip install --no-cache-dir -r requirements.txt \
    && python -c "import pymumble_py3, google.protobuf; print('deps OK', google.protobuf.__version__)"
COPY bot.py .

# recordings volume mount point; config is mounted read-only at runtime
RUN mkdir -p /app/recordings

CMD ["python", "bot.py", "-c", "/app/bot.conf"]
