# -*- coding: utf-8 -*-
"""EVL tools for the watsonx Orchestrate agent (single self-contained module).

Generates and emails verification letters. Every country/letter difference is a
config row in the askhr Mongo ``agent_config`` collection (fetched via the
backend ``get-config`` action); the tool holds only two generic builders, so
adding a letter or a country is a config edit, not a code change.

Design (see docs/evl-rebuild/spec.md):

* Sensitive-data firewall — salary / leave data may pass through the tool's local
  memory but is NEVER returned to the agent/LLM. The firewall rests on one
  invariant: sensitive values never appear in a return value. ``generate``
  re-fetches from Workday rather than trusting anything passed back through the
  agent. There is no redaction layer because the tool never relays a Workday or
  microservice response body to the LLM.
* ``employee_id`` and ``wd_access_token`` are subject-bound from the run context,
  never tool arguments — no QA/test override, no OBO, no Graph/PDF tokens.
* Config model — each ``data.sets`` entry declares a ``scope`` (regions and/or
  countries) and its ``templates``; an employee is offered the union of
  templates from every set whose scope matches their country or region. Each
  template carries a ``delivery`` block interpreted by one of two builders:
  ``dynamic_body`` (fill {placeholders} into a Dynamic-Letter body) or
  ``structured_fields`` (map field_map values into the microservice's own
  template).
* Per-set report override — a set may declare ``report`` ``{path,
  employee_param?}`` naming the Workday RaaS report its data comes from (e.g.
  the Netherlands-specific report). The path is host-relative (no scheme, no
  ``//`` authority, no ``..`` climb), always joined to the credentialed
  ``report_base_url`` — config can choose WHICH report on that host, never the
  host, so a config row can't redirect the employee's Workday bearer elsewhere.
  The default INT0445 record is fetched first (identity + country), then each
  matched set's report is fetched and overlaid onto it, more-specific sets
  winning overlaps; identity/delivery fields (employeeID, email) stay pinned to
  the default record, so config can shape letter content and eligibility but
  never who the letter is addressed or delivered to. Fails closed: a malformed
  or unfetchable declared report fails the request rather than building that
  set's letters from generic data.

The single ``evl_tools`` entry point routes on ``process``:

* ``get_options`` — read the employee's Workday record once, resolve their
  country → get-config → offer every letter scoped to their country/region,
  marking any they are not currently eligible for (the letter is still shown;
  selecting it explains why and re-offers the menu).
* ``select_letter`` — for a gated letter, re-check eligibility against a fresh
  Workday read: on success return the add-a-recipient card; on ineligibility
  return the reason plus the re-offered menu. An ungated letter needs no read.
* ``generate`` — with ``confirmed=false`` return the confirmation card; with
  ``confirmed=true`` RE-FETCH Workday, re-validate, build the letter, and send.
"""
from __future__ import annotations

import asyncio
import base64
import html
import logging
import re
import threading
import time
from datetime import date, datetime, timedelta
from typing import NamedTuple

import httpx
from ibm_watsonx_orchestrate.agent_builder.tools import tool
from ibm_watsonx_orchestrate.agent_builder.connections import ConnectionType
from ibm_watsonx_orchestrate.run import connections
from ibm_watsonx_orchestrate.run.context import AgentRun

logger = logging.getLogger("evl_tools")

APP_ID = "EVLConnection"
_DEFAULT_EMPLOYEE_PARAM = "Employee!Employee_ID"  # INT0445's RaaS prompt qualifier
_WORKDAY_TIMEOUT = 20.0
_CONFIG_TIMEOUT = 10.0
_RESOLVE_TIMEOUT = 10.0
_SEND_TIMEOUT = 45.0
_TOKEN_BUFFER = 60  # refresh the API Hub token this many seconds before expiry

# Stable card IDs — a card's submission comes back tied to its cardId.
CARD_SELECT = "evl-select"
CARD_RECIPIENT = "evl-recipient"
CARD_CONFIRM = "evl-confirm"

# Hardcoded service values preserved from the legacy tool (discovery 01 §3.9).
# These are fixed for every US/Canada letter, so they stay in code; only the
# per-letter body/subject/filename live in config.
FROM_ADDR = "hr-virtualassistant@fiserv.com"
ASKHR_EMAIL_DEFAULT = "askhr@fiserv.com"
ADDRESS_BLOCK = "600 Vel R. Phillips Avenue<br /> Milwaukee, WI, 53203<br /> www.fiserv.com"
SALUTATION_BLOCK = "To Whom It May Concern:"
SIGNATURE_BLOCK = "Sincerely, <br /> Fiserv AskHR Team"
# Every letter is delivered by SecureMail, so every subject is prefixed with it in
# code — config `subject` copy stays clean and the prefix can't be forgotten.
_SUBJECT_PREFIX = "SecureMail - "
_UNMONITORED_NOTICE = (
    "<p style='color: red;'>Please note that this email is sent from an "
    "unmonitored inbox, and replies to this address will not be received.</p>"
)

# Exact user-facing strings. Named so copy changes are one-line and tests assert
# on them. Every error path returns one of these (or a config-authored onFail
# reason) — never a raw Workday or microservice response body.
COUNTRY_UNAVAILABLE = (
    "This service is currently available for employees in the United States, "
    "Canada, and the Netherlands. For a verification letter in your country, "
    "please submit a C360 ticket."
)
_NO_EMPLOYEE_ID = "I couldn't determine your employee ID. Please reopen AskHR and try again."
_WORKDAY_AUTH_MISSING = "I couldn't verify your session. Please reopen AskHR and try again."
_WORKDAY_UNAVAILABLE = "I couldn't reach Workday just now. Please try again in a moment."
_EMPLOYEE_NOT_FOUND = "I couldn't find your employee record in Workday. Please contact AskHR."
_CONFIG_UNAVAILABLE = "I couldn't load the verification-letter options just now. Please try again in a moment."
_NO_LETTERS_AVAILABLE = "There are no verification letters available for your profile right now."
_LETTER_NOT_FOUND = "I couldn't find that letter for your profile. Ask me to show your options."
# Shown in the menu beside a letter the employee can't currently request; the
# specific reason is given only if they select it (select_letter relays the
# reason and re-offers the menu).
_INELIGIBLE_HINT = "Not available for your profile — select to see why."
_NO_RECIPIENT = (
    "I couldn't find a valid registered work email for your letter. Please contact AskHR to correct your work email."
)
_INVALID_RECIPIENT = (
    "That doesn't look like a valid email address. Please provide a valid recipient "
    "email, or skip adding one."
)
_SEND_REQUEST_FAILED = "I couldn't send the letter just now. Please try again in a moment."
_LANGUAGE_REQUIRED = "Please choose one of the available languages before continuing."
_UNSUPPORTED_PROCESS = "process must be get_options, select_letter, or generate, got {process!r}"

