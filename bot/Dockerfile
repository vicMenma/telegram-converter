FROM python:3.12-slim

# Install FFmpeg, aria2c and build tools
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        aria2 \
        gcc \
        g++ \
        cmake \
        python3-dev \
        libssl-dev \
        libboost-all-dev \
        pkg-config \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install libtorrent via pip (compiled wheel for linux)
RUN pip install --no-cache-dir libtorrent || \
    pip install --no-cache-dir lbry-libtorrent || \
    echo "WARNING: libtorrent unavailable"

COPY . .

RUN mkdir -p /tmp/tgvideobot

RUN python3 -c "import libtorrent; print('✅ libtorrent OK:', libtorrent.version)" || \
    echo "⚠️ libtorrent not available"

RUN aria2c --version | head -1 || echo "⚠️ aria2c not available"

CMD ["python", "main.py"]
