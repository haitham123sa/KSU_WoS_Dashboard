#!/usr/bin/env python3
"""Collect WoS Starter records and produce auditable August–July summaries.

Standard library only. --dry-run and --self-test make no network requests and
do not read an API key. All journal quartiles are limited to supplied categories.
"""

import argparse
import calendar
import csv
import datetime as dt
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

ENDPOINT = "https://api.clarivate.com/apis/wos-starter/v1/documents"
SCOPE = "best among supplied categories"
FACULTY_FIELDS = {"faculty_key", "name", "programme", "orcid", "researcher_id", "source_row"}
JOURNAL_FIELDS = {"journal_key", "journal_name", "issn", "eissn", "jcr_year", "category", "jif", "jif_rank", "category_journals", "derived_jif_quartile", "source_file", "source_row"}
MONTH_NAMES = {
    "JAN": 1, "JANUARY": 1, "FEB": 2, "FEBRUARY": 2, "MAR": 3,
    "MARCH": 3, "APR": 4, "APRIL": 4, "MAY": 5, "JUN": 6,
    "JUNE": 6, "JUL": 7, "JULY": 7, "AUG": 8, "AUGUST": 8,
    "SEP": 9, "SEPT": 9, "SEPTEMBER": 9, "OCT": 10,
    "OCTOBER": 10, "NOV": 11, "NOVEMBER": 11, "DEC": 12,
    "DECEMBER": 12,
}
MONTH_PATTERN = re.compile(r"\b(" + "|".join(sorted(MONTH_NAMES, key=len, reverse=True)) + r")\b")
SEASON_PATTERN = re.compile(r"\b(SPR|SPRING|SUM|SUMMER|FALL|AUT|AUTUMN|WIN|WINTER)\b")


def now_utc():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def write_csv(path, fields, rows):
    with Path(path).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: ("" if value is None else value) for key, value in row.items()})


def read_csv(path, required):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{Path(path).name}: missing columns: {', '.join(sorted(missing))}")
        rows = []
        for line, row in enumerate(reader, 2):
            if None in row:
                raise ValueError(f"{Path(path).name}: too many columns on CSV line {line}")
            if any(str(v or "").strip() for v in row.values()):
                rows.append({k: str(v or "").strip() for k, v in row.items()})
        return rows


