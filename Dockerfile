# All 6 scrapers, including mrbilit (needs a real browser).
#
# Pinned by digest, not by the floating `3.11-slim` tag. When that tag moves
# upstream (it did on 2026-08-05, to a trixie base), every layer below it is
# invalidated at once — so a deploy that only changed a few lines of Python
# suddenly has to re-run apt and pip against this server's connection, which
# times out mid-download and fails the build. Pinning keeps rebuilds limited
# to the layers we actually changed. Bump this digest deliberately, on its
# own, when you want a newer base.
FROM python:3.11-slim@sha256:3c35dbe0205e9428cdd671c078cc6c824fc20c86591646eb91c0cdc6c86fb8bd

WORKDIR /app

# This server's link to deb.debian.org and PyPI drops partway through large
# downloads (the 9.6MB package index is the usual casualty). apt's default is
# to give up almost immediately; these make it keep trying instead of failing
# the whole build.
RUN printf '%s\n' \
        'Acquire::Retries "15";' \
        'Acquire::http::Timeout "120";' \
        'Acquire::https::Timeout "120";' \
        'Acquire::http::No-Cache "true";' \
    > /etc/apt/apt.conf.d/99-retries

# lxml (tktfly's HTML parser) needs these at build time on slim images without
# a prebuilt wheel for the target arch. chromium is mrbilit's browser, and it
# comes from Debian's own apt package rather than `playwright install
# chromium` — that command pulls from Playwright's CDN (cdn.playwright.dev),
# which geo-blocks Iranian IPs with an HTTP 403. apt's chromium has no such
# restriction and pulls in the right shared libs automatically.
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
