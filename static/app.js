const state = {
  selectedIncidentId: "",
  toastTimer: null,
};

const $ = (selector) => document.querySelector(selector);

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => toast.classList.remove("show"), 3200);
}

function normalizeLines(value) {
  return value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
}

function selectedValue(form, name) {
  const value = new FormData(form).get(name);
  return value === "" ? null : value;
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const text = await response.text();
  const payload = text ? JSON.parse(text) : {};
  if (!response.ok) {
    throw new Error(payload.detail || `Request failed: ${response.status}`);
  }
  return payload;
}

function badge(value) {
  if (!value) return '<span class="badge">none</span>';
  const safe = String(value).replaceAll("_", " ");
  return `<span class="badge ${value}">${safe}</span>`;
}

function formatDate(seconds) {
  if (!seconds) return "unknown";
  return new Date(seconds * 1000).toLocaleString();
}

async function submitQuickIncident(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  const payload = {
    service: data.get("service"),
    priority: selectedValue(form, "priority"),
    error_logs: normalizeLines(data.get("error_logs")),
  };
  const result = await request("/api/v1/quick-incident/", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  showToast(`Created ${result.incident_id}`);
  await loadIncidents();
  await loadDiagnosis(result.incident_id);
}

async function submitInitIncident(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  const payload = {
    service: data.get("service"),
    instance: data.get("instance"),
    priority: selectedValue(form, "priority"),
    loki_logs: normalizeLines(data.get("loki_logs")),
    prometheus_logs: {
      source: "dashboard",
      received_at: new Date().toISOString(),
    },
    timestamp: Date.now() / 1000,
  };
  const result = await request("/api/v1/init/", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  showToast(`Webhook accepted: ${result.incident_id}`);
  await loadIncidents();
  await loadDiagnosis(result.incident_id);
}

async function loadIncidents() {
  const params = new URLSearchParams();
  const status = $("#statusFilter").value;
  const priority = $("#priorityFilter").value;
  if (status) params.set("status", status);
  if (priority) params.set("priority", priority);
  const suffix = params.toString() ? `?${params}` : "";
  const data = await request(`/api/v1/incidents/${suffix}`);
  const list = $("#incidentList");

  if (!data.incidents.length) {
    list.innerHTML = '<div class="code-panel small">No incidents match the current filters.</div>';
    return;
  }

  list.innerHTML = data.incidents
    .map((incident) => {
      const priorityValue = incident.resolved_priority || incident.declared_priority;
      const route = incident.pr_url || incident.issue_url || "";
      return `
        <article class="incident-card">
          <div>
            <h3>${incident.incident_id}</h3>
            <p>${incident.service || "unlabeled service"} · ${formatDate(incident.created_at)}</p>
          </div>
          ${badge(incident.status)}
          ${badge(priorityValue)}
          <span class="badge">${incident.cache_result || "cache pending"}</span>
          <span class="badge">${route ? "routed" : "no route"}</span>
          <button class="secondary-button" type="button" data-inspect="${incident.incident_id}">Inspect</button>
        </article>
      `;
    })
    .join("");
}

function renderDiagnosis(data) {
  const context = data.code_context && data.code_context.length
    ? data.code_context.map((chunk) => {
        return [
          `${chunk.category || "context"} · ${chunk.file}:${chunk.start_line}-${chunk.end_line}`,
          chunk.content || "",
        ].join("\n");
      }).join("\n\n---\n\n")
    : "No code context available yet.";

  const trace = data.trace && data.trace.length ? data.trace.join("\n") : "No trace events yet.";

  return [
    `Incident: ${data.incident_id}`,
    `Priority: ${data.resolved_priority || "pending"}`,
    `Confidence: ${data.confidence ?? "pending"}`,
    `From cache: ${data.from_cache ? "yes" : "no"}`,
    "",
    "Root Cause",
    data.root_cause || "Diagnosis is still pending.",
    "",
    "Reasoning",
    data.reasoning || "No reasoning available yet.",
    "",
    "Proposed Test",
    data.proposed_test || "No test proposed yet.",
    "",
    "Trace",
    trace,
    "",
    "Code Context",
    context,
  ].join("\n");
}

async function loadDiagnosis(incidentId) {
  const id = incidentId || $("#diagnosisId").value.trim();
  if (!id) {
    showToast("Enter or select an incident id first.");
    return;
  }
  state.selectedIncidentId = id;
  $("#diagnosisId").value = id;
  const data = await request(`/api/v1/incidents/${encodeURIComponent(id)}/diagnosis`);
  $("#diagnosisOutput").textContent = renderDiagnosis(data);
  $("#approveBtn").disabled = !data.root_cause;
  $("#ignoreBtn").disabled = false;
}

async function approveSelected() {
  if (!state.selectedIncidentId) return;
  const result = await request(`/api/v1/incidents/${encodeURIComponent(state.selectedIncidentId)}/approve`, {
    method: "POST",
    body: "{}",
  });
  showToast(`Approved ${result.incident_id}`);
  await loadIncidents();
}

async function ignoreSelected() {
  if (!state.selectedIncidentId) return;
  const result = await request(`/api/v1/incidents/${encodeURIComponent(state.selectedIncidentId)}/ignore`, {
    method: "POST",
    body: "{}",
  });
  showToast(`Ignored ${result.incident_id}`);
  await loadIncidents();
}

async function loadHealth() {
  const data = await request("/api/v1/health");
  const rows = [
    ["Overall", data.status],
    ...Object.entries(data.checks || {}),
    ["Checked", formatDate(data.time)],
  ];
  $("#healthOutput").innerHTML = rows
    .map(([name, value]) => `<div class="status-row"><span>${name}</span>${badge(String(value))}</div>`)
    .join("");
}

async function loadRules() {
  const data = await request("/api/v1/rules");
  $("#rulesOutput").textContent = JSON.stringify(data, null, 2);
}

async function refreshAll() {
  try {
    await Promise.all([loadIncidents(), loadHealth(), loadRules()]);
  } catch (error) {
    showToast(error.message);
  }
}

function guard(handler) {
  return async (event) => {
    try {
      await handler(event);
    } catch (error) {
      showToast(error.message);
    }
  };
}

$("#quickForm").addEventListener("submit", guard(submitQuickIncident));
$("#initForm").addEventListener("submit", guard(submitInitIncident));
$("#diagnosisForm").addEventListener("submit", guard((event) => {
  event.preventDefault();
  return loadDiagnosis();
}));
$("#approveBtn").addEventListener("click", guard(approveSelected));
$("#ignoreBtn").addEventListener("click", guard(ignoreSelected));
$("#refreshIncidents").addEventListener("click", guard(loadIncidents));
$("#refreshAll").addEventListener("click", guard(refreshAll));
$("#statusFilter").addEventListener("change", guard(loadIncidents));
$("#priorityFilter").addEventListener("change", guard(loadIncidents));
$("#incidentList").addEventListener("click", guard((event) => {
  const button = event.target.closest("[data-inspect]");
  if (!button) return;
  return loadDiagnosis(button.dataset.inspect);
}));

refreshAll();