def normalize_orcid(value):
    value = re.sub(r"^https?://(?:www\.)?orcid\.org/", "", value.strip(), flags=re.I).rstrip("/").upper()
    if not value:
        return ""
    if not re.fullmatch(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", value):
        raise ValueError("ORCID format is invalid")
    digits, total = value.replace("-", ""), 0
    for digit in digits[:-1]:
        total = (total + int(digit)) * 2
    check = (12 - total % 11) % 11
    if digits[-1] != ("X" if check == 10 else str(check)):
        raise ValueError("ORCID checksum is invalid")
    return value


def normalize_issn(value):
    value = re.sub(r"\s+", "", str(value or "").upper())
    match = re.fullmatch(r"(?:E?ISSN:?)?(\d{4})-?(\d{3}[\dX])", value)
    return match[1] + "-" + match[2] if match else ""


def load_inputs(faculty_path, journal_path, selected_jcr_year=None):
    faculties = read_csv(faculty_path, FACULTY_FIELDS)
    journals = read_csv(journal_path, JOURNAL_FIELDS)
    if not faculties:
        raise ValueError("Faculty input contains no records")
    seen_keys, owners, warnings = set(), {}, []
    for row in faculties:
        key = row["faculty_key"]
        if not key or key in seen_keys:
            raise ValueError(f"Missing or duplicate faculty_key: {key!r}")
        seen_keys.add(key)
        if not row["name"]:
            raise ValueError(f"Faculty {key}: name is missing")
        try:
            row["orcid"] = normalize_orcid(row["orcid"])
        except ValueError as exc:
            raise ValueError(f"Faculty {key}: {exc}") from exc
        row["researcher_id"] = row["researcher_id"].strip().upper()
        if row["researcher_id"] and not re.fullmatch(r"[A-Z]{1,3}-\d{4}-\d{4}", row["researcher_id"]):
            raise ValueError(f"Faculty {key}: ResearcherID format is invalid")
        if not (row["orcid"] or row["researcher_id"]):
            raise ValueError(f"Faculty {key}: both external identifiers are missing")
        if not row["researcher_id"]:
            warnings.append(f"Faculty {key}: ORCID-only search; ResearcherID is missing")
        for field in ("orcid", "researcher_id"):
            value = row[field]
            if value and (field, value) in owners:
                raise ValueError(f"{field} is shared by faculty keys {owners[(field, value)]!r} and {key!r}")
            if value:
                owners[(field, value)] = key
    years = {row["jcr_year"] for row in journals if row["jcr_year"]}
    if selected_jcr_year:
        journals = [row for row in journals if row["jcr_year"] == str(selected_jcr_year)]
        if not journals:
            raise ValueError("No journal rows match --jcr-year")
    elif len(years) > 1:
        raise ValueError("Journal file contains multiple JCR years; select one with --jcr-year")
    if not journals:
        warnings.append("No journal-category records supplied; all journals will be unmatched")
    if any(not row["jcr_year"] for row in journals):
        warnings.append("JCR data year is missing in some/all rows; classification year remains unspecified")
    for row in journals:
        row["issn"] = normalize_issn(row["issn"])
        row["eissn"] = normalize_issn(row["eissn"])
        row["derived_jif_quartile"] = row["derived_jif_quartile"].upper()
        if not row["journal_key"]:
            raise ValueError("A journal-category row has no journal_key")
    return faculties, journals, warnings


class JournalLookup:
    def __init__(self, rows):
        self.rows = defaultdict(list)
        self.by_issn = defaultdict(set)
        for row in rows:
            key = row["journal_key"]
            self.rows[key].append(row)
            for field in ("issn", "eissn"):
                if row[field]:
                    self.by_issn[row[field]].add(key)

    def match(self, issn, eissn):
        candidates = set()
        for value in (issn, eissn):
            candidates.update(self.by_issn.get(normalize_issn(value), set()))
        result = {"journal_match_status": "unmatched", "jcr_journal_key": "", "jcr_data_year": "", "best_supplied_quartile": "", "quartile_scope": SCOPE, "jcr_category_records": "[]"}
        if not (normalize_issn(issn) or normalize_issn(eissn)):
            result["journal_match_status"] = "missing_or_invalid_issn"
        elif len(candidates) > 1:
            result["journal_match_status"] = "ambiguous_issn"
            result["jcr_journal_key"] = "; ".join(sorted(candidates))
        elif len(candidates) == 1:
            key = next(iter(candidates))
            rows = self.rows[key]
            result["jcr_journal_key"] = key
            result["jcr_category_records"] = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
            years = {r["jcr_year"] for r in rows}
            if len(years) != 1:
                result["journal_match_status"] = "conflicting_or_missing_jcr_year"
                return result
            result["jcr_data_year"] = next(iter(years))
            categories = defaultdict(set)
            for row in rows:
                categories[row["category"]].add((row["derived_jif_quartile"], row["jif"], row["jif_rank"], row["category_journals"]))
            quartiles = {r["derived_jif_quartile"] for r in rows}
            if any(len(values) > 1 for values in categories.values()):
                result["journal_match_status"] = "conflicting_category_records"
            elif len({r["jif"] for r in rows if r["jif"]}) > 1:
                result["journal_match_status"] = "conflicting_jif_values"
            elif any(q not in {"Q1", "Q2", "Q3", "Q4"} for q in quartiles):
                result["journal_match_status"] = "matched_quartile_unavailable"
            else:
                result["best_supplied_quartile"] = min(quartiles)
                result["journal_match_status"] = "matched" if result["jcr_data_year"] else "matched_jcr_year_unspecified"
        return result


def publication_period(source):
    """Return source year, academic-year start, and a transparent precision/status.

    Never invent a day or translate seasons into months. Named month ranges
    are assigned only when their entire range belongs to one August–July year.
    """
    raw_year = str(source.get("publishYear", "") or "").strip()
    raw_month = str(source.get("publishMonth", "") or "").strip().upper()
    year = int(raw_year) if re.fullmatch(r"\d{4}", raw_year) else None
    if year is not None and not 1000 <= year <= 9999:
        return None, None, "invalid_year"
    iso = re.fullmatch(r"(\d{4})-(\d{2})(?:-(\d{2}))?", raw_month)
    if iso:
        iso_year, month = int(iso[1]), int(iso[2])
        if year is not None and year != iso_year:
            return None, None, "conflicting_source_years"
        try:
            dt.date(iso_year, month, int(iso[3] or 1))
        except ValueError:
            return year, None, "invalid_iso_date"
        return iso_year, iso_year if month >= 8 else iso_year - 1, "exact_date" if iso[3] else "month"
    if year is None:
        return None, None, "missing_or_invalid_year"
    if not raw_month:
        return year, None, "year_only"
    if SEASON_PATTERN.search(raw_month):
        return year, None, "season_without_verified_months"
    if re.fullmatch(r"\d{1,2}", raw_month):
        month = int(raw_month)
        if 1 <= month <= 12:
            return year, year if month >= 8 else year - 1, "month"
        return year, None, "invalid_month"
    tokens = MONTH_PATTERN.findall(raw_month)
    if not tokens:
        return year, None, "unrecognized_or_ambiguous_date"
    months = list(dict.fromkeys(MONTH_NAMES[t] for t in tokens))
    residual = MONTH_PATTERN.sub("", raw_month)
    explicit_years = re.findall(r"\b\d{4}\b", residual)
    if any(int(y) != year for y in explicit_years):
        return None, None, "conflicting_source_years"
    residual = re.sub(r"\b\d{4}\b", "", residual)
    if re.search(r"[^\d\s,./\-–—]", residual):
        return year, None, "unrecognized_date_text"
    if len(months) == 1:
        month = months[0]
        days = [int(x) for x in re.findall(r"\d+", residual)]
        if any(day < 1 or day > calendar.monthrange(year, month)[1] for day in days):
            return year, None, "invalid_day_in_month"
        precision = "exact_date" if len(days) == 1 else "month"
        return year, year if month >= 8 else year - 1, precision
    if months != sorted(months):
        return year, None, "month_range_wraps_calendar_year"
    possible = {year if month >= 8 else year - 1 for month in range(min(months), max(months) + 1)}
    if len(possible) != 1:
        return year, None, "month_range_crosses_academic_year"
    return year, next(iter(possible)), "month_range"


def wos_citations(record):
    entries = record.get("citations")
    if not isinstance(entries, list):
        return None, "unavailable"
    values = []
    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("db", "")).upper() == "WOS":
            value = entry.get("count")
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None, "invalid_wos_count"
            values.append(value)
    if not values:
        return None, "unavailable"
    if len(set(values)) > 1:
        return None, "conflicting_wos_counts"
    return values[0], "available"


