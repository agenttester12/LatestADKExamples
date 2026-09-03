# -*- coding: utf-8 -*-
"""Work-Offsite tools for the watsonx Orchestrate agent.

Seven tools let an employee view, submit, cancel, and modify their Workday
work-offsite / work-from-home / flex-work requests, and list the valid reasons.
Each tool returns a dict with human-readable ``text`` and, where an interactive
step helps, a ``card`` — a ChatBlock the agent stages verbatim through
``stage_card`` so the widget can render it.

Everything lives in one module so it imports with a single
``orchestrate tools import -k python -f tools.py``.

A few things worth knowing:

* ``employee_id`` comes from the run context and is subject-bound — the tools
  never accept an employee id as an argument, so the subject can't be spoofed.
* Credentials are a service account from the ``FlexWork`` connection, cached
  process-wide across users behind a lock.
* Workday has no native "modify", so modify = cancel/rescind the old request
  (per its status) then submit a new one.
* The agent's model localizes its own prose when the backend sends a
  ``response_language`` context variable. Card labels stay in English; there is
  no per-language copy table here.
"""
from __future__ import annotations

import base64
import hashlib
import re
import threading
import time
from datetime import date, datetime, timedelta

import httpx
from ibm_watsonx_orchestrate.agent_builder.tools import tool
from ibm_watsonx_orchestrate.agent_builder.connections import ConnectionType
from ibm_watsonx_orchestrate.run import connections
from ibm_watsonx_orchestrate.run.context import AgentRun

# OpenTelemetry is optional — tracing no-ops when it isn't installed.
try:
    from opentelemetry import trace as _otel_trace
    _HAS_OTEL = True
except ImportError:
    _HAS_OTEL = False


APP_ID = "FlexWork"
_HTTP_TIMEOUT = 15.0
_TOKEN_BUFFER = 60           # refresh a bearer token this many seconds early
_CANCEL_MODIFY_DAYS = 180    # cancel/modify show six months of history
_WORKDAY_API_VERSION = "v43.0"
MAX_TABLE_ROWS = 25          # cap staged card/table size

# Valid reasons, resolved in code so the reason is never LLM-guessed.
_REASONS: list[tuple[str, str]] = [
    ("Business Reason", "FLEXIBLE_WORK_ARRANGEMENT_SUBTYPE-3-16"),
    ("Other Reason", "FLEXIBLE_WORK_ARRANGEMENT_SUBTYPE-3-55"),
    ("Remote Flexibility Benefit", "FLEXIBLE_WORK_ARRANGEMENT_SUBTYPE-3-93"),
]
_REASON_DESCRIPTIONS: list[str] = [
    "Business travel, client visits, conferences, job fairs",
    "Service provider visit, caring for an ill family member, bad weather, mild illness",
    "Up to 15 days/year (20 if you exceed expectations) to work from an alternative location",
]

HIGH_RISK_COUNTRY_URL = (
    "https://fiservcorp.sharepoint.com/sites/fuel-dept-general-services/"
    "SitePages/Global%20Strategic%20Sourcing/Travel/"
    "High-Risk-Country-List.aspx?web=1"
)

# Stable card IDs — a card's submission comes back tied to its cardId.
CARD_SUBMIT_FORM = "offsite-submit-form"
CARD_SUBMIT_CONFIRM = "offsite-submit-confirm"
CARD_VIEW = "offsite-view"
CARD_SELECT_CANCEL = "offsite-select-cancel"
CARD_SELECT_MODIFY = "offsite-select-modify"
CARD_CANCEL_CONFIRM = "offsite-cancel-confirm"
CARD_REASONS = "offsite-reasons"

# Workday status -> display label.
_STATUS_DISPLAY = {"Successfully Completed": "Approved"}

# Shown to the employee when Workday can't be reached or returns an opaque
# failure; the underlying detail goes to the trace, not the chat.
_WORKDAY_UNAVAILABLE = "I couldn't reach Workday just now. Please try again in a moment."
_NO_EMPLOYEE_ID = "I couldn't determine your employee ID. Please reopen AskHR and try again."

# Service-account credential caches (same for every user), guarded for the
# concurrent-thread case.
_cache_lock = threading.Lock()
_creds_cache: dict | None = None
_token_cache: dict[tuple[str, str], tuple[str, float]] = {}
_basic_auth_cache: str | None = None


# Card builders. These are public because the tools call them and the test suite
# validates each one directly against the backend ChatBlock schema. The backend
# drops any card that fails that schema, so every builder returns a plain,
# fully-formed dict with string-valued table cells.
def _display_status(raw: str) -> str:
    return _STATUS_DISPLAY.get(raw, raw)


def _date_range(start_iso: str, end_iso: str) -> str:
    """One date for a single day, else 'start → end'."""
    return start_iso if start_iso == end_iso else f"{start_iso} → {end_iso}"


def duration_label(start_iso: str, end_iso: str) -> str:
    """Inclusive duration as '1 day' / '3 days', or '' if the dates don't parse."""
    try:
        count = (date.fromisoformat(end_iso) - date.fromisoformat(start_iso)).days + 1
        if count < 1:
            count = 1
    except ValueError:
        return ""
    return f"{count} day" if count == 1 else f"{count} days"


