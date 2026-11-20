"""Send queued Google Sheets rows and PDF requests on a background thread.

The main sensor loop only adds items to the queue, so a slow or offline Apps
Script can never pause counting. Items are sent strictly in order and retried
with backoff while keeping their request_id, which the Apps Script uses to
ignore duplicates.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional

APPEND_TIMEOUT_SECONDS = 30
PDF_TIMEOUT_SECONDS = 120
RETRY_BASE_SECONDS = 5
RETRY_MAX_SECONDS = 300


class MemoryGoogleQueue:
    """Fallback used only when SQLite cannot be opened; same interface as the store."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: List[Dict[str, Any]] = []
        self._next_id = 1

    def enqueue_google(self, item: Dict[str, Any]) -> bool:
        with self._lock:
            if any(existing["request_id"] == item["request_id"] for existing in self._items):
                return False
            self._items.append(dict(item, queue_id=self._next_id, attempt_count=0, next_retry_at=0.0))
            self._next_id += 1
            return True

    def peek_google(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return dict(self._items[0]) if self._items else None

    def complete_google(self, queue_id: int) -> None:
        with self._lock:
            self._items = [item for item in self._items if item["queue_id"] != queue_id]

    def retry_google(self, queue_id: int, attempt_count: int, next_attempt_at: float, message: str) -> None:
        with self._lock:
            for item in self._items:
                if item["queue_id"] == queue_id:
                    item.update(attempt_count=attempt_count, next_retry_at=next_attempt_at)

    def google_queue_count(self) -> int:
        with self._lock:
            return len(self._items)


def build_google_payload(item: Dict[str, Any]):
    """Return (payload, description, timeout) for one queued item."""
    if item.get("action") == "GENERATE_PDF":
        return ({"action": "GENERATE_PDF", "shift_id": item["shift_id"], "request_id": item["request_id"]},
                f"PDF generation for {item['shift_id']}", PDF_TIMEOUT_SECONDS)
    return ({"action": "APPEND_ROW", "tab_name": item["tab"], "row_data": item["row"],
             "request_id": item["request_id"]},
            f"{item['tab']} data", APPEND_TIMEOUT_SECONDS)


def send_next_google_item(queue, url: str, post: Callable[..., Any], now: Optional[float] = None) -> bool:
    """Try the oldest item once. Returns True when it was delivered."""
    now = time.time() if now is None else now
    item = queue.peek_google()
    if item is None or now < float(item.get("next_retry_at") or 0):
        return False
    payload, description, timeout = build_google_payload(item)
    try:
        if not url:
            raise RuntimeError("No Google Sheets web app URL is configured")
        response = post(url, json=payload, timeout=timeout)
        response.raise_for_status()
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError(f"Apps Script returned non-JSON response: {response.text[:200]}") from exc
        if body.get("status") != "success":
            raise RuntimeError(f"Apps Script rejected the request: {body}")
    except Exception as exc:
        attempts = int(item.get("attempt_count") or 0) + 1
        delay = min(RETRY_BASE_SECONDS * (2 ** min(attempts - 1, 6)), RETRY_MAX_SECONDS)
        queue.retry_google(item["queue_id"], attempts, now + delay, str(exc))
        print(f"[HTTP ERROR] Failed to send {description}; retrying in {delay}s "
              f"with request {item['request_id']}: {exc}")
        return False
    queue.complete_google(item["queue_id"])
    print(f"[HTTP] SUCCESS! Completed Google action: {description}.")
    return True


class GoogleSheetsSender(threading.Thread):
    def __init__(self, queue, url_provider: Callable[[], str], post: Callable[..., Any]) -> None:
        super().__init__(name="sdy2-google-sheets", daemon=True)
        self.queue = queue
        self.url_provider = url_provider
        self.post = post
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()

    def wake(self) -> None:
        self._wake_event.set()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                delivered = send_next_google_item(self.queue, self.url_provider(), self.post)
            except Exception as exc:  # never let the sender thread die
                print(f"[GOOGLE QUEUE ERROR] {exc}")
                delivered = False
            if not delivered:
                self._wake_event.wait(1.0)
                self._wake_event.clear()