def make_publication(record, retrieved_at, lookup):
    source = record.get("source") or {}
    identifiers = record.get("identifiers") or {}
    year, ay, date_status = publication_period(source)
    citations, citation_status = wos_citations(record)
    issn, eissn = str(identifiers.get("issn") or ""), str(identifiers.get("eissn") or "")
    row = {
        "uid": record["uid"], "title": record.get("title", ""),
        "doi": identifiers.get("doi", ""), "journal_name": source.get("sourceTitle", ""),
        "issn": issn, "eissn": eissn,
        "publish_year_raw": source.get("publishYear", ""), "publish_month_raw": source.get("publishMonth", ""),
        "assignment_year": year, "academic_year_start": ay,
        "academic_year": f"{ay}–{ay + 1}" if ay is not None else "", "date_status": date_status,
        "current_wos_citations": citations, "citation_status": citation_status,
        "document_types": "; ".join(str(x) for x in record.get("types", [])),
        "source_types": "; ".join(str(x) for x in record.get("sourceTypes", [])),
        "first_retrieved_at": retrieved_at, "last_retrieved_at": retrieved_at,
        "observations": 1, "citation_count_changed_during_run": False,
        "publication_metadata_changed_during_run": False,
        "wos_url": (record.get("links") or {}).get("record", ""),
    }
    row.update(lookup.match(issn, eissn))
    return row


def merge_publication(publications, row):
    old = publications.get(row["uid"])
    if old:
        row["first_retrieved_at"] = old["first_retrieved_at"]
        row["observations"] = old["observations"] + 1
        row["citation_count_changed_during_run"] = old["citation_count_changed_during_run"] or old["current_wos_citations"] != row["current_wos_citations"]
        fields = ("title", "doi", "publish_year_raw", "publish_month_raw", "issn", "eissn")
        row["publication_metadata_changed_during_run"] = old["publication_metadata_changed_during_run"] or any(old[k] != row[k] for k in fields)
    publications[row["uid"]] = row


class CollectionError(Exception):
    def __init__(self, message, stop_all=False, *, code="collection_error", http_status=None,
                 next_action="", provider_detail=""):
        self.message = message
        self.code = code
        self.http_status = http_status
        self.next_action = next_action
        self.provider_detail = provider_detail
        super().__init__(message + (" " + next_action if next_action else ""))
        self.stop_all = stop_all


def _http_failure(status, provider_detail=""):
    """Map observed HTTP status to advice without claiming an unverified cause."""
    failures = {
        400: ("request_rejected", "The API rejected the request syntax or parameters.",
              "Check the query and parameters against the Starter API documentation."),
        401: ("authentication_rejected", "Web of Science did not accept the API key.",
              "Check that the active Web of Science Starter API key is entered without quotation marks."),
        403: ("access_denied", "Web of Science denied access to this request.",
              "Check this key's Starter API subscription and access to WOS in the Clarivate developer portal; this status alone does not confirm the reason."),
        404: ("endpoint_not_found", "The requested API resource was not found.",
              "Check the Starter API endpoint and Clarivate service documentation."),
        408: ("api_timeout", "The API timed out while handling the request.",
              "Retry later; if it continues, provide the timestamp and status to Clarivate support."),
        429: ("rate_or_quota_limited", "Web of Science limited this request.",
              "Check your remaining API quota and rate limit in the developer portal, then retry when allowed."),
    }
    if status in failures:
        code, message, action = failures[status]
    elif 500 <= status < 600:
        code, message, action = ("api_service_error", "The API returned a server error.",
                                 "Retry later; if it continues, provide the timestamp and status to Clarivate support.")
    else:
        code, message, action = ("http_error", "The API request failed.",
                                 "Review the HTTP status and sanitized service details in the diagnostic report.")
    return CollectionError(f"HTTP {status}: {message}", stop_all=status in (401, 403, 429),
                           code=code, http_status=status, next_action=action,
                           provider_detail=provider_detail)


