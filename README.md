# TradingView analysis webhook

Flask API for Vercel. The TradingView endpoint authenticates and validates an
alert, publishes it to QStash, then returns `202` with a `job_id`. A separately
invoked worker forwards the alert to your configured Grok bot automation. The bot
researches the alert and POSTs a structured result to an authenticated callback,
which stores it in Redis for seven days.
There are no detached threads or model requests in the ingress endpoint.

```text
TradingView -> webhook -> QStash -> authenticated worker -> Grok bot
                                                            |
                          Redis <- authenticated callback <- result
                            |
                   results API / reports / WhatsApp
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
| `GROK_WEBHOOK_URL` | External bot automation's HTTPS trigger URL |
| `GROK_WEBHOOK_KEY` | Raw TradingView bot trigger key; sent as a Bearer token |
| `GROK_CHAT_WEBHOOK_URL` | Separate external bot webhook for WhatsApp conversations |
| `GROK_CHAT_WEBHOOK_KEY` | Raw key for the separate chat webhook |
| `GROK_CALLBACK_TOKEN` | Separate bearer token authenticating finished bot results back to this app |
| `WHATSAPP_VERIFY_TOKEN` | Separate random token matching Meta's webhook Verify token field |
| `WHATSAPP_APP_SECRET` | Meta application's App Secret, used to authenticate WhatsApp deliveries |
| `WHATSAPP_ALLOWED_NUMBERS` | Comma-separated international sender numbers; empty/missing denies all senders |
| `WHATSAPP_ACCESS_TOKEN` | System-user token for outgoing command replies |
| `WHATSAPP_PHONE_NUMBER_ID` | Meta's numeric business phone number ID, not the telephone number |
| `WHATSAPP_GRAPH_API_VERSION` | Optional; defaults to `v25.0`, matching the configured Meta example |

Generate distinct random tokens, for example with `openssl rand -hex 32`.
Do not commit `.env` or actual credentials. `BACKGROUND_ANALYSIS`, `ANALYSIS_BACKEND`,
`XAI_API_KEY`, `XAI_MODEL`, and `XAI_CHAT_MODEL` are no longer used and may be removed
from Vercel. Both analysis and chat use bot webhooks; there is no model SDK dependency.

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

## Grok bot automation and result callback

TradingView analysis uses the Grok bot automation with its saved research
instructions. Configure `GROK_WEBHOOK_URL`, `GROK_WEBHOOK_KEY` (raw value,
without `Bearer `), and a distinct `GROK_CALLBACK_TOKEN` in Vercel. Configure the
bot's fixed callback URL and the same callback token through its secure credential
field before deploying. There is no direct-API analysis fallback. No tokens belong
in committed files, URLs, JSON alert payloads, or bot chat messages. The local
`.env.grok-callback` token handoff file, if generated, is ignored by Git and is
not automatically loaded or deployed; copy its value into Vercel and the bot's
secure field. Only `.env.example` is tracked.

Callback URL:
`https://trading-api-lac.vercel.app/api/v1/grok-callback-73e312d8bab9bfda`

Authentication:

```text
Authorization: Bearer <GROK_CALLBACK_TOKEN>
Content-Type: application/json
```

Give the bot [these callback instructions](docs/grok_bot_instructions.md) and the
[JSON Schema](docs/grok_bot_callback.schema.json). It must echo the original
`job_id`. The completed format is the bot's rich native response:
`status`, `ticker`, `recommendation`, `analysis`, and `summary`, with optional
metadata, reasons, risk rules, disclaimer and sources. `recommendation_text` and
`notification_text` are required, as supplied by the current bot configuration.
The former compact `job_id`/`analysis` format is not interpreted as a completed
recommendation. Like other unexpected formats, it is recorded as an error for
the associated pending job and acknowledged with HTTP 200.
The callback authenticates before reading input, validates known fields and
price-level consistency, and only
accepts jobs the app previously dispatched. Original alert data and completion
timestamps come from the app, not callback-supplied replacements. Meta/TradingView
callback URLs and the QStash worker URL do not change.

