# Local dashboard backend. Bundled after the validated collector and input data.
import copy
import hmac
import http.server
import secrets
import tempfile
import threading
import webbrowser

APP_VERSION = "1.0"
CACHE_SCHEMA_VERSION = 1
MAX_REQUEST_BYTES = 16 * 1024
MAX_CACHE_BYTES = 512 * 1024 * 1024
MAX_RAW_BYTES = 250 * 1024 * 1024


def app_json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def app_bad_json_constant(_value):
    raise ValueError("The data contain an invalid JSON number.")


def app_scrub(value, secret):
    """Remove the credential before retaining any upstream text or metadata."""
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]")
    if isinstance(value, dict):
        return {app_scrub(k, secret) if isinstance(k, str) else k: app_scrub(v, secret) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [app_scrub(v, secret) for v in value]
    return value


class MemoryCapture:
    def __init__(self):
        self.pages = {}
        self.byte_count = 0

    def put(self, key, value):
        size = len(app_json_bytes(value))
        if self.byte_count + size > MAX_RAW_BYTES:
            raise CollectionError("The collection exceeded the local record-size limit.", stop_all=True)
        self.pages[key] = value
        self.byte_count += size


class MemoryPath:
    def __init__(self, capture, parts=()):
        self.capture = capture
        self.parts = parts

    def __truediv__(self, part):
        return MemoryPath(self.capture, self.parts + (str(part),))


_original_core_save_json = save_json


def save_json(path, value):
    """Core collector raw-page writes stay in the single dashboard data file."""
    if isinstance(path, MemoryPath):
        path.capture.put("/".join(path.parts), value)
        return
    _original_core_save_json(path, value)


def app_atomic_save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = app_json_bytes(value)
    if len(encoded) > MAX_CACHE_BYTES:
        raise OSError("Dashboard data are too large to save.")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".DEH_Data_", suffix=".tmp", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def app_error_message(exc):
    if isinstance(exc, CollectionError):
        message = str(exc)
        if message.startswith("HTTP 401"):
            return "Web of Science rejected the API key (HTTP 401). Check that you pasted the complete Starter API key."
        if message.startswith("HTTP 403"):
            return "Web of Science rejected access (HTTP 403). Check that this key has access to the Starter API."
        if "request budget" in message.lower():
            return "The dashboard's per-refresh request limit was reached. This refresh is incomplete."
        if message.startswith("HTTP 429"):
            return "The request allowance was reached. Try again when your Web of Science allowance is available."
        if "Network request failed" in message:
            return "The Web of Science connection failed after retries. Check your internet connection and Python HTTPS certificate setup."
        return message
    return "An unexpected error stopped the refresh. No API key was saved."


class DehDashboardApp:
    def __init__(self, data_path, client_factory=None, faculties=None, journals=None):
        self.data_path = Path(data_path).expanduser().resolve()
        self.faculty = copy.deepcopy(FACULTY_DATA if faculties is None else faculties)
        self.journals = copy.deepcopy(JOURNAL_DATA if journals is None else journals)
        self.keys = [f["faculty_key"] for f in self.faculty]
        if not self.keys or len(set(self.keys)) != len(self.keys):
            raise ValueError("The embedded faculty list is empty or has duplicate identifiers.")
        self.client_factory = client_factory or StarterClient
        self.input_fingerprint = hashlib.sha256(app_json_bytes({"faculty": self.faculty, "journals": self.journals})).hexdigest()
        self.jcr_year = "; ".join(sorted({str(j.get("jcr_year") or "Unspecified") for j in self.journals})) or "Unspecified"
        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(32)
        self.snapshot = None
        self.raw_pages = {}
        self.version = 0
        self.storage_warning = ""
        self.persistence_blocked = False
        self.worker = None
        self.job = self._idle_job()
        self._load_cache()

    def _idle_job(self):
        return {"running": False, "status": "idle", "done": 0, "total": len(self.faculty), "faculty_name": "", "pages": 0, "requests_made": 0,
                "message": "Enter your Starter API key and select Refresh from WoS.", "started_at": None, "finished_at": None}

    def _snapshot_from_records(self, publications, associations, states, requested_year, run_status, collected_at=None, version=None):
        current_year = dt.datetime.now().year
        years = set(range(2025, max(2030, current_year + 2) + 1))
        years.add(requested_year)
        for publication in publications.values():
            year = publication.get("academic_year_start")
            if isinstance(year, int) and not isinstance(year, bool):
                years.add(year)
            elif isinstance(publication.get("assignment_year"), int) and not isinstance(publication.get("assignment_year"), bool):
                source_year = publication["assignment_year"]
                years.update((source_year - 1, source_year))
        years = sorted(y for y in years if 1 <= y <= 9998)
        programme_keys = defaultdict(list)
        groups = []
        for faculty in self.faculty:
            key = faculty["faculty_key"]
            programme = faculty.get("programme") or "Programme not supplied"
            programme_keys[programme].append(key)
            groups.append(("faculty", key, faculty["name"], [key]))
        groups.extend(("programme", programme, programme, keys) for programme, keys in sorted(programme_keys.items()))
        groups.append(("department_union", "all_supplied_faculty", "All supplied faculty", self.keys))
        summaries = []
        for level, key, name, keys in groups:
            summaries.append({"level": level, "group_key": key, "group_name": name, "period": "all", **summary_for(keys, publications, associations, states)})
            for year in years:
                summaries.append({"level": level, "group_key": key, "group_name": name, "period": year, **summary_for(keys, publications, associations, states, year)})
        return {"collected_at": collected_at or now_utc(), "run_status": run_status, "version": version if version is not None else self.version + 1,
                "publications": [publications[key] for key in sorted(publications)],
                "associations": {key: sorted(associations[key]) for key in self.keys}, "states": copy.deepcopy(states),
                "summary_rows": summaries, "years": years, "requested_academic_year": requested_year, "jcr_year": self.jcr_year, "quartile_scope": SCOPE}

    def _validate_cached_records(self, snapshot):
        if not isinstance(snapshot, dict) or snapshot.get("run_status") not in {"complete", "incomplete"}:
            raise ValueError("Invalid snapshot status")
        if not isinstance(snapshot.get("collected_at"), str):
            raise ValueError("Missing collection time")
        records, links, states = snapshot.get("publications"), snapshot.get("associations"), snapshot.get("states")
        if not isinstance(records, list) or not isinstance(links, dict) or not isinstance(states, dict):
            raise ValueError("Invalid saved record structure")
        if set(links) != set(self.keys) or set(states) != set(self.keys):
            raise ValueError("Faculty identifiers do not match")
        publications = {}
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("uid"), str) or not record["uid"] or record["uid"] in publications:
                raise ValueError("Invalid or duplicated publication identifier")
            for field in ("assignment_year", "academic_year_start", "current_wos_citations"):
                value = record.get(field)
                if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                    raise ValueError("Invalid saved publication measure")
                if field not in record:
                    raise ValueError("Missing saved publication measure")
            if record.get("best_supplied_quartile") not in {"", "Q1", "Q2", "Q3", "Q4"}:
                raise ValueError("Invalid saved quartile")
            publications[record["uid"]] = record
        associations = {}
        for key in self.keys:
            if not isinstance(links[key], list) or any(not isinstance(uid, str) or uid not in publications for uid in links[key]):
                raise ValueError("Invalid faculty-publication association")
            if len(set(links[key])) != len(links[key]):
                raise ValueError("Duplicate saved association")
            associations[key] = set(links[key])
            state = states[key]
            if not isinstance(state, dict) or state.get("status") not in {"complete", "incomplete", "failed", "not_queried"}:
                raise ValueError("Invalid faculty collection state")
            if not isinstance(state.get("errors"), list) or any(not isinstance(error, str) for error in state["errors"]):
                raise ValueError("Invalid saved collection errors")
        if snapshot["run_status"] == "complete" and any(states[key]["status"] != "complete" for key in self.keys):
            raise ValueError("Saved completion status is inconsistent")
        return publications, associations, states

    def _load_cache(self):
        if not self.data_path.exists():
            return
        try:
            if not self.data_path.is_file() or self.data_path.stat().st_size > MAX_CACHE_BYTES:
                raise ValueError("Unsupported saved file")
            saved = json.loads(self.data_path.read_text(encoding="utf-8"), parse_constant=app_bad_json_constant)
            app_json_bytes(saved)  # Reject non-finite values such as an overflowing 1e309 anywhere in the file.
            if not isinstance(saved, dict) or saved.get("schema_version") != CACHE_SCHEMA_VERSION or saved.get("input_fingerprint") != self.input_fingerprint:
                raise ValueError("Saved data are incompatible with this app")
            records, links, states = self._validate_cached_records(saved.get("snapshot"))
            old = saved["snapshot"]
            version = old.get("version", 1)
            if not isinstance(version, int) or isinstance(version, bool) or version < 1:
                raise ValueError("Invalid saved version")
            requested_year = saved.get("requested_academic_year", 2025)
            if not isinstance(requested_year, int) or isinstance(requested_year, bool) or not 1000 <= requested_year <= 9998:
                raise ValueError("Invalid saved academic year")
            raw_pages = saved.get("raw_pages", {})
            if not isinstance(raw_pages, dict) or any(not isinstance(key, str) or not isinstance(value, dict) for key, value in raw_pages.items()):
                raise ValueError("Invalid raw records")
            self.snapshot = self._snapshot_from_records(records, links, states, requested_year, old["run_status"], old["collected_at"], version)
            self.raw_pages = raw_pages
            self.version = version
            self.job["message"] = "Saved data loaded. Refresh from WoS when you want updated records and citations."
        except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
            self.persistence_blocked = True
            self.storage_warning = "The existing data file could not be read or uses different inputs. It has been preserved. New results can be viewed and downloaded, but will not overwrite that file."
            self.job["status"] = "failed"
            self.job["message"] = self.storage_warning

    def public_state(self, for_export=False):
        with self.lock:
            value = {"faculty": self.faculty, "journals": self.journals, "storage_path": str(self.data_path),
                     "storage_warning": self.storage_warning, "snapshot": self.snapshot, "job": self.job, "app_version": APP_VERSION}
            value = copy.deepcopy(value)
        if for_export:
            value.pop("storage_path", None)
            value.pop("storage_warning", None)
            value["job"] = {"running": False, "status": "idle", "done": 0, "total": len(self.faculty), "faculty_name": "", "pages": 0,
                            "requests_made": 0, "started_at": None, "finished_at": None, "message": "Saved report. Open the local dashboard app to refresh from WoS."}
        return value

    def status(self):
        with self.lock:
            return {"job": copy.deepcopy(self.job), "snapshot_version": self.version}

    def refresh(self, api_key, academic_year_start):
        if not isinstance(api_key, str) or not 8 <= len(api_key.strip()) <= 512 or any(ord(char) < 32 or ord(char) == 127 for char in api_key):
            raise ValueError("Paste the complete Web of Science Starter API key.")
        api_key = api_key.strip()
        if not isinstance(academic_year_start, int) or isinstance(academic_year_start, bool) or not 1000 <= academic_year_start <= 9998:
            raise ValueError("Enter a valid academic-year starting year.")
        with self.lock:
            if self.job["running"]:
                raise RuntimeError("A refresh is already running.")
            self.job = {**self._idle_job(), "running": True, "status": "running", "started_at": now_utc(),
                        "message": "Connecting to Web of Science. Existing report data remain visible while the refresh runs."}
            self.worker = threading.Thread(target=self._refresh_worker, args=(api_key, academic_year_start), name="DEH-refresh", daemon=True)
            self.worker.start()

    def _refresh_worker(self, api_key, requested_year):
        publications = {}
        associations = {key: set() for key in self.keys}
        states = {key: {"status": "not_queried", "expected_records": None, "retrieved_unique": 0, "pages": 0, "errors": []} for key in self.keys}
        capture = MemoryCapture()
        client = None
        failure = None

        def checkpoint():
            with self.lock:
                self.job.update(done=sum(state["status"] in {"complete", "incomplete", "failed"} for state in states.values()),
                                pages=sum(state.get("pages", 0) for state in states.values()), requests_made=getattr(client, "requests", 0))

        try:
            client = self.client_factory(api_key, max_requests=5000, interval=0.25)
            lookup = JournalLookup(self.journals)
            for faculty in self.faculty:
                with self.lock:
                    self.job["faculty_name"] = faculty["name"]
                    self.job["message"] = "Collecting publication records from Web of Science."
                collect_one(faculty, states[faculty["faculty_key"]], client, MemoryPath(capture), publications, associations, lookup, checkpoint)
        except Exception as exc:
            failure = app_scrub(app_error_message(exc), api_key)
            for state in states.values():
                if state["status"] == "running":
                    state["status"] = "incomplete"
                    state["errors"].append(failure)
        finally:
            checkpoint()
            if client is not None:
                try:
                    client.api_key = ""
                except Exception:
                    pass

        try:
            complete = failure is None and all(state["status"] == "complete" for state in states.values())
            report = self._snapshot_from_records(publications, associations, states, requested_year, "complete" if complete else "incomplete")
            report = app_scrub(report, api_key)
            raw_pages = app_scrub(capture.pages, api_key)
            with self.lock:
                old = self.snapshot
                keep_old = not complete and old is not None and (old.get("run_status") == "complete" or (old.get("publications") and not report["publications"]))
            if keep_old:
                message = (failure or "Some faculty searches were incomplete.") + " The previous saved report has been retained."
            else:
                message = "Refresh complete. Publication records and current citations were updated." if complete else (failure or "Refresh incomplete. Available records are shown, and incomplete totals remain unavailable.")
                saved_ok = False
                if not self.persistence_blocked:
                    payload = {"schema_version": CACHE_SCHEMA_VERSION, "app_version": APP_VERSION, "input_fingerprint": self.input_fingerprint,
                               "faculty": self.faculty, "journals": self.journals, "requested_academic_year": requested_year,
                               "snapshot": report, "raw_pages": raw_pages}
                    payload = app_scrub(payload, api_key)
                    try:
                        app_atomic_save(self.data_path, payload)
                        saved_ok = True
                    except (OSError, ValueError, TypeError):
                        self.storage_warning = "Results are available in this window, but the data file could not be saved. Download a report before closing."
                with self.lock:
                    self.snapshot = report
                    self.raw_pages = raw_pages
                    self.version = report["version"]
                    if saved_ok:
                        self.storage_warning = ""
                if self.storage_warning:
                    message += " " + self.storage_warning
            with self.lock:
                self.job.update(running=False, status="complete" if complete else "incomplete", message=app_scrub(message, api_key), finished_at=now_utc())
        except Exception:
            with self.lock:
                self.job.update(running=False, status="failed", message="The refresh could not finish preparing its report. The previous saved report was retained.", finished_at=now_utc())
        finally:
            api_key = ""

    def render_html(self, offline=False, refresh_context=None):
        remote = bool(offline and refresh_context and refresh_context.get("mode") == "remote")
        if remote:
            bootstrap = {"mode": "remote", "api_base": refresh_context.get("api_base", ""), "state": self.public_state(for_export=True)}
            bootstrap["state"]["refresh_context"] = copy.deepcopy(refresh_context)
        else:
            bootstrap = {"mode": "offline", "state": self.public_state(for_export=True)} if offline else {"mode": "live", "token": self.token}
            if offline and refresh_context:
                bootstrap["state"]["refresh_context"] = copy.deepcopy(refresh_context)
        encoded = json.dumps(bootstrap, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        encoded = encoded.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e").replace("\\u2028", "\\u2028").replace("\\u2029", "\\u2029")
        if UI_HTML.count("__DEH_BOOTSTRAP_JSON__") != 1:
            raise ValueError("Dashboard template is missing its data placeholder.")
        html = UI_HTML.replace("__DEH_BOOTSTRAP_JSON__", encoded)
        if offline and not remote:
            offline_policy = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'"
            html = html.replace("<head>", '<head><meta http-equiv="Content-Security-Policy" content="' + offline_policy + '">', 1)
        elif remote:
            api_origin = str(refresh_context.get("api_base", "")).rstrip("/")
            remote_policy = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'self' " + api_origin + "; base-uri 'none'; form-action 'none'"
            html = html.replace("<head>", '<head><meta http-equiv="Content-Security-Policy" content="' + remote_policy + '">', 1)
        return html.encode("utf-8")


class DehRequestHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "DEHLocal"
    sys_version = ""

    def log_message(self, _format, *args):
        pass

    def _reply(self, status, body, content_type="application/json; charset=utf-8", download=None):
        if not isinstance(body, bytes):
            body = app_json_bytes(body)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.send_header("Connection", "close")
        if download:
            self.send_header("Content-Disposition", 'attachment; filename="' + download + '"')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        self.close_connection = True

    def _validate_request(self, require_token=True, require_origin=False):
        expected_host = f"127.0.0.1:{self.server.server_port}"
        expected_origin = "http://" + expected_host
        if self.headers.get("Host", "") != expected_host:
            self._reply(403, {"error": "This dashboard accepts local requests only."})
            return False
        origin = self.headers.get("Origin")
        if (require_origin and origin != expected_origin) or (origin is not None and origin != expected_origin):
            self._reply(403, {"error": "This request did not come from the local dashboard."})
            return False
        if require_token:
            token = self.headers.get("X-DEH-Token", "")
            if len(token) > 512 or not hmac.compare_digest(token.encode("utf-8"), self.server.app.token.encode("ascii")):
                self._reply(403, {"error": "Reopen the local dashboard page and try again."})
                return False
        return True

    def do_GET(self):
        route = urllib.parse.urlsplit(self.path)
        if route.query or route.fragment:
            self._reply(404, {"error": "Not found."})
            return
        if route.path in {"/", "/favicon.ico"}:
            if not self._validate_request(require_token=False):
                return
            if route.path == "/favicon.ico":
                self._reply(204, b"", "image/x-icon")
            else:
                self._reply(200, self.server.app.render_html(), "text/html; charset=utf-8")
            return
        if not self._validate_request():
            return
        if route.path == "/api/state":
            self._reply(200, self.server.app.public_state())
        elif route.path == "/api/status":
            self._reply(200, self.server.app.status())
        elif route.path == "/api/report":
            if self.server.app.snapshot is None:
                self._reply(409, {"error": "Refresh from WoS before saving a report."})
            else:
                self._reply(200, self.server.app.render_html(offline=True), "text/html; charset=utf-8", "DEH_Research_Report.html")
        else:
            self._reply(404, {"error": "Not found."})

    def do_POST(self):
        if not self._validate_request(require_origin=True):
            return
        if self.path != "/api/refresh":
            self._reply(404, {"error": "Not found."})
            return
        if self.headers.get("Transfer-Encoding"):
            self._reply(400, {"error": "Unsupported request encoding."})
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self._reply(415, {"error": "Send dashboard refresh data as JSON."})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except (TypeError, ValueError):
            self._reply(411, {"error": "A request length is required."})
            return
        if not 1 <= length <= MAX_REQUEST_BYTES:
            self._reply(413, {"error": "The refresh request is too large or empty."})
            return
        try:
            self.connection.settimeout(10)
            payload = json.loads(self.rfile.read(length).decode("utf-8"), parse_constant=app_bad_json_constant)
            app_json_bytes(payload)
            if not isinstance(payload, dict):
                raise ValueError("Invalid refresh request.")
            key = payload.pop("api_key", None)
            year = payload.get("academic_year_start", 2025)
            self.server.app.refresh(key, year)
            key = None
            self._reply(202, {"accepted": True})
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._reply(400, {"error": "The refresh request could not be read."})
        except ValueError as exc:
            self._reply(400, {"error": str(exc)})
        except RuntimeError as exc:
            self._reply(409, {"error": str(exc)})
        except (OSError, TimeoutError):
            self._reply(408, {"error": "The local refresh request timed out. Try again."})

    def do_PUT(self):
        self._reply(405, {"error": "Method not supported."})

    do_DELETE = do_PUT
    do_PATCH = do_PUT
    do_OPTIONS = do_PUT


class DehLocalServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, port, app):
        super().__init__(("127.0.0.1", port), DehRequestHandler)
        self.app = app