def _network_failure(exc):
    """Use exception types, never untrusted exception text containing credentials."""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, ssl.SSLCertVerificationError):
        return CollectionError("TLS certificate verification failed.", stop_all=True,
                               code="tls_certificate_error",
                               next_action="Check the Python certificate installation, system clock, and any institutional proxy certificate. Keep TLS verification enabled.")
    if isinstance(reason, ssl.SSLError):
        return CollectionError("A secure TLS connection could not be established.", stop_all=True,
                               code="tls_connection_error",
                               next_action="Check network or proxy TLS settings and retry. Keep TLS verification enabled.")
    if isinstance(reason, socket.gaierror):
        return CollectionError("The API host name could not be resolved.", stop_all=True,
                               code="dns_error",
                               next_action="Check DNS and internet access to api.clarivate.com, then retry.")
    if isinstance(reason, TimeoutError):
        return CollectionError("The connection to Web of Science timed out.", stop_all=True,
                               code="network_timeout",
                               next_action="Check internet or proxy access to api.clarivate.com and retry.")
    return CollectionError("The network connection to Web of Science failed.", stop_all=True,
                           code="network_error",
                           next_action="Check internet, firewall, and proxy access to api.clarivate.com, then retry.")


def _safe_provider_detail(exc, api_key):
    """Read only small, known JSON error fields; never emit raw HTML or headers."""
    try:
        payload = json.loads(exc.read(8192).decode("utf-8"))
        if not isinstance(payload, dict):
            return ""
        error = payload.get("error")
        values = [payload.get("error_description")]
        if isinstance(error, dict):
            values.extend(error.get(field) for field in ("title", "details", "message"))
        elif isinstance(error, str):
            values.append(error)
        clean = []
        for value in values:
            if isinstance(value, str):
                # Replace after JSON decoding, including escaped credential reflections.
                value = value.replace(api_key, "[REDACTED]") if api_key else value
                clean.append(" ".join(value.split())[:500])
        return "; ".join(clean)[:1000]
    except (OSError, ValueError, UnicodeDecodeError):
        return ""
    finally:
        exc.close()


def _redact_secret(value, secret):
    """Redact decoded JSON strings as well as plain-text credential reflections."""
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]") if secret else value
    if isinstance(value, list):
        return [_redact_secret(item, secret) for item in value]
    if isinstance(value, dict):
        return {_redact_secret(key, secret): _redact_secret(item, secret) for key, item in value.items()}
    return value


class StarterClient:
    def __init__(self, api_key, max_requests=5000, interval=0.25, max_attempts=4, timeout=60):
        self.api_key, self.max_requests, self.interval = api_key, max_requests, interval
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 4:
            raise ValueError("max_attempts must be an integer from 1 to 4")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 60:
            raise ValueError("timeout must be between 0 and 60 seconds")
        self.max_attempts, self.timeout = max_attempts, timeout
        self.requests, self.last_request = 0, 0.0

    def get(self, query, page, limit=50):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("limit must be an integer from 1 to 50")
        params = {"db": "WOS", "q": query, "limit": limit, "page": page, "sortField": "LD+A"}
        url = ENDPOINT + "?" + urllib.parse.urlencode(params)
        for attempt in range(self.max_attempts):
            if self.requests >= self.max_requests:
                raise CollectionError("Configured request budget exhausted", stop_all=True,
                                      code="request_budget_exhausted",
                                      next_action="Review the collection size and remaining API quota before increasing the request budget.")
            delay = self.interval - (time.monotonic() - self.last_request)
            if delay > 0:
                time.sleep(delay)
            request = urllib.request.Request(url, headers={"X-ApiKey": self.api_key, "Accept": "application/json", "User-Agent": "DEH-WoS-Collector/1.0"})
            self.requests += 1
            self.last_request = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read().decode("utf-8")
                # Protect against accidental credential reflection by an upstream service.
                payload = _redact_secret(json.loads(body), self.api_key)
                if not isinstance(payload, dict):
                    raise CollectionError("API returned a non-object JSON response", stop_all=True,
                                          code="unexpected_response_schema",
                                          next_action="Check the service response and current Starter API documentation.")
                return payload, now_utc(), params
            except urllib.error.HTTPError as exc:
                code = exc.code
                retry = (exc.headers or {}).get("Retry-After", "")
                detail = _safe_provider_detail(exc, self.api_key)
                if code in (408, 429, 500, 502, 503, 504) and attempt + 1 < self.max_attempts:
                    wait = min(60, max(1, int(retry))) if retry.isdigit() else min(30, 2 ** (attempt + 1))
                    time.sleep(wait)
                    continue
                raise _http_failure(code, detail) from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                failure = _network_failure(exc)
                if failure.code not in ("tls_certificate_error", "tls_connection_error") and attempt + 1 < self.max_attempts:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise failure from None
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise CollectionError("API returned invalid JSON or text encoding", stop_all=True,
                                      code="invalid_response_json",
                                      next_action="The response was not usable JSON. Check service availability or a proxy/sign-in page intercepting the request.") from None


