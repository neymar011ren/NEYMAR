function newSessionId() {
  if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
  return `s-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

const state = {
  config: null,
  history: [],
  busy: false,
  // 会话 ID 在浏览器这边生成并持久化，刷新页面仍算同一个会话；
  // 点「清空对话」才开新会话。后端据此把逐轮记录归组。
  sessionId: localStorage.getItem("kingswitch_session_id") || newSessionId(),
};
localStorage.setItem("kingswitch_session_id", state.sessionId);

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
  btnOpenMemory: document.getElementById("btn-open-memory"),
  memoryDrawer: document.getElementById("memory-drawer"),
  memSummary: document.getElementById("mem-summary"),
  memLongList: document.getElementById("mem-long-list"),
  memShortList: document.getElementById("mem-short-list"),
  memoryStatus: document.getElementById("memory-status"),
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

const MEMORY_ITEM_PREVIEW_LEN = 180;

function formatTokens(n) {
  const num = Number(n || 0);
  if (num >= 1000000) return `${(num / 1000000).toFixed(1)}M`;
  if (num >= 1000) return `${(num / 1000).toFixed(1)}k`;
  return String(num);
}

function formatTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (v) => String(v).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function renderMemorySummary(totals, longCount, sessionCount) {
  const cached = Number(totals.cached_tokens || 0);
  const prompt = Number(totals.prompt_tokens || 0);
  // 缓存命中率 = 缓存读取 / 总输入，直接决定成本，单独算出来展示
  const hitRate = prompt > 0 ? Math.round((cached / prompt) * 100) : 0;
  els.memSummary.innerHTML = `
    <div class="mem-stat">
      <small>长期记忆</small>
      <strong>${longCount}</strong>
      <span>条</span>
    </div>
    <div class="mem-stat">
      <small>会话</small>
      <strong>${sessionCount}</strong>
      <span>个</span>
    </div>
    <div class="mem-stat">
      <small>输入 token</small>
      <strong>${formatTokens(totals.prompt_tokens)}</strong>
      <span>其中缓存 ${formatTokens(cached)}（${hitRate}%）</span>
    </div>
    <div class="mem-stat">
      <small>输出 token</small>
      <strong>${formatTokens(totals.completion_tokens)}</strong>
      <span>累计 ${formatTokens(totals.total_tokens)}</span>
    </div>
  `;
}


function decodeReadableText(value) {
  // 把记忆内容里的 \uXXXX 转义还原成正常汉字，避免面板出现“乱码”
  let text = String(value ?? "");
  if (!text) return "";
  try {
    text = text.replace(/\\u([0-9a-fA-F]{4})/g, (_, hex) =>
      String.fromCharCode(parseInt(hex, 16))
    );
    text = text.replace(/\\x([0-9a-fA-F]{2})/g, (_, hex) =>
      String.fromCharCode(parseInt(hex, 16))
    );
  } catch (_) {
    /* keep original */
  }
  // UTF-8 字节被当成 Latin-1 显示时，尝试按字节还原
  if (/[\u00C0-\u00FF]{3,}/.test(text) && !/[\u4e00-\u9fff]/.test(text)) {
    try {
      const bytes = Uint8Array.from(text, (ch) => ch.charCodeAt(0) & 0xff);
      const decoded = new TextDecoder("utf-8", { fatal: false }).decode(bytes);
      if (/[\u4e00-\u9fff]/.test(decoded)) text = decoded;
    } catch (_) {
      /* ignore */
    }
  }
  return text.replace(/\uFFFD/g, "").normalize("NFC");
}

function makeExpandableText(raw) {
  const full = decodeReadableText(raw);
  const wrap = document.createElement("div");
  wrap.className = "mem-card-text";
  const isLong = full.length > MEMORY_ITEM_PREVIEW_LEN;
  const preview = isLong ? `${full.slice(0, MEMORY_ITEM_PREVIEW_LEN)}…` : full;
  const textEl = document.createElement("span");
  textEl.textContent = preview;
  wrap.appendChild(textEl);
  if (isLong) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "mem-expand";
    btn.textContent = "展开";
    let expanded = false;
    btn.addEventListener("click", (event) => {
      event.stopPropagation();
      expanded = !expanded;
      textEl.textContent = expanded ? full : preview;
      btn.textContent = expanded ? "收起" : "展开";
    });
    wrap.appendChild(btn);
  }
  return wrap;
}

function renderLongTermMemories(items) {
  els.memLongList.innerHTML = "";
  if (!items.length) {
    els.memLongList.innerHTML = '<p class="mem-empty">暂无长期记忆</p>';
    return;
  }
  items.forEach((item) => {
    const card = document.createElement("div");
    card.className = "mem-card";
    const head = document.createElement("div");
    head.className = "mem-card-head";
    head.innerHTML = `
      <span class="mem-badge mem-badge-long">长期</span>
      <span class="mem-card-time">${escapeHtml(formatTime(item.created_at))}</span>
      <span class="mem-card-backend">${escapeHtml(item.backend || "local")}</span>
    `;
    card.appendChild(head);
    card.appendChild(makeExpandableText(String(item.memory || "")));

    if (item.session_id) {
      const foot = document.createElement("div");
      foot.className = "mem-card-foot";
      const jump = document.createElement("button");
      jump.type = "button";
      jump.className = "mem-jump";
      jump.textContent = "跳转到来源会话 →";
      jump.addEventListener("click", () => replaySession(item.session_id));
      foot.appendChild(jump);
      card.appendChild(foot);
    }
    els.memLongList.appendChild(card);
  });
}

function renderSessions(sessions) {
  els.memShortList.innerHTML = "";
  if (!sessions.length) {
    els.memShortList.innerHTML = '<p class="mem-empty">暂无会话记录</p>';
    return;
  }
  sessions.forEach((session) => {
    const card = document.createElement("div");
    card.className = "mem-card mem-card-session";
    if (session.session_id === state.sessionId) card.classList.add("is-current");
    const usage = session.usage || {};
    const tools = session.tool_calls || [];
    card.innerHTML = `
      <div class="mem-card-head">
        <span class="mem-badge mem-badge-short">短期</span>
        <span class="mem-card-time">${escapeHtml(formatTime(session.last_at))}</span>
        ${session.session_id === state.sessionId ? '<span class="mem-card-current">当前会话</span>' : ""}
      </div>
      <div class="mem-card-title">${escapeHtml(decodeReadableText(session.title || "(无标题会话)"))}</div>
      <div class="mem-chips">
        <span class="mem-chip">${session.turns} 轮</span>
        <span class="mem-chip">输入 ${formatTokens(usage.prompt_tokens)}</span>
        <span class="mem-chip">输出 ${formatTokens(usage.completion_tokens)}</span>
        ${Number(usage.cached_tokens) > 0 ? `<span class="mem-chip mem-chip-cache">缓存 ${formatTokens(usage.cached_tokens)}</span>` : ""}
        ${tools.length ? `<span class="mem-chip mem-chip-tool">工具 ${tools.length} 种</span>` : ""}
      </div>
      ${tools.length ? `<div class="mem-tools">${tools.map((t) => `<span class="mem-chip mem-chip-tool">${escapeHtml(t)}</span>`).join("")}</div>` : ""}
      <div class="mem-card-foot">
        <button type="button" class="mem-jump">在聊天区回放 →</button>
      </div>
    `;
    card.querySelector(".mem-jump").addEventListener("click", () => replaySession(session.session_id));
    els.memShortList.appendChild(card);
  });
}

async function loadMemoryData() {
  els.memoryStatus.textContent = "加载中…";
  els.memoryStatus.className = "status-line";
  let longItems = [];
  let sessions = [];
  let totals = {};
  const notes = [];

  // 长期记忆依赖 enable_memory 开关；关掉时这个接口会返回 400，
  // 但短期记忆（用量统计）仍然应该能看，所以两个请求分开容错。
  try {
    const res = await fetch("/api/memory?limit=50");
    const data = await res.json().catch(() => ({}));
    if (res.ok) longItems = data.items || [];
    else notes.push(data.detail || "长期记忆不可用");
  } catch (err) {
    notes.push(`长期记忆读取失败：${err.message || err}`);
  }

  try {
    const res = await fetch("/api/sessions?limit=50");
    const data = await res.json().catch(() => ({}));
    if (res.ok) {
      sessions = data.sessions || [];
      totals = data.totals || {};
    } else {
      notes.push(data.detail || "会话记录不可用");
    }
  } catch (err) {
    notes.push(`会话记录读取失败：${err.message || err}`);
  }

  renderMemorySummary(totals, longItems.length, sessions.length);
  renderLongTermMemories(longItems);
  renderSessions(sessions);
  els.memoryStatus.textContent = notes.length ? notes.join("；") : "";
  els.memoryStatus.className = notes.length ? "status-line bad" : "status-line";
}

async function replaySession(sessionId) {
  closeMemory();
  const banner = addNotice("正在加载会话记录…");
  try {
    const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`);
    const data = await res.json().catch(() => ({}));
    banner.remove();
    if (!res.ok) {
      addNotice(data.detail || "会话加载失败", "error");
      return;
    }
    const turns = data.turns || [];
    els.chatLog.innerHTML = "";
    const isCurrent = sessionId === state.sessionId;
    addNotice(
      isCurrent
        ? `已回放当前会话的 ${turns.length} 轮记录，可继续对话。`
        : `正在查看历史会话（${turns.length} 轮，只读回放）。继续输入会归入当前会话。`,
    );
    turns.forEach((turn) => {
      addUserBubble(turn.user_message || "");
      const card = createTurnCard();
      // 回放和实时对话共用同一套 token chip 渲染，保证两处看到的数字口径一致
      finalizeAnswer(card, turn.assistant_message || "", [], turn.usage, turn.tool_calls);
    });
    scrollChat();
  } catch (err) {
    banner.remove();
    addNotice(`会话加载失败：${err.message || err}`, "error");
  }
}

