const ALLOWED_ORIGIN = "https://haitham123sa.github.io";
const API_VERSION = "2022-11-28";

function response(body, status = 200, origin = ALLOWED_ORIGIN) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      "Access-Control-Allow-Origin": origin,
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      "Access-Control-Allow-Headers": "Authorization, Content-Type",
      "Access-Control-Max-Age": "86400",
      "Vary": "Origin",
      "X-Content-Type-Options": "nosniff",
      "Referrer-Policy": "no-referrer"
    }
  });
}

function cors(request) {
  const origin = request.headers.get("Origin") || "";
  if (origin !== ALLOWED_ORIGIN) return null;
  return origin;
}


function ghHeaders(env) {
  return {
    "Authorization": `Bearer ${env.GH_TOKEN}`,
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": API_VERSION,
    "User-Agent": "DEH-Research-Dashboard"
  };
}

async function github(env, path, options = {}) {
  const result = await fetch(`https://api.github.com/repos/${env.GH_OWNER}/${env.GH_REPO}${path}`, {
    ...options,
    headers: { ...ghHeaders(env), ...(options.headers || {}) }
  });
  const text = await result.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch {}
  if (!result.ok) {
    const message = data.message || `GitHub API returned ${result.status}`;
    throw new Error(message);
  }
  return data;
}

async function listRuns(env, workflowFile) {
  const data = await github(env, `/actions/workflows/${workflowFile}/runs?branch=main&per_page=10`);
  return Array.isArray(data.workflow_runs) ? data.workflow_runs : [];
}

async function startRefresh(request, env, origin) {
  if (!env.GH_TOKEN || !env.GH_OWNER || !env.GH_REPO) return response({ error: "The backend is not fully configured. Ask the administrator to finish its GitHub connection." }, 503, origin);

  let body = {};
  try { body = await request.json(); } catch { return response({ error: "Invalid request body." }, 400, origin); }
  const year = Number(body.academic_year_start);
  if (!Number.isInteger(year) || year < 2000 || year > 2100) return response({ error: "Select a valid academic-year starting year." }, 400, origin);

  const current = await listRuns(env, "refresh.yml");
  const active = current.find(run => run.status !== "completed");
  if (active) return response({ error: "A research collection is already running.", run_id: active.id, started_at: active.created_at }, 409, origin);

  const startedAt = new Date().toISOString();
  const dispatch = await fetch(`https://api.github.com/repos/${env.GH_OWNER}/${env.GH_REPO}/actions/workflows/refresh.yml/dispatches`, {
    method: "POST",
    headers: ghHeaders(env),
    body: JSON.stringify({ ref: "main", inputs: { academic_year_start: String(year) } })
  });
  if (dispatch.status !== 204) {
    let detail = "";
    try { detail = (await dispatch.json()).message || ""; } catch {}
    return response({ error: detail || `Could not start the research collection (GitHub returned ${dispatch.status}). Check the backend's GitHub permissions.` }, 502, origin);
  }

  let runId = null;
  for (let attempt = 0; attempt < 4 && !runId; attempt++) {
    if (attempt) await new Promise(resolve => setTimeout(resolve, 700));
    const runs = await listRuns(env, "refresh.yml");
    const match = runs.find(run => run.event === "workflow_dispatch" && Date.parse(run.created_at) >= Date.parse(startedAt) - 5000);
    if (match) runId = match.id;
  }
  return response({ accepted: true, run_id: runId, started_at: startedAt, message: "Web of Science collection has been queued." }, 202, origin);
}

async function refreshStatus(request, env, origin) {
  if (!env.GH_TOKEN) return response({ error: "The backend GitHub connection is not configured." }, 503, origin);

  const url = new URL(request.url);
  const requestedId = url.searchParams.get("run_id");
  const startedAt = url.searchParams.get("started_at");
  const runs = await listRuns(env, "refresh.yml");
  const run = requestedId ? runs.find(item => String(item.id) === requestedId) :
    runs.find(item => item.event === "workflow_dispatch" && (!startedAt || Date.parse(item.created_at) >= Date.parse(startedAt) - 5000));
  if (!run) return response({ status: "queued", job: { running: true, status: "queued", done: 0, total: 23, message: "Waiting for GitHub Actions to start the collection." } }, 200, origin);

  if (run.status !== "completed") {
    let stepMessage = run.status === "queued" ? "Collection is queued." : "Collecting Web of Science records for the 23 faculty members.";
    try {
      const jobs = await github(env, `/actions/runs/${run.id}/jobs?per_page=10`);
      const activeJob = (jobs.jobs || []).find(job => job.status === "in_progress");
      const activeStep = activeJob && (activeJob.steps || []).find(step => step.status === "in_progress");
      if (activeStep?.name) stepMessage = activeStep.name;
    } catch {}
    return response({
      status: run.status === "queued" ? "queued" : "running",
      run_id: run.id,
      job: { running: true, status: "running", done: 0, total: 23, message: stepMessage },
      message: stepMessage
    }, 200, origin);
  }

  if (run.conclusion !== "success") {
    return response({
      status: "failed", run_id: run.id, conclusion: run.conclusion,
      job: { running: false, status: "failed", done: 23, total: 23, message: "The Web of Science collection did not complete successfully. The previous published report has been preserved." }
    }, 200, origin);
  }

  const publishRuns = await listRuns(env, "publish-pages.yml");
  const publish = publishRuns.find(item => item.event === "workflow_run" && Date.parse(item.created_at) >= Date.parse(run.updated_at) - 5000);
  if (!publish || publish.status !== "completed") {
    return response({
      status: "publishing", run_id: run.id,
      job: { running: true, status: "publishing", done: 23, total: 23, message: "Collection finished. Publishing the updated dashboard." }
    }, 200, origin);
  }
  if (publish.conclusion !== "success") {
    return response({
      status: "publish_failed", run_id: run.id,
      job: { running: false, status: "failed", done: 23, total: 23, message: "Collection finished, but publishing the updated dashboard failed. The previous report remains online." }
    }, 200, origin);
  }
  return response({
    status: "complete", run_id: run.id,
    job: { running: false, status: "complete", done: 23, total: 23, message: "Research data refreshed and the dashboard has been published." }
  }, 200, origin);
}

export default {
  async fetch(request, env) {
    const origin = cors(request);
    if (!origin) return new Response("Forbidden", { status: 403 });
    const url = new URL(request.url);
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: {
      "Access-Control-Allow-Origin": origin,
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      "Access-Control-Allow-Headers": "Authorization, Content-Type",
      "Access-Control-Max-Age": "86400",
      "Vary": "Origin"
    }});
    try {
      if (url.pathname === "/health" && request.method === "GET") return response({ ok: true, service: "deh-research-api", plan: "free" }, 200, origin);
      if (url.pathname === "/refresh" && request.method === "POST") return await startRefresh(request, env, origin);
      if (url.pathname === "/status" && request.method === "GET") return await refreshStatus(request, env, origin);
      return response({ error: "Not found." }, 404, origin);
    } catch (error) {
      return response({ error: error instanceof Error ? error.message : "Unexpected backend error." }, 502, origin);
    }
  }
};