def query_for(faculty):
    identifiers = [faculty[field] for field in ("researcher_id", "orcid") if faculty[field]]
    return " OR ".join(f"AI=({value})" for value in identifiers)


def collect_one(faculty, state, client, output, publications, associations, lookup, checkpoint):
    state.update(status="running", started_at=now_utc(), query=query_for(faculty))
    page, hit_count = 1, 0
    expected = None
    safe_key = hashlib.sha256(faculty["faculty_key"].encode()).hexdigest()[:16]
    try:
        while True:
            payload, retrieved_at, params = client.get(state["query"], page)
            save_json(output / "raw" / safe_key / f"page_{page:05d}.json", {"faculty_key": faculty["faculty_key"], "retrieved_at": retrieved_at, "request_parameters": params, "response": payload})
            hits, metadata = payload.get("hits"), payload.get("metadata")
            if not isinstance(hits, list) or not isinstance(metadata, dict):
                raise CollectionError("API response lacks hits list or metadata object")
            total = metadata.get("total")
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise CollectionError("API response lacks a valid metadata.total")
            if metadata.get("page", page) != page:
                raise CollectionError("API returned a different page from the requested page")
            if expected is not None and total != expected:
                state["errors"].append("Result count changed during pagination; rerun this faculty search")
            expected = total
            state["expected_records"] = total
            for record in hits:
                if not isinstance(record, dict) or not isinstance(record.get("uid"), str) or not record["uid"]:
                    state["errors"].append(f"Page {page}: record lacks a usable WoS UID")
                    continue
                try:
                    row = make_publication(record, retrieved_at, lookup)
                except (TypeError, ValueError, AttributeError):
                    state["errors"].append(f"Page {page}: unexpected record structure for {record['uid']}")
                    continue
                merge_publication(publications, row)
                associations[faculty["faculty_key"]].add(record["uid"])
            hit_count += len(hits)
            state.update(pages=page, returned_hits=hit_count, retrieved_unique=len(associations[faculty["faculty_key"]]), last_retrieved_at=retrieved_at)
            checkpoint()
            if hit_count >= total:
                break
            if not hits:
                raise CollectionError("Pagination ended before metadata.total was reached")
            page += 1
        if len(associations[faculty["faculty_key"]]) != expected:
            state["errors"].append("Unique retrieved UID count does not equal metadata.total")
        state["status"] = "complete" if not state["errors"] else "incomplete"
    except CollectionError as exc:
        state["errors"].append(str(exc))
        state["status"] = "incomplete" if associations[faculty["faculty_key"]] else "failed"
        if exc.stop_all:
            raise
    finally:
        state["finished_at"] = now_utc()
        checkpoint()


def summary_for(keys, publications, associations, states, academic_year=None):
    uids = set().union(*(associations[key] for key in keys)) if keys else set()
    all_rows = [publications[uid] for uid in sorted(uids)]
    retrieval_complete = all(states[key]["status"] == "complete" for key in keys)
    unresolved = [p for p in all_rows if p["academic_year_start"] is None]
    possible_unassigned = unresolved if academic_year is None else [p for p in unresolved if p["assignment_year"] is None or p["assignment_year"] in (academic_year, academic_year + 1)]
    rows = all_rows if academic_year is None else [p for p in all_rows if p["academic_year_start"] == academic_year]
    complete = retrieval_complete and (academic_year is None or not possible_unassigned)
    missing = sum(p["current_wos_citations"] is None for p in rows)
    known_sum = sum(p["current_wos_citations"] for p in rows if p["current_wos_citations"] is not None)
    q1 = sum(p["best_supplied_quartile"] == "Q1" for p in rows)
    q2 = sum(p["best_supplied_quartile"] == "Q2" for p in rows)
    return {
        "retrieval_status": "complete" if retrieval_complete else "incomplete",
        "period_assignment_status": "all_publications" if academic_year is None else ("complete" if not possible_unassigned else "unassigned_records_may_belong_to_period"),
        "faculty_count": len(keys), "faculty_searches_complete": sum(states[k]["status"] == "complete" for k in keys),
        "publication_total": len(rows) if complete else None,
        "retrieved_publications_in_scope": len(rows),
        "current_citations_total": known_sum if complete and missing == 0 else None,
        "known_citations_sum_on_retrieved_publications": known_sum,
        "publications_missing_citations": missing,
        "q1_publications_supplied_categories": q1 if complete else None,
        "q2_publications_supplied_categories": q2 if complete else None,
        "observed_q1_publications": q1, "observed_q2_publications": q2,
        "publications_without_supplied_quartile": sum(not p["best_supplied_quartile"] for p in rows),
        "unassigned_publications_potentially_in_scope": len(possible_unassigned),
        "quartile_scope": SCOPE,
    }