function openMemory() {
  els.memoryDrawer.classList.add("open");
  els.memoryDrawer.setAttribute("aria-hidden", "false");
  loadMemoryData();
}

function closeMemory() {
  els.memoryDrawer.classList.remove("open");
  els.memoryDrawer.setAttribute("aria-hidden", "true");
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
            <div class="avatar avatar-bot" aria-hidden="true">K</div>
    <div class="msg-col">
      <div class="msg-role">KingSwitch</div>
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
  const readable = decodeReadableText(message);
  details.innerHTML = `
    <summary>
      <span class="rail-ico" aria-hidden="true">◉</span>
      <span class="rail-title">记忆</span>
      <span class="rail-sub">长期记忆</span>
      <span class="chev" aria-hidden="true"></span>
    </summary>
    <div class="rail-body"><div class="memory-plain"></div></div>
  `;
  details.querySelector(".memory-plain").textContent = readable;
  turn.rail.appendChild(details);
  scrollChat();
}

function appendInstallOffer(turn, offer) {
  const card = document.createElement("div");
  card.className = "install-card";
  const osLabel = offer.os?.os_key || offer.os?.system || "当前系统";
  const cmds = (offer.commands || []).map((c) => `<code>${escapeHtml(c)}</code>`).join("<br/>");
  card.innerHTML = `
    <div class="install-head">
      <div>
        <div class="install-kicker">Install · ${escapeHtml(osLabel)}</div>
        <div class="install-title">${escapeHtml(offer.name || offer.agent_id || "Agent")}</div>
        <div class="install-desc">${escapeHtml(offer.notes || "")}</div>
      </div>
      <button type="button" class="primary-btn install-btn">${escapeHtml(offer.button_label || "安装")}</button>
    </div>
    <div class="install-meta">
      <span>包：${escapeHtml(offer.package_name || "—")}</span>
      ${offer.docs_url ? `<a href="${escapeHtml(offer.docs_url)}" target="_blank" rel="noreferrer">文档</a>` : ""}
      ${offer.download_url ? `<a href="${escapeHtml(offer.download_url)}" target="_blank" rel="noreferrer">下载</a>` : ""}
    </div>
    ${cmds ? `<div class="install-cmds">${cmds}</div>` : ""}
    ${offer.search_notes ? `<div class="install-search">${escapeHtml(offer.search_notes)}</div>` : ""}
    ${offer.manual_hint ? `<div class="install-manual">${escapeHtml(offer.manual_hint)}</div>` : ""}
    <pre class="install-log" hidden></pre>
  `;
  const btn = card.querySelector(".install-btn");
  const log = card.querySelector(".install-log");
  if (!offer.auto_installable) {
    btn.textContent = offer.download_url ? "打开下载页" : "查看文档";
    btn.addEventListener("click", () => {
      const url = offer.download_url || offer.docs_url;
      if (url) window.open(url, "_blank", "noreferrer");
    });
  } else {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      btn.textContent = "安装中…";
      log.hidden = false;
      log.textContent = "";
      try {
        const res = await fetch("/api/agents/install", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ agent_id: offer.agent_id }),
        });
        if (!res.ok || !res.body) throw new Error(`安装请求失败 HTTP ${res.status}`);
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let ok = false;
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const parts = buffer.split("\n\n");
          buffer = parts.pop() || "";
          for (const part of parts) {
            const line = part.trim();
            if (!line.startsWith("data:")) continue;
            const payload = JSON.parse(line.slice(5).trim());
            if (payload.type === "log" || payload.type === "status" || payload.type === "command") {
              const text =
                payload.line ||
                payload.message ||
                (payload.command ? `$ ${payload.command}` : "");
              if (text) log.textContent += `${text}\n`;
            } else if (payload.type === "error") {
              log.textContent += `错误：${payload.message || ""}\n`;
              btn.textContent = "重试安装";
              btn.disabled = false;
            } else if (payload.type === "done") {
              ok = !!payload.ok;
              log.textContent += `${payload.message || "完成"}\n`;
              btn.textContent = ok ? "已安装" : "重试安装";
              btn.disabled = ok;
              if (ok) card.classList.add("is-done");
            }
          }
          scrollChat();
        }
        if (!ok && btn.disabled) {
          btn.disabled = false;
          btn.textContent = "重试安装";
        }
      } catch (err) {
        log.textContent += `${err.message || err}\n`;
        btn.disabled = false;
        btn.textContent = "重试安装";
      }
      scrollChat();
    });
  }
  turn.rail.appendChild(card);
  scrollChat();
}