Rich results retain the complete JSON under `bot_response`, including confidence,
entry ranges, all targets/reasons, position sizing, indicator details, sentiment,
news, risk rules, and future additional fields. The original validated alert and
app receipt time remain separate from the bot's reported metadata. A bot-supplied
ticker must match the pending alert (a bare or compatible exchange-qualified
symbol is accepted). Signed reports show the original bot response in an escaped,
expandable section, and conversational lookups can read it. No new model call
rewrites or formats the result.

The app's validated `analysis` view uses the current bot fields: entry_range,
stop_loss, all targets, and time_limit_days. It contains no former buy_price,
sell_target or string time_limit fields. No fill price, midpoint or primary target
is invented. Long stops must be below the entry range and all targets above it.
Both model-written texts are required: recommendation_text is used directly for
the report and notification_text for the WhatsApp preview. There is no summary-to-
report compatibility fallback. WhatsApp `/latest` adds validated long-entry ranges,
stop, all targets and the app-signed report link. Full detail stays in bot_response.

Add top-level `market_data_sufficient` and timestamped `sources` to rich callbacks
for actionable results. Rich bodies without those fields are still accepted and
preserved, with a validated Watch view rather than a fabricated evidence claim.

For an alert the bot cannot process, it can alternatively POST
`{"job_id":"<real app job ID>","status":"rejected","reason":"Missing ticker"}`.
The reason must be nonblank text of at most 2,000 characters. This is a terminal
outcome, acknowledged with HTTP 200 and stored for seven days with the original
alert, `status=rejected`, `reason`, and `analysis=null`. JSON result APIs expose
this outcome; it creates no recommendation or report, and WhatsApp ticker
lookups ignore it. Worker retries acknowledge the rejected status without
starting another bot run. Identical rejection callbacks are idempotent; a
different outcome/reason cannot overwrite an existing result. Only a real,
unexpired pending job can be rejected; `abc-123` is not a valid app job ID.

Authenticated callbacks with valid JSON and a matching pending job that fail
schema validation, contain a mismatched ticker, or cannot be interpreted by the
app are stored as `status=error`, `error_type=unexpected_bot_response`, a readable
`reason`, `analysis=null`, and the original `bot_response`. Field/type diagnostics
are included when available, without copying input values into the diagnostics.
The callback returns HTTP 200 with `status=error`, so the bot need not repeatedly
deliver a response the app cannot use. Error outcomes appear in the JSON result
APIs, expire after seven days, and produce no recommendation/report. Retries of
the same error are idempotent; a different response cannot overwrite a terminal
outcome. These errors finish the job, just like an explicit bot rejection.

The analysis flow is:

```text
TradingView -> existing webhook -> QStash worker -> Grok bot trigger
                                                  |
                          authenticated callback <- finished JSON analysis
                                      |
                         existing Redis results/reports/WhatsApp lookups
```

The worker persists a pending job for 24 hours before dispatch, sends only the
validated alert plus job_id, and returns `202 awaiting_callback` after trigger
acceptance. Its `202` does not mean analysis is complete. Retries after accepted
dispatch do not trigger a new run. If a network timeout makes acceptance uncertain,
the trigger may receive the job again: the bot must deduplicate by job_id. Callback
retries with the same normalized analysis return 200 without changing the record
or expiry; different results return 409. Unknown/expired jobs return 404. Invalid
formats for a known pending job are stored as error outcomes with HTTP 200.
Unparseable JSON or a missing/invalid job ID returns 400 because no job can be
associated, bad authentication returns 401, and storage failures return 503. Accepted
results have the existing seven-day retention and work with the existing result
APIs, signed report links, and WhatsApp lookups.

External results carry `analysis_backend=grok_bot` and
`source_provenance=bot_reported`. Their source URLs cannot be matched against xAI
Responses API citation metadata. The stored limitations explicitly disclose
that the sources are bot-reported. Missing/insufficient market
evidence, or absence of a market-data source timestamp within seven days (allowing
five minutes of clock skew), downgrades the result to Watch with null price levels.
The original bot response remains available if evidence validation changes the
app's decision. Source content and timestamps supplied by the external bot are not independently
verified. Source freshness must still be appropriate for the alert timeframe.

