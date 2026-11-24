"""Bounded, credential-safe checks for the Web of Science Starter API.

The general query verifies a usable authenticated response independently of
faculty identifier coverage. An optional second query checks one faculty member.
Neither query retrieves the complete dataset or proves profile completeness.
"""

from src.collector import (
    CollectionError, ENDPOINT, StarterClient, _redact_secret, now_utc,
    query_for, wos_citations,
)


def _validate_probe(payload):
    """Accept zero only when a valid API result explicitly reports zero."""
    hits = payload.get("hits") if isinstance(payload, dict) else None
    metadata = payload.get("metadata") if isinstance(payload, dict) else None
    if not isinstance(hits, list) or not isinstance(metadata, dict):
        raise CollectionError("The API response did not contain a hits list and metadata object.",
                              stop_all=True, code="unexpected_response_schema",
                              next_action="Check service availability and the current Starter API response format.")
    total = metadata.get("total")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise CollectionError("The API response did not contain a valid metadata.total.",
                              stop_all=True, code="unexpected_response_schema",
                              next_action="A missing count cannot be treated as zero; review the API response format.")
    if metadata.get("page", 1) != 1 or len(hits) > 1 or len(hits) != min(total, 1):
        raise CollectionError("The API response did not match the requested first-page sample.",
                              stop_all=True, code="unexpected_response_schema",
                              next_action="Review the API response format and pagination parameters.")
    if any(not isinstance(row, dict) or not isinstance(row.get("uid"), str) or not row["uid"] for row in hits):
        raise CollectionError("The sampled record did not contain a usable Web of Science UID.",
                              stop_all=True, code="unexpected_response_schema",
                              next_action="Review the API response format before collecting records.")
    return total, hits


def run_preflight(api_key, faculty=None, client_factory=None):
    """Return safe diagnostics after at most two HTTP attempts (one per query).

    ``ok`` permits a full collection. A valid zero-match faculty result is a
    warning, not an authentication failure. Callers must stop on ``ok=False``.
    API credentials and raw publication records are never included in the result.
    ``client_factory`` is an offline-test seam; production uses StarterClient.
    """
    report = {
        "ok": False,
        "status": "failed",
        "code": "not_started",
        "message": "API checks have not started.",
        "next_action": "",
        "endpoint": ENDPOINT,
        "database": "WOS",
        "checked_at": now_utc(),
        "requests_made": 0,
        "checks": [],
    }
    if not isinstance(api_key, str) or not api_key.strip():
        report.update(code="missing_api_key", message="No Web of Science API key was supplied.",
                      next_action="Add the repository Actions secret WOS_API_KEY, then run the workflow again.")
        return report
    key = api_key.strip()
    if len(key) > 512 or any(ord(char) < 32 or ord(char) == 127 for char in key):
        report.update(code="invalid_api_key_format", message="The API key contains unsupported control characters or is too long.",
                      next_action="Copy the active Starter API key into WOS_API_KEY without quotation marks or embedded line breaks.")
        return report

    client = None
    check = None
    try:
        client = (client_factory or StarterClient)(key, max_requests=2, interval=0.25,
                                                   max_attempts=1, timeout=30)
        queries = [("connection", "PY=2020")]
        if faculty is not None:
            query = query_for(faculty)
            if not query:
                raise CollectionError("The selected faculty record has no author identifiers.",
                                      code="missing_faculty_identifier",
                                      next_action="Supply a valid ResearcherID or ORCID before testing this faculty member.")
            queries.append(("faculty_search", query))
        for name, query in queries:
            check = {"name": name, "query": query, "status": "running"}
            if name == "faculty_search":
                check["faculty_key"] = faculty.get("faculty_key", "")
            report["checks"].append(check)
            payload, retrieved_at, _params = client.get(query, page=1, limit=1)
            total, hits = _validate_probe(payload)
            citation_count = wos_citations(hits[0])[0] if hits else None
            check.update(status="passed", retrieved_at=retrieved_at,
                         matching_records=total, sampled_records=len(hits),
                         current_wos_citation_count_present_in_sample=(citation_count is not None) if hits else None)

        report.update(ok=True, status="passed", code="ready",
                      message="The Starter API returned a valid authenticated WOS response.",
                      next_action="Run the full collection to retrieve all faculty records and build the dashboard.")
        if faculty is not None and report["checks"][-1]["matching_records"] == 0:
            report.update(status="warning", code="faculty_no_matches",
                          message="The API connection worked, but the tested faculty identifiers returned zero matching records.",
                          next_action="Check this faculty member's ResearcherID/ORCID and linked publications. Other faculty searches may still return records.")
        elif report["checks"][0]["matching_records"] == 0:
            report.update(status="warning", code="general_query_no_matches",
                          message="The API returned a valid response with zero records for the general connection-test query.",
                          next_action="Verify the key's WOS coverage in the developer portal; a full faculty collection may still proceed.")
    except CollectionError as exc:
        report.update(code=exc.code, message=exc.message, next_action=exc.next_action)
        if exc.http_status is not None:
            report["http_status"] = exc.http_status
        if exc.provider_detail:
            report["provider_detail"] = exc.provider_detail
        if check is not None:
            check.update(status="failed", error_code=exc.code)
    except Exception:
        # Do not print an arbitrary exception: upstream messages may reflect a key.
        report.update(code="diagnostic_internal_error", message="The connection check could not be completed.",
                      next_action="Review the input files and run the project's offline tests. No successful API check has been confirmed.")
        if check is not None:
            check.update(status="failed", error_code="diagnostic_internal_error")
    finally:
        if client is not None:
            report["requests_made"] = getattr(client, "requests", 0)
            client.api_key = ""
    return _redact_secret(report, key)
