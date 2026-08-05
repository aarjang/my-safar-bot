# All 6 scrapers, including mrbilit (needs a real browser). Chromium comes
# from Debian's own apt package, NOT `playwright install chromium` — that
# command pulls from Playwright's own CDN (cdn.playwright.dev), which
# geo-blocks Iranian IPs with an HTTP 403. apt's chromium has no such
# restriction and pulls in the right shared-lib dependencies automatically.
#
# Pinned by digest, not by the floating `3.11-slim` tag. When that tag moves
# upstream (it did on 2026-08-05, to a trixie base), every layer below it is
# invalidated at once — so a deploy that only changed a few lines of Python
# suddenly has to re-run apt and pip against this server's connection, which
# times out mid-download and fails the build. Pinning keeps rebuilds limited
# to the layers we actually changed. Bump this digest deliberately, on its
# own, when you want a newer base — and expect that build to re-run apt and
# pip in full, which on this connection is a ~1h job that needs watching, not
# something to slip into a routine deploy.
FROM python:3.11-slim@sha256:db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93

WORKDIR /app

# Nothing may be inserted between FROM and this RUN, and its text must stay
# byte-identical: Docker keys a layer's cache on the parent image plus the
# exact command string, so even adding an apt.conf tweak above it forces the
# whole ~1h apt+pip rebuild on this connection. Tune apt inside this same
# RUN if it ever needs it, accepting that doing so rebuilds the layer once.
#
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