function appendWriteOffer(turn, offer) {
  const card = document.createElement("div");
  card.className = "install-card write-card";
  const params = offer.params || {};
  card.innerHTML = `
    <div class="install-head">
      <div>
        <div class="install-kicker">Write · 需要确认</div>
        <div class="install-title">${escapeHtml(offer.name || offer.agent_id || "Agent")}</div>
        <div class="install-desc">${escapeHtml(offer.message || "")}</div>
      </div>
      <button type="button" class="primary-btn write-btn">确认写入</button>
    </div>
    <div class="install-meta">
      <span>路径：${escapeHtml(offer.target_path || "—")}</span>
      <span>${offer.will_create_new_file ? "将新建文件" : "将更新已有文件（自动备份）"}</span>
      <span>Key：${escapeHtml(offer.api_key_masked || "***")}</span>
    </div>
    <pre class="install-log write-log" hidden></pre>
  `;
  const btn = card.querySelector(".write-btn");
  const log = card.querySelector(".write-log");
  btn.addEventListener("click", async () => {
    if (!params.base_url || !params.model || !params.api_key) {
      log.hidden = false;
      log.textContent = "缺少必要参数（base_url / model / api_key），请重新走一遍配置流程。";
      return;
    }
    btn.disabled = true;
    btn.textContent = "写入中…";
    log.hidden = false;
    log.textContent = "";
    try {
      const res = await fetch("/api/agents/write-config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(params),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(data.detail || `写入失败 HTTP ${res.status}`);
      }
      const written = data.written || {};
      const lines = [`已写入：${written.path || offer.target_path || ""}`];
      if (written.backup) lines.push(`已备份原文件：${written.backup}`);
      if (written.warning) lines.push(`提示：${written.warning}`);
      log.textContent = lines.join("\n");
      btn.textContent = "已写入";
      card.classList.add("is-done");
    } catch (err) {
      log.textContent = err.message || String(err);
      btn.disabled = false;
      btn.textContent = "重试写入";
    }
    scrollChat();
  });
  turn.rail.appendChild(card);
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

function appendTurnUsage(turn, usage, toolCalls) {
  const u = usage || {};
  const total = Number(u.total_tokens) || 0;
  // 上游没返回 usage 时（部分网关不支持 include_usage）就别显示一行全 0，否则更误导
  if (!total && !(toolCalls || []).length) return;
  const row = document.createElement("div");
  row.className = "turn-usage";
  const bits = [];
  if (total) {
    bits.push(`<span class="mem-chip">输入 ${formatTokens(u.prompt_tokens)}</span>`);
    bits.push(`<span class="mem-chip">输出 ${formatTokens(u.completion_tokens)}</span>`);
    if (Number(u.cached_tokens) > 0) {
      bits.push(`<span class="mem-chip mem-chip-cache">缓存 ${formatTokens(u.cached_tokens)}</span>`);
    }
    bits.push(`<span class="mem-chip mem-chip-total">共 ${formatTokens(total)}</span>`);
  }
  (toolCalls || []).forEach((name) => {
    bits.push(`<span class="mem-chip mem-chip-tool">${escapeHtml(name)}</span>`);
  });
  row.innerHTML = bits.join("");
  turn.root.querySelector(".msg-col").appendChild(row);
}

function finalizeAnswer(turn, content, references, usage, toolCalls) {
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
  appendTurnUsage(turn, usage, toolCalls);
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

function syncMemorySectionState() {
  const section = document.getElementById("memory-section");
  if (!section) return;
  const enabled = !!els.settingsForm.enable_memory.checked;
  section.classList.toggle("is-disabled", !enabled);
  section.querySelectorAll("input, textarea, button").forEach((el) => {
    el.disabled = !enabled;
  });
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
  form.enable_auto_scan.checked = !!config.enable_auto_scan;
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
  syncMemorySectionState();
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
    enable_auto_scan: form.enable_auto_scan.checked,
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
        session_id: state.sessionId,
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
        } else if (payload.type === "install_offer") {
          appendInstallOffer(turn, payload);
        } else if (payload.type === "write_offer") {
          appendWriteOffer(turn, payload);
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
          finalizeAnswer(turn, finalText, payload.references || [], payload.usage, payload.tool_calls);
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
els.settingsForm.enable_memory.addEventListener("change", syncMemorySectionState);
els.btnTest.addEventListener("click", testConnection);
els.chatForm.addEventListener("submit", sendMessage);
els.btnOpenMemory.addEventListener("click", openMemory);
document.getElementById("btn-open-memory-from-settings").addEventListener("click", () => {
  closeSettings();
  openMemory();
});
els.memoryDrawer.querySelectorAll("[data-close-memory]").forEach((node) => {
  node.addEventListener("click", closeMemory);
});
els.memoryDrawer.querySelectorAll(".mem-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    const target = tab.dataset.tab;
    els.memoryDrawer.querySelectorAll(".mem-tab").forEach((t) => t.classList.toggle("is-active", t === tab));
    els.memoryDrawer
      .querySelectorAll(".mem-pane")
      .forEach((pane) => pane.classList.toggle("is-active", pane.dataset.pane === target));
  });
});
document.getElementById("btn-refresh-memory").addEventListener("click", loadMemoryData);
document.getElementById("btn-clear-memory").addEventListener("click", async () => {
  if (!window.confirm("确认清空当前记忆用户 ID 下的全部长期记忆？")) return;
  const res = await fetch("/api/memory", { method: "DELETE" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    els.memoryStatus.textContent = data.detail || "清空失败";
    els.memoryStatus.className = "status-line bad";
    return;
  }
  els.memoryStatus.textContent = "长期记忆已清空";
  els.memoryStatus.className = "status-line ok";
  loadMemoryData();
});
document.getElementById("btn-clear-sessions").addEventListener("click", async () => {
  if (!window.confirm("确认清空当前记忆用户 ID 下的全部会话记录（含 token 统计）？")) return;
  const res = await fetch("/api/sessions", { method: "DELETE" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    els.memoryStatus.textContent = data.detail || "清空失败";
    els.memoryStatus.className = "status-line bad";
    return;
  }
  els.memoryStatus.textContent = `已清空 ${data.cleared ?? 0} 条会话记录`;
  els.memoryStatus.className = "status-line ok";
  loadMemoryData();
});
els.btnClear.addEventListener("click", () => {
  state.history = [];
  els.chatLog.innerHTML = "";
  // 清空对话 = 开一个新会话，之后的轮次会归到新的 session_id 下
  state.sessionId = newSessionId();
  localStorage.setItem("kingswitch_session_id", state.sessionId);
  addNotice("对话已清空，已开始新会话。可先打开右上角「模型配置」填入你的 API。");
});
els.chatInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    els.chatForm.requestSubmit();
  }
});
els.chatInput.addEventListener("input", autoGrow);
autoGrow();