SUMMARY_FIELDS = ["retrieval_status", "period_assignment_status", "faculty_count", "faculty_searches_complete", "publication_total", "retrieved_publications_in_scope", "current_citations_total", "known_citations_sum_on_retrieved_publications", "publications_missing_citations", "q1_publications_supplied_categories", "q2_publications_supplied_categories", "observed_q1_publications", "observed_q2_publications", "publications_without_supplied_quartile", "unassigned_publications_potentially_in_scope", "quartile_scope"]
PUBLICATION_FIELDS = ["uid", "title", "doi", "journal_name", "issn", "eissn", "publish_year_raw", "publish_month_raw", "assignment_year", "academic_year_start", "academic_year", "date_status", "document_types", "source_types", "current_wos_citations", "citation_status", "first_retrieved_at", "last_retrieved_at", "observations", "citation_count_changed_during_run", "publication_metadata_changed_during_run", "journal_match_status", "jcr_journal_key", "jcr_data_year", "best_supplied_quartile", "quartile_scope", "jcr_category_records", "wos_url"]


def export_reports(output, faculties, journals, publications, associations, states, requested_ay):
    collected_at = now_utc()
    jcr_years = "; ".join(sorted({r["jcr_year"] or "UNSPECIFIED" for r in journals}))
    common = {"report_generated_at": collected_at, "jcr_data_years": jcr_years}
    write_csv(output / "publications.csv", PUBLICATION_FIELDS, [publications[k] for k in sorted(publications)])
    links = [{"faculty_key": f["faculty_key"], "name": f["name"], "programme": f["programme"], "uid": uid, "faculty_search_status": states[f["faculty_key"]]["status"]} for f in faculties for uid in sorted(associations[f["faculty_key"]])]
    write_csv(output / "faculty_publications.csv", ["faculty_key", "name", "programme", "uid", "faculty_search_status"], links)
    faculty_rows = []
    programmes = defaultdict(list)
    for f in faculties:
        key = f["faculty_key"]
        programmes[f["programme"] or "Programme not supplied"].append(key)
        row = {**f, **summary_for([key], publications, associations, states), **common}
        row["query_expected_records"] = states[key]["expected_records"]
        row["search_errors"] = "; ".join(dict.fromkeys(states[key]["errors"]))
        faculty_rows.append(row)
    suffix = ["jcr_data_years", "report_generated_at"]
    write_csv(output / "faculty_summary.csv", ["faculty_key", "name", "programme", "orcid", "researcher_id"] + SUMMARY_FIELDS + ["query_expected_records", "search_errors"] + suffix, faculty_rows)
    programme_rows = [{"programme": name, **summary_for(keys, publications, associations, states), **common} for name, keys in sorted(programmes.items())]
    write_csv(output / "programme_summary.csv", ["programme"] + SUMMARY_FIELDS + suffix, programme_rows)
    all_keys = [f["faculty_key"] for f in faculties]
    write_csv(output / "department_union_summary.csv", ["population"] + SUMMARY_FIELDS + suffix, [{"population": "All supplied faculty", **summary_for(all_keys, publications, associations, states), **common}])
    years = {p["academic_year_start"] for p in publications.values() if p["academic_year_start"] is not None}
    for p in publications.values():
        if p["academic_year_start"] is None and p["assignment_year"] is not None:
            years.update([p["assignment_year"] - 1, p["assignment_year"]])
    years.update(requested_ay or [])
    groups = [("faculty", f["faculty_key"], f["name"], [f["faculty_key"]]) for f in faculties]
    groups += [("programme", name, name, keys) for name, keys in sorted(programmes.items())]
    groups += [("department_union", "all_supplied_faculty", "All supplied faculty", all_keys)]
    annual = []
    for level, key, name, keys in groups:
        for year in sorted(years):
            annual.append({"level": level, "group_key": key, "group_name": name, "academic_year": f"{year}–{year + 1}", "period_start": f"{year}-08-01", "period_end": f"{year + 1}-07-31", **summary_for(keys, publications, associations, states, year), **common})
    write_csv(output / "academic_year_summary.csv", ["level", "group_key", "group_name", "academic_year", "period_start", "period_end"] + SUMMARY_FIELDS + suffix, annual)
    unmatched = defaultdict(set)
    for p in publications.values():
        if p["journal_match_status"] != "matched":
            unmatched[(p["journal_name"], p["issn"], p["eissn"], p["journal_match_status"], p["jcr_journal_key"])].add(p["uid"])
    unmatched_rows = [dict(zip(["journal_name", "issn", "eissn", "match_status", "candidate_journal_keys"], key), publication_count=len(uids), publication_uids="; ".join(sorted(uids))) for key, uids in sorted(unmatched.items())]
    write_csv(output / "unmatched_journals.csv", ["journal_name", "issn", "eissn", "match_status", "candidate_journal_keys", "publication_count", "publication_uids"], unmatched_rows)


