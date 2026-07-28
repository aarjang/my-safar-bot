# my-safar-bot — competitor flight-price / markup monitor

Automates the manual check described by the agency (سید / Mohammadi My Safar):
compare MySafar's own fares against FlyToday, tktfly (آماده سفر), Alibaba,
SnappTrip and mrbilit on a route, both directions, over a rolling date window,
and report the markup/premium per cabin class.

## Stage 1 findings (network reconnaissance)

| Site | Access | Endpoint |
|---|---|---|
| **FlyToday** | public JSON API | `POST /api/gateway/V1/flight/search` |
| **Alibaba** | public JSON API, async (poll) | `POST/GET /api/v1/flights/international/proposal-requests` on `ws.alibaba.ir` |
| **SnappTrip** | public JSON API | `POST /api/listing/v1/one-way/search` on `ift.snapptrip.com` |
| **tktfly (آماده سفر)** | server-rendered HTML, no API | `GET /Ticket-<Origin>-<Destination>.html?t=<jalali date>` |
| **MySafar** (ours) | public JSON API | `POST /v1/flight/find` on `api.mysafar.com`, then poll `/v1/flight/search-result/<id>` |
| **mrbilit** | **no public API** — results stream over a SignalR websocket; the generated REST client (`POST /api/Flights` on `flight.atighgasht.com`) 500s on every payload we tried | needs a real/headless browser |

Details and exact payloads are documented in each scraper's module docstring
under `src/msbot/scrapers/`.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# only if you want mrbilit too (headless-browser scraper):
.venv/bin/pip install playwright
```

`playwright install chromium` fails from Iran (Playwright's own CDN 403s by
geolocation). The mrbilit scraper works around this by launching whatever
Chrome/Chromium/Edge is already installed on the machine instead of the
Playwright-managed binary — see `_LOCAL_CHROMES` in
`src/msbot/scrapers/mrbilit.py`. Point it elsewhere with
`scraper_options.mrbilit.executable_path` in the config, or the
`MSBOT_CHROME` env var, if Chrome lives somewhere nonstandard.

## Use

```bash
cp config.example.yaml config.yaml   # edit routes / sources / rate limits

# quick connectivity check, one date, every source:
PYTHONPATH=src .venv/bin/python -m msbot probe --with-browser

# full run: both directions, N days ahead, CSV + Excel report:
PYTHONPATH=src .venv/bin/python -m msbot scrape \
  --start 2026-08-05 --days 30 --both-directions --with-browser \
  --base mysafar   # or: --base file --base-file data/base_fares.csv

# emit a blank net-fare template for the agency to fill in real base fares:
PYTHONPATH=src .venv/bin/python -m msbot base-template --days 30 --both-directions
```

Every `scrape` run appends to `data/history.sqlite` (one row per offer per run)
so trend-over-time queries don't need re-scraping, and writes
`reports/offers_<ts>.csv` (every raw offer), `reports/comparison_<ts>.csv` and
`reports/markup_<ts>.xlsx` (one row per route/date/cabin: our price, each
competitor's price, and the markup or premium against the chosen base).

## On "markup"

`((competitor_price - base_fare) / base_fare) * 100` needs a real net/purchase
fare (نرخ خرید), which none of these sites expose publicly — including ours;
MySafar's admin tooltip with «قیمت خرید / مارک‌آپ اعمال‌شده» comes from an
authenticated endpoint this project doesn't call. Until the agency supplies net
fares (`msbot base-template` → fill `base_fare_rial` → `--base file`), the
default `--base mysafar` reports each competitor's **premium over our own
public price** — the number the client actually compares in the screenshots,
but not a true markup. This is spelled out in `src/msbot/markup.py` and
recorded per row as `base_fare_source` in the report so it's never mistaken for
the real thing.

## Dashboard (web UI)

A local, live dashboard over the same scrapers — origin/destination pickers,
Jalali date-range calendar, per-source toggle chips, a rate-diff table,
competitor breakdown, and a coverage/run-status tab. Modeled on the client's
own mockup, but every number comes from a real, rate-limited scrape; nothing
is randomly generated.

```bash
PYTHONPATH=src .venv/bin/python -m msbot.web         # http://127.0.0.1:8765
# --host 0.0.0.0 --port 8080 --reload also supported
```

**Login is required on every request** (see Security below) — on first run
without any configured credentials, a strong password is generated
automatically and printed once to the console.

Clicking **تحلیل نرخ‌ها** starts a real background scrape (it can take from
~10 seconds to a few minutes depending on the date range and enabled sources)
and polls `/api/jobs/{id}` for live progress — the same per-host rate limiter
and 429 backoff described above, just visible in the UI: the "پوشش و اجرا" tab
shows each source as در انتظار / در حال اجرا / کندشده (۴۲۹) / رد شد (خطاهای
پیاپی) / موفق / خطا. A source that fails 3 times in a row this run is skipped
automatically for its remaining tasks (see Rate limiting & resilience below)
instead of hanging the whole run; a **توقف استعلام** button lets you cancel a
run outright, and it now actually stops within ~1 second instead of waiting
out an in-flight backoff.

Origin/destination is a live search — type any city and it's resolved through
MySafar's own airport database (`/api/airports/search`), so any international
destination MySafar sells can be scraped ad hoc, not just a pinned list.
"میان‌برهای پرکاربرد" (quick-route chips) above the form still come from
`config.example.yaml`'s `routes:` for one-click repeats. `mrbilit` is off by
default in the dashboard (Playwright/browser scraper — slower, and not
included in the Docker image, see Docker below); toggle it on with its chip
when running locally.

The markup pattern itself (which site is the primary benchmark, and the
expected Toman gap per cabin) is editable live from the **تنظیمات مارک‌آپ**
tab — see "On markup" below.

## Rate limiting & resilience

Every scraper goes through one shared, per-host rate limiter (`src/msbot/http.py`)
built to behave like a single patient browser tab, not a script:
- a minimum delay between requests to the same host (configurable per host —
  flytoday needs a much bigger gap than the others before it 429s),
- a full set of real browser headers (Accept-Encoding, sec-ch-ua, etc.),
- on a 429, that host's delay is stretched — but capped at `per_host_ceiling`
  and eased back down (decay-on-success) once the site recovers, so a burst
  of 429s can never compound into an unbounded wait,
- a hard `max_request_budget` per request (default 120s) — a single request
  gives up and reports failure rather than retrying forever,
- a per-source **circuit breaker**: after `max_consecutive_errors` (default 3)
  failures in a row within one run, that source's remaining tasks are marked
  skipped immediately (no further network calls) while every other source
  keeps going normally, and
- cancellation (the dashboard's "توقف استعلام" button, or `Ctrl-C` on the CLI)
  interrupts an in-flight wait/backoff within about a second instead of only
  after the whole sleep elapses.

All of these are tunable under `rate_limit:` in `config.yaml` — see
`config.example.yaml`. (2026-07-28 incident: without the ceiling/decay, a
handful of consecutive 429s from flytoday compounded into a single request
sleeping for 41 minutes, stalling that source — and the dashboard's progress
bar — for the rest of the run. Fixed as above; see `src/msbot/http.py`'s
module docstring for the full postmortem.)

## Security

This dashboard is meant to be put on the open internet (behind HTTPS), so:

- **Every route requires HTTP Basic Auth** — there is no public page, no
  unauthenticated API endpoint, not even `/docs`. See `src/msbot/web/auth.py`.
- Credentials: set `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD_HASH` (a bcrypt
  hash — generate one with the one-liner in `.env.example`) as env vars for
  production. Without them, a strong random password is auto-generated on
  first run, bcrypt-hashed into `data/.dashboard_credentials.json`
  (`chmod 600`, gitignored), and printed **once** to the console/logs.
- Passwords are always compared via bcrypt (`checkpw`) — never stored or
  logged in plaintext after that first printout.
- Failed logins are tracked per client IP; past 5 failures in 5 minutes, that
  IP is tarpitted (each further attempt sleeps longer, up to 20s) rather than
  answered instantly — cheap brute-force friction.
- CORS is locked to same-origin by default (`DASHBOARD_CORS_ORIGINS` to widen
  it) instead of `*`.
- Standard hardening response headers on every response: `X-Content-Type-Options`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, a restrictive
  `Permissions-Policy`.
- TLS itself is terminated at the reverse proxy (nginx + Let's Encrypt in the
  deployed setup) — the app itself only ever speaks plain HTTP on its internal
  port, same as any app behind a proxy.

None of this replaces running it behind HTTPS — Basic Auth sends credentials
base64-encoded, not encrypted, on every request, so it is only as safe as the
transport it runs over.

## Docker

```bash
cp .env.example .env
# generate a real password hash and put it in .env:
python3 -c "import bcrypt; print(bcrypt.hashpw(b'your-password-here', bcrypt.gensalt(rounds=12)).decode())"