const SCAN_STATUS_LABEL = {
  ok: "已就绪",
  probe_failed: "探测未通过",
  not_configured: "未配置渠道",
  not_installed: "未安装",
};

function scanRowMarks(row) {
  const mark = (v) => (v === true ? "✅" : v === false ? "❌" : "—");
  const probe = row.probe || {};
  const fidelity = row.protocol_fidelity;
  return {
    installed: mark(row.installed),
    config: row.config_found ? "✅" : "—",
    ping: probe.ping ? mark(probe.ping.ok) : "—",
    tool: probe.tool_calling ? mark(probe.tool_calling.ok) : "—",
    fidelity: fidelity && !fidelity.skipped ? mark(fidelity.ok) : "—",
  };
}

function renderScanPanel(scan) {
  const rows = scan.agents || [];
  const configuredCount = rows.filter((r) => r.config_found).length;
  const okCount = rows.filter((r) => r.status === "ok").length;
  const reportUrl = scan.report_file ? `/api/scan/report/${encodeURIComponent(scan.report_file)}` : "";

  const card = document.createElement("div");
  card.className = "scan-panel";
  card.innerHTML = `
    <div class="scan-panel-head">
      <div>
        <div class="install-kicker">SCAN · 本机 Agent 概览</div>
        <div class="install-title">巡检完成</div>
        <div class="install-desc">共 ${rows.length} 个已知 Agent，${configuredCount} 个已配置渠道，${okCount} 个探测全部通过。</div>
      </div>
      ${reportUrl ? `<a class="ghost-btn" href="${escapeHtml(reportUrl)}" download>下载报告</a>` : ""}
    </div>
    <div class="scan-table">
      <div class="scan-row scan-row-head">
        <span>Agent</span><span>安装</span><span>渠道</span><span>连通</span><span>工具调用</span><span>协议保真</span>
      </div>
    </div>
  `;
  const table = card.querySelector(".scan-table");
  rows.forEach((row) => {
    const marks = scanRowMarks(row);
    const tr = document.createElement("div");
    tr.className = "scan-row";
    tr.title = SCAN_STATUS_LABEL[row.status] || row.status || "";
    tr.innerHTML = `
      <span class="scan-agent-name">${escapeHtml(row.name || row.agent_id)}</span>
      <span>${marks.installed}</span>
      <span>${marks.config}</span>
      <span>${marks.ping}</span>
      <span>${marks.tool}</span>
      <span>${marks.fidelity}</span>
    `;
    table.appendChild(tr);
  });
  els.chatLog.appendChild(card);
  scrollChat();
  return card;
}

async function runAutoScan() {
  const banner = addNotice("正在巡检本机 Agent…");
  try {
    const res = await fetch("/api/scan");
    const data = await res.json().catch(() => ({}));
    banner.remove();
    if (!res.ok) {
      addNotice(data.detail || "巡检失败", "error");
      return;
    }
    renderScanPanel(data);
  } catch (err) {
    banner.remove();
    addNotice(`巡检失败：${err.message || err}`, "error");
  }
}

addNotice("欢迎使用 KingSwitch。先配置上游模型，再输入例如：我想把金山云模型配置到 WorkBuddy");
loadConfig()
  .then(() => {
    if (state.config && state.config.enable_auto_scan) {
      runAutoScan();
    }
  })
  .catch((err) => {
    addNotice(err.message || String(err), "error");
  });
