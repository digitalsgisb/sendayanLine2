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

The SDY2 stable-read sensor flow, blocked-transition product count,
90-second loading threshold, and cycle-time formula remain unchanged.

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

## Google Apps Script deployment

`appScriptSdy2.js` is the matching backend for the Line 2 spreadsheet. Copy it
into the spreadsheet's Apps Script project and deploy a new web-app version.
Keep the deployed `/exec` URL equal to `WEB_APP_URL` in `sdy2.py`.

The backend combines repeated records in the same hour, selects the latest
same-shift details, validates allowed data tabs, and deduplicates Python HTTP
retries by `request_id`. Run `node tests/test_sdy2_appscript.js` before deployment.
