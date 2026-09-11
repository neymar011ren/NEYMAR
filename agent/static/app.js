const state = {
  config: null,
  history: [],
  busy: false,
};

const els = {
  chatLog: document.getElementById("chat-log"),
  chatForm: document.getElementById("chat-form"),
  chatInput: document.getElementById("chat-input"),
  btnSend: document.getElementById("btn-send"),
  btnClear: document.getElementById("btn-clear"),
  btnOpenSettings: document.getElementById("btn-open-settings"),
  drawer: document.getElementById("settings-drawer"),
  settingsForm: document.getElementById("settings-form"),
  settingsStatus: document.getElementById("settings-status"),
  btnTest: document.getElementById("btn-test"),
  connPill: document.getElementById("conn-pill"),
  metaModel: document.getElementById("meta-model"),
  metaProtocol: document.getElementById("meta-protocol"),
  metaTools: document.getElementById("meta-tools"),
  keyHint: document.getElementById("key-hint"),
};

const PROTOCOL_LABEL = {
  chat_completions: "Chat Completions",
  responses: "Responses",
  anthropic_messages: "Anthropic Messages",
};

function scrollChat() {
  els.chatLog.scrollTop = els.chatLog.scrollHeight;
}

function prettyJson(body) {
  try {
    return JSON.stringify(JSON.parse(body), null, 2);
  } catch {
    return body || "";
  }
}

function addNotice(text, kind = "") {
  const div = document.createElement("div");
  div.className = `sys-banner ${kind}`.trim();
  div.textContent = text;
  els.chatLog.appendChild(div);
  scrollChat();
  return div;
}

function addUserBubble(text) {
  const row = document.createElement("article");
  row.className = "msg msg-user";
  row.innerHTML = `
    <div class="msg-col">
      <div class="msg-role">你</div>
      <div class="user-bubble"></div>
    </div>
    <div class="avatar avatar-user" aria-hidden="true">你</div>
  `;
  row.querySelector(".user-bubble").textContent = text;
  els.chatLog.appendChild(row);
  scrollChat();
  return row;
}

function createTurnCard() {
  const row = document.createElement("article");
  row.className = "msg msg-assistant";
  row.innerHTML = `
    <div class="avatar avatar-bot" aria-hidden="true">J</div>
    <div class="msg-col">
      <div class="msg-role">Judger</div>
      <div class="activity-rail"></div>
      <div class="msg-prose" hidden></div>
      <div class="citation-row" hidden></div>
    </div>
  `;
  const api = {
    root: row,
    rail: row.querySelector(".activity-rail"),
    prose: row.querySelector(".msg-prose"),
    citations: row.querySelector(".citation-row"),
    thinkDetails: null,
    thinkBody: null,
    streamText: "",
    toolMap: new Map(),
  };
  els.chatLog.appendChild(row);
  scrollChat();
  return api;
}

function ensureThink(turn) {
  if (turn.thinkDetails) return;
  const details = document.createElement("details");
  details.className = "rail-card think-card";
  details.open = true;
  details.innerHTML = `
    <summary>
      <span class="rail-ico" aria-hidden="true">◎</span>
      <span class="rail-title">Working</span>
      <span class="rail-sub think-sub">处理中</span>
      <span class="chev" aria-hidden="true"></span>
    </summary>
    <div class="rail-body think-body"></div>
  `;
  turn.rail.appendChild(details);
  turn.thinkDetails = details;
  turn.thinkBody = details.querySelector(".think-body");
}

function appendThink(turn, message) {
  ensureThink(turn);
  const item = document.createElement("div");
  item.className = "think-line";
  item.textContent = message;
  turn.thinkBody.appendChild(item);
  const sub = turn.thinkDetails.querySelector(".think-sub");
  if (sub) sub.textContent = message;
  scrollChat();
}

function appendMemory(turn, message) {
  const details = document.createElement("details");
  details.className = "rail-card memory-card";
  details.innerHTML = `
    <summary>
      <span class="rail-ico" aria-hidden="true">◉</span>
      <span class="rail-title">Memory</span>
      <span class="rail-sub">长期记忆</span>
      <span class="chev" aria-hidden="true"></span>
    </summary>
    <div class="rail-body"><pre>${escapeHtml(message)}</pre></div>
  `;
  turn.rail.appendChild(details);
  scrollChat();
}

