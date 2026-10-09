#!/usr/bin/env python3
"""
CGM bedside dashboard - Flask backend.

One background thread polls LibreLinkUp (via pylibrelinkup) about once a minute
and caches the latest reading in memory. The browser only ever talks to
GET /api/data, which reads that cache, so no matter how many tabs or reloads
hit the page, Abbott sees one request per poll interval.

Configuration comes from environment variables (see config.env.example):

    LIBRE_EMAIL / LIBRE_PASSWORD   LibreLinkUp *follower* account credentials
    LIBRE_REGION                   US, EU, EU2, DE, FR, AU, AP, AE, CA, JP, LA, RU
                                   (a wrong region is auto-corrected via redirect)
    LIBRE_PATIENT_ID               optional; pick one connection if you follow several
    POLL_SECONDS                   upstream poll interval (default 60, minimum 30)
    GLUCOSE_LOW / GLUCOSE_HIGH     alarm thresholds in mg/dL (default 70 / 180)
    ALARM_SNOOZE_MINUTES           tap-to-silence duration (default 15)
    STALE_MINUTES                  reading age that counts as "no signal" (default 10)
    CGM_MOCK=1                     fake sine-wave data, no Abbott login (UI testing)
    HOST / PORT                    bind address (default 127.0.0.1:5000)
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import requests
from flask import Flask, jsonify, render_template

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------


def _env_int(name: str, default: int, minimum: int | None = None) -> int:
    try:
        value = int(os.environ.get(name, default))
    except ValueError:
        value = default
    return max(value, minimum) if minimum is not None else value


LIBRE_EMAIL = os.environ.get("LIBRE_EMAIL", "").strip()
LIBRE_PASSWORD = os.environ.get("LIBRE_PASSWORD", "")
LIBRE_REGION = os.environ.get("LIBRE_REGION", "US").strip().upper()
LIBRE_PATIENT_ID = os.environ.get("LIBRE_PATIENT_ID", "").strip().lower()

MOCK_MODE = os.environ.get("CGM_MOCK", "0").lower() in ("1", "true", "yes")

# LibreLinkUp only gets a new value about once a minute, and Abbott returns
# HTTP 429 to clients that poll harder, so 30 s is the floor.
POLL_SECONDS = _env_int("POLL_SECONDS", 60, minimum=5 if MOCK_MODE else 30)
GLUCOSE_LOW = _env_int("GLUCOSE_LOW", 70)
GLUCOSE_HIGH = _env_int("GLUCOSE_HIGH", 180)
ALARM_SNOOZE_MINUTES = _env_int("ALARM_SNOOZE_MINUTES", 15, minimum=1)
STALE_MINUTES = _env_int("STALE_MINUTES", 10, minimum=2)
FRONTEND_POLL_SECONDS = _env_int("FRONTEND_POLL_SECONDS", 60, minimum=5)

HOST = os.environ.get("HOST", "127.0.0.1")  # localhost only: this is health data
PORT = _env_int("PORT", 5000)

HTTP_TIMEOUT = (10, 20)          # (connect, read) seconds for each Abbott call
MAX_BACKOFF_SECONDS = 15 * 60    # cap for exponential backoff on errors
AUTH_FAILURE_BACKOFF = 15 * 60   # wrong password / unaccepted ToS: retry slowly
                                 # so repeated logins don't lock the account

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("cgm")
# Flask's per-request access log would print a line every minute forever.
logging.getLogger("werkzeug").setLevel(logging.WARNING)


# ----------------------------------------------------------------------------
# pylibrelinkup import + request timeout
# ----------------------------------------------------------------------------

try:
    from pylibrelinkup import (
        APIUrl,
        AuthenticationError,
        EmailVerificationError,
        LLUAPIRateLimitError,
        PatientNotFoundError,
        PrivacyPolicyError,
        PyLibreLinkUp,
        RedirectError,
        TermsOfUseError,
    )
except ImportError:
    if not MOCK_MODE:
        raise
    # Mock mode works without the library installed; these placeholders just
    # keep the isinstance() checks in the error handler valid.
    class _Unavailable(Exception):
        pass

    APIUrl = PyLibreLinkUp = None
    AuthenticationError = EmailVerificationError = LLUAPIRateLimitError = _Unavailable
    PatientNotFoundError = PrivacyPolicyError = RedirectError = _Unavailable
    TermsOfUseError = _Unavailable

FATAL_AUTH_ERRORS = (
    AuthenticationError,
    TermsOfUseError,
    PrivacyPolicyError,
    EmailVerificationError,
)

if not MOCK_MODE:
    import importlib

    class _RequestsWithTimeout:
        """Drop-in stand-in for the `requests` module that adds a default timeout.

        pylibrelinkup calls requests.get()/post() without a timeout, so a
        half-open TCP connection (common on flaky Wi-Fi) would block the
        poller thread forever and the dashboard would silently freeze.
        """

        def __init__(self, module, timeout):
            self._module = module
            self._timeout = timeout

        def get(self, *args, **kwargs):
            kwargs.setdefault("timeout", self._timeout)
            return self._module.get(*args, **kwargs)

        def post(self, *args, **kwargs):
            kwargs.setdefault("timeout", self._timeout)
            return self._module.post(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._module, name)

    try:
        _pllu_core = importlib.import_module("pylibrelinkup.pylibrelinkup")
        _pllu_core.requests = _RequestsWithTimeout(requests, HTTP_TIMEOUT)
    except (ImportError, AttributeError):  # library layout changed; run without it
        log.warning("Could not add HTTP timeouts to pylibrelinkup; continuing without")


# ----------------------------------------------------------------------------
# Poller
# ----------------------------------------------------------------------------


def _utc_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _now_ms() -> int:
    return int(time.time() * 1000)


class LibreLinkPoller(threading.Thread):
    """Fetches the latest reading on a fixed cadence and caches it.

    Efficiency rules this class enforces:
      * one login per session; the token is reused until Abbott rejects it
      * the patient list is fetched once, not on every poll
      * exactly one upstream request per poll (latest() = one GET)
      * 429s honour Retry-After; other failures back off exponentially
    """

    def __init__(self) -> None:
        super().__init__(name="librelinkup-poller", daemon=True)
        self._lock = threading.Lock()
        self._client = None
        self._patient_id = None
        self._region = LIBRE_REGION
        self._failures = 0
        self._snapshot: dict = {
            "status": "starting",
            "error": None,
            "value": None,
            "trend": None,
            "trend_name": None,
            "arrow": None,
            "timestamp": None,
            "timestamp_ms": None,
            "fetched_at_ms": None,
            "next_poll_ms": None,
        }

    # -- public --------------------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            snap = dict(self._snapshot)
        now = _now_ms()
        snap["server_time_ms"] = now
        if snap["timestamp_ms"]:
            snap["age_seconds"] = max(0, (now - snap["timestamp_ms"]) // 1000)
            snap["stale"] = snap["age_seconds"] > STALE_MINUTES * 60
        else:
            snap["age_seconds"] = None
            snap["stale"] = True
        return snap

    def run(self) -> None:
        while True:
            delay = self._poll_once()
            with self._lock:
                self._snapshot["next_poll_ms"] = _now_ms() + int(delay * 1000)
            time.sleep(delay)

    # -- internals -----------------------------------------------------------

    def _poll_once(self) -> float:
        """Do one fetch; return the number of seconds to wait before the next."""
        try:
            m = self._fetch_latest()
        except Exception as exc:  # noqa: BLE001 - poller must never die
            return self._handle_error(exc)

        self._failures = 0
        reading = {
            "status": "ok",
            "error": None,
            "value": int(round(m.value_in_mg_per_dl)),
            "trend": int(m.trend),
            "trend_name": m.trend.name,
            "arrow": m.trend.indicator,
            # factory_timestamp is UTC; `timestamp` is the sensor's wall clock.
            "timestamp": m.factory_timestamp.astimezone(timezone.utc).isoformat(),
            "timestamp_ms": _utc_ms(m.factory_timestamp),
            "fetched_at_ms": _now_ms(),
        }
        with self._lock:
            changed = reading["timestamp_ms"] != self._snapshot.get("timestamp_ms")
            self._snapshot.update(reading)
        if changed:
            log.info("Glucose %s mg/dL %s", reading["value"], reading["arrow"])
        return POLL_SECONDS

    def _fetch_latest(self):
        if self._client is None:
            self._login()
        if self._patient_id is None:
            self._patient_id = self._select_patient()
        return self._client.latest(patient_identifier=self._patient_id)

    def _login(self) -> None:
        region = self._region
        for _ in range(3):  # initial attempt + up to two region redirects
            client = PyLibreLinkUp(
                email=LIBRE_EMAIL,
                password=LIBRE_PASSWORD,
                api_url=APIUrl.from_string(region),
            )
            try:
                client.authenticate()
            except RedirectError as redirect:
                log.info("Account lives in region %s; set LIBRE_REGION=%s to skip "
                         "this redirect", redirect.region.name, redirect.region.name)
                region = redirect.region.name
                continue
            self._client, self._region = client, region
            log.info("Logged in to LibreLinkUp (%s)", region)
            return
        raise AuthenticationError("Too many region redirects during login")

    def _select_patient(self):
        patients = self._client.get_patients()
        if not patients:
            raise PatientNotFoundError()
        if LIBRE_PATIENT_ID:
            for p in patients:
                if str(p.patient_id).lower() == LIBRE_PATIENT_ID:
                    return p.patient_id
            raise PatientNotFoundError()
        chosen = patients[0]
        if len(patients) > 1:
            log.info("Following %d people; using the first. Set LIBRE_PATIENT_ID to "
                     "one of: %s", len(patients),
                     ", ".join(f"{p.first_name}={p.patient_id}" for p in patients))
        return chosen.patient_id

    def _backoff(self) -> float:
        self._failures += 1
        return min(POLL_SECONDS * (2 ** (self._failures - 1)), MAX_BACKOFF_SECONDS)

    def _handle_error(self, exc: Exception) -> float:
        """Decide the retry delay. `screen` is the short text the footer shows;
        `detail` goes to the log with the full explanation."""
        reason = str(exc).strip().rstrip(".") or type(exc).__name__
        if isinstance(exc, LLUAPIRateLimitError):
            delay = max(exc.retry_after or self._backoff(), POLL_SECONDS)
            screen = f"Rate limited \u00b7 retry in {_mins(delay)}"
            detail = f"LibreLinkUp rate limit (HTTP 429); retrying in {int(delay)} s"
        elif isinstance(exc, FATAL_AUTH_ERRORS):
            self._client = None
            self._patient_id = None
            delay = AUTH_FAILURE_BACKOFF
            screen = "Login failed \u00b7 see server log"
            detail = (f"Login failed: {reason}. Check LIBRE_EMAIL/LIBRE_PASSWORD, and "
                      "sign in to the LibreLinkUp app once to accept any new terms. "
                      f"Retrying in {int(delay)} s.")
        elif isinstance(exc, PatientNotFoundError):
            self._patient_id = None
            delay = AUTH_FAILURE_BACKOFF
            screen = "No LibreLinkUp connection"
            detail = ("No matching connection. Accept the sensor wearer's invite in "
                      "the LibreLinkUp app, or fix LIBRE_PATIENT_ID.")
        elif (isinstance(exc, requests.HTTPError) and exc.response is not None
              and exc.response.status_code == 429):
            # The login endpoint raises a plain HTTPError for 429.
            retry_after = exc.response.headers.get("Retry-After", "")
            delay = max(int(retry_after) if retry_after.isdigit() else self._backoff(),
                        POLL_SECONDS)
            screen = f"Rate limited \u00b7 retry in {_mins(delay)}"
            detail = f"LibreLinkUp rate limit on login; retrying in {int(delay)} s"
        elif (isinstance(exc, requests.HTTPError) and exc.response is not None
              and exc.response.status_code in (401, 403)):
            # Token expired or revoked: drop it and log in again. Retry fast
            # only on the first failure so a persistent 401 can't become a
            # login loop.
            self._client = None
            self._patient_id = None
            delay = 5 if self._failures == 0 else self._backoff()
            self._failures = max(self._failures, 1)
            screen = "Reconnecting\u2026"
            detail = f"Session rejected (HTTP {exc.response.status_code}); logging in again"
        elif isinstance(exc, requests.RequestException):
            delay = self._backoff()
            screen = f"Network error \u00b7 retry in {_mins(delay)}"
            detail = f"Network error ({type(exc).__name__}: {reason}); retrying in {int(delay)} s"
        else:
            # e.g. a pydantic ValidationError because Abbott changed a payload
            # or answered with an error body. After a few in a row, start over
            # with a fresh login in case the session is in a bad state.
            delay = self._backoff()
            if self._failures >= 3:
                self._client = None
                self._patient_id = None
            screen = "Error \u00b7 see server log"
            detail = f"Unexpected {type(exc).__name__}; retrying in {int(delay)} s"
            log.exception("Poll failed")

        log.warning(detail)
        with self._lock:
            # Keep the last good reading on screen; the UI shows its age.
            self._snapshot["status"] = "error"
            self._snapshot["error"] = screen
        return delay


def _mins(seconds: float) -> str:
    return f"{int(seconds)} s" if seconds < 60 else f"{round(seconds / 60)} min"


class _MockTrend(int):
    """Quacks like pylibrelinkup.Trend: int value plus .name and .indicator."""

    _NAMES = {1: ("DOWN_FAST", "↓"), 2: ("DOWN_SLOW", "↘"), 3: ("STABLE", "→"),
              4: ("UP_SLOW", "↗"), 5: ("UP_FAST", "↑")}

    def __new__(cls, value: int):
        obj = int.__new__(cls, value)
        obj.name, obj.indicator = cls._NAMES[value]
        return obj


class MockPoller(LibreLinkPoller):
    """Sine wave from ~45 to ~215 mg/dL over 20 minutes; crosses both alarms."""

    PERIOD = 1200  # seconds

    def _fetch_latest(self):
        phase = 2 * math.pi * time.time() / self.PERIOD
        value = 130 + 85 * math.sin(phase)
        per_minute = 85 * math.cos(phase) * 2 * math.pi / self.PERIOD * 60
        # Abbott's arrow buckets: >2 mg/dL/min fast, 1-2 slow, otherwise stable.
        if per_minute > 2:
            trend = 5
        elif per_minute > 1:
            trend = 4
        elif per_minute < -2:
            trend = 1
        elif per_minute < -1:
            trend = 2
        else:
            trend = 3
        return SimpleNamespace(
            value_in_mg_per_dl=value,
            trend=_MockTrend(trend),
            factory_timestamp=datetime.now(timezone.utc),
        )


# ----------------------------------------------------------------------------
# Flask app
# ----------------------------------------------------------------------------

app = Flask(__name__)
poller = MockPoller() if MOCK_MODE else LibreLinkPoller()


@app.get("/")
def index():
    return render_template(
        "index.html",
        config={
            "low": GLUCOSE_LOW,
            "high": GLUCOSE_HIGH,
            "pollMs": FRONTEND_POLL_SECONDS * 1000,
            "snoozeMs": ALARM_SNOOZE_MINUTES * 60 * 1000,
            "staleMs": STALE_MINUTES * 60 * 1000,
            "alarmUrl": "/static/alarm.mp3",
        },
    )


@app.get("/api/data")
def api_data():
    response = jsonify(poller.snapshot())
    response.headers["Cache-Control"] = "no-store"
    return response


def main() -> None:
    if not MOCK_MODE:
        if not (LIBRE_EMAIL and LIBRE_PASSWORD):
            raise SystemExit("Set LIBRE_EMAIL and LIBRE_PASSWORD (see config.env.example), "
                             "or CGM_MOCK=1 to try the UI with fake data.")
        try:
            APIUrl.from_string(LIBRE_REGION)
        except ValueError:
            raise SystemExit(f"Unknown LIBRE_REGION {LIBRE_REGION!r}; use one of: "
                             + ", ".join(u.name for u in APIUrl))
    log.info("Starting %s poller (every %d s); dashboard on http://%s:%d",
             "MOCK" if MOCK_MODE else "LibreLinkUp", POLL_SECONDS, HOST, PORT)
    poller.start()
    # Flask's built-in server is fine for one local kiosk client. threaded=True
    # keeps a slow page load from blocking /api/data. Never enable debug here:
    # the reloader would start a second poller and double the API traffic.
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