_EMAIL_RE = re.compile(r"^[^@\s,;<>]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,63}$")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")
_UNSAFE_FILENAME_RE = re.compile(r"[\\/:*?\"<>|]+")


# --- Config model + eligibility evaluator (pure — no Workday, no HTTP) --------

# Country aliases → the ISO-3 keys config scopes use. Workday's countryISOCode is
# ISO-3 (USA/CAN/NLD); ISO-2 and country names are tolerated, and any unknown
# 3-letter code passes through so a new country needs only a config edit.
_COUNTRY_ALIASES = {
    "US": "USA", "UNITED STATES": "USA", "UNITED STATES OF AMERICA": "USA",
    "CA": "CAN", "CANADA": "CAN",
    "NL": "NLD", "NETHERLANDS": "NLD", "THE NETHERLANDS": "NLD",
}

# leaveEndDate is ISO YYYY-MM-DD (owner-confirmed); other formats are tolerated,
# ISO first so an ambiguous string parses the canonical way.
_DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y"]

_LANGUAGE_LABELS = {"en": "English", "nl": "Nederlands"}

# field_equals compares Workday's serialization against the config value; these
# let 1/true/Yes/bool all read as the same truthy/falsy token.
_TRUTHY = {"1", "true", "yes", "y"}
_FALSY = {"0", "false", "no", "n", ""}


def parse_date(raw) -> "date | None":
    """Parse a Workday date value to a ``date``, or None if unparseable. Fails
    closed: an unparseable/empty value returns None, treated as "not eligible"."""
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = str(raw).strip().split("T")[0].strip()  # tolerate an ISO datetime suffix
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _norm_eq(value) -> str:
    text = str(value).strip().lower()
    if text in _TRUTHY:
        return "1"
    if text in _FALSY:
        return "0"
    return text


def is_eligible(rule: "dict | None", wd: dict, now: date) -> "tuple[bool, str | None]":
    """Evaluate one config eligibility rule against the Workday record. Returns
    ``(eligible, reason_if_not)``; the reason is the rule's ``onFail`` copy.
    Missing keys or an unknown rule kind FAIL CLOSED so a bad config row can
    neither expose a letter nor crash the tool."""
    if rule is None:
        return True, None
    if not isinstance(rule, dict):
        return False, None
    kind = rule.get("rule")
    field = rule.get("field")
    on_fail = rule.get("onFail") if isinstance(rule.get("onFail"), str) else None
    if kind == "field_equals":
        if not isinstance(field, str) or not field or field not in wd or "value" not in rule:
            return False, on_fail
        return (True, None) if _norm_eq(wd.get(field)) == _norm_eq(rule.get("value")) else (False, on_fail)
    if kind == "recent_event":
        if not isinstance(field, str) or not field or field not in wd:
            return False, on_fail
        event = parse_date(wd.get(field))
        if event is None:
            return False, on_fail
        try:
            within = int(rule.get("withinDays"))
        except (TypeError, ValueError):
            return False, on_fail
        if within < 0 or within > 3650:
            return False, on_fail
        # Within [now - withinDays, now]: too old OR still in the future is
        # ineligible (preserves the legacy return-from-leave window).
        if now - timedelta(days=within) <= event <= now:
            return True, None
        return False, on_fail
    return False, on_fail or "Unknown eligibility rule."


def normalize_country(iso: str, name: str) -> "str | None":
    """Map a Workday countryISOCode / country name to an ISO-3 config key, or
    None if it can't be resolved. Any unknown 3-letter code passes through, so
    adding a supported country is a config scope edit, not a code change."""
    for candidate in (iso, name):
        key = str(candidate or "").strip().upper()
        if key in _COUNTRY_ALIASES:
            return _COUNTRY_ALIASES[key]
        if len(key) == 3 and key.isalpha():
            return key
    return None


def _matched_sets(config_data: dict, country: "str | None", region: str) -> "list[tuple[dict, int]]":
    """Every set whose scope matches the employee's country or region, as
    ``(definition, specificity)`` — country match (2) beats region match (1)."""
    if not isinstance(config_data, dict):
        return []
    sets = config_data.get("sets")
    if not isinstance(sets, dict):
        return []
    matched = []
    for definition in sets.values():
        if not isinstance(definition, dict):
            continue
        scope = definition.get("scope")
        if not isinstance(scope, dict):
            continue
        countries = scope.get("countries") or []
        regions = scope.get("regions") or []
        if not isinstance(countries, list) or not isinstance(regions, list):
            continue
        by_country = bool(country and country in countries)
        by_region = bool(region and region in regions)
        if not (by_country or by_region):
            continue
        matched.append((definition, 2 if by_country else 1))
    return matched


_REPORT_INVALID = object()
# Strict allowlist: a host-relative RaaS path only. Anything outside it (spaces,
# control chars, ...) is rejected — a stray newline in a config row would raise
# httpx.InvalidURL, which is NOT an httpx.HTTPError, and crash instead of
# returning the canned fail-closed message.
_REPORT_PATH_RE = re.compile(r"^/[A-Za-z0-9/_\-.%~]+$")


def set_report(definition: dict) -> "tuple[str, str] | None | object":
    """A set's declared report override: None if absent, ``(path,
    employee_param)`` if valid, ``_REPORT_INVALID`` if malformed. The path must
    be host-relative (no scheme, no ``//`` authority) — it is only ever joined
    to the credentialed ``report_base_url``, so config selects a report, never a
    host."""
    report = definition.get("report")
    if report is None:
        return None
    if not isinstance(report, dict):
        return _REPORT_INVALID
    path = report.get("path")
    # `//` would be an authority prefix; a `..` segment would climb out of the
    # RaaS namespace on the credentialed host (httpx normalizes dot-segments).
    if (not isinstance(path, str) or path.startswith("//")
            or not _REPORT_PATH_RE.fullmatch(path)
            or ".." in path.split("/")):
        return _REPORT_INVALID
    param = report.get("employee_param")
    if param is None:  # absent or explicit null both mean the default
        return path, _DEFAULT_EMPLOYEE_PARAM
    # "format" is reserved: as the employee param it would collide with the
    # format=json query key and silently drop the subject filter.
    if not isinstance(param, str) or not param.strip() or param.strip() == "format":
        return _REPORT_INVALID
    return path, param.strip()


def applicable_templates(config_data: dict, country: "str | None", region: str) -> list:
    """Templates the employee can see: the union across every set whose scope
    matches their country or region, deduped by template id (a country-scoped
    set beats a region-scoped one). Templates missing an id or label are skipped
    — a malformed row is never surfaced."""
    chosen: "dict[str, tuple[dict, int]]" = {}  # id -> (template, specificity)
    for definition, specificity in _matched_sets(config_data, country, region):
        templates = definition.get("templates") or []
        languages = definition.get("languages") or ["en"]
        if not isinstance(templates, list) or not isinstance(languages, list):
            continue
        languages = [str(lang).strip().lower() for lang in languages if isinstance(lang, str) and lang.strip()]
        if not languages:
            continue
        for tpl in templates:
            if not isinstance(tpl, dict):
                continue
            tid, label = tpl.get("id"), tpl.get("label")
            if not tid or not label:
                continue
            if tid in chosen and chosen[tid][1] >= specificity:
                continue
            annotated = dict(tpl)
            annotated["_languages"] = languages
            annotated["_requires_encryption"] = bool(definition.get("requires_encryption"))
            chosen[tid] = (annotated, specificity)
    return [tpl for tpl, _ in chosen.values()]