function appendTool(turn, kind, name, body) {
  let entry = turn.toolMap.get(name);
  if (!entry || kind === "call") {
    const details = document.createElement("details");
    details.className = "rail-card tool-card";
    details.innerHTML = `
      <summary>
        <span class="rail-ico" aria-hidden="true">⇢</span>
        <span class="rail-title">Used ${escapeHtml(name)}</span>
        <span class="rail-sub tool-state">running</span>
        <span class="chev" aria-hidden="true"></span>
      </summary>
      <div class="rail-body">
        <div class="tool-pane">
          <div class="pane-label">Request</div>
          <pre class="tool-req"></pre>
        </div>
        <div class="tool-pane tool-res-pane" hidden>
          <div class="pane-label">Response</div>
          <pre class="tool-res"></pre>
        </div>
      </div>
    `;
    turn.rail.appendChild(details);
    entry = {
      details,
      req: details.querySelector(".tool-req"),
      res: details.querySelector(".tool-res"),
      resPane: details.querySelector(".tool-res-pane"),
      state: details.querySelector(".tool-state"),
    };
    turn.toolMap.set(name, entry);
  }
  if (kind === "call") {
    entry.req.textContent = prettyJson(body);
    entry.state.textContent = "running";
    entry.state.classList.add("is-run");
  } else {
    entry.res.textContent = prettyJson(body);
    entry.resPane.hidden = false;
    entry.state.textContent = "done";
    entry.state.classList.remove("is-run");
    entry.state.classList.add("is-done");
  }
  // 思考卡在工具开始后自动收起，更像 ChatGPT
  if (turn.thinkDetails) turn.thinkDetails.open = false;
  scrollChat();
}

function appendDelta(turn, piece) {
  turn.prose.hidden = false;
  turn.streamText += piece;
  turn.prose.classList.add("streaming");
  // 流式阶段用纯文本，避免半截 markdown 闪烁
  turn.prose.textContent = turn.streamText;
  scrollChat();
}

function finalizeAnswer(turn, content, references) {
  turn.prose.hidden = false;
  turn.prose.classList.remove("streaming");
  const text = content || "(空回复)";
  turn.streamText = text;
  turn.prose.innerHTML = renderMarkdown(text);
  if (turn.thinkDetails) {
    turn.thinkDetails.open = false;
    const sub = turn.thinkDetails.querySelector(".think-sub");
    if (sub) sub.textContent = "已完成";
  }
  if (Array.isArray(references) && references.length) {
    turn.citations.hidden = false;
    turn.citations.innerHTML = "";
    const label = document.createElement("div");
    label.className = "citation-label";
    label.textContent = "Sources";
    turn.citations.appendChild(label);
    const wrap = document.createElement("div");
    wrap.className = "citation-chips";
    references.forEach((ref, idx) => {
      const chip = document.createElement(ref.url ? "a" : "span");
      chip.className = "citation-chip";
      chip.textContent = `${idx + 1}. ${ref.title || ref.url || "来源"}`;
      if (ref.url) {
        chip.href = ref.url;
        chip.target = "_blank";
        chip.rel = "noreferrer";
      }
      wrap.appendChild(chip);
    });
    turn.citations.appendChild(wrap);
  }
  scrollChat();
}

function resetAnswerStream(turn) {
  turn.streamText = "";
  turn.prose.textContent = "";
  turn.prose.classList.remove("streaming");
  turn.prose.hidden = true;
}

function setBusy(busy) {
  state.busy = busy;
  els.btnSend.disabled = busy;
  els.chatInput.disabled = busy;
  els.btnSend.classList.toggle("is-busy", busy);
}

function openSettings() {
  els.drawer.classList.add("open");
  els.drawer.setAttribute("aria-hidden", "false");
}

function closeSettings() {
  els.drawer.classList.remove("open");
  els.drawer.setAttribute("aria-hidden", "true");
}