def build_submit_form(
    today_iso: str,
    *,
    start_value: str = "",
    end_value: str = "",
    reason_value: str = "",
) -> dict:
    """Build the submit form card (two date pickers + a reason dropdown).

    The optional *_value arguments prefill what was already understood so a
    re-ask keeps the employee's earlier answers. An unknown ``reason_value`` is
    left unselected.
    """
    valid_labels = [label for label, _ in _REASONS]
    options = [{"value": label, "label": label} for label in valid_labels]

    start_field = {
        "type": "date", "id": "start_date", "label": "Start date",
        "required": True, "min": today_iso,
    }
    if start_value:
        start_field["value"] = start_value

    end_field = {
        "type": "date", "id": "end_date", "label": "End date",
        "required": True, "min": today_iso,
    }
    if end_value:
        end_field["value"] = end_value

    reason_field = {
        "type": "select", "id": "reason", "label": "Reason",
        "required": True, "options": options,
    }
    if reason_value in valid_labels:
        reason_field["value"] = reason_value

    return {
        "kind": "form",
        "cardId": CARD_SUBMIT_FORM,
        "title": "Submit a work-offsite request",
        "subtitle": "Choose your dates and reason. For a single day, use the same date for both.",
        "submitLabel": "Review request",
        "cancelLabel": "Cancel",
        "fields": [start_field, end_field, reason_field],
    }


def build_submit_confirm(
    start_iso: str, end_iso: str, reason_label: str,
    *, for_modify: bool = False, replacing: str | None = None,
) -> dict:
    """Build the confirm card shown before a submit/modify write.

    With ``for_modify`` set, the title and a Note fact make clear the request is
    replaced (rescind + resubmit), not edited in place. ``replacing`` names the
    existing request being replaced so the employee sees exactly what a modify
    will remove before confirming.
    """
    facts = []
    if replacing:
        facts.append({"label": "Replacing", "value": replacing})
    facts.append({"label": "Dates", "value": _date_range(start_iso, end_iso)})
    facts.append({"label": "Reason", "value": reason_label})
    duration = duration_label(start_iso, end_iso)
    if duration:
        facts.append({"label": "Duration", "value": duration})
    if for_modify:
        facts.append({"label": "Note", "value": "This cancels your current request and submits a new one."})

    return {
        "kind": "confirm",
        "cardId": CARD_SUBMIT_CONFIRM,
        "title": "Confirm your modified request" if for_modify else "Confirm your work-offsite request",
        "facts": facts,
        "confirmLabel": "Submit request",
        "cancelLabel": "Cancel",
    }


def _request_ref(entry: dict) -> str:
    """Stable opaque reference for one subject-bound Workday request."""
    canonical = "\x1f".join(str(entry.get(key, "")) for key in (
        "workdayID", "startDate", "endDate", "status", "subtypeID",
    ))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _rows_from_entries(entries: list[dict], *, include_request_ref: bool = False) -> list[dict]:
    """Map Workday entries to string-only table rows (status display-mapped)."""
    rows: list[dict] = []
    for entry in entries[:MAX_TABLE_ROWS]:
        row = {
            "type": str(entry.get("subtype", "")),
            "start": str(entry.get("startDate", "")),
            "end": str(entry.get("endDate", "")),
            "status": _display_status(str(entry.get("status", ""))),
        }
        if include_request_ref:
            # Not listed as a column, so it is not displayed. The widget echoes
            # every row field on selection and the agent passes this value back.
            row["requestRef"] = _request_ref(entry)
        rows.append(row)
    return rows


_TABLE_COLUMNS = [
    {"key": "type", "label": "Type"},
    {"key": "start", "label": "Start"},
    {"key": "end", "label": "End"},
    {"key": "status", "label": "Status"},
]


def build_view_table(entries: list[dict]) -> dict:
    """Build the read-only view table card."""
    return {
        "kind": "table",
        "cardId": CARD_VIEW,
        "title": "Your work-offsite requests",
        "columns": _TABLE_COLUMNS,
        "rows": _rows_from_entries(entries),
    }


def build_select_table(entries: list[dict], action: str) -> dict:
    """Build the single-select table for cancel/modify.

    Each row carries an undisplayed ``requestRef`` that the widget echoes on
    selection. The write tool re-fetches and matches that stable reference, so
    list reordering cannot target a different request.
    """
    is_modify = action == "modify"
    return {
        "kind": "table",
        "cardId": CARD_SELECT_MODIFY if is_modify else CARD_SELECT_CANCEL,
        "title": "Select a request to modify" if is_modify else "Select a request to cancel",
        "columns": _TABLE_COLUMNS,
        "rows": _rows_from_entries(entries, include_request_ref=True),
        "selectable": True,
        "selectionMode": "single",
    }


def build_cancel_confirm(entry: dict) -> dict:
    """Build the confirm card shown before an irreversible cancel/rescind."""
    start = str(entry.get("startDate", ""))
    end = str(entry.get("endDate", ""))
    return {
        "kind": "confirm",
        "cardId": CARD_CANCEL_CONFIRM,
        "title": "Cancel this request?",
        "facts": [
            {"label": "Dates", "value": _date_range(start, end)},
            {"label": "Reason", "value": str(entry.get("subtype", ""))},
            {"label": "Status", "value": _display_status(str(entry.get("status", "")))},
        ],
        "confirmLabel": "Yes, cancel it",
        "cancelLabel": "Keep it",
    }


