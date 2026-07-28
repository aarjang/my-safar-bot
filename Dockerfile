# All 6 scrapers, including mrbilit (needs a real browser). Chromium comes
# from Debian's own apt package, NOT `playwright install chromium` — that
# command pulls from Playwright's own CDN (cdn.playwright.dev), which
# geo-blocks Iranian IPs with an HTTP 403. apt's chromium has no such
# restriction and pulls in the right shared-lib dependencies automatically.
FROM python:3.11-slim

WORKDIR /app

# lxml (tktfly's HTML parser) needs these at build time on slim images without
# a prebuilt wheel for the target arch. chromium is mrbilit's browser (see
# above for why it's apt's package, not Playwright's own download).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libxml2-dev libxslt1-dev \
        chromium \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --timeout=120 --retries=8 -r requirements.txt
# Python driver only — do NOT run `playwright install`, it would try (and
# fail) to fetch a second Chromium build from the blocked CDN. mrbilit.py's
# own _LOCAL_CHROMES fallback finds apt's /usr/bin/chromium instead.
RUN pip install --no-cache-dir --timeout=120 --retries=8 playwright

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
