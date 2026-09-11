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

function addBubble(role, text) {
  const div = document.createElement("div");
  div.className = `bubble ${role}`;
  div.textContent = text;
  els.chatLog.appendChild(div);
  els.chatLog.scrollTop = els.chatLog.scrollHeight;
  return div;
}

function setBusy(busy) {
  state.busy = busy;
  els.btnSend.disabled = busy;
  els.chatInput.disabled = busy;
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
  form.enable_memory.checked = !!config.enable_memory;
  form.memory_user_id.value = config.memory_user_id || "default";
  form.memory_llm_model.value = config.memory_llm_model || "";
  form.memory_embed_model.value = config.memory_embed_model || "text-embedding-3-small";
  form.memory_top_k.value = config.memory_top_k ?? 5;
  form.memory_embed_dims.value = config.memory_embed_dims ?? 1536;
  form.system_prompt.value = config.system_prompt || "";
  els.keyHint.textContent = config.api_key_set
    ? `当前已设置 Key：${config.api_key_masked}`
    : "当前未设置 Key";
}

function refreshMeta(config) {
  els.metaModel.textContent = config.model || "—";
  els.metaProtocol.textContent = PROTOCOL_LABEL[config.protocol] || config.protocol || "—";
  const tools = config.enable_tools ? "工具开" : "工具关";
  const mem = config.enable_memory ? "记忆开" : "记忆关";
  els.metaTools.textContent = `${tools} / ${mem}`;
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
    enable_memory: form.enable_memory.checked,
    memory_user_id: form.memory_user_id.value.trim() || "default",
    memory_llm_model: form.memory_llm_model.value.trim(),
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

  // 先保存当前表单，确保测的是最新填写内容
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

  addBubble("user", message);
  state.history.push({ role: "user", content: message });
  els.chatInput.value = "";
  setBusy(true);

  const statusBubble = addBubble("system", "处理中…");

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
        if (payload.type === "status") {
          statusBubble.textContent = payload.message;
        } else if (payload.type === "tool_call") {
          addBubble("tool", `调用工具 ${payload.name}\n${payload.arguments}`);
        } else if (payload.type === "tool_result") {
          addBubble("tool", `工具结果 ${payload.name}\n${payload.result}`);
        } else if (payload.type === "memory") {
          if (payload.action === "recall" && payload.items?.length) {
            const lines = payload.items.map((item, i) => `${i + 1}. ${item.memory}`).join("\n");
            addBubble("system", `召回 ${payload.count} 条记忆：\n${lines}`);
          } else if (payload.action === "write") {
            addBubble("system", "本轮对话已写入长期记忆");
          } else if (payload.action === "recall_error" || payload.action === "write_error") {
            addBubble("tool", payload.message || "记忆操作失败");
          }
        } else if (payload.type === "final") {
          finalText = payload.content || "";
          statusBubble.remove();
          addBubble("assistant", finalText || "(空回复)");
          state.history.push({ role: "assistant", content: finalText || "" });
        } else if (payload.type === "error") {
          statusBubble.remove();
          addBubble("error", payload.message || "未知错误");
        }
      }
    }
  } catch (err) {
    statusBubble.remove();
    addBubble("error", err.message || String(err));
  } finally {
    setBusy(false);
  }
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
    addBubble("system", `用户 ${data.user_id} 暂无长期记忆`);
  } else {
    addBubble(
      "system",
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
  addBubble("system", "长期记忆已清空");
});
els.btnClear.addEventListener("click", () => {
  state.history = [];
  els.chatLog.innerHTML = "";
  addBubble("system", "对话已清空。可先打开右上角「模型配置」填入你的 API。");
});
els.chatInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    els.chatForm.requestSubmit();
  }
});

addBubble("system", "欢迎使用 Forge Agent。先打开「模型配置」，填入你的模型 API，然后即可对话。");
loadConfig().catch((err) => {
  addBubble("error", err.message || String(err));
});
