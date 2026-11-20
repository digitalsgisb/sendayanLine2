# Sendayan Line 2 Production Tracker

`sdy2.py` keeps the existing Sendayan Line 2 sensor-counting and timer rules,
while adding the same production lifecycle safeguards used by the ABB trackers.

## Added lifecycle features

- Restart recovery through local SQLite snapshots.
- Durable SQLite event outbox for a future central API.
- Automatic shift end calculated from `workingTime` and overtime.
- Final partial-hour record queued before shift PDF generation.
- Production-day boundary reset at 08:00.
- A 45-minute shift-entry window at 08:00, using machine status `7`.
- Products made inside that window are adopted by the submitted shift without a
  second reset; if no shift arrives, the buffered production is discarded.
- A different shift ID cannot replace an active shift merely because the form
  was submitted again.
- Retained MQTT status and live recovery metadata.

## Counting

- **Waterjet (main output)**, the Line 2 site rule (Nov 2026):
  - **WJ1** (prox sensor 1, GPIO 5) and **WJ2** (prox sensor 2, GPIO 27): every
    HIGH -> LOW edge counts one product, at most once per 0.15 s per waterjet.
    The sensor must go back HIGH before it can count again, and a sensor that is
    already LOW when production resumes does not count.
  - Both are sampled about every 50 ms. WJ1 + WJ2 is the main output:
    `hourly_output`, `product_count`, `total_product_count`, the Hourly_Data
    "actual" column, the PDF, and the cycle time (`batch_run_time / total_output`).
    Tablet count adjustments change this count.
- **Forming:** the limit switch (GPIO 17) counts `forming_output` for the live
  panel only (also in the MQTT payload and the `shift.ended` event). It is not
  written to the Sheet or PDF.
- Both count only in NORMAL mode (and during the 08:00 shift-entry buffer).
- Machine status is unchanged: both prox sensors blocked = LOADING, then DELAY
  after 10 seconds; otherwise RUN.

The live payload adds shift totals `waterjet_output`, `waterjet_a_output`,
`waterjet_b_output` and `forming_output` (Smart Checksheet panels), and keeps the
site fields `wj1_count`, `wj2_count`, `wj_total_count` and `mf_count`
(model counts, reset on model change).

## Google Sheets sending

Sheets rows and shift PDFs are queued in SQLite (`google_queue`) and sent by a
background thread, so a slow or offline Apps Script never pauses sensor
counting, and unsent rows survive a restart. Items are sent in order with the
same `request_id` on every retry.

The Apps Script URL is set from Smart Checksheet: it publishes a retained
`sdy2/config` message such as
`{"google_web_app_url": "https://script.google.com/macros/s/.../exec"}`. The
tracker saves it in SQLite, uses it immediately and answers on
`sdy2/config_result`. An empty value returns to the built-in
`DEFAULT_WEB_APP_URL`.

## Runtime files

By default SQLite is stored at `runtime/sdy2_state.db`. Override it with:

```env
SDY2_SQLITE_PATH=/var/lib/sdy2/sdy2_state.db
```

Optional API outbox delivery settings are:

```env
SDY2_API_ENABLED=true
SDY2_API_EVENTS_URL=http://172.19.3.72:8088/api/v1/events
SDY2_API_KEY=<private-key>
```

Do not commit `.env`, API keys, or runtime database files.

