# GitHub workflow notes

## Available workflows

| Workflow | When it runs | What it does |
| --- | --- | --- |
| Tests | Push, pull request, or **Run workflow** | Builds the local dashboard and runs offline Python and JavaScript checks. No WoS credentials are supplied. |
| Test WoS connection | **Run workflow**, or a push to `main` changing this workflow or `scripts/check_auth.py` | Uses the `WOS_API_KEY` repository secret to test API access and a faculty identifier search. A rejected key also receives a bounded independent authentication check. Produces a readable run summary and diagnostics. |
| Refresh research data | **Run workflow** | Performs the connection check, collects faculty records, and creates a dashboard artifact with an HTML report, saved data, and diagnostics. |

Leave the refresh workflow's starting-year field blank to select the current August–July academic year in `Asia/Riyadh`. Use `2025` to select 1 August 2025 through 31 July 2026. This input controls the initially selected reporting period; collection retrieves the available faculty publication history so other years can be selected in the report.

No workflow is scheduled. Collection starts when a repository user runs the refresh workflow. Connection tests can also run after changes to their own workflow or independent authentication-check script on `main`; ordinary application or documentation changes do not trigger a connection test. The two API workflows share a concurrency group, so they do not collect simultaneously. Additional pending runs can be replaced by later queued runs under GitHub's concurrency rules.

## Credentials and output

The API secret is supplied only to API-call steps as an environment variable. It is not a workflow input, command-line argument, or tracked source file. Workflows request only `contents: read`, and checkout does not persist GitHub credentials. Retrieved data is uploaded as a private repository workflow artifact, not committed into source control or published to GitHub Pages.

When the normal connection test reports HTTP 401, `scripts/check_auth.py` checks for obvious copying problems and compares curl requests to the documented Starter v1 and v2 endpoints. It makes at most two additional requests, without retries, redirects or disabled TLS verification. The key is sent to curl through its standard input configuration and removed from the child environment; it never appears in command arguments or an output file. Reports include only boolean format flags, status codes and sanitized result summaries. A successful independent request is diagnostic evidence, not permission to ignore a failed primary check. The result is saved in `DEH_Authentication_Check.json` beside the original diagnostics.

Artifacts are configured for 90 days of retention, subject to the repository or organization's allowed limit. They are not a permanent research archive. Download reports and their saved data before expiry if you need to preserve a reporting snapshot. A new collection has a distinct artifact name and does not replace previous run artifacts.

Failed connection checks and incomplete collections return a nonzero exit status. Diagnostic upload and the run summary use `if: always()` so an ordinary collection failure still has an explanation. A cancelled, timed-out, or infrastructure-failed run may stop before a diagnostics file can be created; in that case, inspect its failed step.

## Action versions

The workflow actions are pinned to full commit SHAs verified against their upstream release pages on 21 November 2026:

| Action | Release | Commit |
| --- | --- | --- |
| [actions/checkout](https://github.com/actions/checkout/releases/tag/v7.0.1) | v7.0.1 | `3d3c42e5aac5ba805825da76410c181273ba90b1` |
| [actions/setup-python](https://github.com/actions/setup-python/releases/tag/v7.1.0) | v7.1.0 | `6d4fd22b20465ccc0f7354476588e8df7c9ca0e0` |
| [actions/setup-node](https://github.com/actions/setup-node/releases/tag/v7.1.0) | v7.1.0 | `949feb2413d6458794dcd2491c4babbbce0c15c1` |
| [actions/upload-artifact](https://github.com/actions/upload-artifact/releases/tag/v7.0.2) | v7.0.2 | `cf430e030ddbb5b0abf93d22962f4752f3646cd9` |

The workflows use GitHub-hosted Ubuntu runners, Python 3.12, and Node 24 for the offline interface checks. Runtime collection uses Python's standard library. The independent authentication check uses the runner's curl executable.

## Official documentation

- [Run a workflow manually](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow).
- [Use repository secrets in GitHub Actions](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets).
- [Download workflow artifacts](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/download-workflow-artifacts).
- [Artifact retention settings](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository).
- [Control workflow concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).