def _find_template(templates: list, letter_type: str) -> "dict | None":
    """Find the chosen template by its label (what the selection card submits) or
    its stable id."""
    wanted = str(letter_type or "").strip()
    if not wanted:
        return None
    for tpl in templates:
        if wanted == str(tpl.get("label")) or wanted == str(tpl.get("id")):
            return tpl
    return None


def _language_label(language: str) -> str:
    return _LANGUAGE_LABELS.get(str(language or "").strip().lower(), "")


def _resolve_language(template: dict, language: str) -> "tuple[str | None, str | None]":
    languages = template.get("_languages")
    if not isinstance(languages, list) or not languages:
        return None, _CONFIG_UNAVAILABLE
    requested = str(language or "").strip().lower()
    if len(languages) == 1:
        return languages[0], None
    if requested not in languages:
        return None, _LANGUAGE_REQUIRED
    return requested, None


def _delivery_config_valid(template: dict) -> bool:
    delivery = template.get("delivery")
    if not isinstance(delivery, dict):
        return False
    if delivery.get("kind") == "dynamic_body":
        required = ("templatename", "subject", "filename", "body")
        return all(isinstance(delivery.get(field), str) and delivery.get(field).strip() for field in required)
    if delivery.get("kind") != "structured_fields":
        return False
    field_map = delivery.get("field_map")
    field_defaults = delivery.get("field_defaults")
    if not isinstance(field_map, dict) or not field_map or not isinstance(field_defaults, dict):
        return False
    if any(not isinstance(key, str) or not key or not isinstance(value, str) or not value
           for key, value in field_map.items()):
        return False
    languages = template.get("_languages")
    if not isinstance(languages, list) or not languages:
        return False
    for field in ("templatename", "display_name"):
        configured = delivery.get(field)
        if isinstance(configured, str) and configured.strip():
            continue
        if not isinstance(configured, dict) or any(
            not isinstance(configured.get(language), str) or not configured.get(language).strip()
            for language in languages
        ):
            return False
    return True


# --- Context (subject binding) ------------------------------------------------
def _ctx(context: AgentRun, key: str) -> "str | None":
    """Read a context variable, tolerating IBM's nested request_context bug and a
    sensitive field arriving under a leading underscore (e.g. ``_wd_access_token``
    when the registry marks it sensitive)."""
    if not context:
        return None
    req_ctx = getattr(context, "request_context", None)
    if req_ctx is None:
        return None
    for source in (req_ctx, req_ctx.get("request_context") if hasattr(req_ctx, "get") else None):
        if source is None or not hasattr(source, "get"):
            continue
        for name in (key, "_" + key):
            val = source.get(name)
            if isinstance(val, str):
                val = val.strip()
            if val:
                return val
    return None


def _get_employee_id(context: AgentRun) -> "str | None":
    """The subject-bound employee_id — ALWAYS from the run context, never a tool
    argument, so the subject can't be spoofed."""
    return _ctx(context, "employee_id")


# --- Connection + HTTP layer --------------------------------------------------
_creds_lock = threading.Lock()
_creds_cache: "dict | None" = None


def _get_creds() -> dict:
    """Load EVLConnection credentials (cached per process — best-effort; WxO tool
    processes are short-lived, so a miss just re-reads). No azure/OBO/graph/pdf
    keys: the tool carries no secrets for generation. ``config_api_key`` is an
    optional gateway credential; ``evl_token_url`` + ``evl_username`` /
    ``evl_password`` are the API Hub client-credentials for the microservice."""
    global _creds_cache
    with _creds_lock:
        if _creds_cache is not None:
            return _creds_cache
        kv = connections.key_value(APP_ID)
        report_base = (kv.get("report_base_url") or "").rstrip("/")
        report_path = kv.get("report_path") or ""
        employee_details_url = (
            report_base + report_path if report_path.startswith("/")
            else report_base + "/" + report_path
        )
        _creds_cache = {
            "report_base_url": report_base,
            "employee_details_url": employee_details_url,
            "dynamic_evl_url": kv.get("dynamic_evl_url"),
            "config_url": kv.get("config_url"),
            "resolve_token_url": kv.get("resolve_token_url"),
            "config_api_key": kv.get("config_api_key"),
            "evl_token_url": kv.get("evl_token_url"),
            "evl_username": kv.get("evl_username"),
            "evl_password": kv.get("evl_password"),
        }
        return _creds_cache


def _bearer(token: str) -> str:
    t = str(token).strip()
    return t if t.lower().startswith("bearer ") else f"Bearer {t}"


def _basic_auth(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


async def _get(url, params, headers):
    async with httpx.AsyncClient(timeout=_WORKDAY_TIMEOUT) as client:
        return await client.get(url, params=params, headers=headers)


async def _post(url, params, json_body, headers, timeout=_WORKDAY_TIMEOUT):
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.post(url, params=params, json=json_body, headers=headers)


async def _post_form(url, data, auth_header):
    async with httpx.AsyncClient(timeout=_WORKDAY_TIMEOUT) as client:
        return await client.post(url, data=data, headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": auth_header,
        })


# API Hub (Apigee) token for the microservice — a shared service credential (same
# for every user), cached per process behind a lock.
_token_lock = threading.Lock()
_evl_token_cache: "dict | None" = None  # {"value": str, "exp": float}


