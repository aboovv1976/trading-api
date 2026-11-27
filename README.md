# TradingView analysis webhook

Flask API for Vercel. The TradingView endpoint authenticates and validates an
alert, publishes it to QStash, then returns `202` with a `job_id`. A separately
invoked worker asks Grok to research market data using Web Search, performs
analysis, and stores the result in Redis for seven days.
There are no detached threads or model requests in the ingress endpoint.

```text
TradingView -> webhook -> QStash -> authenticated worker -> Grok + Web Search -> Redis
                                                               ^
                                     authenticated results API |
```

## Services and deployment

Provision a [QStash](https://upstash.com/docs/qstash/overall/getstarted) account
and a persistent Redis database (Upstash Redis with its TLS Redis URL works).
This code does not provision services or deploy anything automatically.

Configure these variables in the Vercel project; `.env.example` lists them:

| Variable | Purpose |
| --- | --- |
| `TV_WEBHOOK_SECRET` | Dedicated shared webhook token; never a broker password or API key |
| `QSTASH_TOKEN` | QStash publish API credential |
| `QSTASH_URL` | Regional API base URL copied from your QStash console |
| `WORKER_TOKEN` | Separate random token forwarded by QStash to the worker |
| `WORKER_URL` | Exact production HTTPS URL ending in `/api/v1/analyze-e8f8323f5eecdfce`; no query or redirect |
| `REDIS_URL` | Persistent database connection URL, preferably `rediss://…` |
| `RESULT_API_TOKEN` | Separate random token for reading stored results |
| `XAI_API_KEY` | xAI API credential, available only to the server |
| `XAI_MODEL` | Optional; defaults to `grok-4.7`; must support Responses API, Web Search, and structured outputs |
| `WHATSAPP_VERIFY_TOKEN` | Separate random token matching Meta's webhook Verify token field |
| `WHATSAPP_APP_SECRET` | Meta application's App Secret, used to authenticate WhatsApp deliveries |

Generate distinct random tokens, for example with `openssl rand -hex 32`.
Do not commit `.env` or actual credentials. `BACKGROUND_ANALYSIS` is no longer used.

Deploy this repository as a Flask project on Vercel. `pyproject.toml` explicitly
identifies `main:app`; `.python-version` selects Python 3.12. `vercel.json` sets
the function maximum duration to 300 seconds, the Hobby/free-tier maximum with
Fluid compute enabled. Ensure Fluid compute is enabled for your project. Use a stable
production domain for `WORKER_URL`. TradingView
must reach the webhook and QStash must reach the worker; configure Vercel
Deployment Protection accordingly. Application authentication remains required.

QStash uses the configured service URL and forwards `WORKER_TOKEN` as a bearer token.
The worker route has a random suffix to reduce unsolicited traffic; the bearer
token remains required. When changing this path, update `WORKER_URL` and deploy
the matching code together. Previously queued jobs retain their original URLs;
let them drain before changing the path, or replay them to the new path afterward.
The worker authenticates this token rather than validating QStash signatures;
any holder of that token can invoke it. Keep it separate from the TradingView token.

## WhatsApp Business incoming webhook

Callback URL:
`https://trading-api-lac.vercel.app/api/v1/whatsapp-webhook-9119d4cb5e7c7192`

1. Generate a separate verify token with `openssl rand -hex 32`. Set it as
   `WHATSAPP_VERIFY_TOKEN` in Vercel, and use that same value in Meta's **Verify
   token** field. Do not use a WhatsApp access token here.
2. Copy your Meta application's **App Secret** from its app settings into
   Vercel's `WHATSAPP_APP_SECRET`. This is a different secret from the verify
   token. The existing `REDIS_URL` is also required for receiving deliveries.
3. Redeploy with these variables. Ensure Meta can reach the production callback
   through Vercel Deployment Protection. Enter the callback URL above in Meta,
   then select **Verify and save**. Leave the client-certificate option off
   unless you separately configure mutual TLS.
4. Subscribe to the `messages` webhook field and ensure the app is subscribed
   to your WhatsApp Business Account. This field delivers incoming messages and
   outgoing-message status updates. Follow Meta's publishing requirements; the
   dashboard warns that unpublished apps only receive dashboard test webhooks.

GET verifies `hub.verify_token` and returns `hub.challenge` as plain text. POST
checks `X-Hub-Signature-256` using HMAC-SHA256 of the exact request body with the
App Secret before parsing or storing it. The randomized path supplements these
checks; it does not replace authentication. A plain browser visit without
verification parameters returns an error by design.

Accepted event envelopes are stored in the existing Redis database as
`wa:webhook:<event_id>`, with `event_id`, UTC `received_at`, and the complete
`payload`. Records include message content and sender identifiers, so restrict
Redis access. They expire after seven days and can currently be inspected in
the Redis console; the trading-results API does not list this inbox.
Identical envelopes (ignoring JSON key order and whitespace) are acknowledged
without overwriting or extending their expiry. Rebatched messages can appear
in different envelopes; any future message processing should also deduplicate
by WhatsApp message ID. Batches up to 3 MiB are accepted without changing the
TradingView endpoint's 128 KiB limit.

Storage failures return `503` so Meta can retry. Logs contain event IDs and
error types, without message contents or sender numbers. This inbox does not
send replies, download media, or call Grok; no WhatsApp access token is needed
for receiving events. Existing trading alerts and reports are independent.

See Meta's [WhatsApp webhook guide](https://developers.facebook.com/documentation/business-messaging/whatsapp/webhooks/overview)
and [verification and signature documentation](https://developers.facebook.com/docs/graph-api/webhooks/getting-started/).

## TradingView alert message

Use the production URL `/api/v1/tv-alert-x7k9m2p` and valid JSON:

```json
{
  "secret": "YOUR_DEDICATED_WEBHOOK_TOKEN",
  "alert_id": "my-strategy-v1:{{exchange}}:{{ticker}}:{{interval}}:{{time}}:green",
  "ticker": "{{exchange}}:{{ticker}}",
  "signal": "green",
  "price": "{{close}}",
  "interval": "{{interval}}",
  "time": "{{timenow}}"
}
```

Use `red` for the opposite signal and change its `alert_id` accordingly.
`alert_id` is optional: without it, normalized alert content determines the job
ID. With it, the ID must identify exactly one event with identical payload data
on retries. Include a strategy identifier and, if multiple events can occur in
one bar, an event sequence or other unique value generated by your script.
Some placeholders depend on alert type; inspect an actual TradingView payload.

Required fields: `ticker`, `signal` (`green` or `red`), positive finite `price`,
`interval`, and ISO 8601 `time` including timezone. Numeric price strings are
accepted. Unknown fields, invalid JSON, nonobject JSON, and
bodies over 128 KiB are rejected. Alerts must be no older than 24 hours and no
more than five minutes in the future.

### Supertrend + ADX indicator

Use [tradingview/supertrend_adx.pine](tradingview/supertrend_adx.pine) for the
rich alert format (Pine Script v6). Paste the file into TradingView's Pine Editor, save it, and
add it to the desired standard OHLC chart. In the indicator settings, set the
webhook token to the same value as Vercel's `TV_WEBHOOK_SECRET`. Use a different
strategy ID for each parameter set. Keep generated alert IDs within 128 characters.

Create an alert on this indicator with **Any alert() function call**, and set
the webhook URL to the production endpoint above. The script builds its JSON
message dynamically; you do not paste a separate JSON message or use placeholders.
Both green and red signals are delivered through this one alert, only after bar
close with ADX above the configured threshold. Changing the script or settings
requires recreating the TradingView alert, which uses a saved snapshot.

The script uses `alert()` because `alertcondition()` requires a constant message
and cannot concatenate runtime `str.tostring()` values. See
[TradingView's alert documentation](https://www.tradingview.com/pine-script-docs/concepts/alerts/).
It sends the signal bar's OHLCV, Supertrend, ADX, 20-bar high/low and averages,
plus five previous closes (newest first). Missing numeric values become JSON
`null`; alerts wait for indicator warmup. No candle array is sent. The script
does not backfill signals that occurred before the indicator warmed up.

The API accepts these optional fields: `open`, `high`, `low`, `close`, `volume`,
`supertrend`, `adx`, `highest20`, `lowest20`, `avgVol20`, `avgClose20`, and
`prevCloses`. If `price` is absent, `close` supplies it; when both are present they
must match. ADX must be in [0, 100], volume must be nonnegative, supplied OHLC
values must have consistent ranges, and `prevCloses` must have five entries.
Grok receives these values as sender-supplied context and still performs Web Search.

Pine script version `2.0.0` also sends the following optional context:

| Fields | Meaning |
| --- | --- |
| `bar_open_time`, `bar_close_time`, `bar_confirmed` | Exact signal-bar boundaries in UTC and confirmation state; `time` remains alert-generation time |
| `currency`, `instrument_type`, `exchange_timezone` | Quote currency, asset type and exchange timezone |
| `chart_session`, `session_state` | Dataset session setting and intraday state (`regular`, `premarket`, `postmarket`, `other`); non-intraday state is `not_applicable` |
| `is_standard_chart` | False for nonstandard charts whose OHLC values may be synthetic |
| `atr`, `atr_pct` | ATR using the Supertrend ATR period, and `100 * ATR / close` (percentage points) |
| `di_plus`, `di_minus` | Directional indicators from the same DMI calculation as ADX |
| `strategy_id`, `script_version` | Strategy-instance label and payload-producing script version |
| `indicator_settings` | `atr_period`, `atr_multiplier`, `adx_length`, `adx_smoothing`, `adx_threshold`; DMI length and ADX smoothing both use the current ADX Length input |

These are summaries and metadata, not candle arrays or independently verified
data. ATR values can be zero; unavailable numeric values remain null. Session
labels follow TradingView's feed/exchange definitions. Bar timestamps must carry
a timezone, opening must precede closing, and a confirmed close cannot be later
than alert generation. Grok is instructed to disclose synthetic chart prices and
unconfirmed signals. New fields do not change the existing flip/ADX trigger rules.

**Deploy the updated API before installing this script version**; older API
deployments reject unknown fields. Then manually paste the updated Pine script,
save/update it on the chart, set its webhook token, and recreate the TradingView
alert so its saved snapshot includes the new payload. Existing smaller alert
payloads remain supported by the updated API.
Absent fields from this context expansion are omitted from canonical job
identities so pre-upgrade queued alerts and completed-result fingerprints remain
compatible. Supplied context values still contribute to the fingerprint.

Pine compilation and actual alert delivery must be verified in TradingView;
the local test suite verifies the corresponding API payload and analysis input.

### Market evidence

TradingView only needs to send the alert above. **No candles are required.**
The worker uses xAI's Responses API with its server-side `web_search` tool to
let Grok find historical prices/OHLCV, technical evidence, and relevant news
and sentiment before producing a structured recommendation. No additional
market-data API key is required for this search-based approach. xAI model and
search usage are billed by xAI.

Set `XAI_MODEL=grok-4.7` or another model documented to support these features.
If you previously set `grok-4`, update the Vercel variable and redeploy; an
explicit environment value overrides the new default. Search runs on xAI's
servers; there is no local browser or exchange connection in this application.
See [Web Search](https://docs.x.ai/developers/tools/web-search) and
[structured outputs with tools](https://docs.x.ai/developers/model-capabilities/text/structured-outputs).

The model must resolve the instrument/exchange/currency and obtain sufficient
evidence for the alert's timeframe. It must disclose missing history, delayed
quotes, and timeframe substitutions, and must not fabricate candles or indicators.
Sources include a URL, title, category, and observation/publication timestamp
(`as_of`, or null if unknown).

The application checks that a Web Search call completed and matches model-listed
source URLs against API-provided citation metadata. A recommendation requires the
model to report sufficient market evidence and at least one cited market-data
source timestamped within seven days of research time (allowing five minutes of
clock skew). Otherwise it stores `Watch` with null price levels. Uncited source
URLs also force `Watch`. Source lists remain available for review.

These checks establish search provenance, not independent verification of source
content, timestamps, OHLCV completeness, or the model's interpretation. The
seven-day limit is a coarse maximum allowing market closures; the prompt requires
freshness appropriate to the actual instrument and timeframe. Web Search cannot
guarantee complete intraday candles or exchange-quality real-time quotes. If data
cannot be found, `Watch` is the expected result. The API never places trades.

The alert schema has no `candles` field. Grok retrieves its own market evidence
through Web Search.

## Responses and result retrieval

Ingress returns `202` only after QStash acknowledges publication:

```json
{"status":"accepted","job_id":"<64-character SHA-256 ID>"}
```

Invalid input returns `400`, authentication failures `401`, oversized bodies
`413`, and missing configuration or publication failure `503`. No provider
exception details or webhook secrets are returned or intentionally logged.

Fetch a completed result from a trusted client:

```bash
curl --fail-with-body \
  -H "Authorization: Bearer $RESULT_API_TOKEN" \
  "https://YOUR_DOMAIN/api/v1/results-8dc2a850b73dc31d/JOB_ID"
```

Results contain the normalized alert, completion timestamp, and structured
analysis: `decision`, `buy_price`, `stop_loss`, `sell_target`, `time_limit`,
`rationale`, `limitations`, `market_data_sufficient`, `sources`, `recommendation_text`,
and `notification_text`. A `Buy` response requires positive price levels
with `stop_loss < buy_price < sell_target`. Invalid or incomplete model responses
fail the worker and are retried. Results expire after seven days. `404` means
not available: queued, failed, unknown, and expired jobs are not distinguished.
`GET /` serves the public Trade-notify company landing page. `GET /health`
reports application liveness only, not downstream service health. Neither route
requires credentials or makes a Redis/xAI request. The landing page's sample
alert is illustrative and does not expose stored results or API credentials.

### Readable recommendation

Grok generates both `recommendation_text` (a full plain-text report) and
`notification_text` (a plain-text summary of up to 300 characters) in the original
analysis request alongside the structured fields. The prompt requires TRIGGER,
INPUTS, RECOMMENDATION, RISKS AND LIMITATIONS, and SOURCES sections, matching the
structured decision and price levels. The report must distinguish sender claims
from independently retrieved evidence and identify missing or conflicting inputs.
The application validates the schema but cannot guarantee the model's prose is
factually or semantically consistent; review reports before relying on them.

The separate narrative formatter has been removed. Application-level stale-alert
or evidence-check failures produce a short Watch notice rather than exposing a
potentially contradictory model-written recommendation. These notices are not
full research reports. The structured limitations and sources remain alongside.

Single-result JSON and each list entry include `recommendation_text`,
`notification_text`, `report_url`, `report_expires_at`, and `notification_message`,
alongside the original `alert` and `analysis`. Text fields are also stored inside
`analysis`. `notification_message` appends the signed report URL to the model's
short summary, ready for a delivery integration. It may span multiple SMS segments.

For a human-readable plain-text response:

```bash
curl --fail-with-body \
  -H "Authorization: Bearer $RESULT_API_TOKEN" \
  "https://YOUR_DOMAIN/api/v1/results-8dc2a850b73dc31d/JOB_ID?format=text"
```

The default is `format=json`; other formats return `400`. Authentication is
required for both formats. Text output returns Grok's stored report directly.
Older records without model-written report fields remain available as JSON with
null report/text metadata; `format=text` and link creation return `409`. Nothing
rewrites these records or reruns Grok just to create a report.

### Share a browser report

Generate a signed link for a completed result with model-written report text:

```bash
curl --fail-with-body -X POST \
  -H "Authorization: Bearer $RESULT_API_TOKEN" \
  "https://YOUR_DOMAIN/api/v1/reports-da0db27189a6e42d/JOB_ID/link"
```

The response contains `job_id`, `report_url`, `report_expires_at`,
`notification_text`, and `notification_message`. This endpoint generates a link,
not a new analysis: no paid model call, Redis write, or retention extension occurs.
A result missing from Redis returns `404`; one without report text returns `409`.

Open `report_url` in a browser to view a mobile-friendly report page with the
Grok-written report, original validated alert, structured JSON and source links.
The URL uses `/api/v1/reports-da0db27189a6e42d/JOB_ID?expires=...&signature=...`.
It grants read-only access to that one report without an Authorization header.
**Anyone who has the complete signed URL can read that report.** Treat it as a
shareable credential; never append `RESULT_API_TOKEN` to messages or URLs.

Links expire seven days after the result's `completed_at`, and stop working sooner
if the Redis result expires or is removed. Regenerating a link does not extend its
expiry. The existing `RESULT_API_TOKEN` signs links with a separate HMAC purpose;
rotating it invalidates all previously issued report links. Individual revocation
requires removing the stored result (which also removes the analysis).

The link origin is derived from your configured `WORKER_URL`, never an incoming
Host header. Pages escape model text and JSON, load no external resources, use
`no-store` and `no-referrer`, and request no search-engine indexing. Indexing
directives do not replace the signed-link access check. Deploy the updated code,
including `templates/report.html`; no new environment variables or Pine changes
are required.

WhatsApp, SMS and email sending are not implemented. A future notification worker
can send the returned `notification_message` separately from Grok processing.

### List completed results

```bash
curl --fail-with-body \
  -H "Authorization: Bearer $RESULT_API_TOKEN" \
  "https://YOUR_DOMAIN/api/v1/results-8dc2a850b73dc31d?count=20&cursor=0"
```

The response is `{"results": [...], "next_cursor": "..."}`. Each entry contains
the same complete record returned by the single-result endpoint. For all results,
repeat the request with the returned `next_cursor` until it is `"0"`. An empty
page with a nonzero cursor is not the end. `count` accepts 1–50 (default 20) and
is a Redis SCAN work hint, not an exact page-size limit. Keep the cursor as a
string; do not round it through JavaScript's numeric type.

Listing is unordered and not a snapshot. Redis SCAN may return duplicates, so
deduplicate by `job_id` when collecting pages. Concurrent additions/expirations
can change the listing during traversal. Only completed, unexpired results are
included; locks, queued jobs, and failed jobs are not listed. Use a small `count`
when records are large. Redis errors return a generic `503`.

Both result routes require `Authorization: Bearer <RESULT_API_TOKEN>` and use the
randomized `results-8dc2a850b73dc31d` prefix. The old `/api/v1/results/...` path
is removed. The prefix reduces discovery; bearer-token authentication is still
required. Deploy the updated code and update any result API consumers; no new
environment variable or Redis migration is needed.

## Reliability and operations

- TradingView allows only [three seconds](https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/)
  for a response. Queue publishing has a 0.5-second per-operation timeout and no
  inline retries. Cold starts, DNS, and network delays still require deployment
  measurement; this is not a guaranteed total response deadline.
- QStash is configured for five retries and a 330-second delivery timeout. Monitor
  its delivery logs and dead-letter queue, and replay exhausted jobs after fixing
  the cause. Check your QStash plan supports these settings.
- QStash deduplicates identical publish IDs for ten minutes. Redis suppresses
  completed duplicates for seven days. Redis must retain data and avoid eviction
  during this period. Reusing an `alert_id` with different data is a worker `409`
  and requires fixing the sender.
- A token-owned Redis lease prevents concurrent processing; Lua atomically stores
  the result and releases the lock. Worker failures return `503`; a killed worker's
  lease expires in 360 seconds so a later retry can recover.
- This is at-least-once delivery, not exactly-once billing: a crash after a model
  call but before storing its result can cause another paid call on retry. Alerts
  older than 24 hours at processing time produce `Watch` without a model call.
- SDK retries are disabled; QStash owns retries. Research requests have a 240-second
  per-operation timeout (20 seconds for connection establishment) and a 4,000-token
  output budget; Vercel imposes the invocation's 300-second hard limit. QStash's
  330-second delivery timeout allows additional time for startup. Search adds
  variable latency and charges.
  Measure live requests before relying on this budget; if searches repeatedly
  time out, adjust the Vercel duration, QStash delivery timeout, Redis lock TTL,
  and SDK timeout together to fit your plans. Failed or incomplete research is
  retried, while completed research with inadequate evidence is stored as `Watch`.
- Worker failure logs include the processing stage, elapsed seconds, exception
  class, and underlying cause class without logging provider bodies or secrets.
  For example, `APITimeoutError` with `cause=ReadTimeout` indicates a stalled
  response; `cause=ConnectTimeout` points to connection establishment. A timeout
  alone does not establish whether xAI processing or the network caused the delay.
- Timeout headers are stored on QStash messages at publication time. After
  deploying timeout changes, publish a new test alert or replay old messages
  with the updated delivery timeout; already queued messages retain the old one.
- Rate limiting can be configured in Vercel Firewall. Keep ingress and worker
  secrets separate and monitor invocation counts and paid API usage.
- Results are stored for polling; Telegram/email delivery is not implemented.

## Local development and tests

```bash
uv sync --group dev
# Export the variables from .env.example in your shell, then:
uv run flask --app main run --port 3000
uv run python -m pytest -q
```

Flask does not load `.env` automatically in this dependency set. Do not point a
development worker at production Redis. End-to-end QStash testing requires a
public HTTPS worker URL. Automated tests use fake Redis with actual Lua script
execution and mocked queue/model calls; they do not contact paid services.
