"""Optional HTTP front door for the orchestrator (useful for a deployed demo).

  uvicorn orchestration.api:app --port 8100
  POST /workflows                 {"org_id": "org_demo_alpha", "unit_id": "UNIT-0002"}   -> Workflow State (runs it)
  GET  /workflows/{id}            -> Workflow State
  GET  /workflows/{id}/evidence   -> the workflow plus all its evidence records
  POST /workflows/{id}/resume     -> continue after a halt / decision / failure
  POST /workflows/{id}/overrides  {"record_id": "...", "new_verdict": "PASS", "actor": "...", "reason": "..."}
  GET  /health                    -> orchestrator and every agent in the flow
No authentication is included. Add it before you deploy anywhere public.
"""
from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from shared.utils import sample_data

from .clients import HttpClient, client_for, load_manifest
from .orchestrator import apply_override, bundle, default_flow_path, flow_stages, load_flow, resume, run_workflow
from .store import EvidenceConflict, FileStore

app = FastAPI(title="CUBE Round 3 orchestrator")
FLOW = os.environ.get("ORCH_FLOW") or default_flow_path()
OUT_DIR = os.environ.get("OUT_DIR") or ("/tmp/out" if os.environ.get("VERCEL") else "out")
STORE = FileStore(OUT_DIR)


@app.get("/")
def root():
    return {
        "service": "CUBE Round 3 Pod Orchestrator",
        "docs": "/docs",
        "health": "/health",
        "workflows": "/workflows",
    }


@app.get("/health")
def health() -> dict:
    agents = {}
    for stage in flow_stages(load_flow(FLOW)):
        client = client_for(stage)
        try:
            agents[stage] = client.health() if isinstance(client, HttpClient) else {"status": "ok", "mode": "inproc"}
        except Exception as exc:
            agents[stage] = {"status": "down", "error": str(exc)[:200], "owner": load_manifest(stage)["owner"]}
    ok = all(a["status"] == "ok" for a in agents.values())
    return {"status": "ok" if ok else "degraded", "flow": load_flow(FLOW)["flow_id"], "agents": agents}


@app.post("/workflows")
def create(body: dict) -> dict:
    org, subject = body.get("org_id"), body.get("subject_id") or body.get("unit_id")
    if not org or not subject:
        raise HTTPException(422, "org_id and unit_id (or subject_id) are required")
      
    case = {
        "org_id": org,
        "unit_id": subject,
        "route": body.get("route") or sample_data.route(subject, org),
        "returned": body.get(
            "returned",
            sample_data.has("returns", subject, org)
        )
    }

    if body.get("run_id"):
        case["run_id"] = str(body["run_id"])

    return run_workflow(case, load_flow(FLOW), STORE)

    


def _get(workflow_id: str) -> dict:
    wf = STORE.load_workflow(workflow_id)
    if wf is None:
        raise HTTPException(404, f"no workflow {workflow_id}")
    return wf


@app.get("/workflows/{workflow_id}")
def get(workflow_id: str) -> dict:
    return _get(workflow_id)


@app.get("/workflows/{workflow_id}/evidence")
def evidence(workflow_id: str) -> dict:
    return bundle(_get(workflow_id), STORE)


@app.post("/workflows/{workflow_id}/resume")
def resume_workflow(workflow_id: str) -> dict:
    _get(workflow_id)
    return resume(workflow_id, load_flow(FLOW), STORE)