async def _get_evl_token(force_refresh: bool = False) -> "tuple[str | None, str | None]":
    """Fetch + cache the API Hub client-credentials token used to call the EVL
    microservice through Apigee. Returns (token, error). This is the gateway
    caller credential — not the employee's wd_access_token and not a Graph/PDF
    token (the microservice mints those itself)."""
    global _evl_token_cache
    creds = _get_creds()
    url = creds.get("evl_token_url")
    username = creds.get("evl_username")
    password = creds.get("evl_password")
    if not url or not username or not password:
        return None, _SEND_REQUEST_FAILED
    with _token_lock:
        if not force_refresh and _evl_token_cache and time.time() < _evl_token_cache["exp"]:
            return _evl_token_cache["value"], None
    try:
        resp = await _post_form(
            url, {"grant_type": "client_credentials"},
            _basic_auth(username, password),
        )
    except (httpx.HTTPError, httpx.InvalidURL):
        logger.warning("evl api-hub token request failed to connect")
        return None, _SEND_REQUEST_FAILED
    if resp.status_code >= 400:
        logger.warning("evl api-hub token request rejected: status=%s", resp.status_code)
        return None, _SEND_REQUEST_FAILED
    try:
        data = resp.json()
    except ValueError:
        return None, _SEND_REQUEST_FAILED
    token = data.get("access_token")
    if not token:
        return None, _SEND_REQUEST_FAILED
    try:
        expires_in = int(data.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600
    with _token_lock:
        _evl_token_cache = {"value": token, "exp": time.time() + expires_in - _TOKEN_BUFFER}
    return token, None


def _auth_context(context: AgentRun) -> "tuple[str | None, str | None]":
    """Return (auth_header, None) from the in-context wd_access_token, or
    (None, error). The tool uses the platform-delivered bearer first; its
    resolver fallback delegates any OBO exchange back to AskHR."""
    token = _ctx(context, "wd_access_token")
    if token:
        return _bearer(token), None
    return None, _WORKDAY_AUTH_MISSING


async def _resolve_workday_token(context: AgentRun) -> "tuple[str | None, str | None]":
    """Resolve the session-bound Workday token from AskHR.

    The request intentionally contains only ``sessionId``. AskHR selects the
    employee from its server-side session; neither the model nor this tool can
    choose a different subject.
    """
    session_id = _ctx(context, "session_id")
    creds = _get_creds()
    url = creds.get("resolve_token_url")
    if not session_id or not url:
        return None, _WORKDAY_AUTH_MISSING
    headers = {"Content-Type": "application/json"}
    key = creds.get("config_api_key")
    if key:
        headers["x-api-key"] = key
    try:
        resp = await _post(url, None, {"sessionId": session_id}, headers, timeout=_RESOLVE_TIMEOUT)
    except (httpx.HTTPError, httpx.InvalidURL):
        logger.warning("evl resolve-token failed to connect")
        return None, _WORKDAY_AUTH_MISSING
    if resp.status_code >= 400:
        logger.warning("evl resolve-token rejected: status=%s", resp.status_code)
        return None, _WORKDAY_AUTH_MISSING
    try:
        token = resp.json().get("workdayToken")
    except (AttributeError, ValueError):
        return None, _WORKDAY_AUTH_MISSING
    if not isinstance(token, str) or not token.strip():
        return None, _WORKDAY_AUTH_MISSING
    return _bearer(token), None


async def _recover_workday_auth(
    context: AgentRun,
    auth_state: dict,
    rejected_auth: "str | None" = None,
) -> "tuple[str | None, str | None]":
    """Resolve at most once for a group of parallel Workday reads."""
    recovery_lock = auth_state.get("recovery_lock")
    if recovery_lock is None:
        recovery_lock = asyncio.Lock()
        auth_state["recovery_lock"] = recovery_lock

    async with recovery_lock:
        current_auth = auth_state.get("authorization")
        if current_auth and (rejected_auth is None or current_auth != rejected_auth):
            return current_auth, None
        if auth_state.get("recovery_attempted"):
            return None, auth_state.get("recovery_error") or _WORKDAY_AUTH_MISSING

        auth_state["recovery_attempted"] = True
        recovered_auth, error = await _resolve_workday_token(context)
        if recovered_auth and not error:
            auth_state["authorization"] = recovered_auth
        else:
            auth_state["recovery_error"] = error or _WORKDAY_AUTH_MISSING
        return recovered_auth, error


# --- Workday employee-details fetch + normalization (from discovery 01) --------
def _entries(data) -> list:
    if not isinstance(data, dict):
        return []
    entries = data.get("Report_Entry", [])
    return entries if isinstance(entries, list) else []


def _clean_value(value):
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, list):
        cleaned = [_clean_value(element) for element in value]
        cleaned = [element for element in cleaned if element not in (None, "")]
        if not cleaned:
            return None
        return cleaned[0] if len(cleaned) == 1 else cleaned
    return value


def _pick_value(entry: dict, *keys: str):
    for key in keys:
        if key not in entry:
            continue
        value = _clean_value(entry.get(key))
        if value not in (None, ""):
            return value
    return None


def _normalize_employee_details(entry: dict) -> dict:
    """Provide the stable EVL field names the config placeholders / field_map and
    eligibility rules reference."""
    normalized = {key: _clean_value(value) for key, value in entry.items()}
    aliases = {
        "employeeID": _pick_value(normalized, "employeeID", "employeeId", "Employee_ID"),
        "employeeName": _pick_value(normalized, "employeeName", "fullLegalName", "workerName"),
        "fullLegalName": _pick_value(normalized, "fullLegalName", "employeeName", "workerName"),
        "preferredFirstName": _pick_value(normalized, "preferredFirstName", "firstName", "legalFirstName"),
        "preferredLastName": _pick_value(normalized, "preferredLastName", "lastName", "legalLastName"),
        "email": _pick_value(normalized, "email", "userEmail", "workEmail", "primaryWorkEmail"),
        "userEmail": _pick_value(normalized, "userEmail", "email", "workEmail", "primaryWorkEmail"),
        "timeType": _pick_value(normalized, "timeType", "workerType"),
        "businessTitle": _pick_value(normalized, "businessTitle", "jobTitle", "position"),
        "company": _pick_value(normalized, "company", "companyName"),
        "annualSalary": _pick_value(normalized, "annualSalary", "salaryAmount", "baseSalary"),
        "hireDate": _pick_value(normalized, "hireDate", "originalHireDate"),
        "isRemote": _pick_value(normalized, "isRemote", "is_remote", "remoteWorker", "workingRemotely"),
        "leaveStartDate": _pick_value(normalized, "leaveStartDate", "leaveStart", "leaveStartDateNP"),
        "leaveEndDate": _pick_value(normalized, "leaveEndDate", "leaveEnd", "leaveEndDateNP"),
        "currentDate": _pick_value(normalized, "currentDate"),
        "countryISOCode": _pick_value(normalized, "countryISOCode", "workCountryISOCode"),
        "workCountryISOCode": _pick_value(normalized, "workCountryISOCode", "countryISOCode"),
        "userCountry": _pick_value(normalized, "userCountry", "workCountry", "country"),
    }
    for key, value in aliases.items():
        if value is not None:
            normalized[key] = value
    if not normalized.get("currentDate"):
        normalized["currentDate"] = datetime.now().strftime("%m/%d/%Y")
    return normalized


def _country_from_details(details: dict) -> "str | None":
    """The ISO-3 config country implied by a normalized Workday record."""
    return normalize_country(
        details.get("countryISOCode") or details.get("workCountryISOCode") or "",
        details.get("userCountry") or "",
    )


