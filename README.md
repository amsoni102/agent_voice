# AgentVoice

An AI sales rep that phones small businesses across the US, has a natural conversation,
qualifies them and books a strategy call to sell websites and mobile apps.

- **Linux app** (`server/`): the voice agent, auto-dialer, web dashboard, CLI and systemd service.
- **Android app** (`android/`): monitor and control everything from your phone.

## How it works

```
 Phone / Android Talk  ⇄  AgentVoice server (Linux)
                            ├─ default: Whisper STT → Ollama (llama3.2 @ :11434) → Piper TTS
                            ├─ optional: OpenAI Realtime (speech-to-speech) if OPENAI_API_KEY is set
                            ├─ dialer + calling rules, SQLite, REST API
                            └─ web dashboard / Android app
```

Talk/practice is local by default (no paid API). Phone calls can use the same local stack
once a carrier is wired; OpenAI Realtime is optional later for faster speech-to-speech.

Each call goes like this:

1. **Pre-call research.** The agent checks the prospect's website (mobile-friendly? HTTPS? slow?
   outdated copyright year? online booking?) to find a personal hook.
2. **Opening.** It waits for "Hello?", then introduces itself with the required AI/recording
   disclosure and asks permission to continue.
3. **Discovery → pitch → close.** It asks one question at a time, listens, digs into pain points,
   ties one service to that pain, and offers two concrete meeting times from your calendar.
4. **Tools during the call:** `record_lead_info`, `check_availability`, `book_meeting`,
   `schedule_callback`, `set_outcome`, `add_to_do_not_call`, `transfer_to_human`, `end_call`.
5. **After the call:** a manager-style summary, a hot/warm/cold score, a next step and one
   coaching note, plus an optional webhook to your CRM or calendar when a meeting is booked.

## Quick start (Linux)

```bash
make install          # creates .venv and .env
make run              # dashboard on http://localhost:8080
make token            # API token for the dashboard and the Android app
```

**Talk/practice is local by default.** It uses the Ollama already running on this machine
(`llama3.2` at `http://127.0.0.1:11434`) plus Whisper (listen) and the Piper voice from
the management assistant. No OpenAI key. Open the Android **Talk** tab (or Practice in the
dashboard), tap Start, say “Hello?”.

Keep Ollama up: `ollama serve` (and `ollama pull llama3.2` once). Optional: set
`OPENAI_API_KEY` later for cloud Realtime; `VOICE_BACKEND=auto` switches when that key is present.

Other targets: `make test`, `make install-service` (runs in the background and starts on login),
and `make install-desktop` (adds an "AgentVoice" launcher that starts the server and opens the dashboard).

CLI: `.venv/bin/agentvoice --help` (`serve`, `token`, `import-leads`, `import-dnc`, `campaign`, `stats`, `prompt`).

## Phone calls (Twilio)

1. Create a Twilio account, buy a US local number, and fill `TWILIO_ACCOUNT_SID`,
   `TWILIO_AUTH_TOKEN` and `TWILIO_FROM_NUMBER` in `.env`.
2. Register the number for **STIR/SHAKEN / caller ID (Trust Hub)** so calls aren't labeled "Spam Likely".
3. Make the server reachable over HTTPS and set `PUBLIC_BASE_URL` (next section).
4. In the Twilio console → your number → *Voice configuration*:
   - "A call comes in" → Webhook `POST {PUBLIC_BASE_URL}/twilio/inbound`, so people calling back reach the agent.
   - "Call status changes" → `POST {PUBLIC_BASE_URL}/twilio/status`
5. In the dashboard → *Agent settings*, set your own phone as **Test phone number**, then use
   *Calls → Test call to my phone*.

### Making the server reachable

Twilio has to reach your server over public HTTPS/WSS. Pick one:

- **Cloudflare Tunnel** (no open ports, works from a home PC):
  `cloudflared tunnel --url http://localhost:8080` for a quick test, or a named tunnel on your
  domain for production. Put the HTTPS URL in `PUBLIC_BASE_URL`.
- **VPS** (any small Ubuntu server): `make install && make install-service`, with Caddy in front:
  `agent.yourcompany.com { reverse_proxy localhost:8080 }`.

Every Twilio webhook is checked against its signature. Each media stream needs a one-time
per-call token. The API needs the bearer token.

## Android app