@app.post("/workflows/{workflow_id}/overrides")
def override(workflow_id: str, body: dict) -> dict:
    _get(workflow_id)
    try:
        return apply_override(workflow_id, STORE, record_id=body.get("record_id", ""), new_verdict=body.get("new_verdict", ""),
                              actor=body.get("actor", ""), reason=body.get("reason", ""), new_outcome=body.get("new_outcome"))
    except (ValueError, EvidenceConflict) as exc:
        raise HTTPException(422, str(exc)) from exc
 
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CUBE Round 3 | Workflow Dashboard</title>
<style>
body {
  font-family: Arial, sans-serif;
  background: #f3f6fb;
  color: #172033;
  max-width: 1100px;
  margin: 35px auto;
  padding: 0 18px;
}
h1 { margin-bottom: 6px; }
.card {
  background: white;
  padding: 20px;
  border-radius: 12px;
  margin: 18px 0;
  box-shadow: 0 3px 15px #0000000d;
}
input, select, button {
  padding: 11px;
  margin: 5px;
  border: 1px solid #ccd3df;
  border-radius: 7px;
}
button {
  background: #243f90;
  color: white;
  cursor: pointer;
}
table {
  width: 100%;
  border-collapse: collapse;
  background: white;
}
th, td {
  text-align: left;
  padding: 12px 8px;
  border-bottom: 1px solid #e5e9f0;
  overflow-wrap: anywhere;
}
.badge {
  padding: 5px 9px;
  border-radius: 15px;
  font-weight: bold;
  font-size: 12px;
}
.pass, .completed { background: #dcfce7; color: #166534; }
.fail { background: #fee2e2; color: #991b1b; }
.skipped { background: #e5e7eb; color: #374151; }
pre {
  overflow: auto;
  background: #101827;
  color: #e4edff;
  padding: 16px;
  border-radius: 8px;
}
.muted { color: #64748b; }
</style>
</head>
<body>
<h1>CUBE Round 3</h1>
<p class="muted">Integrated Commerce Workflow Monitor</p>

<div class="card">
<h2>Run a workflow</h2>
<form id="runForm">
  <input id="org" value="org_demo_alpha"
         placeholder="Organization ID" required>
  <input id="unit" value="UNIT-0014"
         placeholder="Unit ID" required>
  <select id="route">
    <option value="fba">FBA — Prep</option>
    <option value="mfn">MFN — Pack</option>
  </select>
  <label>
    <input id="returned" type="checkbox" checked>
    Returned
  </label>
  <button type="submit">Run Workflow</button>
</form>
<p id="message" class="muted"></p>
</div>

<div id="results" class="card" hidden>
<h2>Workflow Results</h2>
<p id="summary"></p>
<p id="final"></p>
<div style="overflow-x:auto">
<table>
<thead>
<tr>
<th>Agent</th><th>Agent ID</th><th>Status</th>
<th>Verdict</th><th>Record ID</th><th>Duration</th>
</tr>
</thead>
<tbody id="stages"></tbody>
</table>
</div>
<button id="evidenceButton">View All Evidence</button>
</div>

<div id="evidencePanel" class="card" hidden>
<h2>Evidence Records</h2>
<pre id="evidence"></pre>
</div>

<script>
let currentWorkflowId = "";

function safe(value) {
  return String(value ?? "—").replace(/[&<>"']/g, ch => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;",
    '"': "&quot;", "'": "&#39;"
  })[ch]);
}

document.getElementById("runForm").addEventListener(
  "submit", async event => {
    event.preventDefault();
    const message = document.getElementById("message");
    message.textContent = "Running workflow...";

    document.getElementById("results").hidden = true;
    document.getElementById("evidencePanel").hidden = true;

    const payload = {
      org_id: document.getElementById("org").value.trim(),
      unit_id: document.getElementById("unit").value.trim(),
      route: document.getElementById("route").value,
      returned: document.getElementById("returned").checked
    };

    try {
      const response = await fetch("/workflows", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload)
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(JSON.stringify(data));
      }

      currentWorkflowId = data.workflow_id;
      document.getElementById("summary").textContent =
        "Workflow: " + currentWorkflowId +
        " | Status: " + data.status;

      document.getElementById("stages").innerHTML =
        (data.stage_results || []).map(stage => {
          const state = String(stage.state || "").toLowerCase();
          const verdict = String(stage.verdict || "").toLowerCase();
          const badgeClass =
            state === "skipped" ? "skipped" :
            verdict === "fail" ? "fail" :
            verdict === "pass" ? "pass" : "completed";

          return `<tr>
            <td>${safe(stage.stage)}</td>
            <td>${safe(stage.agent_id)}</td>
            <td><span class="badge ${badgeClass}">
              ${safe(stage.state)}
            </span></td>
            <td>${safe(stage.verdict)}</td>
            <td>${safe(stage.record_id)}</td>
            <td>${safe(stage.duration_ms)} ms</td>
          </tr>`;
        }).join("");

      const outcome = data.final_outcome || {};
      document.getElementById("final").textContent =
        "Final outcome: " + (outcome.outcome || "Not available") +
        " | Claimable USD: " +
        (outcome.claimable_usd ?? "Not available");

      document.getElementById("results").hidden = false;
      message.textContent = "Workflow completed. Review the results below.";
    } catch (error) {
      message.textContent = "Error: " + error.message;
    }
  }
);

document.getElementById("evidenceButton").addEventListener(
  "click", async () => {
    try {
      const response = await fetch(
        "/workflows/" +
        encodeURIComponent(currentWorkflowId) + "/evidence"
      );
      const data = await response.json();
      if (!response.ok) throw new Error(JSON.stringify(data));

      document.getElementById("evidence").textContent =
        JSON.stringify(data.evidence, null, 2);
      document.getElementById("evidencePanel").hidden = false;
    } catch (error) {
      alert("Could not load evidence: " + error.message);
    }
  }
);
</script>
</body>
</html>
"""
     
