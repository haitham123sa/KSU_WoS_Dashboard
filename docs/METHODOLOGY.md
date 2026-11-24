# Reporting methodology

## Scope and identity

This project searches Web of Science Core Collection through the **Web of Science Starter API** using the ResearcherID and/or ORCID supplied for each faculty member. The query is the union of the available identifiers. ORCID syntax/checksum checks and ResearcherID format checks validate input structure; they do not verify ownership or completeness of linked publications.

The initial prepared roster has 23 faculty members: 13 in Dental Technology and 10 in Dental Hygiene. One faculty member has an ORCID without a ResearcherID. Names and programme labels follow the supplied roster. Details of input normalization and original file hashes are recorded in `data/input_manifest.json` and `data/validation.json`.

This collection does not restrict records to current institutional affiliations, employment dates or selected document types. “All years” describes all records returned through the configured identifier searches and API coverage, not an independently verified complete career bibliography.

## Record completeness and deduplication

The collector paginates records and compares distinct WoS identifiers with the reported result count. It marks errors, missing pages, changing result counts and duplicate-page anomalies as incomplete rather than silently treating partial output as a complete result.

For each faculty member, a WoS record is counted once even if both identifiers match it. Programme and whole-roster totals use a union of record identifiers. If two faculty members coauthor a publication, it appears in both individual faculty views and once in the combined group view. Citation totals use the same deduplicated publication set.

## Academic years

Academic years run from **1 August through 31 July**. The starting year identifies the period:

| Starting year | Inclusive period |
| --- | --- |
| 2025 | 1 August 2025–31 July 2026 |
| 2026 | 1 August 2026–31 July 2027 |
| 2027 | 1 August 2027–31 July 2028 |

The collector uses the source publication date fields returned by Starter. A month and year suffice to identify this boundary; an exact day is not invented. A named month range is assigned only if the whole range belongs to one academic year. Year-only, seasonal, inconsistent or boundary-crossing dates remain unassigned. The report shows unassigned records and flags periods they could affect.

It does not substitute a record-loading date for a publication date. The Starter response does not provide all date detail found in other Web of Science products; in particular, this implementation does not reconstruct separate Early Access histories.

## Citations

“Current citations” uses the WoS Core Collection count available for each retrieved publication at collection time. A citation total for an academic-year cohort adds the current counts on papers assigned to that cohort.

For example, a paper published in September 2025 belongs to 2025–26. Its current count may include citations received after 31 July 2026. This project therefore does **not** label the cohort sum as “citations received in 2025–26.” A citing-record history or appropriate historical source would be needed to measure that different quantity.

An absent citation field is unknown, not zero. An observed numeric zero remains zero. If a required count is missing or the relevant retrieval is incomplete, complete citation totals remain unavailable; the interface can still show the known sum and the missing-record count with that limitation.

## JCR matching and quartiles

The initial reference data use **JCR data year 2025**, independently of the selected academic year. Matching uses normalized ISSN/eISSN. Missing identifiers, ambiguous matches and conflicting category records are retained as explicit unresolved states; journal titles are not used to guess a match.

The supplied exports cover three categories:

| Category | Exported journal-category rows |
| --- | ---: |
| Dentistry, Oral Surgery & Medicine | 165 |
| Materials Science, Biomaterials | 58 |
| Public, Environmental & Occupational Health | 445 |
| Total | 668 |

These rows represent 662 distinct journal records, with six journals occurring in more than one supplied category. The exact input counts and hashes are in the manifest.

For a category rank `r` among `N` journals, the prepared data derive the JIF quartile using:

```text
quartile number = ceil(4 × r / N)
```

The exported rank and denominator are retained for audit. Tied ranks can mean that the four quartiles do not contain equal numbers of journals. JIF values such as `<0.1` are preserved as provided.

For a journal in multiple supplied categories, the publication's Q1/Q2 label uses the best available quartile **among those supplied categories only**. It does not assert that all of the journal's JCR categories have been examined. Journals absent from the supplied references remain unclassified. The journal-level JCR Total Citations field is never substituted for publication-level or faculty citation counts.

See the [official JCR glossary](https://journalcitationreports.zendesk.com/hc/en-gb/articles/28351666061457-Glossary) for rank and quartile terminology.

## Refresh and retention

The GitHub collection workflow takes a new observation of currently accessible records and citations. It stores the report, data snapshot and sanitized diagnostics as private-repository run artifacts. It does not commit retrieved records to the source branch. Artifact retention is requested as 90 days, subject to GitHub repository policy; download files needed for long-term records.

The standalone HTML report is a saved snapshot. Its selectors change the displayed scope; they do not contact WoS. Refreshing source data requires a new workflow run and a newly downloaded report. The report's collection time distinguishes those versions.

The local app preserves a previous complete saved report when a subsequent refresh is incomplete. GitHub runs are isolated: a failed new run does not replace artifacts from an older run, and its diagnostics must be read to determine whether any newly retrieved records are usable.

## Updating inputs

Edit the prepared faculty CSV when the roster changes, preserving unique `faculty_key` values and valid identifiers. Validate ownership and profile coverage separately. The reference workbook and original source files are retained for audit; the collector reads the prepared CSV files.

For a new JCR data year, prepare a consistent set of journal-category records, preserve source rank/denominator and year, and update the provenance manifest and reference workbook. Merely changing the academic-year input does not update quartiles. The collector supports an explicitly selected single JCR reference year; mixing years without selection is rejected.

## API documentation

The endpoint, authentication header, query fields and response structure are documented in the [official Starter API specification](https://developer.clarivate.com/apis/wos-starter/swagger). Access, request limits and returned fields depend on the API application's plan. Offline tests cannot confirm the permissions of a key that has not been used in a live connection test.