async def _fetch_employee_details(
    context: AgentRun,
    employee_id: str,
    report_path: "str | None" = None,
    employee_param: "str | None" = None,
    auth_state: "dict | None" = None,
) -> "tuple[dict | None, str | None]":
    """Fetch + normalize a Workday RaaS employee record — the default INT0445
    Employee Details report, or a config-declared per-set report (its
    host-relative ``report_path`` joined to the credentialed base URL). The
    report must echo the employee ID: the subject-match check below runs on
    every fetch, so a report that isn't prompt-filtered to the subject fails
    closed. Tool-local only — the returned dict holds sensitive fields and MUST
    NOT reach a return value."""
    auth_state = auth_state if auth_state is not None else {}
    auth = auth_state.get("authorization")
    err = None
    if not auth:
        auth, err = _auth_context(context)
    if err:
        auth, err = await _recover_workday_auth(context, auth_state)
        if err:
            return None, err
    auth_state["authorization"] = auth
    creds = _get_creds()
    url = creds["report_base_url"] + report_path if report_path else creds["employee_details_url"]
    params = {(employee_param or _DEFAULT_EMPLOYEE_PARAM): employee_id, "format": "json"}
    headers = {"Accept": "application/json", "Authorization": auth}
    try:
        resp = await _get(url, params, headers)
        if resp.status_code in (401, 403):
            refreshed_auth, refresh_err = await _recover_workday_auth(
                context, auth_state, rejected_auth=auth,
            )
            if refresh_err:
                return None, refresh_err
            headers["Authorization"] = refreshed_auth
            auth_state["authorization"] = refreshed_auth
            resp = await _get(url, params, headers)
    except (httpx.HTTPError, httpx.InvalidURL):
        logger.warning("evl workday fetch failed to connect")
        return None, _WORKDAY_UNAVAILABLE
    if resp.status_code in (401, 403):
        return None, _WORKDAY_AUTH_MISSING
    if resp.status_code >= 400:
        logger.warning("evl workday fetch rejected: status=%s", resp.status_code)
        return None, _WORKDAY_UNAVAILABLE
    try:
        data = resp.json()
    except ValueError:
        return None, _WORKDAY_UNAVAILABLE
    entries = _entries(data)
    if not entries:
        return None, _EMPLOYEE_NOT_FOUND
    details = _normalize_employee_details(entries[0])
    returned_employee_id = str(details.get("employeeID") or "").strip()
    if not returned_employee_id or returned_employee_id != str(employee_id).strip():
        logger.warning("evl workday subject mismatch or missing employee id")
        return None, _EMPLOYEE_NOT_FOUND
    return details, None


async def _fetch_config(context: AgentRun, country: str, region: str) -> "tuple[dict | None, str | None]":
    """Fetch the EVL config via the backend get-config action. Returns the most
    specific matching row's ``data`` (config is non-sensitive)."""
    creds = _get_creds()
    url = creds.get("config_url")
    if not url:
        return None, _CONFIG_UNAVAILABLE
    body = {
        "action": "get-config",
        "agentId": "evl_agent",
        "country_iso": country or "GLOBAL",
        "region": region or "GLOBAL",
    }
    headers = {"Content-Type": "application/json"}
    key = creds.get("config_api_key")
    if key:
        headers["x-api-key"] = key  # internal-agents credential, if not gateway-injected
    try:
        resp = await _post(url, None, body, headers, timeout=_CONFIG_TIMEOUT)
    except (httpx.HTTPError, httpx.InvalidURL):
        logger.warning("evl get-config failed to connect")
        return None, _CONFIG_UNAVAILABLE
    if resp.status_code >= 400:
        logger.warning("evl get-config rejected: status=%s", resp.status_code)
        return None, _CONFIG_UNAVAILABLE
    try:
        entries = resp.json()
    except ValueError:
        return None, _CONFIG_UNAVAILABLE
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list):
        return None, _CONFIG_UNAVAILABLE

    # Select one unambiguous most-specific row. Equal-specificity rows are a
    # configuration error: choosing by Mongo response order could make the
    # offered/sent letter vary between calls.
    def specificity(entry):
        if not isinstance(entry, dict):
            return -1
        return (2 if entry.get("country_iso") is not None else 0) + (1 if entry.get("region") is not None else 0)

    candidates = []
    for entry in entries:
        data = entry.get("data") if isinstance(entry, dict) else None
        if isinstance(data, dict) and isinstance(data.get("sets"), dict) and data.get("sets"):
            candidates.append((specificity(entry), data))
    if not candidates:
        return None, _CONFIG_UNAVAILABLE
    best_score = max(score for score, _ in candidates)
    best = [data for score, data in candidates if score == best_score]
    if len(best) != 1:
        logger.warning("evl get-config returned ambiguous rows at specificity=%s", best_score)
        return None, _CONFIG_UNAVAILABLE
    return best[0], None


# Ceiling on distinct set-declared report fetches per request — each is a
# Workday round-trip, so config size must not amplify a chat turn.
_MAX_SET_REPORTS = 3
# Always taken from the default record: a config-declared report can shape
# letter content and eligibility, never the subject's identity or where the
# letter is delivered.
_PINNED_BASE_FIELDS = ("employeeID", "email", "userEmail")


async def _overlay_set_reports(
    context: AgentRun,
    employee_id: str,
    details: dict,
    config_data: dict,
    country: "str | None",
    region: str,
    auth_state: dict,
) -> "tuple[dict | None, str | None]":
    """Fetch each matched set's declared report and overlay its fields onto the
    default-report record, set-specific values winning. Region-scoped sets
    overlay before country-scoped ones, so on an overlapping key the more
    specific set wins — the same convention as template dedup. Fails closed: a
    set that declares a report it can't deliver (malformed path, fetch failure,
    subject mismatch) fails the whole request — its letters are never offered
    or built from generic data. Sets without a ``report`` cost nothing extra."""
    merged = details
    fetched: set = set()
    reports = []
    for definition, _ in sorted(_matched_sets(config_data, country, region), key=lambda match: match[1]):
        templates = definition.get("templates")
        if not isinstance(templates, list) or not templates:
            continue  # a set that can offer no letters gets no say over the record
        report = set_report(definition)
        if report is None:
            continue
        if report is _REPORT_INVALID:
            logger.warning("evl config declared a malformed set report")
            return None, _CONFIG_UNAVAILABLE
        if report in fetched:
            continue
        if len(fetched) >= _MAX_SET_REPORTS:
            logger.warning("evl config declared more than %d set reports", _MAX_SET_REPORTS)
            return None, _CONFIG_UNAVAILABLE
        fetched.add(report)
        reports.append(report)

    # These reports are independent reads for the same subject. Fetching them
    # concurrently keeps the tool inside WxO's execution ceiling; results are
    # still applied in the deterministic region-before-country order above.
    results = await asyncio.gather(*(
        _fetch_employee_details(
            context,
            employee_id,
            report_path=path,
            employee_param=employee_param,
            auth_state=auth_state,
        )
        for path, employee_param in reports
    ))
    for extra, err in results:
        if err:
            return None, err
        if merged is details:
            merged = dict(details)
        merged.update({key: value for key, value in extra.items() if value is not None})
    if merged is not details:
        for key in _PINNED_BASE_FIELDS:
            merged.pop(key, None)
            if details.get(key) is not None:
                merged[key] = details[key]
    return merged, None