def app_main(argv=None):
    parser = argparse.ArgumentParser(description="Open the DEH research dashboard on this Mac. Data and API requests stay local to this application and Web of Science.")
    parser.add_argument("--no-browser", action="store_true", help="Print the local address without opening a browser")
    parser.add_argument("--port", type=int, default=0, help="Local port; the default chooses a free port")
    parser.add_argument("--data-file", type=Path, help="Optional data-file location; default is beside this program")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("Port must be from 0 through 65535.")
    data_path = args.data_file or Path(__file__).resolve().with_name("DEH_Research_Data.json")
    try:
        app = DehDashboardApp(data_path)
        server = DehLocalServer(args.port, app)
    except (OSError, ValueError):
        print("The local dashboard could not start. Check that this folder is accessible and that the requested port is available.", file=sys.stderr)
        return 1
    address = f"http://127.0.0.1:{server.server_port}/"
    print("DEH research dashboard", flush=True)
    print("Open: " + address, flush=True)
    print("Data: " + str(app.data_path), flush=True)
    print("Keep this window open while using the dashboard. Press Control+C to close it.", flush=True)
    if not args.no_browser:
        try:
            webbrowser.open(address, new=2)
        except Exception:
            print("Open the local address above in your browser.", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nDashboard closed. Saved data remain in the data file.", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(app_main())