def build_reasons_choice() -> dict:
    """Build the choice card listing the valid reasons.

    Selecting an option returns its reason label, which the agent passes to
    ``validate_offsite_request`` to start a submit with the reason prefilled.
    """
    return {
        "kind": "choice",
        "cardId": CARD_REASONS,
        "title": "Which reason fits your request?",
        "options": [
            {"value": label, "label": label, "description": description}
            for (label, _), description in zip(_REASONS, _REASON_DESCRIPTIONS)
        ],
    }


def _trace_event(name: str, attributes: dict) -> None:
    """Record an event on the current OTel span, if one is recording."""
    if not _HAS_OTEL:
        return
    span = _otel_trace.get_current_span()
    if not span or not span.is_recording():
        return
    span.add_event(name, attributes={
        k: v if isinstance(v, (str, bool, int, float)) else str(v)
        for k, v in attributes.items()
    })


def _basic_auth(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def _bearer_headers(token: str) -> dict:
    return {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}


def _cutoff_date(days: int) -> str:
    """Return the YYYY-MM-DD string for N days ago."""
    return (datetime.now().date() - timedelta(days=days)).strftime("%Y-%m-%d")


def _get_employee_id(ctx: AgentRun) -> str | None:
    """Read the subject-bound employee_id from the run context.

    The backend sets this on the session. The tools never accept an employee id
    as an argument, so the subject can't be spoofed by the model or the user.
    """
    request_context = getattr(ctx, "request_context", None)
    if not request_context:
        return None
    return str(request_context.get("employee_id") or "").strip() or None


def _resolve_reason(reason: str) -> tuple[str | None, str | None, str | None]:
    """Resolve a reason from a number, exact label, or partial match.

    Accepts "1", "(1)", "1.", "1)", "#1", "Business Reason", "business", etc.
    Returns (label, subtype_id, error_message).
    """
    reason = (reason or "").strip()
    if not reason:
        valid = " | ".join(f"{i}. {label}" for i, (label, _) in enumerate(_REASONS, 1))
        return None, None, f"A reason is required. Valid options: {valid}"

    num = re.sub(r"^[\(#]?\s*(\d+)\s*[\)\.\,]?$", r"\1", reason)
    if num in ("1", "2", "3"):
        label, sid = _REASONS[int(num) - 1]
        return label, sid, None

    lower = reason.lower()
    for label, sid in _REASONS:
        if lower == label.lower():
            return label, sid, None
    for label, sid in _REASONS:
        if lower in label.lower() or label.lower() in lower:
            return label, sid, None

    valid = " | ".join(f"{i}. {label}" for i, (label, _) in enumerate(_REASONS, 1))
    return None, None, f'Invalid reason "{reason}". Valid options: {valid}'


_DATE_FORMATS: list[str] = [
    "%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%m-%d-%y",
    "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y",
]

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _parse_relative_date(text: str, today: date) -> date | None:
    """Resolve common relative dates against ``today``.

    Handles today, tomorrow, yesterday, "in N days", "next/this <weekday>", and
    "next week". Returns a date, or None if the phrase isn't recognized. A
    standalone agent has to resolve these itself rather than lean on the model.
    """
    t = text.strip().lower()
    if t in ("today", "tonight"):
        return today
    if t == "tomorrow":
        return today + timedelta(days=1)
    if t == "yesterday":
        return today - timedelta(days=1)
    if t == "next week":
        return today + timedelta(days=7)

    m = re.fullmatch(r"in (\d{1,3}) days?", t)
    if m:
        return today + timedelta(days=int(m.group(1)))

    m = re.fullmatch(r"(next|this|coming)\s+(\w+)", t)
    if m and m.group(2) in _WEEKDAYS:
        delta = (_WEEKDAYS[m.group(2)] - today.weekday()) % 7
        return today + timedelta(days=delta or 7)  # always the next occurrence

    if t in _WEEKDAYS:
        delta = (_WEEKDAYS[t] - today.weekday()) % 7
        return today + timedelta(days=delta or 7)

    return None


def _parse_date(raw: str, today: date | None = None) -> tuple[str | None, str | None]:
    """Normalize a user-supplied date to YYYY-MM-DD.

    Accepts explicit formats (2025-11-02, 11/2/25, November 2 2025, ...) and
    relative phrases (tomorrow, next Monday, in 3 days), and tolerates copy-paste
    artifacts like unicode spaces and ordinal suffixes.
    Returns (yyyy_mm_dd, error_message).
    """
    if today is None:
        today = datetime.now().date()

    cleaned = (raw or "").strip()
    if not cleaned:
        return None, "Date is required."

    cleaned = re.sub(r"[\s ​  ]+", " ", cleaned).strip()
    cleaned = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", cleaned, flags=re.IGNORECASE)

    relative = _parse_relative_date(cleaned, today)
    if relative is not None:
        return relative.strftime("%Y-%m-%d"), None

    for candidate in (cleaned, cleaned.title()):
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(candidate, fmt).strftime("%Y-%m-%d"), None
            except ValueError:
                continue

    return None, (
        f'Could not parse "{raw.strip()}" as a date. '
        "Please use a format like 2025-11-02, 11/2/25, November 2 2025, or 'tomorrow'."
    )


def _normalize_write_request(
    start_date: str, end_date: str, reason: str,
) -> tuple[dict | None, str | None]:
    """Re-parse and validate confirmed write inputs. Returns (values, error).

    submit/modify run this even though validate already did — a write tool must
    never assume validate ran first. ``values`` has start, end, reason_label,
    and subtype_id.
    """
    start_iso, err = _parse_date(start_date)
    if err:
        return None, f"Start date: {err}"
    end_iso, err = _parse_date(end_date)
    if err:
        return None, f"End date: {err}"
    reason_label, subtype_id, err = _resolve_reason(reason)
    if err:
        return None, err
    if end_iso < start_iso:
        return None, "The end date can't be before the start date."
    return {
        "start": start_iso, "end": end_iso,
        "reason_label": reason_label, "subtype_id": subtype_id,
    }, None


# Connection access + HTTP layer. Every helper returns (value, error): error is
# None on success, otherwise a clean, employee-facing string. Upstream bodies
# and identifiers stay internal; failures collapse to _WORKDAY_UNAVAILABLE and
# traces contain only the operation and status.
def _get_creds() -> dict:
    """Load the FlexWork connection credentials (cached after the first call)."""
    global _creds_cache
    with _cache_lock:
        if _creds_cache is not None:
            return _creds_cache
        creds = connections.key_value(APP_ID)
        _creds_cache = {
            "bearer_url": creds.get("bearer_url"),
            "username": creds.get("username"),
            "password": creds.get("password"),
            "view_flex_url": creds.get("view_flex_url"),
            "add_flex_url": creds.get("add_flex_url"),
            "cancel_flex_url": creds.get("cancel_flex_url"),
            "rescind_flex_url": creds.get("rescind_flex_url"),
            "get_end_flex_url": creds.get("get_end_flex_url"),
            "workday_user": creds.get("workday_user"),
            "workday_pass": creds.get("workday_pass"),
        }
        return _creds_cache


def _get_basic_auth_header(creds: dict) -> str:
    """Return the cached Basic auth header for Workday RaaS calls."""
    global _basic_auth_cache
    with _cache_lock:
        if _basic_auth_cache is None:
            _basic_auth_cache = _basic_auth(creds["workday_user"], creds["workday_pass"])
        return _basic_auth_cache


async def _get_bearer_token(url: str, username: str, password: str) -> str:
    """Fetch and cache a bearer token from the API Hub auth endpoint.

    The lock guards the cache dict only. On a cold cache two threads may each
    fetch a token; that's harmless — both are valid and the last write wins.
    """
    cache_key = (url, username)
    with _cache_lock:
        cached = _token_cache.get(cache_key)
        if cached and time.time() < cached[1]:
            return cached[0]

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.post(
            url,
            data={"grant_type": "client_credentials"},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": _basic_auth(username, password),
            },
        )
    resp.raise_for_status()
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise ValueError("No access_token in token response")
    expires_in = int(data.get("expires_in", 3600))
    with _cache_lock:
        _token_cache[cache_key] = (token, time.time() + expires_in - _TOKEN_BUFFER)
    return token