# --- Card builders (pure; validated in tests against the ChatBlock schema) ----
def build_selection_card(templates: list) -> dict:
    """Single-tap selection menu — a ``choice`` card: tapping an option selects
    it immediately, no submit button. Every letter scoped to the employee is
    listed; one they can't currently request is kept in the list but flagged with
    ``_INELIGIBLE_HINT`` (selecting it relays the reason and re-offers the menu).
    The option value is the template label, which the agent passes to
    select_letter (and which the widget also matches when the user types it)."""
    options = []
    for template in templates:
        option = {"value": template.get("label"), "label": template.get("label")}
        if template.get("_eligible", True) is False:
            option["description"] = _INELIGIBLE_HINT
        elif template.get("description"):
            option["description"] = template["description"]
        options.append(option)
    return {
        "kind": "choice",
        "cardId": CARD_SELECT,
        "title": "Request a verification letter",
        "options": options,
    }


def mark_eligibility(templates: list, details: dict) -> list:
    """Annotate each template with ``_eligible`` (evaluated against the tool-local
    Workday record) so the menu can flag a letter the employee can't currently
    request. Display marker only — eligibility is still enforced on selection and
    again at send."""
    now = datetime.now().date()
    for template in templates:
        eligible, _ = is_eligible(template.get("eligibility"), details, now)
        template["_eligible"] = eligible
    return templates


def build_recipient_card(languages: "list | None" = None, requires_encryption: bool = False) -> dict:
    """Optional add-a-recipient form. When the chosen letter offers more than one
    language, a required language select is prepended. Skipping (cancel) or an
    empty value means no additional recipient."""
    languages = languages or ["en"]
    fields = []
    if len(languages) > 1:
        fields.append({
            "type": "select", "id": "language", "label": "Language", "required": True,
            "options": [{"value": lang, "label": _language_label(lang) or lang} for lang in languages],
        })
    fields.append({
        "type": "text", "id": "additional_recipient",
        "label": "Additional recipient email", "required": False,
        "placeholder": "name@example.com",
    })
    subtitle = "Optional — add an email address to also receive a copy of this letter."
    if requires_encryption:
        subtitle += " This letter is delivered by secure email."
    return {
        "kind": "form",
        "cardId": CARD_RECIPIENT,
        "title": "Send a copy to someone else?",
        "subtitle": subtitle,
        "submitLabel": "Continue",
        "cancelLabel": "Skip",
        "fields": fields,
    }


def build_confirm_card(letter_type: str, language: str, recipient: str) -> dict:
    """Final confirmation gate. Shows only non-sensitive facts — never salary,
    and the employee's own address is referred to generically."""
    facts = [{"label": "Letter type", "value": str(letter_type)}]
    lang_label = _language_label(language)
    if lang_label:
        facts.append({"label": "Language", "value": lang_label})
    facts.append({"label": "Send to", "value": "Your registered work email"})
    rec = str(recipient or "").strip()
    if rec:
        facts.append({"label": "Also to", "value": rec})
    return {
        "kind": "confirm",
        "cardId": CARD_CONFIRM,
        "title": "Confirm your verification letter",
        "facts": facts,
        "confirmLabel": "Send letter",
        "cancelLabel": "Cancel",
    }


# --- Letter construction + send (tool-local; no Graph/PDF tokens) -------------
def _fill(template: "str | None", values: dict, *, escape: bool = True) -> str:
    """Substitute {field} placeholders with values from the normalized Workday
    record. Unknown placeholders resolve to empty. The template is operator-authored
    config (trusted); only substituted values are handled here.

    Values are HTML-escaped by default, for the HTML letter body — so a Workday
    value can't inject markup. Pass ``escape=False`` for plain-text sinks (the email
    subject and the PDF filename), where an entity like ``&#x27;`` would render
    literally (e.g. O'Brien)."""
    def repl(match):
        raw = str(values.get(match.group(1), "") or "")
        return html.escape(raw) if escape else raw
    return re.sub(r"\{(\w+)\}", repl, template or "")


def _plain_text(value, max_length: int = 255) -> str:
    """Remove control characters before a value reaches an email header or
    filename sink. Workday/config text remains human-readable but cannot inject
    a second header line."""
    cleaned = _CONTROL_CHARS_RE.sub(" ", str(value or ""))
    return re.sub(r"\s+", " ", cleaned).strip()[:max_length]


def _safe_filename(value) -> str:
    cleaned = _UNSAFE_FILENAME_RE.sub("-", _plain_text(value, 180)).strip(" .")
    return cleaned or "Verification Letter"


def _valid_email(value) -> bool:
    text = str(value or "").strip()
    return len(text) <= 254 and bool(_EMAIL_RE.fullmatch(text))


def _split_recipients(recipients) -> list:
    if isinstance(recipients, (list, tuple)):
        items = list(recipients)
    else:
        items = re.split(r"[,;\s]+", str(recipients or ""))
    return [str(x).strip() for x in items if x and str(x).strip()]


def _additional_recipient(recipients) -> "tuple[str, str | None]":
    additional = _split_recipients(recipients)
    if len(additional) > 1 or any(not _valid_email(address) for address in additional):
        return "", _INVALID_RECIPIENT
    return (additional[0] if additional else ""), None


def _recipient_list(recipients, user_email: str) -> "tuple[list | None, str | None]":
    """Delivery list: the logged-in employee's own registered email, always
    included, plus at most one valid additional recipient."""
    employee_email = str(user_email or "").strip()
    if not _valid_email(employee_email):
        return None, _NO_RECIPIENT
    additional, err = _additional_recipient(recipients)
    if err:
        return None, err
    to_list = [employee_email]
    if additional and additional.lower() != employee_email.lower():
        to_list.append(additional)
    return to_list, None


def _employee_first_name(details: dict) -> str:
    first_name = _plain_text(details.get("preferredFirstName") or "", 80)
    if first_name:
        return first_name
    full_name = _plain_text(details.get("employeeName") or details.get("fullLegalName") or "", 120)
    return full_name.split()[0] if full_name else ""


def _recipient_summary(details: dict, additional_recipient: str) -> str:
    employee_email = str(details.get("email") or "").strip()
    recipients = [employee_email] if employee_email else []
    if additional_recipient and additional_recipient.lower() != employee_email.lower():
        recipients.append(additional_recipient)
    return ", ".join(recipients) or "Your registered work email"