function fillSettingsForm(config) {
  const form = els.settingsForm;
  form.api_base_url.value = config.api_base_url || "";
  form.api_key.value = "";
  form.model.value = config.model || "";
  form.protocol.value = config.protocol || "chat_completions";
  form.temperature.value = config.temperature ?? 0.7;
  form.max_tokens.value = config.max_tokens ?? 4096;
  form.request_timeout_seconds.value = config.request_timeout_seconds ?? 120;
  form.enable_tools.checked = !!config.enable_tools;
  form.enable_web_search.checked = !!config.enable_web_search;
  form.enable_memory.checked = !!config.enable_memory;
  form.memory_user_id.value = config.memory_user_id || "default";
  form.memory_llm_model.value = config.memory_llm_model || "";
  form.memory_embed_base_url.value = config.memory_embed_base_url || "";
  form.memory_embed_api_key.value = "";
  form.memory_embed_model.value = config.memory_embed_model || "text-embedding-3-small";
  form.memory_top_k.value = config.memory_top_k ?? 5;
  form.memory_embed_dims.value = config.memory_embed_dims ?? 1536;
  form.system_prompt.value = config.system_prompt || "";
  els.keyHint.textContent = config.api_key_set
    ? `当前已设置 Key：${config.api_key_masked}`
    : "当前未设置 Key";
  const embedHint = document.getElementById("embed-key-hint");
  if (embedHint) {
    embedHint.textContent = config.memory_embed_api_key_set
      ? `当前已设置向量 Key：${config.memory_embed_api_key_masked}`
      : "当前未设置向量 Key";
  }
}

function refreshMeta(config) {
  els.metaModel.textContent = config.model || "—";
  els.metaProtocol.textContent = PROTOCOL_LABEL[config.protocol] || config.protocol || "—";
  const tools = config.enable_tools ? "工具开" : "工具关";
  const web = config.enable_web_search ? "联网开" : "联网关";
  const mem = config.enable_memory ? "记忆开" : "记忆关";
  els.metaTools.textContent = `${tools} / ${web} / ${mem}`;
}

async function loadConfig() {
  const res = await fetch("/api/config");
  if (!res.ok) throw new Error("读取配置失败");
  const config = await res.json();
  state.config = config;
  fillSettingsForm(config);
  refreshMeta(config);
}

async function saveConfig(event) {
  event.preventDefault();
  const form = els.settingsForm;
  const payload = {
    api_base_url: form.api_base_url.value.trim(),
    api_key: form.api_key.value,
    model: form.model.value.trim(),
    protocol: form.protocol.value,
    temperature: Number(form.temperature.value),
    max_tokens: Number(form.max_tokens.value),
    request_timeout_seconds: Number(form.request_timeout_seconds.value),
    enable_tools: form.enable_tools.checked,
    enable_web_search: form.enable_web_search.checked,
    enable_memory: form.enable_memory.checked,
    memory_user_id: form.memory_user_id.value.trim() || "default",
    memory_llm_model: form.memory_llm_model.value.trim(),
    memory_embed_base_url: form.memory_embed_base_url.value.trim(),
    memory_embed_api_key: form.memory_embed_api_key.value,
    memory_embed_model: form.memory_embed_model.value.trim() || "text-embedding-3-small",
    memory_top_k: Number(form.memory_top_k.value),
    memory_embed_dims: Number(form.memory_embed_dims.value),
    system_prompt: form.system_prompt.value,
  };

  els.settingsStatus.textContent = "保存中…";
  els.settingsStatus.className = "status-line";

  const res = await fetch("/api/config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    els.settingsStatus.textContent = data.detail || "保存失败";
    els.settingsStatus.className = "status-line bad";
    return;
  }
  state.config = data;
  fillSettingsForm(data);
  refreshMeta(data);
  els.settingsStatus.textContent = "配置已保存，立即生效";
  els.settingsStatus.className = "status-line ok";
}