Keep the bot trigger's saved instructions focused on analysis. It should POST its
result back here, not message WhatsApp recipients or place trades. It may also
post a copy in its own chat. Confirm a real end-to-end callback after deployment;
local tests mock the external bot. WhatsApp conversations use a separate bot
webhook, described below. If the bot trigger
is unavailable, the worker returns 503 for QStash to retry; it never starts a
second analysis through the direct API.

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

After signature validation, messages and statuses are filtered against
`WHATSAPP_ALLOWED_NUMBERS`. Use country-code-prefixed digits, separated by commas;
an optional leading `+` is accepted in the configuration. An empty or missing list
denies all senders. Unapproved messages and contact records are not stored or
processed; Meta still receives `200 OK` to prevent delivery retries. Allowlist
changes affect new deliveries, not records stored before the change.

Filtered event envelopes are stored in the existing Redis database as
`wa:webhook:<event_id>`, with `event_id`, UTC `received_at`, and the filtered
`payload`. Records include approved message content and sender identifiers, so restrict
Redis access. They expire after seven days and can currently be inspected in
the Redis console; the trading-results API does not list this inbox.
Identical envelopes (ignoring JSON key order and whitespace) are acknowledged
without overwriting or extending their expiry. Command replies are also
deduplicated by incoming message ID across different envelopes.
Batches up to 3 MiB are accepted without changing the
TradingView endpoint's 128 KiB limit.

Storage or command-enqueue failures return `503` so Meta can retry. Logs contain
event/job IDs and error types, without message contents or sender numbers.

### WhatsApp commands

Approved users interact entirely through WhatsApp:

| Message | Response |
| --- | --- |
| `/latest NVDA` | The single newest stored recommendation for NVDA |
| `/latest NVDA, AAPL` | One newest recommendation for each ticker; maximum 10 tickers |
| `/latest NASDAQ:NVDA` | Match the exact exchange and ticker |
| `/latest all` | List all tickers with stored recommendations available |
| `/help` | List supported commands |
| `/reset` | Ask the chat bot to reset this conversation and wait for confirmation |
| Any text not starting with `/` | Start or continue a Grok conversation about stocks or Trade-notify |

Commands are case insensitive. Ambiguous bare symbols require an exchange
prefix; the app asks instead of guessing. Unknown slash commands return help.
Plain text from approved senders is queued for the separate chat bot webhook.
`/help` and `/latest all` use stored data only. `/latest TICKER` requests a
new daily analysis if no stored recommendation exists. `/reset` calls the chat webhook's reset operation.
Newest means the most recent analysis completion time, across all stored
intervals/strategies for that ticker. Replies show the completion time, interval,
short recommendation, and an expiring full-report link when available. No stored
result triggers on-demand analysis and a WhatsApp reply when ready. Results expire after seven days.

Configure the outgoing access token and phone number ID above, with
`whatsapp_business_messaging` permission and access to the business account.
The existing QStash variables and `WORKER_TOKEN` are reused. The reply worker is
`/api/v1/whatsapp-reply-71f295b68cd903ea` on the production origin derived from
`WORKER_URL`; no additional worker-URL variable is required. Meta's callback URL
and the TradingView worker URL stay the same. Allow QStash to reach this new
route through Vercel Deployment Protection, then push and deploy the new code.

The ingress persists commands and publishes their IDs to QStash. A separate
authenticated worker rechecks the allowlist, retrieves stored results, and sends
text replies quoting the incoming WhatsApp message ID. Status notifications do
not trigger replies. Delayed commands older than 24 hours are not replied to;
the worker does not send templates or initiate unrelated conversations.

Reply parts are reserved in Redis before sending, preventing duplicate sends
on retries. If an HTTP timeout or process crash makes delivery uncertain, that
part is not sent automatically again; it may be missing and the user can issue
a new command. HTTP 429 responses can retry. Other explicit errors are recorded
without automatic resending. Command records and part statuses are retained for
seven days under `wa:command:<job_id>*`. Worker outcomes distinguish completed,
delivery_failed, and delivery_uncertain; an uncertain or explicitly rejected part
is not reported as a successful delivery on a later retry. No delivery receipt is implied by an
accepted API request. Stored-result scans are capped at 10,000 records; exceeding
the cap returns an explanatory response instead of incomplete results.

### Bot conversations, history and app information