async def _acquire_bearer(creds: dict) -> tuple[str | None, str | None]:
    """Get a bearer token. Returns (token, error)."""
    try:
        token = await _get_bearer_token(creds["bearer_url"], creds["username"], creds["password"])
        return token, None
    except Exception:
        _trace_event("auth.error", {})
        return None, _WORKDAY_UNAVAILABLE


async def _fetch_requests(
    creds: dict, employee_id: str, *, start_after: str | None = None,
) -> tuple[list[dict], str | None]:
    """Fetch flex requests from Workday RaaS INT0441. Returns (entries, error)."""
    url = creds["view_flex_url"]
    params: dict[str, str] = {"Worker!Employee_ID": employee_id, "format": "json"}
    if start_after:
        params["flexStartDate"] = start_after
    headers = {"Authorization": _get_basic_auth_header(creds)}

    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(url, params=params, headers=headers)
    except (httpx.HTTPError, httpx.InvalidURL):
        _trace_event("api.error", {"endpoint": "view"})
        return [], _WORKDAY_UNAVAILABLE
    if resp.status_code >= 400:
        _trace_event("api.error", {"endpoint": "view", "status": resp.status_code})
        return [], _WORKDAY_UNAVAILABLE
    try:
        data = resp.json()
    except ValueError:
        _trace_event("api.error", {"endpoint": "view", "error": "unparseable json"})
        return [], _WORKDAY_UNAVAILABLE

    normalized = []
    for e in data.get("Report_Entry", []):
        normalized.append({
            "startDate": e.get("Start_Date", e.get("startDate", "")),
            "endDate": e.get("End_Date", e.get("endDate", "")),
            "status": e.get("Request_Status", e.get("request_status", e.get("Status", e.get("status", "")))),
            "subtype": e.get("Flex_Work_Arrangement_Subtype", e.get("subtype", "")),
            "subtypeID": e.get("Flex_Work_Arrangement_Subtype_ID", e.get("subtypeID", "")),
            "workdayID": e.get("WID", e.get("workdayID", "")),
            "flexComplete": e.get("Flex_Complete", e.get("flexComplete", "0")),
        })
    return normalized, None