```bash
make compile_apk      # -> android/agentvoice-debug.apk  (same toolchain as ger_app: JDK 21, SDK 35)
adb install -r android/agentvoice-debug.apk
```

Open it and enter the server address (your `PUBLIC_BASE_URL`, or `http://<pc-ip>:8080` on the
same Wi-Fi) and the token from `make token`. Screens: overview with live calls and campaign
start/pause, leads (search, filters, CSV import, add, call now, do-not-call), call history with
live transcripts and hang-up, meetings, and agent and playbook settings.

Signed release build: create `android/keystore.properties` with `storeFile`, `storePassword`,
`keyAlias`, `keyPassword`, then run `make release_apk`.

## Training the agent

*Sales playbook* (dashboard) is the agent's training: name, company, offer, price ranges,
differentiators, **real** proof points (it never invents any), discovery questions and
objection responses. *Campaign focus* adds per-campaign instructions (e.g. "these are dentists;
lead with online booking"). *Preview full prompt* shows exactly what the model receives.

Ways to improve results:
- Do practice calls and read each call's **coaching** line. Turn repeated issues into playbook rules.
- Add objections you actually hear, with the answer your best human rep would give.
- Voices `marin` and `cedar` sound the most human. Set *turn-taking eagerness* to `low`
  if the agent cuts people off.

## Lead CSV format

Headers are detected automatically. Only `phone` is required:

```
business,phone,contact,email,website,industry,city,state,consent,consent_source,notes
Joe's Tacos,(512) 555-0142,Joe Ruiz,,joestacos.com,restaurant,Austin,TX,yes,web form 2026-10-02,
```

## Legal requirements (read before dialing)

This is not legal advice. Have a US telemarketing attorney review your setup. The software
enforces safe defaults, but **you** are responsible for your lead sources and consent.

- **TCPA + AI voices.** Since the FCC's February 2024 ruling, AI-generated voices count as
  "artificial voice". Calls to **cell phones** (and residential lines) need **prior express
  written consent** for telemarketing. Buying a list of small-business mobile numbers does not
  give you consent. The dialer therefore only calls leads marked `consent=yes` by default
  (*Agent settings → require consent*). Turn this off only for numbers your attorney confirms
  are exempt (e.g. verified business landlines).
- **Disclosure.** Every call opens by saying it's an AI assistant and that the call is recorded
  (needed in all-party-consent states such as CA, FL, PA and WA). Whenever asked, the agent
  says truthfully that it's an AI. It sounds natural, but it never pretends to be human.
  You can reword the disclosure, but you can't remove it.
- **Do-not-call.** Opt-outs during a call are added to the internal list immediately and
  honored forever. Import the **National DNC Registry** for your area codes (*Do-not-call →
  Import registry file* or `agentvoice import-dnc`) and refresh it at least every 31 days.
- **Calling hours.** Calls only go out 9:00–18:00 on weekdays in the prospect's local time zone
  (all zones of states that span two), skip major holidays, and allow at most one call per
  number per 24 hours. The window can never go outside 08:00–20:00, which covers the stricter
  states (FL, OK, MD, WA). Florida, Oklahoma and Maryland also limit call frequency. Keep
  attempts low.
- **Caller ID** must be a real number you own that people can call back. Inbound calls to
  it are answered by the agent.

## Project layout

```
server/agentvoice/
  api.py          REST API, Twilio webhooks, media-stream + practice WebSockets, dashboard
  realtime.py     phone/browser audio ⇄ OpenAI Realtime bridge (barge-in, tools, hangup)
  local_voice.py  local Talk path: Whisper STT + Ollama + Piper TTS
  ollama_llm.py   Ollama HTTP client (same API as on this machine, port 11434)
  playbook.py     sales playbook + system-prompt builder (persona, style, flow, objections)
  tools.py        agent tools (booking, callbacks, DNC, transfer...) and calendar slots
  calls.py        dialer, carrier status, retries, post-call summaries
  compliance.py   calling windows, time zones, holidays, consent, DNC, frequency caps
  research.py     pre-call website analysis
  importer.py     CSV import for leads / DNC lists
  web/            dashboard (HTML/JS, includes practice mode)
server/tests/     pytest suite (includes an end-to-end bridge test against a fake Realtime server)
android/          Kotlin + Jetpack Compose control app
deploy/           systemd unit, desktop launcher
```