## Install and verify

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
.venv/bin/python sdy2_sqlite_status.py
mosquitto_sub -h localhost -t 'sdy2/status' -v
```

The systemd template expects this project at `/home/<user>/sdy2`:

```bash
sudo install -m 0644 deploy/sdy2@.service /etc/systemd/system/sdy2@.service
sudo systemctl daemon-reload
sudo systemctl enable --now "sdy2@$USER.service"
```

See `SQLITE_RECOVERY_GUIDE.md` for restart, buffer, and outbox behavior.

## Updating the Raspberry Pi

`deploy/sdy2-update.sh` does the whole update safely:

1. Refuses to run if the Pi has local code edits.
2. Fetches `origin/main`; stops if it is already up to date.
3. Waits for a **safe moment**: no active shift, or mode REST / PLANNED STOP.
   It checks every minute for up to 12 hours.
4. Fast-forwards, runs `pip install`, then runs the tests.
5. Restarts `sdy2@<user>.service` and checks it stays running for 20 seconds.
6. If any step fails, it resets to the previous commit and restarts the old
   version. Everything is logged to `/var/log/sdy2-update.log`.

### One-time install (does NOT change the running tracker)

Run on the Pi. `git fetch` + `git show` only download; the files in `~/sdy2`
stay untouched until the update runs.

```bash
cd "$HOME/sdy2"
git fetch origin main
git show origin/main:deploy/sdy2-update.sh | sudo install -m 0755 /dev/stdin /usr/local/sbin/sdy2-update.sh
git show 'origin/main:deploy/sdy2-update@.service' | sudo install -m 0644 /dev/stdin /etc/systemd/system/sdy2-update@.service
git show 'origin/main:deploy/sdy2-update@.timer' | sudo install -m 0644 /dev/stdin /etc/systemd/system/sdy2-update@.timer
sudo systemctl daemon-reload
sudo systemctl enable --now "sdy2-update@$USER.timer"
systemctl list-timers "sdy2-update@$USER.timer" --no-pager
```

Check the Pi's clock and time zone first (`timedatectl`): the timer uses local
time. Do not run `git pull` on the Pi before the scheduled update.

### Later updates

Edit `OnCalendar=` in the timer for a new planned date, or start one now (it
still waits for a safe moment):

```bash
sudo systemctl start --no-block "sdy2-update@$USER.service"
tail -f /var/log/sdy2-update.log
```

If the service does not start, inspect its latest logs:

```bash
sudo journalctl -u "sdy2@$USER.service" -n 100 --no-pager
```

## Google Apps Script deployment

`appScriptSdy2.js` is the matching backend for the Line 2 spreadsheet. Copy it
into the spreadsheet's Apps Script project and deploy a new web-app version.
Keep the deployed `/exec` URL equal to `WEB_APP_URL` in `sdy2.py`.

The backend combines repeated records in the same hour, selects the latest
same-shift details, validates allowed data tabs, and deduplicates Python HTTP
retries by `request_id`. Run `node tests/test_sdy2_appscript.js` before deployment.

### Duplicate-delivery protection

- Each data tab receives a trailing `Request_ID` column automatically. Keep this
  column in place: it is the durable idempotency record used after cache expiry,
  network timeouts, and process restarts.
- Hourly rows and shift PDFs use deterministic request IDs, so the same logical
  event is rejected even if it is queued twice or sent by a restarted client.
  MQTT reject, downtime, shift, and parameter events reuse their supplied
  `event_id` when one is available.
- Exact legacy duplicate rows are ignored when the PDF aggregates hourly,
  rejection, and downtime totals.
- `GENERATE_PDF` reuses the latest existing PDF for the same shift. Send
  `"force_regenerate": true` with a new `request_id` only when an intentional
  replacement report is required after correcting source data.
- The Python sender allows up to 120 seconds for PDF export and retries failures
  with exponential backoff while preserving the original `request_id`.

After changing `appScriptSdy2.js`, create a new Apps Script web-app deployment
version; saving the editor is not enough. Confirm the deployed `/exec` URL still
matches `WEB_APP_URL`, then update and restart the Raspberry Pi service.

## Smart Checksheet tablet connection

The tracker speaks the same protocol as the ABB trackers, so the Smart Checksheet
tablet app can drive it:

- Live snapshots on `smartchecksheet/live` include `line_code: SDY2`, the shift
  crew, `hourly_plan`, `live_plan`, the pending shift and the shared downtime timer.
- Every submitted shift form is answered on `sdy2/shift_form_result` as `active`,
  `pending` (a form for a later start waits and starts by itself) or `rejected`
  with a reason. A running timer is closed and logged when the shift ends.
- The shared downtime timer is controlled on `nodered/downtime_session` and
  answered on `sdy2/downtime_result`; the machine mode cannot be changed while it
  runs. `deploy`-time restarts keep the pending form and the timer (SQLite).
- The tablet's count correction (`{"adjust": 1}` or `-1`) is accepted, and reject
  and parameter events carry the individual readings the tablet displays.

New files: `sdy_downtime_session.py` and `tests/test_tablet_protocol.py` (runs the
tracker with fake GPIO/MQTT: `python -m unittest discover -s tests`). This was
tested with mocked hardware only; verify on the Pi during a planned stop.
