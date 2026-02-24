# ── Base image ─────────────────────────────────────────────────────
FROM python:3.12-slim

# ── Install FFmpeg ─────────────────────────────────────────────────
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# ── Working directory ──────────────────────────────────────────────
WORKDIR /app

# ── Install Python dependencies ────────────────────────────────────
# Copy requirements first so Docker caches this layer
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Copy bot source code ───────────────────────────────────────────
COPY . .

# ── Create temp directory for conversions ─────────────────────────
RUN mkdir -p /tmp/tgvideobot

# ── Start the bot ─────────────────────────────────────────────────
CMD ["python", "main.py"]
