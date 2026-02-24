FROM python:3.12-slim

# Install FFmpeg and gcc (needed for tgcrypto)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg gcc python3-dev && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /tmp/tgvideobot

CMD ["python", "main.py"]
