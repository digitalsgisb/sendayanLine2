# SDY2 SQLite Recovery and Shift Lifecycle

The default database is `runtime/sdy2_state.db`. It contains:

- `runtime_state`: the latest SDY2 shift, counter, timer, mode, and entry-window
  snapshot;
- `outbox_events`: durable production events waiting for API delivery.
- `google_queue`: Google Sheets rows and PDF requests not yet accepted by the
  Apps Script. They are sent in order by a background thread and survive restarts.
- `settings`: values pushed from Smart Checksheet, such as the Apps Script URL.

Outbox retention: once an hour, events older than 7 days are deleted only if the
Smart Checksheet relay has delivered them (its `checksheet_deliveries` table)
and, when `SDY2_API_ENABLED=true`, the API sender marked them `SENT` or
`DEAD_LETTER`. Until the relay is installed, nothing is deleted.

Counters are checkpointed after accepted counts and operational changes.
Continuously changing timers are checkpointed every five seconds. On restart,
the saved state is restored before MQTT starts publishing live data. Any event
left in `SENDING` is returned to `PENDING` for retry.

## Reset rules

- The scheduled end from `workingTime` ends the active shift.
- At 08:00, a non-overtime production day is closed and reset if necessary.
- At 08:00 with no active shift, a 45-minute entry window begins. Counts and
  timers continue locally with status `7` (`WAITING_FOR_SHIFT`).
- A valid shift submitted by 08:45 adopts those buffered values without another
  reset.
- If no shift arrives by 08:45, the buffered values are cleared and the line is
  treated as no production.
- Submitting the same active shift ID updates its details without resetting
  counters. A different ID is rejected until the current shift is ended.

At a scheduled or manual shift end, any unfinalized hourly values are queued
first, followed by the shift-end data and PDF request.

Run `python3 sdy2_sqlite_status.py` for a read-only snapshot and outbox count.
