#!/usr/bin/python3
import RPi.GPIO as GPIO
import time
from datetime import datetime, timedelta
import json
import os
import re
import threading
import uuid
import paho.mqtt.client as mqtt
import requests

from sdy_downtime_session import empty_session, transition as downtime_transition
import sdy_hourly_plan as hourly_plan
from sdy_google_sender import GoogleSheetsSender, MemoryGoogleQueue
from sdy_persistence import ProductionOutboxSender, ProductionSQLiteStore
from sdy_shift_schedule import (
    calculate_shift_end_datetime,
    calculate_shift_start_datetime,
    coerce_bool,
    hour_slot_for_shift_end,
)

# ==========================================
# PIN CONFIGURATION & SETUP
# ==========================================
PROX_SENSOR_1_PIN = 5   
PROX_SENSOR_2_PIN = 27   
LIMIT_SWITCH_PIN = 17   # SDY2 forming limit switch

GPIO.setmode(GPIO.BCM)
GPIO.setup(PROX_SENSOR_1_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(PROX_SENSOR_2_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(LIMIT_SWITCH_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

SENSOR_DEBOUNCE_TIME = 0.05
LIMIT_SWITCH_COUNT_DEBOUNCE_TIME = 2.0
# SDY2 site rule (Nov 2026): one HIGH -> LOW edge per waterjet = one product.
# Each sensor must return HIGH before another piece can be counted.
WJ_COUNT_DEBOUNCE_TIME = 0.15

# ==========================================
# GLOBAL VARIABLES & COUNTERS
# ==========================================
# Shift Totals
run_time = loading_time = delay_time = downtime = 0.0
total_rest_time = planned_stop_time = model_change_time = total_machine_time = 0.0

# Real Operating Time (resets on shift end and model change)
real_operating_time = 0.0
# Total Real Operating Time (resets on shift end only)
total_real_operating_time = 0.0

# Tracks active time for the current batch (resets on model change & shift end)
batch_run_time = 0.0 

# Output Trackers. The MAIN output is the waterjet count (waterjet A + waterjet B):
# it feeds the hourly Sheet row, the PDF, plan-vs-actual and the cycle time.
total_output = 0            # Waterjet product count (resets on model change)
shift_total_output = 0      # Waterjet product count (resets on shift end only)
hourly_output = 0           # Waterjet product count this hour
total_rejects = 0           # Sum of dashboard rejects received via MQTT
waterjet_a_output = 0       # WJ1 shift total (GPIO5), resets on shift end only
waterjet_b_output = 0       # WJ2 shift total (GPIO27), resets on shift end only
forming_output = 0          # Forming shift total (GPIO17), live panel only
mf_count = 0                # Forming model count (GPIO17), resets on model change too
wj1_count = 0               # Waterjet 1 model count (GPIO5)
wj2_count = 0               # Waterjet 2 model count (GPIO27)

# Hourly Trackers
hourly_rest_time = 0.0
lost_time_this_hour = 0.0 
base_time_this_hour = 0.0   # Accurately tracks base time even if model changes mid-hour
# Fixed hourly plan: planned seconds (rest, planned stop, model change) in the running segment, and when it started.
hourly_plan_state = {"planned_lost": 0.0, "segment_started_at": None}

# Sensor & Cycle Trackers
sensor_blocked = False
blockage_start_time = 0.0
current_cycle_time = 0.0
limit_switch_prev = 1       # Assumes 1 (HIGH) is unpressed
last_limit_switch_count_time = 0.0

wj1_prev = GPIO.input(PROX_SENSOR_1_PIN)
wj2_prev = GPIO.input(PROX_SENSOR_2_PIN)
last_wj1_count_time = 0.0
last_wj2_count_time = 0.0

# ==========================================
# MQTT & GOOGLE SHEETS CONFIGURATION
# ==========================================
LINE_CODE = "SDY2"
PRODUCTION_DAY_BOUNDARY_HOUR = 8
SHIFT_ENTRY_GRACE_MINUTES = 45
MQTT_BROKER = "localhost"
MQTT_PORT = 1883
MQTT_TOPIC_DATA = "sensor2/data"
MQTT_TOPIC_STATUS = "sdy2/status"

# Node-RED Topics
MQTT_TOPIC_SHIFT_FORM = "nodered/newshift"
MQTT_TOPIC_SETUP = "nodered/modeltarget" 
MQTT_TOPIC_NR_REJECT = "nodered/reject"
MQTT_TOPIC_NR_DOWNTIME = "nodered/downtime"
MQTT_TOPIC_DOWNTIME_SESSION = "nodered/downtime_session"
MQTT_TOPIC_NR_ENDSHIFT = "nodered/endshift" 
MQTT_TOPIC_MODE = "nodered/mode"
MQTT_TOPIC_PARAM_CONDITION = "noderedparam/condition"
MQTT_TOPIC_ADJUST_COUNT = "nodered/adjust_count"
# Smart Checksheet only (password checked there): end the active shift now and start a new one.
# Deliberately not nodered/endshift, so the Node-RED OEE flow is not reset a second time.
MQTT_TOPIC_SHIFT_OVERRIDE = "checksheet/shift_override"

MQTT_TOPIC_LIVE_HOURLY = "smartchecksheet/hourly"
MQTT_TOPIC_LIVE_SNAPSHOT = "smartchecksheet/live"

# Settings pushed by Smart Checksheet (Lines & machines page) as a retained message.
MQTT_TOPIC_CONFIG = "sdy2/config"
MQTT_TOPIC_CONFIG_RESULT = "sdy2/config_result"

# Google Sheets Web App URL. This built-in value is only the fallback: a URL saved
# from Smart Checksheet (stored in SQLite) takes priority, with no restart needed.
DEFAULT_WEB_APP_URL = "https://script.google.com/macros/s/AKfycbyVm-Ghh2w3sQfWnh1A7mQTXZb4F3XOdE681GiVIynpvrXPE3dJ_769NxmucZT6O1fxqQ/exec"
WEB_APP_URL_SETTING = "google_web_app_url"
WEB_APP_URL_PREFIX = "https://script.google.com/macros/s/"
# Same shape as Smart Checksheet's shared/sheetsUrl.js (Workspace /a/macros/<domain>/s/ links too).
WEB_APP_URL_PATTERN = re.compile(
    r"^https://script\.google\.com/(?:a/macros/[A-Za-z0-9.-]{1,100}|macros)/s/[A-Za-z0-9_-]{10,200}/exec$"
)
OUTBOX_RETENTION_DAYS = 7
OUTBOX_PRUNE_INTERVAL_SECONDS = 3600

SCRIPT_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
SQLITE_DATABASE_PATH = os.environ.get(
    "SDY2_SQLITE_PATH", os.path.join(SCRIPT_DIRECTORY, "runtime", "sdy2_state.db")
)
TRUSTED_API_ENABLED = os.environ.get("SDY2_API_ENABLED", "false").lower() in {
    "1", "true", "yes", "on"
}
TRUSTED_API_EVENTS_URL = os.environ.get("SDY2_API_EVENTS_URL", "")
TRUSTED_API_KEY = os.environ.get("SDY2_API_KEY", "")
STATE_CHECKPOINT_INTERVAL_SECONDS = 5.0

# ==========================================
# STATE & SHIFT MANAGEMENT
# ==========================================
STATUS_RUN = 0
STATUS_LOADING = 1
STATUS_DELAY = 2
STATUS_REST = 3
STATUS_DOWN = 4
STATUS_PLANNED_STOP = 5
STATUS_MODEL_CHANGE = 6
STATUS_WAITING_FOR_SHIFT = 7

MODE_NORMAL = "NORMAL"
MODE_DOWN = "DOWN"
MODE_MODEL_CHANGE = "MODEL CHANGE"
MODE_REST = "REST"
MODE_PLANNED_STOP = "PLANNED STOP"

current_mode = MODE_NORMAL
current_status = STATUS_RUN
previous_status = STATUS_RUN 
last_sent_hour = -1
last_live_snapshot_publish = 0.0
last_runtime_checkpoint = 0.0
recovered_from_sqlite = False
shift_entry_window_started_at = None
shift_entry_window_ends_at = None
# Smart Checksheet tablet protocol: a form for a later start waits here, and one
# shared downtime timer is owned by this program (never by a browser).
pending_shift_form = None
downtime_session = empty_session()
shift_state_lock = threading.RLock()
last_production_day = (datetime.now() - timedelta(hours=PRODUCTION_DAY_BOUNDARY_HOUR)).date()

current_shift = {
    "shift_id": "NO PROD", "line": "NO PROD", "date": "NO PROD", "shift": "NO PROD", "group": "-",
    "model": "NO PROD", "lot_number": "NO PROD", "target": 0, "standard_cycle": 1.0,
    "supervisor": "-", "leader": "-", "workingTime": "-", "overtime": False,
    "scheduled_end_at": None,
    "forming": "-", "waterjet": "-", "assembly": "-", "quality": "-"
}

persistence_store = None
outbox_sender = None
try:
    persistence_store = ProductionSQLiteStore(SQLITE_DATABASE_PATH)
    outbox_sender = ProductionOutboxSender(
        persistence_store,
        TRUSTED_API_EVENTS_URL,
        TRUSTED_API_KEY,
        TRUSTED_API_ENABLED,
        "sdy2-api-outbox",
    )
except Exception as exc:
    print(f"[SQLITE CRITICAL] Persistence disabled; production continues: {exc}")

# Queued Sheets rows live in SQLite so they survive a restart; memory only if SQLite failed.
google_queue = persistence_store if persistence_store is not None else MemoryGoogleQueue()
google_sender = None
last_outbox_prune = 0.0


def is_valid_web_app_url(url):
    return bool(WEB_APP_URL_PATTERN.match(str(url or "").strip()))


def load_web_app_url():
    if persistence_store is not None:
        try:
            saved = persistence_store.get_setting(WEB_APP_URL_SETTING)
            if is_valid_web_app_url(saved):
                return saved, "smartchecksheet"
        except Exception as exc:
            print(f"[CONFIG ERROR] Could not read the saved Google Sheets URL: {exc}")
    return DEFAULT_WEB_APP_URL, "default"


google_web_app_url, google_web_app_url_source = load_web_app_url()


def current_web_app_url():
    return google_web_app_url


def safe_int(val):
    try: return int(val)
    except (ValueError, TypeError): return 0

def build_google_request_id(scope, identity=None):
    if identity is None:
        return str(uuid.uuid4())
    canonical_identity = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{LINE_CODE}:{scope}:{canonical_identity}",
        )
    )

def enqueue_google_item(item):
    try:
        google_queue.enqueue_google(item)
    except Exception as exc:
        print(f"[GOOGLE QUEUE ERROR] Could not queue {item.get('tab') or item.get('action')}: {exc}")
        return
    if google_sender is not None:
        google_sender.wake()


def queue_google_sheet_row(tab_name, row_data, event_id=None):
    request_identity = event_id
    if request_identity is None and tab_name == "Hourly_Data":
        request_identity = row_data
    enqueue_google_item({
        "tab": tab_name,
        "row": row_data,
        "request_id": build_google_request_id(tab_name, request_identity),
    })

def get_hour_slot(dt=None):
    if dt is None:
        dt = datetime.now()
    return f"{dt.hour}.00-{(dt.hour + 1) % 24}.00"


def get_current_shift_end_datetime():
    if current_shift.get("shift_id") == "NO PROD":
        return None
    stored_end = current_shift.get("scheduled_end_at")
    if stored_end:
        try:
            return datetime.fromisoformat(stored_end)
        except (TypeError, ValueError):
            print(f"[SCHEDULE WARNING] Invalid saved shift end: {stored_end!r}")
    try:
        shift_end = calculate_shift_end_datetime(
            current_shift.get("date"),
            current_shift.get("shift"),
            current_shift.get("overtime", False),
            current_shift.get("workingTime"),
        )
        current_shift["scheduled_end_at"] = shift_end.isoformat()
        return shift_end
    except ValueError as exc:
        print(f"[SCHEDULE ERROR] Cannot calculate shift end: {exc}")
        return None


def build_runtime_snapshot():
    return {
        "schema_version": 1,
        "current_shift": dict(current_shift),
        "pending_shift_form": pending_shift_form,
        "downtime_session": downtime_session,
        "current_mode": current_mode,
        "current_status": current_status,
        "last_sent_hour": last_sent_hour,
        "sensor_blocked": sensor_blocked,
        "shift_entry_window": {
            "started_at": shift_entry_window_started_at,
            "ends_at": shift_entry_window_ends_at,
        },
        "counters": {
            "total_output": total_output,
            "shift_total_output": shift_total_output,
            "hourly_output": hourly_output,
            "total_rejects": total_rejects,
            "waterjet_a_output": waterjet_a_output,
            "waterjet_b_output": waterjet_b_output,
            "forming_output": forming_output,
            "mf_count": mf_count,
            "wj1_count": wj1_count,
            "wj2_count": wj2_count,
        },
        "cycle": {"current_cycle_time": current_cycle_time},
        "timers": {
            "run_time": run_time,
            "loading_time": loading_time,
            "delay_time": delay_time,
            "downtime": downtime,
            "total_rest_time": total_rest_time,
            "planned_stop_time": planned_stop_time,
            "model_change_time": model_change_time,
            "total_machine_time": total_machine_time,
            "real_operating_time": real_operating_time,
            "total_real_operating_time": total_real_operating_time,
            "batch_run_time": batch_run_time,
            "hourly_rest_time": hourly_rest_time,
            "lost_time_this_hour": lost_time_this_hour,
            "base_time_this_hour": base_time_this_hour,
            "planned_lost_this_hour": hourly_plan_state["planned_lost"],
            "segment_started_at": hourly_plan_state["segment_started_at"],
        },
        "saved_at": datetime.now().astimezone().isoformat(),
    }


def runtime_state_status():
    if shift_entry_window_is_active():
        return "WAITING_FOR_SHIFT"
    return "ACTIVE" if current_shift.get("shift_id") != "NO PROD" else "NO_PROD"


def checkpoint_runtime_state(force=False, reason="periodic"):
    global last_runtime_checkpoint
    if persistence_store is None:
        return False
    now = time.time()
    if not force and now - last_runtime_checkpoint < STATE_CHECKPOINT_INTERVAL_SECONDS:
        return True
    try:
        persistence_store.save_runtime_state(
            LINE_CODE, build_runtime_snapshot(), runtime_state_status()
        )
        last_runtime_checkpoint = now
        return True
    except Exception as exc:
        print(f"[SQLITE ERROR] Runtime checkpoint failed ({reason}): {exc}")
        return False


def checkpoint_and_queue_event(event_type, payload, shift_id=None, event_id=None):
    global last_runtime_checkpoint
    if persistence_store is None:
        print(f"[SQLITE ERROR] Cannot queue {event_type}; persistence is unavailable.")
        return None
    try:
        stable_id = persistence_store.save_state_and_enqueue(
            line_code=LINE_CODE,
            state=build_runtime_snapshot(),
            state_status=runtime_state_status(),
            event_type=event_type,
            shift_id=shift_id or current_shift.get("shift_id", "NO PROD"),
            payload=payload,
            event_id=event_id,
            occurred_at=datetime.now().astimezone().isoformat(),
        )
        last_runtime_checkpoint = time.time()
        print(f"[SQLITE] Queued {event_type} ({stable_id}).")
        return stable_id
    except Exception as exc:
        print(f"[SQLITE ERROR] Failed to queue {event_type}: {exc}")
        return None


def restore_runtime_state():
    global run_time, loading_time, delay_time, downtime
    global total_rest_time, planned_stop_time, model_change_time, total_machine_time
    global real_operating_time, total_real_operating_time, batch_run_time
    global total_output, shift_total_output, hourly_output, total_rejects
    global waterjet_a_output, waterjet_b_output, forming_output, mf_count, wj1_count, wj2_count
    global hourly_rest_time, lost_time_this_hour, base_time_this_hour
    global current_cycle_time, current_mode, current_status, last_sent_hour
    global current_shift, sensor_blocked, recovered_from_sqlite
    global shift_entry_window_started_at, shift_entry_window_ends_at
    global pending_shift_form, downtime_session

    if persistence_store is None:
        return False
    try:
        snapshot = persistence_store.load_runtime_state(LINE_CODE)
    except Exception as exc:
        print(f"[SQLITE ERROR] Could not restore state: {exc}")
        return False
    if snapshot is None:
        checkpoint_runtime_state(force=True, reason="first startup")
        return False

    restored_shift = snapshot.get("current_shift", {})
    if isinstance(restored_shift, dict):
        current_shift.update(restored_shift)
    counters = snapshot.get("counters", {})
    timers = snapshot.get("timers", {})
    entry_window = snapshot.get("shift_entry_window", {})
    total_output = int(counters.get("total_output", 0))
    shift_total_output = int(counters.get("shift_total_output", 0))
    hourly_output = int(counters.get("hourly_output", 0))
    total_rejects = int(counters.get("total_rejects", 0))
    waterjet_a_output = int(counters.get("waterjet_a_output", 0))
    waterjet_b_output = int(counters.get("waterjet_b_output", 0))
    forming_output = int(counters.get("forming_output", 0))
    mf_count = int(counters.get("mf_count", total_output))
    wj1_count = int(counters.get("wj1_count", 0))
    wj2_count = int(counters.get("wj2_count", 0))
    current_cycle_time = float(snapshot.get("cycle", {}).get("current_cycle_time", 0.0))
    run_time = float(timers.get("run_time", 0.0))
    loading_time = float(timers.get("loading_time", 0.0))
    delay_time = float(timers.get("delay_time", 0.0))
    downtime = float(timers.get("downtime", 0.0))
    total_rest_time = float(timers.get("total_rest_time", 0.0))
    planned_stop_time = float(timers.get("planned_stop_time", 0.0))
    model_change_time = float(timers.get("model_change_time", 0.0))
    total_machine_time = float(timers.get("total_machine_time", 0.0))
    real_operating_time = float(timers.get("real_operating_time", 0.0))
    total_real_operating_time = float(timers.get("total_real_operating_time", 0.0))
    batch_run_time = float(timers.get("batch_run_time", 0.0))
    hourly_rest_time = float(timers.get("hourly_rest_time", 0.0))
    lost_time_this_hour = float(timers.get("lost_time_this_hour", 0.0))
    base_time_this_hour = float(timers.get("base_time_this_hour", 0.0))
    hourly_plan_state.update(planned_lost=float(timers.get("planned_lost_this_hour", 0.0)), segment_started_at=timers.get("segment_started_at"))
    downtime_session = snapshot.get("downtime_session") or empty_session()
    pending_shift_form = snapshot.get("pending_shift_form")
    if not isinstance(pending_shift_form, dict):
        pending_shift_form = None
    current_mode = str(snapshot.get("current_mode", MODE_NORMAL))
    current_status = int(snapshot.get("current_status", STATUS_RUN))
    last_sent_hour = int(snapshot.get("last_sent_hour", -1))
    shift_entry_window_started_at = entry_window.get("started_at")
    shift_entry_window_ends_at = entry_window.get("ends_at")
    # Read the physical inputs afresh after startup; restored edge state is unsafe.
    sensor_blocked = False
    recovered_from_sqlite = True
    print(
        f"[SQLITE] Restored {LINE_CODE}: Shift={current_shift.get('shift_id')} | "
        f"Hourly={hourly_output} | Model={total_output} | Shift Total={shift_total_output}"
    )
    return True

def record_waterjet_product(name, sample_time):
    """One finished waterjet cycle: the main output (hourly, model, shift) plus per-waterjet totals."""
    global wj1_count, wj2_count, waterjet_a_output, waterjet_b_output
    global hourly_output, total_output, shift_total_output, current_cycle_time
    global last_wj1_count_time, last_wj2_count_time
    if name == "WJ1":
        wj1_count += 1
        waterjet_a_output += 1
        last_wj1_count_time = sample_time
    else:
        wj2_count += 1
        waterjet_b_output += 1
        last_wj2_count_time = sample_time
    hourly_output += 1
    total_output += 1
    shift_total_output += 1
    current_cycle_time = (batch_run_time / total_output) / 60.0
    checkpoint_runtime_state(force=True, reason=f"{name} confirmed cycle")
    print(f"[{name}] Product +1 | WJ1: {wj1_count} | WJ2: {wj2_count} | "
          f"Model: {total_output} | Shift: {shift_total_output}")


def apply_waterjet_sample(p1, p2, sample_time, accept_counts=False):
    """SDY2: each waterjet counts one product on a HIGH -> LOW edge (0.15 s debounce).

    The previous levels are always tracked, also outside NORMAL production, so a
    sensor that is already LOW when production resumes never counts falsely.
    """
    global wj1_prev, wj2_prev
    if accept_counts:
        if (wj1_prev == 1 and p1 == 0
                and sample_time - last_wj1_count_time >= WJ_COUNT_DEBOUNCE_TIME):
            record_waterjet_product("WJ1", sample_time)
        if (wj2_prev == 1 and p2 == 0
                and sample_time - last_wj2_count_time >= WJ_COUNT_DEBOUNCE_TIME):
            record_waterjet_product("WJ2", sample_time)
    wj1_prev, wj2_prev = p1, p2
    return p1, p2


def sample_waterjet_inputs(accept_counts=False):
    p1 = GPIO.input(PROX_SENSOR_1_PIN)
    p2 = GPIO.input(PROX_SENSOR_2_PIN)
    return apply_waterjet_sample(p1, p2, time.monotonic(), accept_counts)


def get_stable_machine_state(accept_counts=False):
    """Keep original machine-state rule: BOTH proximity sensors LOW in 3/5 samples.

    Every sample also feeds the waterjet counters (about every 50 ms).
    """
    blocked_count = 0
    for _ in range(5):
        p1, p2 = sample_waterjet_inputs(accept_counts=accept_counts)
        if p1 == 0 and p2 == 0:
            blocked_count += 1
        time.sleep(SENSOR_DEBOUNCE_TIME)
    return 1 if blocked_count > 2 else 0


def reset_production_runtime_counters():
    global run_time, loading_time, delay_time, downtime, total_rest_time, planned_stop_time, model_change_time
    global total_machine_time, total_output, hourly_output, hourly_rest_time, lost_time_this_hour, base_time_this_hour, sensor_blocked
    global shift_total_output, total_rejects, current_cycle_time, real_operating_time, limit_switch_prev
    global total_real_operating_time, batch_run_time, last_sent_hour
    global waterjet_a_output, waterjet_b_output, forming_output, mf_count, wj1_count, wj2_count
    global wj1_prev, wj2_prev

    run_time = loading_time = delay_time = downtime = 0.0
    total_rest_time = planned_stop_time = model_change_time = total_machine_time = 0.0
    total_output = hourly_output = shift_total_output = total_rejects = 0
    waterjet_a_output = waterjet_b_output = forming_output = 0
    mf_count = wj1_count = wj2_count = 0
    wj1_prev = GPIO.input(PROX_SENSOR_1_PIN)
    wj2_prev = GPIO.input(PROX_SENSOR_2_PIN)
    hourly_rest_time = lost_time_this_hour = base_time_this_hour = 0.0
    hourly_plan_state.update(planned_lost=0.0, segment_started_at=None)
    real_operating_time = total_real_operating_time = current_cycle_time = batch_run_time = 0.0
    sensor_blocked = False
    limit_switch_prev = 1
    last_sent_hour = -1


def parse_shift_entry_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def shift_entry_window_is_active(moment=None):
    moment = moment or datetime.now()
    window_end = parse_shift_entry_datetime(shift_entry_window_ends_at)
    return window_end is not None and moment < window_end


def clear_shift_entry_window():
    global shift_entry_window_started_at, shift_entry_window_ends_at
    shift_entry_window_started_at = None
    shift_entry_window_ends_at = None


def start_shift_entry_window(boundary_time):
    global shift_entry_window_started_at, shift_entry_window_ends_at
    window_end = boundary_time + timedelta(minutes=SHIFT_ENTRY_GRACE_MINUTES)
    if datetime.now() >= window_end:
        clear_shift_entry_window()
        return False
    shift_entry_window_started_at = boundary_time.isoformat()
    shift_entry_window_ends_at = window_end.isoformat()
    checkpoint_runtime_state(force=True, reason="shift entry window started")
    print(
        "[SHIFT BUFFER] Waiting for the next shift form until "
        f"{window_end.strftime('%Y-%m-%d %I:%M %p')}."
    )
    return True


def expire_shift_entry_window(moment=None):
    moment = moment or datetime.now()
    window_end = parse_shift_entry_datetime(shift_entry_window_ends_at)
    if window_end is None or moment < window_end:
        return False
    print(
        f"[SHIFT BUFFER] No form received in {SHIFT_ENTRY_GRACE_MINUTES} minutes; "
        f"discarding {shift_total_output} unassigned product(s)."
    )
    reset_production_runtime_counters()
    clear_shift_entry_window()
    checkpoint_runtime_state(force=True, reason="shift entry window expired")
    return True


def reset_shift_data(save_checkpoint=True):
    global current_shift

    print("\n[EVENT] Executing Shift Data Reset...")
    if globals().get("downtime_session", {}).get("status") in {"running", "stopped"}:
        process_downtime_command({"action": "shift_end"}, automatic=True)
    reset_production_runtime_counters()
    clear_shift_entry_window()

    current_shift = {
        "shift_id": "NO PROD", "line": "NO PROD", "date": "NO PROD", "shift": "NO PROD", "group": "-",
        "model": "NO PROD", "lot_number": "NO PROD", "target": 0, "standard_cycle": 1.0,
        "supervisor": "-", "leader": "-", "workingTime": "-", "overtime": False,
        "scheduled_end_at": None,
        "forming": "-", "waterjet": "-", "assembly": "-", "quality": "-"
    }

    if save_checkpoint:
        checkpoint_runtime_state(force=True, reason="shift reset")


def production_day_key(moment):
    return (moment - timedelta(hours=PRODUCTION_DAY_BOUNDARY_HOUR)).date()


def current_shift_matches_production_day(moment):
    if current_shift.get("shift_id") == "NO PROD":
        return False
    shift_date = None
    for date_format in ("%Y-%m-%d", "%Y%m%d"):
        try:
            shift_date = datetime.strptime(
                str(current_shift.get("date") or "").strip(), date_format
            ).date()
            break
        except ValueError:
            continue
    return shift_date == production_day_key(moment)


def force_production_day_reset(moment=None):
    global last_sent_hour
    moment = moment or datetime.now()
    boundary_time = moment.replace(
        hour=PRODUCTION_DAY_BOUNDARY_HOUR, minute=0, second=0, microsecond=0
    )
    buffered_from = parse_shift_entry_datetime(shift_entry_window_started_at)
    if (
        current_shift.get("shift_id") == "NO PROD"
        and shift_entry_window_is_active(moment)
        and buffered_from == boundary_time
    ):
        return
    preserve_day_shift = (
        str(current_shift.get("shift", "")).strip().upper() == "DAY"
        and current_shift_matches_production_day(moment)
    )
    print("\n[SYSTEM] 08:00 production-day boundary reached. Resetting counters.")
    if preserve_day_shift:
        reset_production_runtime_counters()
        clear_shift_entry_window()
        last_sent_hour = moment.hour
        checkpoint_runtime_state(force=True, reason="08:00 production-day reset")
        print("[SYSTEM] Today's Day shift form was preserved.")
    else:
        reset_shift_data()
        start_shift_entry_window(boundary_time)
        print("[SYSTEM] Waiting up to 45 minutes for the Day shift form.")


def reset_stale_shift_after_startup(moment=None):
    moment = moment or datetime.now()
    if (
        current_shift.get("shift_id") != "NO PROD"
        and not current_shift_matches_production_day(moment)
    ):
        scheduled_end = get_current_shift_end_datetime()
        if scheduled_end is not None and moment >= scheduled_end:
            execute_end_shift(
                hour_slot_override=hour_slot_for_shift_end(scheduled_end),
                end_reason="MISSED SCHEDULED SHIFT END RECOVERED AT STARTUP",
            )
            start_shift_entry_window(scheduled_end)
        else:
            reset_shift_data()
        return True
    return False


def execute_end_shift(hour_slot_override=None, end_reason="MANUAL SHIFT END"):
    ending_shift_id = current_shift.get("shift_id", "NO PROD")
    if ending_shift_id == "NO PROD":
        print("\n[EVENT] No active shift. Resetting background state only.")
        reset_shift_data()
        return

    print("\n[EVENT] End Shift Triggered!")
    # Close an open shared downtime session first so its time is logged to this shift.
    if downtime_session.get("status") in {"running", "stopped"}:
        process_downtime_command({"action": "shift_end"}, automatic=True)
    if (
        hourly_output != 0
        or base_time_this_hour >= 0.5
        or lost_time_this_hour >= 0.5
        or hourly_rest_time >= 0.5
    ):
        push_hourly_to_sheets(
            is_model_change=False,
            hour_slot_override=hour_slot_override,
            reason_override=f"{end_reason} FINALIZATION",
        )

    ending_event = {
        "shift_id": ending_shift_id,
        "line_code": LINE_CODE,
        "model": current_shift.get("model"),
        "lot_number": current_shift.get("lot_number"),
        "scheduled_end_at": current_shift.get("scheduled_end_at"),
        "shift_total_output": shift_total_output,
        "model_total_output": total_output,
        "waterjet_output": shift_total_output,
        "waterjet_a_output": waterjet_a_output,
        "waterjet_b_output": waterjet_b_output,
        "forming_output": forming_output,
        "wj1_count": wj1_count,
        "wj2_count": wj2_count,
        "mf_count": mf_count,
        "total_rejects": total_rejects,
        "end_reason": end_reason,
        "ended_at": datetime.now().astimezone().isoformat(),
    }
    reset_shift_data(save_checkpoint=False)
    checkpoint_and_queue_event(
        "shift.ended", ending_event, shift_id=ending_shift_id
    )
    enqueue_google_item({
        "action": "GENERATE_PDF",
        "shift_id": ending_shift_id,
        "request_id": build_google_request_id("GENERATE_PDF", ending_shift_id),
    })
    print(f"[GOOGLE QUEUE] PDF generation queued for Shift: {ending_shift_id}.")

def current_segment_plan(now=None, closing=False, standard=False):
    """(plan, segment end) under the fixed-plan rule; the shift's own start and end bound the hour."""
    return hourly_plan.segment_plan(
        now or datetime.now(), hourly_plan_state["segment_started_at"],
        current_shift.get("scheduled_start_at"), get_current_shift_end_datetime(),
        hourly_plan_state["planned_lost"], current_shift.get("standard_cycle", 1.0),
        closing=closing, standard=standard)


def close_segment_for_setup(data):
    """A new model or lot gets its own hourly row: close the running one under the old model/lot first."""
    if current_shift.get("shift_id") == "NO PROD":
        return
    old_model, old_lot = current_shift.get("model"), current_shift.get("lot_number")
    new_model = data.get("model", old_model) if "model" in data else old_model
    new_lot = data.get("lot_number", old_lot) if "lot_number" in data else old_lot
    if (new_model, new_lot) == (old_model, old_lot) or str(old_model or "-").strip().upper() in ("-", "", "NO PROD", "PENDING", "UNKNOWN"):
        return
    # Nothing made yet under the old model/lot: no row. The time (changeover included) belongs to the new one.
    if hourly_output <= 0:
        if new_model != old_model:
            reset_model_counters()
        return
    if new_model != old_model:
        push_hourly_to_sheets(is_model_change=True, reason_override="MODEL CHANGE")
    else:
        push_hourly_to_sheets(hour_slot_override=get_hour_slot(), reason_override="LOT CHANGE")


def reset_model_counters():
    """The per-model totals start again for a new model (what push_hourly_to_sheets does on a model change)."""
    global batch_run_time, mf_count, real_operating_time, total_output, wj1_count, wj2_count
    print("[EVENT] New model -> Resetting Total Output and Real Operating Time to 0.")
    total_output = 0
    mf_count = 0
    wj1_count = wj2_count = 0
    batch_run_time = 0.0
    real_operating_time = 0.0


def push_hourly_to_sheets(
    is_model_change=False,
    hour_slot_override=None,
    reason_override=None,
):
    global hourly_output, lost_time_this_hour, base_time_this_hour, total_output, hourly_rest_time, batch_run_time, real_operating_time, mf_count, wj1_count, wj2_count
    
    std_cycle = current_shift.get("standard_cycle", 1.0)
    base_minutes = base_time_this_hour / 60.0
    lost_minutes = lost_time_this_hour / 60.0
    available_minutes = max(0.0, base_minutes - lost_minutes)
    
    # Fixed plan: minutes of this hour/segment inside the shift, less planned time only (see the hourly_plan module).
    plan_output, plan_segment_end = current_segment_plan(closing=bool(hour_slot_override or is_model_change), standard=not (hour_slot_override or is_model_change))
    now = datetime.now()
    
    if hour_slot_override:
        h_slot = hour_slot_override
        reason = reason_override or "SHIFT END FINALIZATION"
    elif is_model_change:
        h_slot = get_hour_slot(now)
        reason = reason_override or "MODEL CHANGE OVERRIDE"
    else:
        h_slot = f"{(now.hour - 1) % 24}.00-{now.hour}.00"
        reason = reason_override or "STANDARD HOURLY PUSH"

    rest_mins = round(hourly_rest_time / 60.0, 2)
    
    row_data = [
        current_shift["shift_id"], current_shift["date"], current_shift["model"], 
        h_slot, plan_output, hourly_output, current_shift["lot_number"], rest_mins
    ]

    print(f"\n[EVENT - HOURLY CALCULATION] Reason: {reason}")
    print(f" â””â”€â”€> QUEUING DATA: {row_data}")
    
    queue_google_sheet_row("Hourly_Data", row_data)

    hourly_event = {
        "shift_id": current_shift["shift_id"],
        "date": current_shift["date"],
        "line": current_shift["line"],
        "shift": current_shift["shift"],
        "model": current_shift["model"],
        "hour_slot": h_slot,
        "plan": plan_output,
        "actual": hourly_output,
        "lot_number": current_shift["lot_number"],
        "rest_time": rest_mins,
        "rest_time_unit": "minutes",
        "reason": reason,
        "finalized_at": datetime.now().astimezone().isoformat(),
    }
    
    hourly_output = 0
    lost_time_this_hour = 0.0
    base_time_this_hour = 0.0
    hourly_rest_time = 0.0
    hourly_plan_state.update(planned_lost=0.0, segment_started_at=plan_segment_end.isoformat())
    
    if is_model_change:
        print("[EVENT] Model Change Flag triggered -> Resetting Total Output and Real Operating Time to 0.")
        total_output = 0 
        mf_count = 0
        wj1_count = wj2_count = 0
        batch_run_time = 0.0      
        real_operating_time = 0.0 

    checkpoint_and_queue_event("hourly.finalized", hourly_event)
    try:
        mqtt_client.publish(MQTT_TOPIC_LIVE_HOURLY, json.dumps(hourly_event), qos=1)
    except Exception as exc:
        print(f"[MQTT ERROR] Failed to publish finalized hourly row: {exc}")

# ==========================================
# SMART CHECKSHEET SHIFT FORMS AND SHARED DOWNTIME TIMER
# ==========================================
def shift_id_for_form(data):
    date_clean = str(data.get("prodDate", "")).replace("-", "")
    return f"{date_clean}-{data.get('shift', '')}-{str(data.get('productionLine', '')).replace(' ', '')}"


def parse_operator_names(op_data):
    if isinstance(op_data, list):
        return ", ".join(op_data)
    return str(op_data) if op_data else "-"


def publish_shift_form_result(status, data, message="", navigate_to_hourly=True):
    """Acknowledge every form so the tablet knows it was accepted, queued or refused."""
    result = {"status": status, "source_payload": data, "message": message,
              "active_shift": dict(current_shift), "pending_shift": pending_shift_form,
              "navigate_to_hourly": navigate_to_hourly}
    try:
        mqtt_client.publish(f"{LINE_CODE.lower()}/shift_form_result", json.dumps(result), qos=1)
    except Exception as exc:
        print(f"[SHIFT ACK ERROR] {exc}")


def apply_shift_form(data, navigate_to_hourly=True, shift_id_override=None, started_at=None):
    """Activate a validated form, preserving a matching late-entry buffer."""
    global current_shift
    expire_shift_entry_window()
    previous_shift_id = current_shift.get("shift_id", "NO PROD")
    new_shift_id = shift_id_override or shift_id_for_form(data)
    if previous_shift_id != "NO PROD" and previous_shift_id != new_shift_id:
        print("[SHIFT WARNING] A different shift is already active. The new form was rejected to preserve its counters.")
        publish_shift_form_result("rejected", data, "Another shift is still active.")
        return False
    adopting_shift_buffer = previous_shift_id == "NO PROD" and shift_entry_window_is_active()
    current_shift["shift_id"] = new_shift_id
    current_shift["source_form"] = dict(data)
    current_shift["scheduled_start_at"] = (started_at or calculate_shift_start_datetime(
        data.get("prodDate"), data.get("shift"), data.get("overtime", False), data.get("workingTime")
    )).isoformat()
    current_shift.update({
        "date": data.get("prodDate", "-"),
        "line": data.get("productionLine", "-"),
        "shift": data.get("shift", "-"),
        "group": data.get("group", "-"),
        "workingTime": data.get("workingTime", "-"),
        "overtime": coerce_bool(data.get("overtime", False)),
        "supervisor": data.get("supervisor", "-"),
        "leader": data.get("lineLeader", "-"),
        "forming": parse_operator_names(data.get("formingOperator")),
        "waterjet": parse_operator_names(data.get("waterjetOperator")),
        "assembly": parse_operator_names(data.get("assemblyOperator")),
        "quality": data.get("qualityOperator", "-"),
    })
    try:
        scheduled_end = calculate_shift_end_datetime(
            current_shift["date"], current_shift["shift"], current_shift["overtime"], current_shift["workingTime"],
        )
        current_shift["scheduled_end_at"] = scheduled_end.isoformat()
        print(f"[SCHEDULE] Shift will automatically end at {scheduled_end.strftime('%Y-%m-%d %I:%M %p')}.")
    except ValueError as exc:
        current_shift["scheduled_end_at"] = None
        print(f"[SCHEDULE ERROR] Automatic shift end disabled: {exc}")
    if adopting_shift_buffer:
        clear_shift_entry_window()
        print(f"[SHIFT BUFFER] Buffered production assigned to {current_shift['shift_id']} without reset.")
    print(f"\n[MQTT EVENT] New Shift Started: {current_shift['shift_id']}")
    queue_google_sheet_row("Shift_Data", [
        current_shift["shift_id"], current_shift["date"], current_shift["line"],
        current_shift["shift"], current_shift["group"], current_shift["workingTime"],
        current_shift["supervisor"], current_shift["leader"],
        current_shift["forming"], current_shift["waterjet"],
        current_shift["assembly"], current_shift["quality"],
    ], data.get("event_id"))
    checkpoint_and_queue_event(
        "shift.updated" if previous_shift_id == current_shift["shift_id"] else "shift.started",
        {
            "line_code": LINE_CODE,
            "source_shift_id": current_shift["shift_id"],
            "shift": dict(current_shift),
            "source_payload": data,
        },
        event_id=data.get("event_id"),
    )
    publish_shift_form_result("active", data, navigate_to_hourly=navigate_to_hourly)
    return True


def receive_shift_form(data, moment=None, navigate_to_hourly=True):
    """Decide what a submitted form means: start now, wait for its start time, or refuse."""
    global pending_shift_form, last_production_day
    moment = moment or datetime.now()
    with shift_state_lock:
        if data.get("action") == "resend_active":
            if current_shift.get("shift_id") == "NO PROD":
                publish_shift_form_result("rejected", data, "There is no active shift to resend.")
                return False
            source = current_shift.get("source_form")
            if not isinstance(source, dict):
                publish_shift_form_result("rejected", data, "Re-enter the active shift details once before using resend.")
                return False
            data = dict(source)
        try:
            start = calculate_shift_start_datetime(data.get("prodDate"), data.get("shift"),
                                                   data.get("overtime", False), data.get("workingTime"))
            end = calculate_shift_end_datetime(data.get("prodDate"), data.get("shift"),
                                               data.get("overtime", False), data.get("workingTime"))
        except (ValueError, TypeError) as exc:
            publish_shift_form_result("rejected", data, str(exc))
            return False
        if moment >= end:
            publish_shift_form_result("rejected", data, "This shift has already ended. Check the production date.")
            return False
        active_end = get_current_shift_end_datetime()
        new_id = shift_id_for_form(data)
        if moment < start:
            if active_end is not None and start < active_end:
                publish_shift_form_result("rejected", data, "Next shift overlaps the active shift. Check OT and working hours.")
                return False
            pending_shift_form = dict(data)
            checkpoint_runtime_state(force=True, reason="pending shift saved")
            publish_shift_form_result("pending", data, f"Waiting until {start.isoformat()}")
            return True
        if active_end is not None and moment >= active_end:
            execute_end_shift(hour_slot_override=hour_slot_for_shift_end(active_end),
                              end_reason="SCHEDULED SHIFT END")
            start_shift_entry_window(active_end)
        elif current_shift.get("shift_id") != "NO PROD" and current_shift.get("shift_id") != new_id:
            publish_shift_form_result("rejected", data, "Another shift is still active.")
            return False
        if production_day_key(moment) != last_production_day:
            force_production_day_reset(moment)
            last_production_day = production_day_key(moment)
        if current_shift.get("shift_id") == "NO PROD":
            buffered_start = parse_shift_entry_datetime(shift_entry_window_started_at)
            if buffered_start != start:
                reset_production_runtime_counters()
                clear_shift_entry_window()
                start_shift_entry_window(start)
        # Clear the pending copy before the activation checkpoint, for restart safety.
        if pending_shift_form is not None and shift_id_for_form(pending_shift_form) == new_id:
            pending_shift_form = None
        return apply_shift_form(data, navigate_to_hourly=navigate_to_hourly)


def next_override_shift_id(base_id, ending_id):
    """Give a restarted shift its own ID so Sheets rows, PDFs and checksheet records stay separate."""
    if ending_id != base_id and not ending_id.startswith(base_id + "-R"):
        return base_id
    suffix = ending_id[len(base_id) + 2:]
    return f"{base_id}-R{(int(suffix) if suffix.isdigit() else 1) + 1}"


def override_shift(data, moment=None):
    """Smart Checksheet override: end the active shift now and start this shift at once, from zero."""
    global pending_shift_form, last_production_day
    moment = moment or datetime.now()
    with shift_state_lock:
        reason = str(data.get("overrideReason") or "").strip()[:200]
        if not reason:
            publish_shift_form_result("rejected", data, "A reason is required to end and start a new shift.")
            return False
        try:
            start = calculate_shift_start_datetime(data.get("prodDate"), data.get("shift"),
                                                   data.get("overtime", False), data.get("workingTime"))
            end = calculate_shift_end_datetime(data.get("prodDate"), data.get("shift"),
                                               data.get("overtime", False), data.get("workingTime"))
        except (ValueError, TypeError) as exc:
            publish_shift_form_result("rejected", data, str(exc))
            return False
        if moment >= end:
            publish_shift_form_result("rejected", data, "This shift has already ended. Check the production date.")
            return False
        ending_id = current_shift.get("shift_id", "NO PROD")
        print(f"\n[MQTT EVENT] Shift override from Smart Checksheet by {data.get('overrideBy', '-')}: {reason}")
        if ending_id != "NO PROD":
            execute_end_shift(end_reason="OVERRIDE SHIFT END")
        # Start from zero: never adopt production buffered before the override.
        reset_production_runtime_counters()
        clear_shift_entry_window()
        if production_day_key(moment) != last_production_day:
            force_production_day_reset(moment)
            last_production_day = production_day_key(moment)
        pending_shift_form = None
        return apply_shift_form(data, shift_id_override=next_override_shift_id(shift_id_for_form(data), ending_id),
                                started_at=moment if moment < start else None)


def advance_shift_schedule(moment=None):
    """End finished shifts, apply the production-day reset, then start any pending form that is due."""
    global last_production_day, pending_shift_form
    moment = moment or datetime.now()
    changed = False
    with shift_state_lock:
        scheduled_end = get_current_shift_end_datetime()
        if scheduled_end is not None and moment >= scheduled_end:
            print(f"[SYSTEM] Scheduled shift end reached: {scheduled_end.isoformat()}")
            execute_end_shift(hour_slot_override=hour_slot_for_shift_end(scheduled_end),
                              end_reason="SCHEDULED SHIFT END")
            start_shift_entry_window(scheduled_end)
            changed = True
        production_day = production_day_key(moment)
        if production_day != last_production_day:
            force_production_day_reset(moment)
            last_production_day = production_day
            changed = True
        if pending_shift_form is not None:
            form = dict(pending_shift_form)
            try:
                start = calculate_shift_start_datetime(form.get("prodDate"), form.get("shift"),
                                                       form.get("overtime", False), form.get("workingTime"))
                end = calculate_shift_end_datetime(form.get("prodDate"), form.get("shift"),
                                                   form.get("overtime", False), form.get("workingTime"))
                if moment >= end:
                    pending_shift_form = None
                    checkpoint_runtime_state(force=True, reason="expired pending shift discarded")
                    publish_shift_form_result("rejected", form, "Pending shift expired while offline.")
                elif moment >= start:
                    if not receive_shift_form(form, moment, navigate_to_hourly=False):
                        pending_shift_form = None
                        checkpoint_runtime_state(force=True, reason="conflicting pending shift discarded")
                    changed = True
            except (ValueError, TypeError) as exc:
                pending_shift_form = None
                checkpoint_runtime_state(force=True, reason="invalid pending shift discarded")
                publish_shift_form_result("rejected", form, str(exc))
    return changed


def record_downtime(data):
    global current_mode
    if current_mode != MODE_NORMAL:
        print(f"\n[STATE CHANGE] Downtime logged. Auto-reverting mode: {current_mode} -> {MODE_NORMAL}")
        current_mode = MODE_NORMAL
    queue_google_sheet_row("Downtime_Data", [
        current_shift["shift_id"], get_hour_slot(),
        data.get("category", ""), data.get("code", ""),
        data.get("durationMinutes", ""), data.get("description", ""), data.get("remarks", "")
    ], data.get("event_id"))
    if current_shift["shift_id"] != "NO PROD":
        checkpoint_and_queue_event(
            "downtime.recorded",
            {
                "shift_id": current_shift["shift_id"],
                "line_code": LINE_CODE,
                "hour_slot": get_hour_slot(),
                "category": data.get("category", ""),
                "code": data.get("code", ""),
                "duration_minutes": data.get("durationMinutes", ""),
                "description": data.get("description", ""),
                "remarks": data.get("remarks", ""),
                "action_taken": data.get("actionTaken", ""),
            },
            event_id=data.get("event_id"),
        )
    else:
        checkpoint_runtime_state(force=True, reason="downtime without active shift")


def process_downtime_command(command, automatic=False):
    """Apply one start/stop/log/cancel command to the shared timer and answer on <line>/downtime_result."""
    global downtime_session, current_mode
    with shift_state_lock:
        now_ms = int(time.time() * 1000)
        result = {"state": downtime_session, "server_ms": now_ms, "mode": current_mode,
                  "event": None, "log": None, "error": "", "command_id": command.get("command_id"),
                  "client": command.get("client")}
        try:
            updated, effects = downtime_transition(downtime_session, command, now_ms,
                                                    current_shift.get("shift_id", "NO PROD"), automatic)
            # Model Change mode no longer closes the hourly row: the model/lot change itself does (close_segment_for_setup).
            downtime_session = updated
            if effects["mode"] is not None:
                current_mode = effects["mode"]
            if effects["log"] is not None:
                record_downtime(effects["log"])
            elif command.get("action") != "get":
                checkpoint_runtime_state(force=True, reason="shared downtime session")
            result.update(state=downtime_session)
            result.update(effects)
            result["mode_changed"] = effects["mode"] is not None
            result["mode"] = current_mode
        except (ValueError, TypeError) as exc:
            result["error"] = str(exc)
        try:
            mqtt_client.publish(f"{LINE_CODE.lower()}/downtime_result", json.dumps(result), qos=1)
        except Exception as exc:
            print(f"[DOWNTIME ACK ERROR] {exc}")
        return result


def finish_due_downtime():
    with shift_state_lock:
        if downtime_session.get("status") == "running" and downtime_session.get("target_ms") is not None:
            if time.time() * 1000 >= downtime_session["target_ms"]:
                process_downtime_command({"action": "expire"}, automatic=True)


# ==========================================
# MQTT CALLBACKS
# ==========================================
def on_connect(client, userdata, flags, rc):
    client.subscribe([(MQTT_TOPIC_SHIFT_FORM, 0), (MQTT_TOPIC_SETUP, 0),
                      (MQTT_TOPIC_NR_REJECT, 0), (MQTT_TOPIC_NR_DOWNTIME, 0),
                      (MQTT_TOPIC_NR_ENDSHIFT, 0), (MQTT_TOPIC_MODE, 0),
                      (MQTT_TOPIC_PARAM_CONDITION, 0), (MQTT_TOPIC_ADJUST_COUNT, 0), (MQTT_TOPIC_SHIFT_OVERRIDE, 0),
                      (MQTT_TOPIC_DOWNTIME_SESSION, 1), (MQTT_TOPIC_CONFIG, 1)])
    client.publish(
        MQTT_TOPIC_STATUS,
        json.dumps({
            "line_code": LINE_CODE,
            "status": "ONLINE",
            "recovered_from_sqlite": recovered_from_sqlite,
            "timestamp": datetime.now().astimezone().isoformat(),
        }),
        qos=1,
        retain=True,
    )
    print(f"\n[SYSTEM] Connected to MQTT Broker. Ready to receive commands.")

def apply_line_config(data):
    """Apply settings from Smart Checksheet. An empty URL returns to the built-in fallback."""
    global google_web_app_url, google_web_app_url_source
    result = {"line_code": LINE_CODE, "status": "applied", "message": "",
              "request_id": data.get("request_id") if isinstance(data, dict) else None}
    if not isinstance(data, dict) or WEB_APP_URL_SETTING not in data:
        result.update(status="rejected", message=f"Send JSON with {WEB_APP_URL_SETTING}.")
    else:
        url = str(data.get(WEB_APP_URL_SETTING) or "").strip()
        if url and not is_valid_web_app_url(url):
            result.update(status="rejected",
                          message=f"Use an Apps Script web app link: {WEB_APP_URL_PREFIX}.../exec")
        else:
            try:
                if persistence_store is not None:
                    persistence_store.set_setting(WEB_APP_URL_SETTING, url or None)
                google_web_app_url = url or DEFAULT_WEB_APP_URL
                google_web_app_url_source = "smartchecksheet" if url else "default"
                print(f"[CONFIG] Google Sheets URL now from {google_web_app_url_source}.")
                if google_sender is not None:
                    google_sender.wake()
            except Exception as exc:
                result.update(status="rejected", message=f"Could not save the setting: {exc}")
    result.update(google_web_app_url=google_web_app_url, google_web_app_url_source=google_web_app_url_source)
    try:
        mqtt_client.publish(MQTT_TOPIC_CONFIG_RESULT, json.dumps(result), qos=1, retain=True)
    except Exception as exc:
        print(f"[CONFIG ACK ERROR] {exc}")
    return result


def on_message(client, userdata, msg):
    # Added total_output, shift_total_output, hourly_output, current_cycle_time, batch_run_time to global vars
    global current_mode, current_shift, total_rejects, total_output, shift_total_output, hourly_output, current_cycle_time, batch_run_time
    topic = msg.topic
    
    try:
        payload_str = msg.payload.decode('utf-8').strip()
        try: data = json.loads(payload_str); is_json = True
        except ValueError: data = payload_str; is_json = False

        if is_json and isinstance(data, dict) and coerce_bool(data.get("testMode", False)):
            print("[TEST] Command ignored; production state is unchanged.")
            return

        if topic == MQTT_TOPIC_CONFIG:
            apply_line_config(data if is_json else None)

        elif topic == MQTT_TOPIC_DOWNTIME_SESSION and is_json:
            process_downtime_command(data)

        elif topic == MQTT_TOPIC_SHIFT_FORM and is_json:
            receive_shift_form(data)

        elif topic == MQTT_TOPIC_SHIFT_OVERRIDE and is_json:
            override_shift(data)

        elif topic == MQTT_TOPIC_SETUP and is_json:
            close_segment_for_setup(data)
            if "model" in data: current_shift["model"] = data.get("model", "-")
            if "lot_number" in data: current_shift["lot_number"] = data.get("lot_number", "-")
            if "hourly_plan" in data:
                current_shift["target"] = data.get("hourly_plan", 0)
            elif "total_target" in data:
                current_shift["target"] = data.get("total_target", 0)
            if "standard_cycle" in data: current_shift["standard_cycle"] = float(data.get("standard_cycle", 1.0))
            if current_shift["shift_id"] != "NO PROD":
                checkpoint_and_queue_event(
                    "model.configured",
                    {
                        "shift_id": current_shift["shift_id"],
                        "line_code": LINE_CODE,
                        "model": current_shift["model"],
                        "lot_number": current_shift["lot_number"],
                        "target": current_shift["target"],
                        "standard_cycle": current_shift["standard_cycle"],
                        "source_payload": data,
                    },
                    event_id=data.get("event_id"),
                )
            else:
                checkpoint_runtime_state(force=True, reason="model setup without active shift")

        elif topic == MQTT_TOPIC_NR_REJECT and is_json:
            new_dashboard_rejects = safe_int(data.get("totalRejectNG"))
            total_rejects += new_dashboard_rejects

            row_data = [
                current_shift["shift_id"], get_hour_slot(), 
                data.get("totalSlabReject", ""), data.get("slabRejectCode", ""), 
                data.get("totalReturnRoll", ""), data.get("ohtNumber", ""), 
                data.get("totalRejectNG", ""), data.get("ngRejectCode", ""), 
                data.get("totalLoftLayerReject", ""), data.get("loftLayerRejectCode", "")
            ]
            queue_google_sheet_row("Reject_Data", row_data, data.get("event_id"))
            if current_shift["shift_id"] != "NO PROD":
                checkpoint_and_queue_event(
                    "reject.recorded",
                    {
                        "shift_id": current_shift["shift_id"],
                        "line_code": LINE_CODE,
                        "hour_slot": get_hour_slot(),
                        "slab_quantity": data.get("totalSlabReject", ""),
                        "slab_code": data.get("slabRejectCode", ""),
                        "return_roll_quantity": data.get("totalReturnRoll", ""),
                        "oht_number": data.get("ohtNumber", ""),
                        "ng_quantity": data.get("totalRejectNG", ""),
                        "ng_code": data.get("ngRejectCode", ""),
                        "loft_quantity": data.get("totalLoftLayerReject", ""),
                        "loft_code": data.get("loftLayerRejectCode", ""),
                        "source_payload": data,
                    },
                    event_id=data.get("event_id"),
                )
            else:
                checkpoint_runtime_state(force=True, reason="reject without active shift")

        elif topic == MQTT_TOPIC_NR_DOWNTIME and is_json:
            record_downtime(data)

        elif topic == MQTT_TOPIC_NR_ENDSHIFT:
            is_end = data.get("value", False) if isinstance(data, dict) else data
                
            if is_end == True or str(is_end).lower() == "true":
                execute_end_shift()

        elif topic == MQTT_TOPIC_PARAM_CONDITION and is_json:
            param_data = data.get("paramData", [{}])[0] if data.get("paramData") else {}
            temp_data = data.get("tempData", [{}])[0] if data.get("tempData") else {}
            glue_data = data.get("glueData", [{}])[0] if data.get("glueData") else {}
            
            model = param_data.get("model") or current_shift["model"]
            row_data = [
                current_shift["shift_id"], model,
                param_data.get("heating", ""), param_data.get("cooling", ""), 
                param_data.get("shuttle", ""), param_data.get("waterjet", ""),
                temp_data.get("rh", ""), temp_data.get("ctr", ""), temp_data.get("lh", ""),
                glue_data.get("std", ""), glue_data.get("act", "")
            ]
            queue_google_sheet_row("Parameter_Data", row_data, data.get("event_id"))
            if current_shift["shift_id"] != "NO PROD":
                checkpoint_and_queue_event(
                    "parameter.recorded",
                    {
                        "shift_id": current_shift["shift_id"],
                        "line_code": LINE_CODE,
                        "model": model,
                        "heating": param_data.get("heating", ""),
                        "cooling": param_data.get("cooling", ""),
                        "shuttle": param_data.get("shuttle", ""),
                        "waterjet": param_data.get("waterjet", ""),
                        "rh": temp_data.get("rh", ""),
                        "ctr": temp_data.get("ctr", ""),
                        "lh": temp_data.get("lh", ""),
                        "glue_standard": glue_data.get("std", ""),
                        "glue_actual": glue_data.get("act", ""),
                        "source_payload": data,
                    },
                    event_id=data.get("event_id"),
                )

        elif topic == MQTT_TOPIC_MODE:
            if downtime_session.get("status") == "running":
                print("[MODE WARNING] Stop the shared downtime timer before changing mode.")
                return
            new_mode = str(data).upper()
            valid_modes = [MODE_NORMAL, MODE_DOWN, MODE_MODEL_CHANGE, MODE_REST, MODE_PLANNED_STOP]
            if new_mode in valid_modes:
                # Model Change mode no longer closes the hourly row: the model/lot change itself does (close_segment_for_setup).
                
                if current_mode != new_mode:
                    current_mode = new_mode
                    checkpoint_runtime_state(force=True, reason="machine mode change")

        # NEW LOGIC: Adjust Product Count safely
        elif topic == MQTT_TOPIC_ADJUST_COUNT and is_json:
            action = data.get("action", "")
            amount = safe_int(data.get("amount", 1)) 

            if "adjust" in data and not action:
                adjust_val = safe_int(data.get("adjust", 0))
                if adjust_val not in (1, -1):
                    print(f"[COUNT WARNING] Ignored adjustment {adjust_val!r}; use +1 or -1.")
                    return
                print(f"[EVENT] Manual Count Adjustment from tablet: {adjust_val:+d}")
                hourly_output = max(0, hourly_output + adjust_val)
                total_output = max(0, total_output + adjust_val)
                shift_total_output = max(0, shift_total_output + adjust_val)
                current_cycle_time = (batch_run_time / total_output) / 60.0 if total_output > 0 else 0.0
                if current_shift["shift_id"] != "NO PROD":
                    checkpoint_and_queue_event(
                        "count.adjusted",
                        {
                            "shift_id": current_shift["shift_id"],
                            "line_code": LINE_CODE,
                            "adjustment": adjust_val,
                            "hourly_output_after": hourly_output,
                            "model_output_after": total_output,
                            "shift_output_after": shift_total_output,
                            "reason": data.get("reason", ""),
                            "operator": data.get("operator", ""),
                            "source_payload": data,
                        },
                        event_id=data.get("event_id"),
                    )
                else:
                    checkpoint_runtime_state(force=True, reason="count adjustment without active shift")

            elif action == "decrease":
                print(f"\n[EVENT] Manual Count Decrease Triggered. Amount: {amount}")
                
                if total_output >= amount:
                    total_output -= amount
                else:
                    total_output = 0
                    
                if shift_total_output >= amount:
                    shift_total_output -= amount
                else:
                    shift_total_output = 0
                    
                if hourly_output >= amount:
                    hourly_output -= amount
                else:
                    hourly_output = 0

                if total_output > 0:
                    current_cycle_time = (batch_run_time / total_output) / 60.0
                else:
                    current_cycle_time = 0.0
                    
                print(f"[STATUS] Total Output: {total_output} | Shift Output: {shift_total_output} | Hourly Output: {hourly_output}")
                if current_shift["shift_id"] != "NO PROD":
                    checkpoint_and_queue_event(
                        "count.adjusted",
                        {
                            "shift_id": current_shift["shift_id"],
                            "line_code": LINE_CODE,
                            "adjustment": -amount,
                            "hourly_output_after": hourly_output,
                            "model_output_after": total_output,
                            "shift_output_after": shift_total_output,
                            "source_payload": data,
                        },
                        event_id=data.get("event_id"),
                    )
                else:
                    checkpoint_runtime_state(force=True, reason="count adjustment without active shift")

    except Exception as e: 
        print(f"\n[ERROR] MQTT parsing failed: {e}")

restore_runtime_state()
reset_stale_shift_after_startup()

mqtt_client = mqtt.Client()
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message 
mqtt_client.will_set(
    MQTT_TOPIC_STATUS,
    json.dumps({"line_code": LINE_CODE, "status": "OFFLINE"}),
    qos=1,
    retain=True,
)
mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
mqtt_client.loop_start()
if outbox_sender is not None:
    outbox_sender.start()

# ==========================================
# MAIN EXECUTION LOOP
# ==========================================
google_sender = GoogleSheetsSender(google_queue, current_web_app_url, requests.post)
google_sender.start()

_google_queue_length_cache = (0.0, 0)


def google_queue_length():
    """Queued Sheets requests, refreshed at most every 5 seconds for the live payload."""
    global _google_queue_length_cache
    checked_at, length = _google_queue_length_cache
    if time.time() - checked_at >= 5.0:
        try:
            length = google_queue.google_queue_count()
        except Exception:
            pass
        _google_queue_length_cache = (time.time(), length)
    return length


def prune_outbox_if_due():
    """Hourly: delete outbox events older than 7 days that every consumer has delivered."""
    global last_outbox_prune
    if persistence_store is None or time.time() - last_outbox_prune < OUTBOX_PRUNE_INTERVAL_SECONDS:
        return
    last_outbox_prune = time.time()
    try:
        deleted = persistence_store.prune_finished_events(TRUSTED_API_ENABLED, OUTBOX_RETENTION_DAYS)
        if deleted:
            print(f"[SQLITE] Removed {deleted} delivered outbox event(s) older than {OUTBOX_RETENTION_DAYS} days.")
    except Exception as exc:
        print(f"[SQLITE ERROR] Outbox cleanup failed: {exc}")

def publish_live_data():
    global last_live_snapshot_publish

    # Same available-time formula used by push_hourly_to_sheets().
    standard_cycle = current_shift.get("standard_cycle", 1.0)
    live_base_minutes = base_time_this_hour / 60.0
    live_lost_minutes = lost_time_this_hour / 60.0
    live_available_minutes = max(0.0, live_base_minutes - live_lost_minutes)
    live_plan = int(live_available_minutes / standard_cycle) if standard_cycle > 0 else 0
    full_hour_plan = int(60.0 / standard_cycle) if standard_cycle > 0 else 0
    # The plan shown on the tablet is fixed for the hour (planned stops only lower it), not the minutes so far.
    live_plan = current_segment_plan()[0]

    payload = {
        "shift_id": current_shift["shift_id"],
        "line_code": LINE_CODE,
        "hour_slot": get_hour_slot(),
        "line": current_shift["line"],
        "date": current_shift["date"],
        "shift": current_shift["shift"],
        "group": current_shift["group"],
        "working_time": current_shift["workingTime"],
        "overtime": current_shift.get("overtime", False),
        "scheduled_end_at": current_shift.get("scheduled_end_at"),
        "scheduled_start_at": current_shift.get("scheduled_start_at"),
        "pending_shift": pending_shift_form,
        "downtime_session": downtime_session,
        "server_ms": int(time.time() * 1000),
        "persistence_enabled": persistence_store is not None,
        "supervisor": current_shift["supervisor"],
        "leader": current_shift["leader"],
        "forming_operator": current_shift["forming"],
        "waterjet_operator": current_shift["waterjet"],
        "assembly_operator": current_shift["assembly"],
        "quality_inspector": current_shift["quality"],
        "model": current_shift["model"],
        "lot_number": current_shift["lot_number"],
        "hourly_plan": current_shift.get("target", 0),
        "machine_status": current_status,
        "current_mode": current_mode,
        "product_count": total_output,
        "total_product_count": shift_total_output,
        "hourly_output": hourly_output,
        "waterjet_output": shift_total_output,
        "waterjet_a_output": waterjet_a_output,
        "waterjet_b_output": waterjet_b_output,
        "forming_output": forming_output,
        "mf_count": mf_count,
        "wj1_count": wj1_count,
        "wj2_count": wj2_count,
        "wj_total_count": wj1_count + wj2_count,
        "google_sheets_url_source": google_web_app_url_source,
        "google_queue_length": google_queue_length(),
        "live_plan": live_plan,
        "full_hour_plan": full_hour_plan,
        "hour_base_minutes": round(live_base_minutes, 2),
        "hour_lost_minutes": round(live_lost_minutes, 2),
        "hour_available_minutes": round(live_available_minutes, 2),
        "total_reject": total_rejects,
        "recovered_from_sqlite": recovered_from_sqlite,
        "shift_entry_status": (
            "WAITING_FOR_SHIFT" if shift_entry_window_is_active() else
            ("ACTIVE" if current_shift["shift_id"] != "NO PROD" else "NO_PROD")
        ),
        "shift_entry_window_ends_at": shift_entry_window_ends_at,
        "current_cycle_time": round(current_cycle_time, 2), 
        "standard_cycle_time": current_shift.get("standard_cycle", 1.0),
        "real_operating_time": round(real_operating_time, 2), 
        "total_real_operating_time": round(total_real_operating_time, 2),
        "run_time": round(run_time, 2),
        "loading_time": round(loading_time, 2),
        "delay_time": round(delay_time, 2),
        "downtime": round(downtime, 2),
        "total_rest_time": round(total_rest_time, 2),
        "hourly_rest_time": round(hourly_rest_time, 2),
        "planned_stop_time": round(planned_stop_time, 2),
        "model_change_time": round(model_change_time, 2),
        "total_machine_time": round(total_machine_time, 2)
    }
    try:
        mqtt_client.publish(MQTT_TOPIC_DATA, json.dumps(payload))
        now = time.time()
        if now - last_live_snapshot_publish >= 1.0:
            mqtt_client.publish(
                MQTT_TOPIC_LIVE_SNAPSHOT,
                json.dumps(payload),
                qos=1,
                retain=True,
            )
            last_live_snapshot_publish = now
    except Exception as e:
        print(f"[MQTT ERROR] Failed to publish live data: {e}")
    return payload

print("=====================================================")
print("  PRS Production Tracker Started (Verbose Debug Mode)")
print("=====================================================")

last_loop_time = time.time()
last_debug_print_time = time.time()
limit_switch_prev = GPIO.input(LIMIT_SWITCH_PIN)
wj1_prev = GPIO.input(PROX_SENSOR_1_PIN)
wj2_prev = GPIO.input(PROX_SENSOR_2_PIN)

try:
    while True:
        current_time = time.time()
        loop_delta = current_time - last_loop_time
        last_loop_time = current_time

        now = datetime.now()
        expire_shift_entry_window(now)

        # This program owns shift end/start and the production-day reset,
        # including a form the tablet submitted for a later start.
        if advance_shift_schedule(now):
            continue

        finish_due_downtime()

        # 1. Check for Standard Hourly Push
        if (
            current_shift["shift_id"] != "NO PROD"
            and now.minute == 0
            and now.hour != last_sent_hour
        ):
            push_hourly_to_sheets(is_model_change=False)
            last_sent_hour = now.hour

        # 2. Google Sheets rows are sent by the background sender thread.
        prune_outbox_if_due()

        # 3. Timer Accumulation (same Sendayan formulas, lifecycle-gated)
        active_shift = current_shift.get("shift_id") != "NO PROD"
        buffering_shift_entry = shift_entry_window_is_active(now)
        production_tracking_enabled = active_shift or buffering_shift_entry
        production_loop_delta = loop_delta if production_tracking_enabled else 0.0
        base_time_this_hour += production_loop_delta
        total_machine_time += production_loop_delta
        
        if current_mode in (MODE_REST, MODE_PLANNED_STOP, MODE_MODEL_CHANGE):
            hourly_plan_state["planned_lost"] += production_loop_delta
        if current_mode in [MODE_NORMAL, MODE_DOWN]:
            real_operating_time += production_loop_delta
            total_real_operating_time += production_loop_delta

        # 4. Main State & Counting Logic
        if current_mode == MODE_NORMAL:
            batch_run_time += production_loop_delta

            # --- FORMING COUNTING (Sensor 3 / Limit Switch): live panel only ---
            limit_switch_current = GPIO.input(LIMIT_SWITCH_PIN)
            # Detects transition from UNBLOCKED (1) to BLOCKED (0)
            if (limit_switch_current == 0 and limit_switch_prev == 1 and
                    current_time - last_limit_switch_count_time >= LIMIT_SWITCH_COUNT_DEBOUNCE_TIME and
                    production_tracking_enabled):
                forming_output += 1
                mf_count += 1
                last_limit_switch_count_time = current_time
                checkpoint_runtime_state(force=True, reason="MF detection")
                print(f"[LIMIT] MF: {mf_count} | Forming (Shift): {forming_output} | WJ Product: {total_output}")

            limit_switch_prev = limit_switch_current

            # --- STATE LOGIC (Prox Sensors 1 & 2, unchanged: both blocked = loading/delay) ---
            # MAIN OUTPUT: every one of these samples also counts WJ1 / WJ2 (HIGH -> LOW edges).
            current_machine_state = get_stable_machine_state(accept_counts=production_tracking_enabled)

            if current_machine_state == 1: # BLOCKED (Either prox sensor blocked)
                if not sensor_blocked:
                    sensor_blocked = True
                    blockage_start_time = current_time
                
                duration = current_time - blockage_start_time
                
                # The New 10-Second Logic
                if duration < 10:
                    current_status = STATUS_LOADING
                    loading_time += production_loop_delta
                else:
                    current_status = STATUS_DELAY
                    delay_time += production_loop_delta

            else: # UNBLOCKED (Both prox sensors clear)
                sensor_blocked = False
                current_status = STATUS_RUN
                run_time += production_loop_delta

        else:
            # Update previous GPIO levels without counting while outside NORMAL mode.
            limit_switch_prev = GPIO.input(LIMIT_SWITCH_PIN)
            sample_waterjet_inputs(accept_counts=False)
            # Non-normal modes
            if current_mode == MODE_DOWN:
                current_status = STATUS_DOWN
                downtime += production_loop_delta
                lost_time_this_hour += production_loop_delta
            elif current_mode == MODE_REST:
                current_status = STATUS_REST
                total_rest_time += production_loop_delta
                hourly_rest_time += production_loop_delta
                lost_time_this_hour += production_loop_delta
            elif current_mode == MODE_PLANNED_STOP:
                current_status = STATUS_PLANNED_STOP
                planned_stop_time += production_loop_delta
                lost_time_this_hour += production_loop_delta
            elif current_mode == MODE_MODEL_CHANGE:
                current_status = STATUS_MODEL_CHANGE
                model_change_time += production_loop_delta
                lost_time_this_hour += production_loop_delta

        if buffering_shift_entry:
            current_status = STATUS_WAITING_FOR_SHIFT

        if current_status != previous_status:
            status_names = {0: "RUN", 1: "LOADING", 2: "DELAY", 3: "REST", 4: "DOWN", 5: "PLANNED STOP", 6: "MODEL CHANGE", 7: "WAITING FOR SHIFT"}
            print(f"\n[STATE CHANGE] Machine Status changed: {status_names.get(previous_status)} -> {status_names.get(current_status)}")
            previous_status = current_status

        checkpoint_runtime_state(reason="timer checkpoint")
        publish_live_data()
        
        if current_time - last_debug_print_time >= 3.0:
            last_debug_print_time = current_time
            debug_status_names = {
                STATUS_RUN: "RUNNING",
                STATUS_LOADING: "LOADING",
                STATUS_DELAY: "DELAY",
                STATUS_REST: "REST",
                STATUS_DOWN: "DOWN",
                STATUS_PLANNED_STOP: "PLANNED STOP",
                STATUS_MODEL_CHANGE: "MODEL CHANGE",
                STATUS_WAITING_FOR_SHIFT: "WAITING FOR SHIFT"
            }
            print(
                f"[MACHINE STATUS] {debug_status_names.get(current_status, 'UNKNOWN')} | "
                f"Mode: {current_mode} | Waterjet Model: {total_output} | "
                f"Waterjet Shift: {shift_total_output} (A={waterjet_a_output} B={waterjet_b_output}) | "
                f"Forming Shift: {forming_output} | MF: {mf_count} | WJ1: {wj1_count} | WJ2: {wj2_count}"
            )
            
        time.sleep(0.05)

except KeyboardInterrupt: 
    print("\n\n[SYSTEM] Keyboard Interrupt Detected. Shutting down gracefully...")
finally: 
    checkpoint_runtime_state(force=True, reason="graceful shutdown")
    try:
        mqtt_client.publish(
            MQTT_TOPIC_STATUS,
            json.dumps({
                "line_code": LINE_CODE,
                "status": "OFFLINE",
                "timestamp": datetime.now().astimezone().isoformat(),
            }),
            qos=1,
            retain=True,
        )
    except Exception:
        pass
    if outbox_sender is not None:
        outbox_sender.stop()
        if outbox_sender.is_alive():
            outbox_sender.join(timeout=6.0)
    if google_sender is not None:
        google_sender.stop()
        # A request in flight may take up to 120 s; it stays queued and is resent after restart.
        google_sender.join(timeout=2.0)
    GPIO.cleanup()
    mqtt_client.disconnect()
    mqtt_client.loop_stop()
    if persistence_store is not None:
        persistence_store.close()
    print("[SYSTEM] Exit complete.")