def _build_dynamic_body(template: dict, details: dict, recipients) -> "tuple[dict | None, dict | None, str | None]":
    """Build a US/Canada Dynamic-Letter payload from the template's config body /
    subject / filename and tool-local Workday data. Returns (payload, params, error)."""
    delivery = template.get("delivery") or {}
    required = ("templatename", "subject", "filename", "body")
    if not isinstance(delivery, dict) or delivery.get("kind") != "dynamic_body" or any(
        not isinstance(delivery.get(field), str) or not delivery.get(field).strip() for field in required
    ):
        return None, None, _CONFIG_UNAVAILABLE
    to_list, recipient_err = _recipient_list(recipients, details.get("email") or "")
    if recipient_err:
        return None, None, recipient_err
    values = dict(details)
    values["askHr"] = ASKHR_EMAIL_DEFAULT
    first = html.escape(str(details.get("preferredFirstName") or ""))
    last = html.escape(str(details.get("preferredLastName") or ""))
    label = html.escape(str(template.get("label") or ""))
    email_body = (
        "<p>Hello,</p><p> Please find <b>" + first + " " + last + "</b>'s "
        + label + " letter attached.</p><p>Regards,<br>Fiserv AskHR</p>" + _UNMONITORED_NOTICE
    )
    payload = {
        "addressBlock": ADDRESS_BLOCK,
        "currentDate": details.get("currentDate") or datetime.now().strftime("%m/%d/%Y"),
        "salutationBlock": SALUTATION_BLOCK,
        "bodyBlock": _fill(delivery.get("body"), values),
        "signatureBlock": SIGNATURE_BLOCK,
        "filename": _safe_filename(_fill(delivery.get("filename"), values, escape=False)),
        "to": to_list,
        "from": FROM_ADDR,
        "subject": _plain_text(_SUBJECT_PREFIX + _fill(delivery.get("subject"), values, escape=False)),
        "bodyFormat": "HTML",
        "body": email_body,
    }
    params = {"templatename": delivery.get("templatename") or "Dynamic-Letter", "sendemail": "true"}
    return payload, params, None


def _build_structured_fields(template: dict, details: dict, recipients, language: str) -> "tuple[dict | None, dict | None, str | None]":
    """Build a structured-template payload (e.g. the Netherlands Employer
    Statement): map the config field_map (microserviceKey -> workdayField) plus
    field_defaults into the microservice's own template. Returns (payload, params, error)."""
    delivery = template.get("delivery") or {}
    field_map = delivery.get("field_map")
    field_defaults = delivery.get("field_defaults")
    if (not isinstance(delivery, dict) or delivery.get("kind") != "structured_fields"
            or not isinstance(field_map, dict) or not isinstance(field_defaults, dict)):
        return None, None, _CONFIG_UNAVAILABLE
    if any(not isinstance(key, str) or not key or not isinstance(value, str) or not value
           for key, value in field_map.items()):
        return None, None, _CONFIG_UNAVAILABLE
    body_fields = dict(field_defaults)
    for ms_key, wd_field in field_map.items():
        value = details.get(wd_field)
        if value is not None:
            body_fields[ms_key] = value

    employee_name = (
        details.get("employeeName")
        or details.get("fullLegalName")
        or (f"{details.get('preferredFirstName') or ''} {details.get('preferredLastName') or ''}".strip())
        or "Employee"
    )
    lang, language_err = _resolve_language(template, language)
    if language_err:
        return None, None, language_err
    templatename = delivery.get("templatename")
    if isinstance(templatename, dict):
        templatename = templatename.get(lang)
    display = delivery.get("display_name")
    if isinstance(display, dict):
        display_name = display.get(lang)
    else:
        display_name = display or template.get("label")
    if not isinstance(templatename, str) or not templatename.strip() or not isinstance(display_name, str) or not display_name.strip():
        return None, None, _CONFIG_UNAVAILABLE

    to_list, recipient_err = _recipient_list(recipients, details.get("email") or "")
    if recipient_err:
        return None, None, recipient_err
    body = (
        "<p>Hello,</p><p>Please find <b>" + html.escape(str(employee_name)) + "</b>'s "
        + html.escape(str(display_name)) + " attached.</p><p>Regards,<br>Fiserv AskHR</p>"
        + _UNMONITORED_NOTICE
    )
    payload = {
        **body_fields,
        "filename": _safe_filename(f"{display_name} - {employee_name}"),
        "to": to_list,
        "from": FROM_ADDR,
        "subject": _plain_text(_SUBJECT_PREFIX + f"{display_name} for {employee_name}"),
        "bodyFormat": "HTML",
        "body": body,
    }
    params = {"templatename": templatename, "sendemail": "true"}
    return payload, params, None


async def _send_letter(payload: dict, params: dict) -> "tuple[object, str | None]":
    """Send the letter to the EVL microservice through API Hub (Apigee): obtain a
    client-credentials token, then POST with it as the Authorization bearer (retry
    once on 401 with a fresh token). No Graph/PDF token headers — the microservice
    mints its own. On any HTTP error a canned message is returned; the raw
    response body is never relayed to the agent (its detail is logged instead)."""
    creds = _get_creds()
    url = creds.get("dynamic_evl_url")
    if not url:
        return None, _SEND_REQUEST_FAILED

    token, err = await _get_evl_token()
    if err:
        return None, err

    def _call(tok):
        headers = {"Content-Type": "application/json", "Authorization": _bearer(tok)}
        return _post(url, params, payload, headers, timeout=_SEND_TIMEOUT)

    try:
        resp = await _call(token)
        if resp.status_code == 401:
            token, err = await _get_evl_token(force_refresh=True)
            if err:
                return None, err
            resp = await _call(token)
    except (httpx.HTTPError, httpx.InvalidURL):
        logger.warning("evl microservice send failed to connect")
        return None, _SEND_REQUEST_FAILED
    if resp.status_code >= 400:
        logger.warning("evl microservice send rejected: status=%s", resp.status_code)
        return None, _SEND_REQUEST_FAILED
    return resp, None


# --- The single registered tool -----------------------------------------------
class _Request(NamedTuple):
    """The resolved, subject-bound request shared by every process."""
    details: dict
    country: str
    region: str
    config: dict


async def _resolve_request(context: AgentRun) -> "tuple[_Request | None, dict | None]":
    """Shared preamble for every process: subject-bound employee id → fresh Workday
    read → country → matching config → overlay of any set-declared reports.
    Resolving this in one place keeps all three
    processes agreeing on country (the Workday record first, the ``country_iso``
    context var as a fallback), so a letter offered in the menu is always findable at
    select and generate time — no process can drift into a dead-end. ``generate``
    calls this on its write path, so it still RE-FETCHES Workday (the firewall).
    Returns ``(request, None)`` on success or ``(None, error)`` — a ready-to-return
    dict — on any failure. No sensitive value ever crosses back in the error.
    """
    emp_id = _get_employee_id(context)
    if not emp_id:
        return None, {"text": _NO_EMPLOYEE_ID}
    auth_state: dict = {}
    details, err = await _fetch_employee_details(context, emp_id, auth_state=auth_state)
    if err:
        return None, {"text": err}
    country = _country_from_details(details) or normalize_country(_ctx(context, "country_iso") or "", "")
    if not country:
        return None, {"text": COUNTRY_UNAVAILABLE}
    region = _ctx(context, "region") or ""
    config_data, err = await _fetch_config(context, country, region)
    if err:
        return None, {"text": err}
    details, err = await _overlay_set_reports(
        context, emp_id, details, config_data, country, region, auth_state,
    )
    if err:
        return None, {"text": err}
    return _Request(details=details, country=country, region=region, config=config_data), None


