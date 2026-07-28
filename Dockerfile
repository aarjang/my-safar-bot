# Core 5 HTTP-based scrapers (flytoday, alibaba, snapptrip, tktfly, mysafar)
# plus the dashboard. mrbilit needs Playwright + a real Chromium, which is a
# heavy, separate concern (~500MB, extra system deps) — it's intentionally
# left out of this image; run it locally with `--with-browser` instead, or
# extend this Dockerfile with `playwright install --with-deps chromium` if you
# want it in the container too.
FROM python:3.11-slim

WORKDIR /app

# lxml (used for tktfly's HTML parsing) needs these at build time on slim images
# without a prebuilt wheel for the target arch.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libxml2-dev libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY config.example.yaml ./config.yaml

ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1

RUN useradd --create-home --uid 1000 msbot \
    && mkdir -p /app/data /app/reports \
    && chown -R msbot:msbot /app
USER msbot

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import socket; socket.create_connection(('localhost',8765),timeout=3)" || exit 1

CMD ["python", "-m", "msbot.web", "--host", "0.0.0.0", "--port", "8765"]