For a message that receives no reply, first test `/help`, which bypasses the chat
bot. Application INFO logs show incoming messages being filtered or queued,
worker outcomes, bot dispatch/callback, and Meta send acceptance. An HTTP 200
on the incoming webhook alone is not proof that a reply was queued: it also
acknowledges unapproved senders and status-only events. Look for
`reason=sender_not_allowed`, `reason=business_phone_mismatch`, or named missing
configuration variables, then the correlated job ID. If a message was queued,
check QStash delivery to `/api/v1/whatsapp-reply-71f295b68cd903ea`. Meta send failures
log HTTP status and numeric error/subcode, without provider text, message content,
phone numbers or tokens. Progress logs must be viewed on the deployment containing
these changes; deploying environment variables alone does not update source code.

Configure `GROK_CHAT_WEBHOOK_URL` and `GROK_CHAT_WEBHOOK_KEY` separately from the
TradingView automation. They use the same `GROK_CALLBACK_TOKEN` and existing
callback URL. The app uses no direct model calls and sends no guide or transcript
with chat messages. Copy [the app guide](prompts/whatsapp_app_guide.md) and
[chat bot instructions](docs/grok_chat_instructions.md) into the chat automation's
saved configuration once. Edit the bot's instructions when app information changes;
changing/deploying the local Markdown alone does not update the bot.

The outbound body has `type=message`, an opaque `conversation_id`, the incoming
WhatsApp `message_id`, and `user_message`. The conversation ID hashes business
phone ID and sender, keeping routes distinct across users/numbers without sending
the raw phone number as metadata. After a successful single-ticker `/latest`
lookup, the next chat messages also include that result's `job_id`; multi-ticker
lookups clear that context instead of guessing. This ID provides context and
must not override a ticker explicitly requested in the new message.

The bot owns conversation history and app/topic rules. It must isolate histories
by conversation_id, restrict discussion to stocks/Trade-notify, and use read-only
alert analyses. The app cannot independently verify the bot's internal file or
history isolation. It does bind each callback to the exact stored conversation
and message and chooses the WhatsApp recipient only from that stored request.
A user cannot supply a conversation ID by embedding it in their message text.

The bot responds asynchronously (reportedly about a minute) to the existing
callback with `type=conversation_reply`, `status=completed`, conversation_id,
message_id, message and optionally answered_at. The callback persists the reply
and queues WhatsApp delivery; an HTTP acknowledgement does not guarantee final
WhatsApp delivery. The allowed-number list, business phone ID and original
message's 24-hour reply window are rechecked by the delivery worker. Replies quote
the original WhatsApp message and long text is split into parts. Duplicate
callbacks do not repeat sends; if queue publishing fails after reply storage,
the same callback can retry publishing. A different response for the same message
returns 409. Unknown/expired message references return 404. Unexpected known
responses are recorded as errors, acknowledged, and produce a short failure notice.
Chat callback records live at `wa:command:<local_job_id>:chat_result`; chat does not
write or replace TradingView recommendations.

`/reset` sends `type=reset` with conversation_id and message_id. Only a matching
`reset_done` callback produces a reset confirmation. A new reset generation
suppresses late replies from before the reset; new chats wait in the queue for
reset acknowledgement. The bot must deduplicate both messages and resets by
conversation_id/message_id. If a reset callback never arrives, resolve the bot
failure and retry the reset before continuing. Reset does not delete seven-day
local webhook/command audit records. It clears the app's remembered analysis ID.

Redis retains routing requests, callback records and delivery state for seven
days; there is no local conversational transcript. History retention/reset rules
inside the external bot must be configured there. Input is capped at 6,000
characters and callback reply text at 12,000. Dispatch uses a 10-second timeout
and returns without waiting for analysis, followed by callback delivery. After
accepted dispatch, worker retries do not trigger another run; on an ambiguous
network timeout the bot may receive the same message ID again and must resend
its cached answer. No direct model API fallback is attempted.

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
`prevCloses`. `price` is required; when `close` is also supplied, they must match. ADX must be in [0, 100], volume must be nonnegative, supplied OHLC
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
The complete validated alert determines its fingerprint, with normalized prices
and UTC timestamps. An alert_id determines the job ID when present; otherwise
the full alert content does. There are no historical hash compatibility rules.
Use a fresh alert after deployment rather than replaying old queue entries with
fingerprints from the previous schema.