async function testConnection() {
  els.settingsStatus.textContent = "正在测试连接…";
  els.settingsStatus.className = "status-line";
  els.connPill.textContent = "检测中";
  els.connPill.className = "pill";
  await saveConfig(new Event("submit"));
  const res = await fetch("/api/config/test", { method: "POST" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = data.detail || "连接失败";
    els.settingsStatus.textContent = msg;
    els.settingsStatus.className = "status-line bad";
    els.connPill.textContent = "连接失败";
    els.connPill.className = "pill bad";
    return;
  }
  els.settingsStatus.textContent = `连接成功：${data.sample || "ok"}`;
  els.settingsStatus.className = "status-line ok";
  els.connPill.textContent = "连接正常";
  els.connPill.className = "pill ok";
}

async function sendMessage(event) {
  event.preventDefault();
  if (state.busy) return;
  const message = els.chatInput.value.trim();
  if (!message) return;

  addUserBubble(message);
  state.history.push({ role: "user", content: message });
  els.chatInput.value = "";
  autoGrow();
  setBusy(true);

  const turn = createTurnCard();
  appendThink(turn, "处理中…");

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message,
        history: state.history.slice(0, -1),
      }),
    });
    if (!res.ok || !res.body) {
      throw new Error(`请求失败: HTTP ${res.status}`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let finalText = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const chunks = buffer.split("\n\n");
      buffer = chunks.pop() || "";
      for (const chunk of chunks) {
        const line = chunk.trim();
        if (!line.startsWith("data:")) continue;
        const payload = JSON.parse(line.slice(5).trim());
        if (payload.type === "phase" || payload.type === "status") {
          appendThink(turn, payload.message || "");
        } else if (payload.type === "delta") {
          appendDelta(turn, payload.content || "");
        } else if (payload.type === "round_reset") {
          resetAnswerStream(turn);
        } else if (payload.type === "tool_call") {
          appendTool(turn, "call", payload.name, payload.arguments || "");
        } else if (payload.type === "tool_result") {
          appendTool(turn, "result", payload.name, payload.result || "");
        } else if (payload.type === "memory") {
          if (payload.action === "recall" && payload.items?.length) {
            const lines = payload.items.map((item, i) => `${i + 1}. ${item.memory}`).join("\n");
            appendMemory(turn, `召回 ${payload.count} 条：\n${lines}`);
          } else if (payload.action === "write") {
            appendMemory(turn, `本轮已写入长期记忆（${payload.backend || "local"}）`);
          } else if (payload.action === "recall_error" || payload.action === "write_error") {
            appendMemory(turn, payload.message || "记忆操作失败");
          }
        } else if (payload.type === "final") {
          finalText = payload.content || payload.raw || "";
          finalizeAnswer(turn, finalText, payload.references || []);
          state.history.push({ role: "assistant", content: payload.raw || finalText || "" });
        } else if (payload.type === "error") {
          addNotice(payload.message || "未知错误", "error");
        }
      }
    }
  } catch (err) {
    addNotice(err.message || String(err), "error");
  } finally {
    setBusy(false);
  }
}

function autoGrow() {
  const el = els.chatInput;
  el.style.height = "auto";
  el.style.height = `${Math.min(180, Math.max(52, el.scrollHeight))}px`;
}

els.btnOpenSettings.addEventListener("click", openSettings);
els.drawer.querySelectorAll("[data-close]").forEach((node) => {
  node.addEventListener("click", closeSettings);
});
els.settingsForm.addEventListener("submit", saveConfig);
els.btnTest.addEventListener("click", testConnection);
els.chatForm.addEventListener("submit", sendMessage);
document.getElementById("btn-list-memory").addEventListener("click", async () => {
  els.settingsStatus.textContent = "读取记忆中…";
  els.settingsStatus.className = "status-line";
  const res = await fetch("/api/memory?limit=20");
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    els.settingsStatus.textContent = data.detail || "读取失败";
    els.settingsStatus.className = "status-line bad";
    return;
  }
  const items = data.items || [];
  if (!items.length) {
    addNotice(`用户 ${data.user_id} 暂无长期记忆`);
  } else {
    addNotice(
      `用户 ${data.user_id} 的记忆（${items.length}）：\n` +
        items.map((item, i) => `${i + 1}. ${item.memory}`).join("\n"),
    );
  }
  els.settingsStatus.textContent = `已加载 ${items.length} 条记忆`;
  els.settingsStatus.className = "status-line ok";
  closeSettings();
});
document.getElementById("btn-clear-memory").addEventListener("click", async () => {
  if (!window.confirm("确认清空当前记忆用户 ID 下的全部长期记忆？")) return;
  const res = await fetch("/api/memory", { method: "DELETE" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    els.settingsStatus.textContent = data.detail || "清空失败";
    els.settingsStatus.className = "status-line bad";
    return;
  }
  els.settingsStatus.textContent = "记忆已清空";
  els.settingsStatus.className = "status-line ok";
  addNotice("长期记忆已清空");
});
els.btnClear.addEventListener("click", () => {
  state.history = [];
  els.chatLog.innerHTML = "";
  addNotice("对话已清空。可先打开右上角「模型配置」填入你的 API。");
});
els.chatInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    els.chatForm.requestSubmit();
  }
});
els.chatInput.addEventListener("input", autoGrow);
autoGrow();

addNotice("欢迎使用 Judger Agent。先配置上游模型，再输入例如：我想把金山云模型配置到 WorkBuddy");
loadConfig().catch((err) => {
  addNotice(err.message || String(err), "error");
});
