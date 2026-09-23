const $ = (id) => document.getElementById(id);

const state = {
  sources: [],
  selected: new Set(["claude"]),
  currentBrowse: null,
};

function appendLog(event) {
  const view = $("logView");
  const ts = (event.ts || new Date().toISOString()).slice(11, 19);
  const status = event.status || "";
  const cls =
    status === "added" ? "added" :
    status === "changed" ? "changed" :
    status === "deleted" || status === "error" || event.type === "job_error" ? "error" :
    event.type === "run_done" || event.type === "source_done" ? "ok" :
    "info";
  const line = document.createElement("div");
  line.innerHTML = `<span class="ts">[${ts}]</span> <span class="${cls}"></span>`;
  line.querySelector(`.${cls}`).textContent = event.message || JSON.stringify(event);
  view.appendChild(line);
  view.scrollTop = view.scrollHeight;
}

function setJobPill(running, watch) {
  const pill = $("jobPill");
  if (running) {
    pill.textContent = watch ? "Watch 运行中" : "同步中";
    pill.classList.add("running");
  } else {
    pill.textContent = "空闲";
    pill.classList.remove("running");
  }
  $("startBtn").disabled = running;
  $("stopBtn").disabled = !running;
}

function renderSourceChips() {
  const box = $("sourceList");
  box.innerHTML = "";
  state.sources.forEach((s) => {
    const label = document.createElement("label");
    label.className = `source-chip${state.selected.has(s.key) ? " active" : ""}`;
    label.innerHTML = `<input type="checkbox" value="${s.key}" ${state.selected.has(s.key) ? "checked" : ""}/><span>${s.name}</span>`;
    const input = label.querySelector("input");
    input.addEventListener("change", () => {
      if (input.checked) state.selected.add(s.key);
      else state.selected.delete(s.key);
      label.classList.toggle("active", input.checked);
    });
    box.appendChild(label);
  });
}

function renderSourceCards() {
  const box = $("sourceCards");
  box.innerHTML = "";
  state.sources.forEach((s) => {
    const card = document.createElement("div");
    card.className = "card";
    const updated = s.updated_at ? new Date(s.updated_at).toLocaleString() : "尚未同步";
    card.innerHTML = `
      <strong>${s.name}</strong>
      <div class="meta">${s.page_count || 0} pages · ${updated}</div>
      <div class="meta">${s.llms_txt}</div>
    `;
    box.appendChild(card);
  });

  const select = $("browseSource");
  const prev = select.value;
  select.innerHTML = state.sources.map((s) => `<option value="${s.key}">${s.name}</option>`).join("");
  if (prev) select.value = prev;
}