Pine compilation and actual alert delivery must be verified in TradingView;
the local test suite verifies the corresponding API payload and analysis input.

### Market evidence

TradingView sends indicator context; **no candle array is required**. The bot's
saved research instructions govern its market-data retrieval, swing-trading
analysis, and report generation. Configure those instructions using
[docs/grok_bot_instructions.md](docs/grok_bot_instructions.md), which includes the
callback contract and evidence requirements. The app forwards rich alert fields
without a second direct model request. It does not impose API-specific Web Search
options on the external bot or independently verify that a search ran.

The bot must resolve the instrument, exchange, currency and timeframe; disclose
missing history, delayed quotes and timeframe substitutions; and never fabricate
candles, prices, indicators, source URLs or source timestamps. Sources include a
URL, title, kind (market_data/news/sentiment), and the timezone-qualified timestamp
of the observation/publication (`as_of`, or null if unknown).

The callback validates the structured recommendation and requires sufficient
market evidence plus a market-data source timestamp within seven days of receipt,
allowing five minutes of clock skew. Otherwise it stores Watch with null price
levels. This coarse age limit does not establish intraday freshness. Source URLs,
content, timestamps, history completeness and the bot's interpretation are
bot-reported, not independently verified. The resulting limitations disclose
this provenance. The app never places trades.

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
analysis: `decision`, `entry_range`, `stop_loss`, `targets`, `time_limit_days`,
`rationale`, `limitations`, `market_data_sufficient`, `sources`, `recommendation_text`,
and `notification_text`. Rich
results also contain the complete `bot_response`. Buy requires a positive entry
range, stop, at least one target and a time limit, with the stop below
entry and all targets above it. Unexpected callback shapes are recorded as error
outcomes for known pending jobs; transport/storage failures can retry. Results expire after seven
days. Explicit bot rejections return their stored status/reason; `404` means no
terminal result is available (pending, unknown or expired).
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

WhatsApp command and conversation replies are implemented as described above.
Proactive alert notifications, SMS, and email delivery are not implemented.

### List stored results

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
can change the listing during traversal. Completed recommendations and explicit
bot rejections/error outcomes are included until expiry; locks, pending jobs, and transport
failures without a terminal callback are not listed. Use a small `count`
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
- A token-owned Redis lease prevents concurrent dispatch. The callback atomically
  stores completed results and rejects conflicting replacements. Worker failures
  return 503; a killed dispatch worker's lease expires in 360 seconds so a later
  retry can recover.
- Dispatch uses a 10-second per-operation timeout with a 3-second connection
  timeout and no inline retries. QStash owns retries. A timeout after bot acceptance
  can cause the same job to be delivered again; the bot must deduplicate by job_id.
  Pending jobs expire after 24 hours. Stale alerts are not sent for new analysis.
- Analysis runs asynchronously outside Vercel; the bot POSTs its result back when
  finished. A successful dispatch does not guarantee a callback. Monitor pending
  jobs and bot runs as well as QStash. There is no direct-API fallback.
- Worker failure logs include processing stage, elapsed time, exception class and
  underlying cause class without provider bodies or secrets. A dispatch timeout
  alone does not establish whether the bot accepted the alert.
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

### On-demand recommendations

`/latest NVDA` returns the newest stored recommendation. If none exists, the
WhatsApp worker requests analysis through `GROK_WEBHOOK_URL` using
`{"type":"analyze","job_id":"<app-generated ID>","ticker":"NVDA","interval":"1D"}`.
No TradingView signal, price, or indicators are invented. The existing authenticated
analysis callback stores the result with `source: "on_demand"` (TradingView jobs use
`source: "tradingview"`) and queues the recommendation and signed report link back
to the requesting WhatsApp user. The normal allowlist and 24-hour reply window
still apply. A single requested ticker sets the conversation's analysis job for
follow-up questions; multiple tickers leave no single implicit selection.
`/latest all` only lists available stored tickers. No additional environment
variables are required. Configure the analysis bot to accept the `analyze` payload
and return its normal analysis callback with the supplied job ID.