def self_test():
    import tempfile
    import unittest

    class CollectorTests(unittest.TestCase):
        def test_academic_year_boundaries(self):
            self.assertEqual(publication_period({"publishYear": 2025, "publishMonth": "AUG 1"})[1], 2025)
            self.assertEqual(publication_period({"publishYear": 2026, "publishMonth": "2026-07-31"})[1], 2025)
            self.assertEqual(publication_period({"publishYear": 2026, "publishMonth": "JUN-JUL"})[1], 2025)
            self.assertIsNone(publication_period({"publishYear": 2026, "publishMonth": "JUL-AUG"})[1])
            self.assertIsNone(publication_period({"publishYear": 2026, "publishMonth": "SUM"})[1])
            self.assertIsNone(publication_period({"publishYear": 2026})[1])
            self.assertIsNone(publication_period({"publishYear": 2026, "publishMonth": "DEC-JAN"})[1])
            self.assertIsNone(publication_period({"publishYear": 2025, "publishMonth": "FEB 30"})[1])

        def test_missing_citations_not_zero(self):
            self.assertEqual(wos_citations({}), (None, "unavailable"))
            self.assertEqual(wos_citations({"citations": [{"db": "WOS", "count": 0}]}), (0, "available"))
            self.assertIsNone(wos_citations({"citations": [{"db": "WOK", "count": 8}]})[0])
            self.assertIsNone(wos_citations({"citations": [{"db": "WOS", "count": -1}]})[0])

        def test_deduplication_and_partial_results(self):
            p = {"uid": "synthetic:1", "academic_year_start": 2025, "assignment_year": 2025, "current_wos_citations": 7, "best_supplied_quartile": "Q1"}
            pubs = {p["uid"]: p}
            links = {"A": {p["uid"]}, "B": {p["uid"]}}
            states = {"A": {"status": "complete"}, "B": {"status": "complete"}}
            result = summary_for(["A", "B"], pubs, links, states)
            self.assertEqual(result["publication_total"], 1)
            self.assertEqual(result["current_citations_total"], 7)
            states["B"]["status"] = "failed"
            self.assertIsNone(summary_for(["A", "B"], pubs, links, states)["publication_total"])
            self.assertEqual(summary_for(["A", "B"], pubs, links, states)["retrieved_publications_in_scope"], 1)
            states["B"]["status"] = "complete"
            p["academic_year_start"] = None
            self.assertIsNone(summary_for(["A"], pubs, links, states, 2025)["publication_total"])
            self.assertEqual(summary_for(["A"], pubs, links, states, 2030)["publication_total"], 0)

        def test_ambiguous_journal_match(self):
            base = {field: "" for field in JOURNAL_FIELDS}
            first = {**base, "journal_key": "A", "issn": "1234-567X", "category": "C1", "derived_jif_quartile": "Q2"}
            second = {**first, "journal_key": "B"}
            result = JournalLookup([first, second]).match("1234-567X", "")
            self.assertEqual(result["journal_match_status"], "ambiguous_issn")
            self.assertEqual(result["best_supplied_quartile"], "")

        def test_pagination_and_failed_second_page(self):
            class FakeClient:
                def __init__(self, fail=False):
                    self.fail = fail

                def get(self, query, page):
                    if self.fail and page == 2:
                        raise CollectionError("Synthetic second-page failure")
                    indexes = range(50) if page == 1 else [50]
                    hits = [{"uid": f"synthetic:{i}", "source": {"publishYear": 2025, "publishMonth": "AUG"}, "citations": [{"db": "WOS", "count": 0}]} for i in indexes]
                    return {"metadata": {"total": 51, "page": page}, "hits": hits}, "2026-01-01T00:00:00+00:00", {"page": page}

            faculty = {"faculty_key": "synthetic:A", "researcher_id": "SYN-0001-2025", "orcid": ""}
            for fail in (False, True):
                with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
                    state = {"status": "not_queried", "errors": [], "expected_records": None}
                    publications, associations = {}, {faculty["faculty_key"]: set()}
                    collect_one(faculty, state, FakeClient(fail), Path(folder), publications, associations, JournalLookup([]), lambda: None)
                    self.assertEqual(state["status"], "incomplete" if fail else "complete")
                    self.assertEqual(len(publications), 50 if fail else 51)

    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(CollectorTests))
    return 0 if result.wasSuccessful() else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--faculty", type=Path)
    parser.add_argument("--journals", type=Path)
    parser.add_argument("--output", type=Path, help="New or empty output directory")
    parser.add_argument("--jcr-year", help="Select one data year when inputs contain multiple years")
    parser.add_argument("--academic-year-start", type=int, action="append", help="Include this August-starting year even when no records are found; repeatable")
    parser.add_argument("--max-requests", type=int, default=5000)
    parser.add_argument("--request-interval", type=float, default=0.25, help="Seconds between requests; minimum 0.2")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    if not args.faculty or not args.journals:
        parser.error("--faculty and --journals are required")
    if args.max_requests < 1 or args.request_interval < 0.2:
        parser.error("Positive --max-requests and --request-interval >= 0.2 are required")
    if any(not 1000 <= year <= 9998 for year in (args.academic_year_start or [])):
        parser.error("Academic-year start must be between 1000 and 9998")
    try:
        faculties, journals, warnings = load_inputs(args.faculty, args.journals, args.jcr_year)
    except (ValueError, OSError) as exc:
        print(f"Input error: {exc}", file=sys.stderr)
        return 1
    validation = {"faculty_records": len(faculties), "journal_category_records": len(journals), "unique_journal_keys": len({r["journal_key"] for r in journals}), "categories": sorted({r["category"] for r in journals}), "jcr_data_years": sorted({r["jcr_year"] or "UNSPECIFIED" for r in journals}), "warnings": warnings, "quartile_scope": SCOPE, "network_requests": 0}
    if args.dry_run:
        print(json.dumps(validation, indent=2, ensure_ascii=False))
        return 0
    api_key = os.environ.get("WOS_API_KEY", "").strip()
    if not api_key:
        if not sys.stdin.isatty():
            print("WOS_API_KEY is not set; use an environment variable or an interactive terminal prompt.", file=sys.stderr)
            return 1
        api_key = getpass.getpass("WoS API key (hidden): ").strip()
    if not api_key:
        print("No API key supplied.", file=sys.stderr)
        return 1
    output = args.output or Path("wos_run_" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        print("Output directory must be new or empty to prevent mixing collection dates.", file=sys.stderr)
        return 1
    output.mkdir(parents=True, exist_ok=True)
    client = StarterClient(api_key, args.max_requests, args.request_interval)
    lookup, publications = JournalLookup(journals), {}
    associations = {f["faculty_key"]: set() for f in faculties}
    states = {f["faculty_key"]: {"status": "not_queried", "expected_records": None, "retrieved_unique": 0, "pages": 0, "errors": []} for f in faculties}
    manifest = {"collector_version": "1.0", "started_at": now_utc(), "api_endpoint": ENDPOINT, "database": "WOS", "document_type_filter": None, "academic_year_rule": "1 August through 31 July inclusive", "citation_definition": "Current WOS citation counts at record retrieval; not citations received within the academic year", "quartile_scope": SCOPE, "quartile_basis": "Input derived_jif_quartile, derived from exported JIF category rank", "input_validation": validation, "faculty_input_sha256": hashlib.sha256(args.faculty.read_bytes()).hexdigest(), "journal_input_sha256": hashlib.sha256(args.journals.read_bytes()).hexdigest(), "faculty_searches": states, "limitations": ["Identifier-query completeness is not proof of complete faculty career coverage or correct ownership.", "Records are retrieved sequentially; the collection is not an atomic database snapshot.", "Journal quartiles cover supplied categories only and are not global best quartiles.", "JCR year remains unspecified where source input does not establish it.", "Season-only and ambiguous publication dates remain unassigned.", "All document types returned by WoS are retained.", "Publication affiliations are not validated as institutional output."]}

    def checkpoint():
        manifest.update(updated_at=now_utc(), requests_made=client.requests, unique_publications_retrieved=len(publications))
        save_json(output / "manifest.json", manifest)

    try:
        for index, faculty in enumerate(faculties, 1):
            print(f"Collecting faculty {index}/{len(faculties)}: {faculty['name']}", flush=True)
            collect_one(faculty, states[faculty["faculty_key"]], client, output, publications, associations, lookup, checkpoint)
    except CollectionError as exc:
        manifest["run_error"] = str(exc)
        print(f"Collection stopped: {exc}. Partial results will be labeled incomplete.", file=sys.stderr)
    except KeyboardInterrupt:
        manifest["run_error"] = "Interrupted by user"
        for state in states.values():
            if state["status"] == "running":
                state["status"] = "incomplete"
                state["errors"].append("Interrupted by user")
        print("Interrupted. Saving incomplete results.", file=sys.stderr)
    finally:
        manifest["finished_at"] = now_utc()
        manifest["run_status"] = "complete" if all(s["status"] == "complete" for s in states.values()) else "incomplete"
        export_reports(output, faculties, journals, publications, associations, states, args.academic_year_start)
        checkpoint()
    print(f"Saved {manifest['run_status']} collection to {output}")
    return 0 if manifest["run_status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