async def _get_options(context: AgentRun) -> dict:
    # One Workday read up front lets the menu flag letters the employee can't
    # currently request. Only labels + a card cross back — no salary / leave / PII.
    req, err = await _resolve_request(context)
    if err:
        return err
    templates = applicable_templates(req.config, req.country, req.region)
    if not templates:
        return {"text": COUNTRY_UNAVAILABLE}
    mark_eligibility(templates, req.details)
    labels = [t.get("label") for t in templates]
    lines = ["Here are the verification letters for your profile:", ""] + [f"- {label}" for label in labels]
    logger.info("evl get_options: country=%s region=%s offered=%d", req.country, req.region or "-", len(templates))
    return {"card": build_selection_card(templates), "text": "\n".join(lines)}


async def _select_letter(context: AgentRun, letter_type: str) -> dict:
    if not str(letter_type or "").strip():
        return {"text": "Which letter would you like? Ask me to show your options."}
    # The one Workday read here is reused for the eligibility check below.
    req, err = await _resolve_request(context)
    if err:
        return err
    templates = applicable_templates(req.config, req.country, req.region)
    template = _find_template(templates, letter_type)
    if not template:
        return {"text": _LETTER_NOT_FOUND}

    # Eligibility gate, checked against the record already read above.
    ok, reason = is_eligible(template.get("eligibility"), req.details, datetime.now().date())
    if not ok:
        # Explain, then re-offer the whole menu (re-marked from the same
        # record) so the employee can pick another without starting over.
        mark_eligibility(templates, req.details)
        logger.info("evl select_letter: ineligible id=%s", template.get("id"))
        return {
            "card": build_selection_card(templates),
            "text": f"{reason or _NO_LETTERS_AVAILABLE} Here are your options again:",
        }

    if not _delivery_config_valid(template):
        return {"text": _CONFIG_UNAVAILABLE}
    if not _valid_email(req.details.get("email")):
        return {"text": _NO_RECIPIENT}

    return {
        "card": build_recipient_card(template.get("_languages"), bool(template.get("_requires_encryption"))),
        "text": (
            f"Thank you for your selection. I'll prepare the {template.get('label') or 'verification letter'}; "
            "once you confirm, it will be sent to your registered email address.\n\n"
            "Would you like to add an additional recipient, such as a landlord or financial institution, "
            "to receive a copy of the letter?"
        ),
    }


async def _generate(context: AgentRun, letter_type: str, language: str, recipients: str, confirmed: bool) -> dict:
    if not str(letter_type or "").strip():
        return {"text": "Which verification letter would you like? Ask me to show your options."}

    additional_recipient, recipient_err = _additional_recipient(recipients)
    if recipient_err:
        return {"text": recipient_err}

    # Confirmation is a pure gate: no Workday/config read and no send before the
    # employee explicitly confirms. The write path below re-fetches and validates
    # the canonical template, language, recipient, and eligibility.
    if not confirmed:
        requested_language = str(language or "").strip().lower()
        if requested_language and requested_language not in _LANGUAGE_LABELS:
            return {"text": _LANGUAGE_REQUIRED}
        return {
            "card": build_confirm_card(letter_type, requested_language, additional_recipient),
            "text": f"Please confirm: {letter_type}. I'll send it to your registered work email.",
        }

    # Confirmed write: RE-FETCH Workday and resolve the canonical config rather
    # than trusting anything carried through the agent.
    req, err = await _resolve_request(context)
    if err:
        return err
    template = _find_template(applicable_templates(req.config, req.country, req.region), letter_type)
    if not template:
        return {"text": _LETTER_NOT_FOUND}
    if not _valid_email(req.details.get("email")):
        return {"text": _NO_RECIPIENT}
    resolved_language, language_err = _resolve_language(template, language)
    if language_err:
        return {
            "card": build_recipient_card(template.get("_languages"), bool(template.get("_requires_encryption"))),
            "text": language_err,
        }
    if not _delivery_config_valid(template):
        return {"text": _CONFIG_UNAVAILABLE}

    ok, reason = is_eligible(template.get("eligibility"), req.details, datetime.now().date())  # defense in depth
    if not ok:
        return {"text": reason or _NO_LETTERS_AVAILABLE}

    delivery = template.get("delivery") or {}
    if delivery.get("kind") == "structured_fields":
        payload, params, err = _build_structured_fields(template, req.details, additional_recipient, resolved_language)
    elif delivery.get("kind") == "dynamic_body":
        payload, params, err = _build_dynamic_body(template, req.details, additional_recipient)
    else:
        return {"text": _CONFIG_UNAVAILABLE}
    if err:
        return {"text": err}

    _resp, send_err = await _send_letter(payload, params)
    if send_err:
        return {"text": send_err}

    logger.info("evl generate: sent id=%s country=%s", template.get("id"), req.country)
    # The model must see this non-sensitive success marker so it can execute the
    # required report_action call. A user-only audience block would terminate
    # agent reasoning before that follow-up tool call.
    display_type = template.get("label") or letter_type
    greeting = _employee_first_name(req.details)
    greeting_block = f"{greeting},\n\n" if greeting else ""
    recipients_line = _recipient_summary(req.details, additional_recipient)
    confirmation = (
        f"{greeting_block}Your {display_type} has been securely emailed as a PDF.\n\n"
        f"Recipients: {recipients_line}\n\n"
        "Regards,\n"
        "Fiserv AskHR"
    )
    return {"text": confirmation, "sent": True, "action_type": "evl_generate"}


@tool(
    expected_credentials=[
        {"app_id": APP_ID, "type": ConnectionType.KEY_VALUE},
    ],
)
async def evl_tools(
    context: AgentRun,
    process: str = "get_options",
    letter_type: str = "",
    language: str = "",
    recipients: str = "",
    confirmed: bool = False,
) -> dict:
    """Employee Verification Letter tool.

    Args:
        context: Agent run context (employee_id and wd_access_token are read here
            — the subject is always the logged-in employee, never an argument).
        process: ``get_options`` | ``select_letter`` | ``generate``.
        letter_type: The chosen letter (its label from the selection card).
        language: ``en`` / ``nl`` for a multi-language letter; ignored otherwise.
        recipients: Optional single additional recipient email.
        confirmed: For ``generate`` — false returns the confirm card, true sends.
    Returns:
        A dict with ``text`` and, where interaction helps, a ``card`` to stage;
        on a successful generate, ``sent=true`` plus non-sensitive confirmation
        text so the agent can emit ``report_action``.
    """
    if process == "get_options":
        return await _get_options(context)
    if process == "select_letter":
        return await _select_letter(context, letter_type)
    if process == "generate":
        return await _generate(context, letter_type, language, recipients, confirmed)
    return {"text": _UNSUPPORTED_PROCESS.format(process=process)}
