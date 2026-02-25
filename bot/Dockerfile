FROM python:3.12-slim

# Install system dependencies + libtorrent
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        gcc \
        python3-dev \
        libboost-python-dev \
        libboost-system-dev \
        libssl-dev \
        libtorrent-rasterbar-dev \
        python3-libtorrent \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Make system libtorrent visible to pip-installed Python
# python3-libtorrent installs to /usr/lib/python3/dist-packages
ENV PYTHONPATH="/usr/lib/python3/dist-packages:${PYTHONPATH}"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /tmp/tgvideobot

CMD ["python", "main.py"]