function renderSummary(summaries) {
  const box = $("summaryTable");
  if (!summaries || !summaries.length) {
    box.className = "empty";
    box.textContent = "尚未同步";
    return;
  }
  box.className = "";
  box.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Source</th><th>Added</th><th>Changed</th><th>Unchanged</th><th>Deleted</th><th>Errors</th>
        </tr>
      </thead>
      <tbody>
        ${summaries.map((s) => `
          <tr>
            <td>${s.source}</td>
            <td class="num">${s.added}</td>
            <td class="num">${s.changed}</td>
            <td class="num">${s.unchanged}</td>
            <td class="num">${s.deleted}</td>
            <td class="num">${s.errors}</td>
          </tr>
        `).join("")}
      </tbody>
    </table>
  `;
}

async function loadSources() {
  const res = await fetch("/api/sources");
  const data = await res.json();
  state.sources = data.sources || [];
  if (data.defaults) {
    $("concurrency").value = data.defaults.concurrency ?? 4;
    $("delay").value = data.defaults.delay_sec ?? 0.2;
  }
  renderSourceChips();
  renderSourceCards();
  if (!state.currentBrowse && state.sources[0]) {
    $("browseSource").value = state.sources[0].key;
    await loadPages();
  }
}

async function loadStatus() {
  const res = await fetch("/api/status");
  const data = await res.json();
  setJobPill(data.running, data.watch);
  renderSummary(data.summaries);
  if (!data.running) await loadSources();
}

async function startSync() {
  const sources = [...state.selected];
  if (!sources.length) {
    appendLog({ message: "请至少选择一个源", type: "job_error", ts: new Date().toISOString() });
    return;
  }
  const maxPagesRaw = $("maxPages").value;
  const body = {
    sources,
    max_pages: maxPagesRaw === "" ? null : Number(maxPagesRaw),
    concurrency: Number($("concurrency").value) || 4,
    delay_sec: Number($("delay").value) || 0,
    dry_run: $("dryRun").checked,
    watch: $("watch").checked,
    interval_sec: Number($("interval").value) || 60,
  };
  $("logView").innerHTML = "";
  appendLog({ message: `请求同步: ${sources.join(", ")}`, type: "info", ts: new Date().toISOString() });
  const res = await fetch("/api/sync", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    appendLog({ message: err.detail || "启动失败", type: "job_error", ts: new Date().toISOString() });
    return;
  }
  setJobPill(true, body.watch);
}

async function stopSync() {
  await fetch("/api/stop", { method: "POST" });
}

async function loadPages() {
  const source = $("browseSource").value;
  state.currentBrowse = source;
  const list = $("pageList");
  list.className = "page-list";
  list.innerHTML = `<div class="empty" style="padding:0.8rem">加载中…</div>`;
  const res = await fetch(`/api/pages?source=${encodeURIComponent(source)}&limit=200`);
  const data = await res.json();
  if (!data.pages || !data.pages.length) {
    list.innerHTML = `<div class="empty" style="padding:0.8rem">暂无本地页面，先同步一次</div>`;
    return;
  }
  list.innerHTML = "";
  data.pages.forEach((p) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "page-item";
    const title = (p.path || p.url || "").split("/").slice(-2).join("/");
    btn.innerHTML = `${title}<small>${p.url}</small>`;
    btn.addEventListener("click", async () => {
      [...list.querySelectorAll(".page-item")].forEach((el) => el.classList.remove("active"));
      btn.classList.add("active");
      await openPage(source, p.path, p.url);
    });
    list.appendChild(btn);
  });
}

async function openPage(source, path, url) {
  $("pageMeta").textContent = `${source} · ${path || ""}`;
  $("pageContent").textContent = "加载中…";
  const res = await fetch(`/api/page?source=${encodeURIComponent(source)}&path=${encodeURIComponent(path)}`);
  if (!res.ok) {
    $("pageContent").textContent = "读取失败";
    return;
  }
  const data = await res.json();
  $("pageMeta").textContent = `${url}\n${path}`;
  $("pageContent").textContent = data.content;
}

function connectEvents() {
  const es = new EventSource("/api/events");
  es.onmessage = async (msg) => {
    try {
      const event = JSON.parse(msg.data);
      if (event.type !== "hello") appendLog(event);
      if (event.type === "job_start") setJobPill(true, event.params?.watch);
      if (event.type === "job_done" || event.type === "job_cancelled") {
        setJobPill(false, false);
        await loadStatus();
        await loadPages();
      }
      if (event.type === "run_done" && event.summaries) {
        renderSummary(event.summaries);
      }
    } catch {
      /* ignore */
    }
  };
  es.onerror = () => {
    // browser will reconnect
  };
}

$("startBtn").addEventListener("click", startSync);
$("stopBtn").addEventListener("click", stopSync);
$("refreshBtn").addEventListener("click", async () => {
  await loadSources();
  await loadStatus();
  await loadPages();
});
$("clearLog").addEventListener("click", () => { $("logView").innerHTML = ""; });
$("browseSource").addEventListener("change", loadPages);

(async function init() {
  await loadSources();
  await loadStatus();
  connectEvents();
})();