async def _api_submit(
    bearer: str, url: str, employee_id: str,
    start_date: str, end_date: str, subtype_id: str,
) -> tuple[dict, str | None]:
    """POST to the API Hub add-flex endpoint. Returns (result, error).

    Upstream error bodies are never returned because they can contain internal
    Workday identifiers or tenant details.
    """
    body = {
        "Auto_Complete": False,
        "EmployeeID": employee_id,
        "startDate": start_date,
        "endDate": end_date,
        "subtype": subtype_id,
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(url, json=body, headers=_bearer_headers(bearer))
    except (httpx.HTTPError, httpx.InvalidURL):
        _trace_event("api.error", {"endpoint": "submit"})
        return {}, _WORKDAY_UNAVAILABLE
    if resp.status_code >= 400:
        _trace_event("api.error", {"endpoint": "submit", "status": resp.status_code})
        return {}, _WORKDAY_UNAVAILABLE
    try:
        result = resp.json()
    except ValueError:
        _trace_event("api.error", {"endpoint": "submit", "error": "unparseable json"})
        return {}, _WORKDAY_UNAVAILABLE

    if result.get("error") or result.get("errors"):
        return {}, _WORKDAY_UNAVAILABLE
    return result, None


async def _api_cancel(
    bearer: str, cancel_url: str, wid: str, comment: str, logged_user: str,
) -> tuple[bool, str | None]:
    """POST to the API Hub cancel/rescind endpoint. Returns (ok, error)."""
    body = {
        "WID": wid, "Comment": comment,
        "version": _WORKDAY_API_VERSION, "appUsed": logged_user,
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(cancel_url, json=body, headers=_bearer_headers(bearer))
    except (httpx.HTTPError, httpx.InvalidURL):
        _trace_event("api.error", {"endpoint": "cancel"})
        return False, _WORKDAY_UNAVAILABLE
    if resp.status_code >= 400:
        _trace_event("api.error", {"endpoint": "cancel", "status": resp.status_code})
        return False, _WORKDAY_UNAVAILABLE
    return True, None


async def _get_end_flex_wid(
    creds: dict, employee_id: str, start_date: str, end_date: str, subtype_id: str,
) -> tuple[str | None, str | None]:
    """Fetch the end-flex WID from INT0441B RaaS (to rescind a completed
    arrangement). Returns (wid, error); wid is None when no record exists."""
    url = creds["get_end_flex_url"]
    params = {
        "Start_Date": start_date, "End_Date": end_date, "subtypeID": subtype_id,
        "Worker!Employee_ID": employee_id, "format": "json",
    }
    headers = {"Authorization": _get_basic_auth_header(creds)}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        _trace_event("api.error", {"endpoint": "end_flex_wid"})
        return None, _WORKDAY_UNAVAILABLE

    entries = data.get("Report_Entry", [])
    if entries and isinstance(entries[0], dict):
        return entries[0].get("workdayID") or entries[0].get("endFlexWID"), None
    return data.get("workdayID") or data.get("endFlexWID"), None


async def _do_cancel_or_rescind(
    bearer: str, creds: dict, employee_id: str, entry: dict, comment: str,
) -> tuple[bool, str | None]:
    """Cancel (In Progress) or rescind (Successfully Completed) a request.

    Returns (ok, error). A completed arrangement whose flex event finished is
    rescinded end-event first, then the original.
    """
    wid = entry["workdayID"]
    status = entry["status"]

    if status == "In Progress":
        return await _api_cancel(bearer, creds["cancel_flex_url"], wid, comment, employee_id)

    if status == "Successfully Completed":
        # Rescinding a finished arrangement is two calls (end event, then the
        # original) and isn't transactional. If the second fails after the first
        # succeeds, a retry self-heals: the re-fetch sees flexComplete cleared and
        # skips straight to the original.
        rescind_url = creds["rescind_flex_url"]
        if str(entry.get("flexComplete", "0")).strip().lower() in ("1", "true"):
            end_wid, err = await _get_end_flex_wid(
                creds, employee_id, entry["startDate"], entry["endDate"], entry["subtypeID"],
            )
            if err:
                return False, err
            if not end_wid:
                return False, (
                    "I couldn't complete the cancellation. Please try again, or open "
                    "an AskHR Inquiry if it keeps happening."
                )
            ok, err = await _api_cancel(bearer, rescind_url, end_wid, comment, employee_id)
            if not ok:
                return False, err
        return await _api_cancel(bearer, rescind_url, wid, comment, employee_id)

    return False, (
        f'This request is "{_display_status(status)}", so it can\'t be canceled. '
        "Only in-progress or approved requests can be canceled."
    )


async def _select_request(
    creds: dict, employee_id: str, request_ref: str, action: str,
) -> tuple[dict | None, str | None]:
    """Re-fetch and match the opaque reference issued in the selection card."""
    entries, err = await _fetch_requests(creds, employee_id, start_after=_cutoff_date(_CANCEL_MODIFY_DAYS))
    if err:
        return None, err
    if not entries:
        return None, f"You have no work-offsite requests to {action}. Would you like to submit one instead?"
    if not isinstance(request_ref, str) or not re.fullmatch(r"[a-f0-9]{64}", request_ref):
        return None, "That request selection is invalid. Please open the request list and choose it again."
    for entry in entries:
        if _request_ref(entry) == request_ref:
            return entry, None
    return None, "That request changed or is no longer available. Please open the request list and choose it again."


# Text fallbacks + business copy. The plain-text summaries are a safety net for
# when a card is dropped; the submit/cancel copy is preserved from the reference
# integration.
def _text_requests_table(entries: list[dict], *, days_back: int) -> str:
    if not entries:
        return f"No work-offsite requests found in the past {days_back} days. Would you like to submit one?"
    lines = [f"**Your work-offsite requests — past {days_back} days**", ""]
    for e in entries[:MAX_TABLE_ROWS]:
        lines.append(f"- {e['subtype']} · {_date_range(e['startDate'], e['endDate'])} · {_display_status(str(e['status']))}")
    return "\n".join(lines)


def _text_select_list(entries: list[dict], action: str) -> str:
    verb = "modify" if action == "modify" else "cancel"
    lines = [f"**Select a request to {verb}**", ""]
    for i, e in enumerate(entries[:MAX_TABLE_ROWS], 1):
        lines.append(f"{i}. {e['subtype']} · {_date_range(e['startDate'], e['endDate'])} · {_display_status(str(e['status']))}")
    return "\n".join(lines)


def _format_submit_success(
    start_date: str, end_date: str, reason_label: str, *, modified: bool = False,
) -> str:
    title = "Work-Offsite Request Modified" if modified else "Work-Offsite Request Submitted"
    lines = [
        f"**{title}**\n",
        f"- **Start Date:** {start_date}",
        f"- **End Date:** {end_date}",
        f"- **Reason:** {reason_label}",
    ]
    lines.append(
        "\nIf you are Grade 14 or below, Workday will route the request to your "
        "manager for approval."
    )
    if not modified:
        lines.append("\nHeads up — I can't check for duplicate entries, so take a quick look at your list if you're unsure.")
    lines.append("\nWould you like to **view your requests** or do something else?")
    return "\n".join(lines)


def _format_reason_list() -> str:
    lines = ["Here are the work-offsite reasons you can choose from:", ""]
    for i, (label, _) in enumerate(_REASONS):
        lines.append(f"{i + 1}. **{label}** — {_REASON_DESCRIPTIONS[i]}")
    lines.append("")
    lines.append(
        "The Remote Flexibility Benefit excludes travel-restricted countries — "
        f"see the [high-risk country list]({HIGH_RISK_COUNTRY_URL})."
    )
    lines.append("")
    lines.append("Please discuss with your manager before submitting.")
    return "\n".join(lines)


# The seven registered tools.
@tool(expected_credentials=[{"app_id": APP_ID, "type": ConnectionType.KEY_VALUE}])
async def view_offsite_requests(
    context: AgentRun,
    days_back: int = 90,
) -> dict:
    """View the employee's work-offsite requests from Workday.

    Returns a read-only table card plus a plain-text summary. This is a read —
    never confirm before calling it.

    Args:
        context: Agent run context (employee_id is read from here).
        days_back: How many days of history to show. Clamped to 1–365.
    Returns:
        {"card": <table ChatBlock>, "text": <markdown>} or {"text": <message>}.
    """
    eid = _get_employee_id(context)
    if not eid:
        return {"text": _NO_EMPLOYEE_ID}

    days_back = max(1, min(days_back, 365))

    creds = _get_creds()
    entries, err = await _fetch_requests(creds, eid, start_after=_cutoff_date(days_back))
    if err:
        return {"text": err}

    if not entries:
        return {"text": _text_requests_table(entries, days_back=days_back)}
    return {
        "card": build_view_table(entries),
        "text": _text_requests_table(entries, days_back=days_back),
    }


@tool(expected_credentials=[{"app_id": APP_ID, "type": ConnectionType.KEY_VALUE}])
async def list_offsite_requests_for_action(
    context: AgentRun,
    action: str,
) -> dict:
    """List requests so the employee can pick one to cancel or modify.

    Uses a fixed 180-day window and returns a stable hidden request reference on
    each row. Pass the selected row's ``requestRef`` to the write tool.

    Args:
        context: Agent run context.
        action: Either "cancel" or "modify".
    Returns:
        {"card": <select table ChatBlock>, "text": <markdown>} or {"text": <message>}.
    """
    action = (action or "").strip().lower()
    if action not in ("cancel", "modify"):
        return {"text": "Would you like to cancel a request or modify one?"}

    eid = _get_employee_id(context)
    if not eid:
        return {"text": _NO_EMPLOYEE_ID}

    creds = _get_creds()
    entries, err = await _fetch_requests(creds, eid, start_after=_cutoff_date(_CANCEL_MODIFY_DAYS))
    if err:
        return {"text": err}
    if not entries:
        return {"text": f"You have no work-offsite requests to {action}. Would you like to submit one instead?"}

    return {
        "card": build_select_table(entries, action),
        "text": _text_select_list(entries, action),
    }


@tool
async def validate_offsite_request(
    start_date: str | None = None,
    end_date: str | None = None,
    reason: str | None = None,
    for_modify: bool = False,
    replacing_start: str | None = None,
    replacing_end: str | None = None,
    replacing_reason: str | None = None,
) -> dict:
    """Validate a work-offsite request in code and return the next card.

    Call this to drive the submit/modify input phase:
    * No details yet → a blank submit form (ok=false).
    * Partial or invalid details → the form prefilled with what was understood,
      plus an error (ok=false).
    * Complete and valid → a confirmation card and exact ``write_args`` for the
      matching submit/modify tool (ok=true).

    Dates are parsed deterministically (including relative dates like
    "tomorrow"); the reason resolves to a fixed subtype in code. A single date
    fills both ends. Set ``for_modify=true`` when validating replacement details
    for a modify so the confirm card states the request is replaced.

    Args:
        start_date: Start date in any common or relative format. Omit if unknown.
        end_date: End date. Omit for a single-day request or if unknown.
        reason: Reason label or number (1, 2, or 3). Omit if unknown.
        for_modify: True when validating replacement details for a modify.
        replacing_start: Modify only — the start date of the request being
            replaced (pass the selected row's start so the confirm names it).
        replacing_end: Modify only — the end date of the request being replaced.
        replacing_reason: Modify only — the reason of the request being replaced.

    Returns:
        {"ok": bool, "card": <ChatBlock>, "text": str, "write_args"?: {...}, "error"?: str}
    """
    today = datetime.now().date()
    today_iso = today.strftime("%Y-%m-%d")

    everything_empty = not (start_date or "").strip() and not (end_date or "").strip() and not (reason or "").strip()
    if everything_empty:
        return {
            "ok": False,
            "card": build_submit_form(today_iso),
            "text": "Please choose your dates and reason.",
        }

    errors: list[str] = []

    start_iso, start_err = (None, None)
    if (start_date or "").strip():
        start_iso, start_err = _parse_date(start_date, today)
        if start_err:
            errors.append(start_err)

    end_iso, end_err = (None, None)
    if (end_date or "").strip():
        end_iso, end_err = _parse_date(end_date, today)
        if end_err:
            errors.append(end_err)

    # A single supplied date fills both ends.
    if start_iso and not end_iso and not (end_date or "").strip():
        end_iso = start_iso
    if end_iso and not start_iso and not (start_date or "").strip():
        start_iso = end_iso

    reason_label, subtype_id, reason_err = (None, None, None)
    if (reason or "").strip():
        reason_label, subtype_id, reason_err = _resolve_reason(reason)
        if reason_err:
            errors.append(reason_err)

    if start_iso and end_iso and not start_err and not end_err and end_iso < start_iso:
        errors.append("The end date can't be before the start date.")

    have_dates = bool(start_iso and end_iso and not start_err and not end_err and end_iso >= start_iso)
    have_reason = bool(reason_label)

    if errors or not have_dates or not have_reason:
        card = build_submit_form(
            today_iso,
            start_value=start_iso if (start_iso and not start_err) else "",
            end_value=end_iso if (end_iso and not end_err) else "",
            reason_value=reason_label or "",
        )
        message = "; ".join(errors) if errors else "Please complete the remaining details below."
        return {"ok": False, "error": message, "card": card, "text": message}

    replacing = None
    if for_modify and (replacing_start or "").strip() and (replacing_end or "").strip():
        replacing = _date_range(replacing_start.strip(), replacing_end.strip())
        if (replacing_reason or "").strip():
            replacing = f"{replacing} ({replacing_reason.strip()})"

    write_args = (
        {
            "new_start_date": start_iso,
            "new_end_date": end_iso,
            "new_reason": reason_label,
        }
        if for_modify
        else {
            "start_date": start_iso,
            "end_date": end_iso,
            "reason": reason_label,
        }
    )
    return {
        "ok": True,
        "write_args": write_args,
        "card": build_submit_confirm(start_iso, end_iso, reason_label, for_modify=for_modify, replacing=replacing),
        "text": f"Please confirm: {start_iso} to {end_iso}, {reason_label}.",
    }


@tool(expected_credentials=[{"app_id": APP_ID, "type": ConnectionType.KEY_VALUE}])
async def submit_offsite_request(
    context: AgentRun,
    start_date: str,
    end_date: str,
    reason: str,
    confirmed: bool = False,
) -> dict:
    """Submit a new work-offsite request to Workday (a write).

    Call this with ``confirmed=true`` only after the employee confirms the
    confirmation card. Pass ``write_args`` from ``validate_offsite_request``
    unchanged so the submitted request matches what was confirmed. On success, emit
    ``report_action`` with action_type="work_offsite_submit".

    Args:
        context: Agent run context.
        start_date: Confirmed start date (normalized ISO from validate).
        end_date: Confirmed end date (normalized ISO from validate).
        reason: Confirmed reason label or number.
        confirmed: True only after an explicit employee confirmation.
    Returns:
        A result with ``ok``, ``text``, and ``action_type`` on success.
    """
    eid = _get_employee_id(context)
    if not eid:
        return {"ok": False, "text": _NO_EMPLOYEE_ID}
    if not confirmed:
        return {"ok": False, "text": "Please confirm the request before I submit it."}

    values, err = _normalize_write_request(start_date, end_date, reason)
    if err:
        return {"ok": False, "text": err}

    creds = _get_creds()
    bearer, auth_err = await _acquire_bearer(creds)
    if auth_err:
        return {"ok": False, "text": auth_err}

    _trace_event("submit.start", {})

    result, api_err = await _api_submit(
        bearer, creds["add_flex_url"], eid, values["start"], values["end"], values["subtype_id"],
    )
    if api_err:
        return {"ok": False, "text": api_err}
    return {
        "ok": True,
        "action_type": "work_offsite_submit",
        "text": _format_submit_success(values["start"], values["end"], values["reason_label"]),
    }


@tool(expected_credentials=[{"app_id": APP_ID, "type": ConnectionType.KEY_VALUE}])
async def cancel_offsite_request(
    context: AgentRun,
    request_ref: str,
    confirmed: bool = False,
) -> dict:
    """Cancel or rescind a work-offsite request (a write, gated by confirmation).

    Pass the selected row's opaque ``requestRef`` from the cancel list.
    With ``confirmed=false`` this returns a confirmation card describing the
    request; call again with ``confirmed=true`` only after the employee confirms.
    In Progress requests are canceled; Successfully Completed requests are
    rescinded (end-flex event first when applicable). On success, emit
    ``report_action`` with action_type="work_offsite_cancel".

    Args:
        context: Agent run context.
        request_ref: Opaque reference from the selected card row.
        confirmed: False to preview + confirm; True to execute.
    Returns:
        {"card": <confirm ChatBlock>, "text": str} before confirm, else {"text": ...}.
    """
    eid = _get_employee_id(context)
    if not eid:
        return {"ok": False, "text": _NO_EMPLOYEE_ID}

    creds = _get_creds()
    selected, sel_err = await _select_request(creds, eid, request_ref, "cancel")
    if sel_err:
        return {"ok": False, "text": sel_err}

    if not confirmed:
        return {
            "card": build_cancel_confirm(selected),
            "text": f"Cancel the {selected['subtype']} request ({selected['startDate']} to {selected['endDate']})?",
        }

    bearer, auth_err = await _acquire_bearer(creds)
    if auth_err:
        return {"ok": False, "text": auth_err}

    _trace_event("cancel.start", {"status": selected["status"]})

    ok, err = await _do_cancel_or_rescind(
        bearer, creds, eid, selected, comment="Canceled by HR Virtual Assistant",
    )
    if not ok:
        return {"ok": False, "text": err}

    action = "rescinded" if selected["status"] == "Successfully Completed" else "canceled"
    return {"ok": True, "action_type": "work_offsite_cancel", "text": (
        f"**Request {action.title()}**\n\n"
        f"Your work-offsite request has been successfully {action}.\n\n"
        f"- **Dates:** {selected['startDate']} to {selected['endDate']}\n"
        f"- **Type:** {selected['subtype']}\n\n"
        f"Would you like to **view your updated requests** or do something else?"
    )}


@tool(expected_credentials=[{"app_id": APP_ID, "type": ConnectionType.KEY_VALUE}])
async def modify_offsite_request(
    context: AgentRun,
    request_ref: str,
    new_start_date: str,
    new_end_date: str,
    new_reason: str,
    confirmed: bool = False,
) -> dict:
    """Modify a work-offsite request — rescind the old one and submit a new one.

    Workday has no native modify: this cancels/rescinds the selected request
    (per its status) then submits a new request with the new details. Only call
    with ``confirmed=true`` after the employee confirms the modify confirmation
    card (from ``validate_offsite_request`` with for_modify=true). Pass its
    ``write_args`` unchanged. If the resubmit fails after the undo, the reply says the
    old one was removed and to resubmit manually. On success, emit
    ``report_action`` with action_type="work_offsite_modify".

    Args:
        context: Agent run context.
        request_ref: Opaque reference from the selected card row.
        new_start_date: New start date (normalized ISO from validate).
        new_end_date: New end date (normalized ISO from validate).
        new_reason: New reason label or number.
        confirmed: True only after an explicit employee confirmation.
    Returns:
        A result with ``ok``, ``text``, and ``action_type`` on success; a
        partial replacement failure also carries ``partial=true``.
    """
    eid = _get_employee_id(context)
    if not eid:
        return {"ok": False, "text": _NO_EMPLOYEE_ID}
    if not confirmed:
        return {"ok": False, "text": "Please confirm the change before I modify the request."}

    values, err = _normalize_write_request(new_start_date, new_end_date, new_reason)
    if err:
        return {"ok": False, "text": err}

    creds = _get_creds()
    selected, sel_err = await _select_request(creds, eid, request_ref, "modify")
    if sel_err:
        return {"ok": False, "text": sel_err}

    bearer, auth_err = await _acquire_bearer(creds)
    if auth_err:
        return {"ok": False, "text": auth_err}

    _trace_event("modify.rescind", {"old_status": selected["status"]})

    ok, rescind_err = await _do_cancel_or_rescind(
        bearer, creds, eid, selected,
        comment="Modify offsite request. Canceled by HR Virtual Assistant",
    )
    if not ok:
        return {"ok": False, "text": rescind_err}

    _trace_event("modify.submit", {})

    result, api_err = await _api_submit(
        bearer, creds["add_flex_url"], eid, values["start"], values["end"], values["subtype_id"],
    )
    if api_err:
        return {"ok": False, "partial": True, "text": (
            f"The old request ({selected['startDate']} to {selected['endDate']}) "
            f"was rescinded, but the new request failed:\n\n{api_err}\n\n"
            f"Please submit a new request manually."
        )}

    return {
        "ok": True,
        "action_type": "work_offsite_modify",
        "text": _format_submit_success(
            values["start"], values["end"], values["reason_label"], modified=True,
        ),
    }


@tool
async def get_offsite_reasons() -> dict:
    """List the valid work-offsite reasons.

    Use when the employee asks what reasons are available, or to help them pick
    one before submitting. Returns a choice card the employee can select from,
    plus a text summary with the high-risk-country link and manager note. This
    is a read — never confirm before calling it.

    Returns:
        {"card": <choice ChatBlock>, "text": <reason list>}.
    """
    return {"card": build_reasons_choice(), "text": _format_reason_list()}