docker compose up -d --build
```

This builds the core 5 HTTP scrapers + dashboard into one image (`Dockerfile`)
and runs it via `docker-compose.yml`, with `./data` and `./reports` mounted so
history/settings/reports survive a rebuild. `mrbilit` (the Playwright/browser
scraper) is intentionally left out of the image — it's a much heavier,
separate concern; run it locally (`--with-browser`) instead, or extend the
Dockerfile with `playwright install --with-deps chromium` if you want it
containerized too.

Running locally without Docker (`PYTHONPATH=src python -m msbot.web`) still
works exactly as before — Docker is an additional way to run this, not a
replacement for local dev.

REST surface, if you want to script against it instead of using the page:
`GET /api/meta`, `POST /api/scrape`, `GET /api/jobs/{id}`,
`GET /api/comparison?job_id=`, `GET /api/csv?job_id=&kind=comparison|offers`.

## Adding a new competitor

Drop a module in `src/msbot/scrapers/`, subclass `BaseScraper`
(`src/msbot/scrapers/base.py`), implement `fetch()` returning `FlightOffer`s,
`@register` it, and add its `name` to `sources` in the config. Nothing else in
the pipeline changes.

## Layout

```
src/msbot/
  models.py       FlightOffer / RouteSpec / cabin normalization
  http.py         rate-limited, retrying HTTP client (per-host backoff on 429)
  jalali.py       Gregorian <-> Jalali (tktfly's URLs are Jalali)
  scrapers/       one module per site + base.py contract/registry
  storage.py      SQLite history
  markup.py       base-fare resolution + markup% math
  report.py       CSV / Excel report generation
  config.py       YAML config + defaults
  orchestrator.py shared scrape-runner (used by both cli.py and web/jobs.py)
  cli.py          `python -m msbot {scrape,probe,base-template}`
  web/            FastAPI dashboard — app.py (routes), jobs.py (background
                   job manager), static/dashboard.html (the page itself)
```

## Known gaps

- A handful of tktfly rows (Mahan, Iran Airtour, some Qeshm Air flights) carry
  no visible cabin chip and no `C`/`P` flight-code suffix, so they land in
  `cabin=unknown` rather than being guessed as economy — see the comparison
  report's `unknown` cabin rows.
- SnappTrip/Alibaba/FlyToday cabin coverage per request is what the site
  returns for an economy-floor search; if a site starts gating business fares
  behind a separate call this will need revisiting (documented per-scraper).
- Proxy rotation is wired (`proxy:` in config) but untested against an actual
  IP block — start with the per-host rate limits before reaching for it.
