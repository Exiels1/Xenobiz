// XenoBiz frontend
const PREFS_KEY = "xeno_prefs";
const ACTIVE_CONVO_KEY = "xeno_active_conversation_id";
const HISTORY_COLLAPSED_KEY = "xeno_history_collapsed";
let activeConversationId = null;
let pendingAttachmentIds = [];

const prefs = loadPrefs();

function loadPrefs() {
  try {
    return JSON.parse(localStorage.getItem(PREFS_KEY)) || { name: "", style: "concise", theme: "quantum", creative: false };
  } catch {
    return { name: "", style: "concise", theme: "quantum", creative: false };
  }
}

function savePrefs() {
  localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
  applyTheme();
  updateModeBadge();
}

function saveActiveConversation() {
  if (activeConversationId) localStorage.setItem(ACTIVE_CONVO_KEY, String(activeConversationId));
  else localStorage.removeItem(ACTIVE_CONVO_KEY);
}

function loadActiveConversation() {
  const raw = localStorage.getItem(ACTIVE_CONVO_KEY);
  if (!raw) return null;
  const parsed = parseInt(raw, 10);
  return Number.isFinite(parsed) ? parsed : null;
}

function applyTheme() {
  document.body.setAttribute("data-theme", prefs.theme || "quantum");
}

function updateModeBadge() {
  const badge = document.getElementById("modeBadge");
  if (badge) badge.textContent = prefs.creative ? "Creative" : "Normal";
  const signalMode = document.getElementById("signalMode");
  if (signalMode) signalMode.textContent = prefs.creative ? "Exploration" : "Standard";
}

const chatArea = document.getElementById("chatArea");
const chatForm = document.getElementById("chatForm");
const messageInput = document.getElementById("messageInput");
const toggleCreative = document.getElementById("toggleCreative");
const btnProfile = document.getElementById("btnProfile");
const profileDrawer = document.getElementById("profileDrawer");
const btnCloseProfile = document.getElementById("btnCloseProfile");
const prefName = document.getElementById("prefName");
const prefStyle = document.getElementById("prefStyle");
const prefTheme = document.getElementById("prefTheme");
const btnSavePrefs = document.getElementById("btnSavePrefs");
const btnMic = document.getElementById("btnMic");
const btnUpload = document.getElementById("btnUpload");
const fileInput = document.getElementById("fileInput");
const attachmentTray = document.getElementById("attachmentTray");
const promptChips = document.querySelectorAll(".prompt-chip");
const btnRefreshGraph = document.getElementById("btnRefreshGraph");
const btnReloadHistory = document.getElementById("btnReloadHistory");
const btnNewChat = document.getElementById("btnNewChat");
const historyList = document.getElementById("historyList");
const btnToggleHistory = document.getElementById("btnToggleHistory");

applyTheme();
updateModeBadge();
if (toggleCreative) toggleCreative.checked = !!prefs.creative;
if (prefName) prefName.value = prefs.name || "";
if (prefStyle) prefStyle.value = prefs.style || "concise";
if (prefTheme) prefTheme.value = prefs.theme || "quantum";
applyHistoryCollapsedState(localStorage.getItem(HISTORY_COLLAPSED_KEY) === "1");
renderAttachmentTray();

// Enforce icon-only composer actions, even if an older cached template is rendered.
function normalizeComposerButtons() {
  const form = document.getElementById("chatForm");
  if (!form) return;

  const uploadButton = document.getElementById("btnUpload")
    || Array.from(form.querySelectorAll("button")).find((b) => /file/i.test((b.textContent || "").trim()));
  if (uploadButton) {
    uploadButton.classList.add("icon-btn");
    uploadButton.innerHTML = "&#x2B06;";
    uploadButton.setAttribute("title", "Upload file/image");
    uploadButton.setAttribute("aria-label", "Upload file/image");
  }

  const micButton = document.getElementById("btnMic")
    || Array.from(form.querySelectorAll("button")).find((b) => /mic/i.test((b.textContent || "").trim()));
  if (micButton) {
    micButton.classList.add("icon-btn");
    micButton.innerHTML = "&#x25C9;";
    micButton.setAttribute("title", "Voice input");
    micButton.setAttribute("aria-label", "Voice input");
  }

  const sendButton = form.querySelector(".send-btn")
    || Array.from(form.querySelectorAll("button[type='submit'], button")).find((b) => /send/i.test((b.textContent || "").trim()));
  if (sendButton) {
    sendButton.classList.add("icon-btn", "send-btn");
    sendButton.innerHTML = "&#x27A4;";
    sendButton.setAttribute("title", "Send message");
    sendButton.setAttribute("aria-label", "Send message");
  }

  Array.from(form.querySelectorAll("button")).forEach((button) => {
    const id = (button.id || "").toLowerCase();
    const text = (button.textContent || "").trim().toLowerCase();
    if (id.includes("url") || text === "url") {
      button.remove();
    }
  });
}

normalizeComposerButtons();

function inferSentiment(text) {
  const t = (text || "").toLowerCase();
  const pos = ["great", "awesome", "nice", "love", "cool", "thanks", "perfect", "good", "yes"];
  const neg = ["bad", "hate", "angry", "annoyed", "wtf", "no", "broken", "sad", "error", "issue", "problem"];
  const score = pos.reduce((s, w) => s + (t.includes(w) ? 1 : 0), 0) - neg.reduce((s, w) => s + (t.includes(w) ? 1 : 0), 0);
  if (score > 0) return "happy";
  if (score < 0) return "sad";
  return "neutral";
}

function extractUrlsFromText(text, maxUrls = 3) {
  const matches = (text || "").match(/https?:\/\/[^\s<>"')]+/gi) || [];
  const unique = [];
  const seen = new Set();
  for (const raw of matches) {
    const url = raw.trim().replace(/[.,!?;:]+$/, "");
    if (!url || seen.has(url)) continue;
    seen.add(url);
    unique.push(url);
    if (unique.length >= maxUrls) break;
  }
  return unique;
}

let typingEl = null;
function showTyping() {
  if (typingEl || !chatArea) return;
  typingEl = document.createElement("div");
  typingEl.className = "typing";
  typingEl.innerHTML = `<div class="dots"><span></span><span></span><span></span></div>`;
  chatArea.appendChild(typingEl);
}
function hideTyping() {
  if (!typingEl) return;
  typingEl.remove();
  typingEl = null;
}

function appendMessage(role, text, mood = null, ts = null) {
  if (!chatArea) return;
  const el = document.createElement("div");
  el.className = `msg ${role} ${mood || ""}`;

  const content = document.createElement("div");
  content.className = "content";
  content.textContent = text;

  if (role !== "system") {
    const meta = document.createElement("div");
    meta.className = "meta";
    const who = role === "user" ? (prefs.name || "You") : "Xeno";
    const when = ts ? new Date(ts) : new Date();
    meta.textContent = `${who} - ${when.toLocaleTimeString()}`;
    el.appendChild(meta);
  }

  el.appendChild(content);
  chatArea.appendChild(el);
  chatArea.scrollTop = chatArea.scrollHeight;
}

function renderAttachmentTray() {
  if (!attachmentTray) return;
  attachmentTray.innerHTML = "";
  pendingAttachmentIds.forEach((a) => {
    const chip = document.createElement("div");
    chip.className = "attachment-chip";
    chip.innerHTML = `<span>${a.name}</span>`;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "x";
    btn.addEventListener("click", () => {
      pendingAttachmentIds = pendingAttachmentIds.filter((x) => x.id !== a.id);
      renderAttachmentTray();
    });
    chip.appendChild(btn);
    attachmentTray.appendChild(chip);
  });
}

function fmt(ts) {
  const d = ts ? new Date(ts) : new Date();
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function preview(text) {
  if (!text) return "(No messages yet)";
  return text.length > 70 ? `${text.slice(0, 70)}...` : text;
}

async function loadConversations() {
  const res = await fetch("/conversations");
  if (res.status === 401) {
    window.location.href = "/login";
    return [];
  }
  const rows = await res.json();
  if (activeConversationId && !rows.some((r) => String(r.id) === String(activeConversationId))) {
    activeConversationId = null;
  }
  if (!activeConversationId && rows.length) activeConversationId = rows[0].id;
  saveActiveConversation();
  renderConversationSidebar(rows);
  return rows;
}

function renderConversationSidebar(rows) {
  if (!historyList) return;
  historyList.innerHTML = "";
  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "history-empty";
    empty.textContent = "No chats yet.";
    historyList.appendChild(empty);
    return;
  }

  rows.forEach((row) => {
    const item = document.createElement("div");
    item.className = "history-item";
    if (String(row.id) === String(activeConversationId)) item.classList.add("active");
    item.innerHTML = `
      <button type="button" class="history-open">
        <div class="history-item-title">${fmt(row.updated_at)}</div>
        <div class="history-item-preview">${preview(row.title || row.last_message)}</div>
      </button>
      <div class="history-item-controls">
        <button type="button" class="history-ctl rename" title="Rename">Rename</button>
        <button type="button" class="history-ctl delete" title="Delete">Delete</button>
      </div>
    `;
    const openBtn = item.querySelector(".history-open");
    const renameBtn = item.querySelector(".history-ctl.rename");
    const deleteBtn = item.querySelector(".history-ctl.delete");

    openBtn.addEventListener("click", async () => {
      activeConversationId = row.id;
      saveActiveConversation();
      await loadHistory();
      await loadConversations();
    });

    renameBtn.addEventListener("click", () => {
      startInlineRename(item, row);
    });

    deleteBtn.addEventListener("click", async () => {
      const ok = window.confirm("Delete this conversation?");
      if (!ok) return;
      try {
        const res = await fetch(`/conversations/${row.id}`, { method: "DELETE" });
        if (res.status === 401) {
          window.location.href = "/login";
          return;
        }
        const out = await res.json().catch(() => ({}));
        if (!res.ok) {
          window.alert(out.error || "Failed to delete conversation.");
          return;
        }
        activeConversationId = out.active_conversation_id || null;
        saveActiveConversation();
        await loadConversations();
        await loadHistory();
      } catch {
        window.alert("Failed to delete conversation.");
      }
    });

    historyList.appendChild(item);
  });
}

function applyHistoryCollapsedState(collapsed) {
  document.body.classList.toggle("history-collapsed", !!collapsed);
  if (btnToggleHistory) {
    btnToggleHistory.textContent = collapsed ? "Show Chats" : "Hide Chats";
    btnToggleHistory.setAttribute("aria-pressed", collapsed ? "true" : "false");
  }
}

function setHistoryCollapsed(collapsed) {
  localStorage.setItem(HISTORY_COLLAPSED_KEY, collapsed ? "1" : "0");
  applyHistoryCollapsedState(collapsed);
}

async function renameConversation(conversationId, title) {
  const res = await fetch(`/conversations/${conversationId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  if (res.status === 401) {
    window.location.href = "/login";
    return false;
  }
  const out = await res.json().catch(() => ({}));
  if (!res.ok) {
    window.alert(out.error || "Failed to rename conversation.");
    return false;
  }
  return true;
}

function startInlineRename(item, row) {
  if (item.querySelector(".history-edit")) return;
  const openBtn = item.querySelector(".history-open");
  const controls = item.querySelector(".history-item-controls");
  if (!openBtn || !controls) return;

  openBtn.style.display = "none";
  controls.style.display = "none";

  const wrapper = document.createElement("div");
  wrapper.className = "history-edit";
  const input = document.createElement("input");
  input.className = "history-edit-input";
  input.type = "text";
  input.maxLength = 120;
  input.value = row.title || "";
  const actions = document.createElement("div");
  actions.className = "history-edit-actions";
  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.className = "history-ctl save";
  saveBtn.textContent = "Save";
  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.className = "history-ctl cancel";
  cancelBtn.textContent = "Cancel";
  actions.appendChild(saveBtn);
  actions.appendChild(cancelBtn);
  wrapper.appendChild(input);
  wrapper.appendChild(actions);
  item.appendChild(wrapper);

  const teardown = () => {
    wrapper.remove();
    openBtn.style.display = "";
    controls.style.display = "";
  };

  const commit = async () => {
    const title = (input.value || "").trim();
    if (!title) {
      window.alert("Title is required.");
      return;
    }
    const ok = await renameConversation(row.id, title);
    if (!ok) return;
    teardown();
    await loadConversations();
  };

  input.addEventListener("keydown", async (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      await commit();
    } else if (e.key === "Escape") {
      e.preventDefault();
      teardown();
    }
  });
  saveBtn.addEventListener("click", async () => { await commit(); });
  cancelBtn.addEventListener("click", teardown);

  input.focus();
  input.select();
}

async function newChat() {
  const res = await fetch("/conversations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: "New Chat" }),
  });
  if (res.status === 401) {
    window.location.href = "/login";
    return;
  }
  const row = await res.json();
  activeConversationId = row.id;
  saveActiveConversation();
  pendingAttachmentIds = [];
  renderAttachmentTray();
  await loadConversations();
  await loadHistory();
}

async function loadHistory() {
  if (!activeConversationId) {
    chatArea.innerHTML = "";
    return;
  }
  const res = await fetch(`/history?conversation_id=${encodeURIComponent(activeConversationId)}`);
  if (res.status === 401) {
    window.location.href = "/login";
    return;
  }
  const data = await res.json();
  chatArea.innerHTML = "";
  pendingAttachmentIds = [];
  renderAttachmentTray();
  data.forEach((row) => {
    const text = row.content || "";
    appendMessage(row.role === "assistant" ? "assistant" : "user", text, inferSentiment(text), row.timestamp);
  });
  if (!data.length) {
    appendMessage("system", "New chat started. Describe your issue with one specific detail.");
  }
}

function buildContextPrefix() {
  const name = prefs.name ? `User name: ${prefs.name}. ` : "";
  const style = `Style: ${prefs.style}. `;
  const mode = prefs.creative ? "Mode: creative. " : "Mode: normal. ";
  return `[PREFERENCES] ${name}${style}${mode}`;
}

if (chatForm) {
  chatForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const raw = messageInput.value.trim();
    if (!raw || !activeConversationId) return;

    const userMsg = raw;
    appendMessage("user", userMsg, inferSentiment(userMsg));
    messageInput.value = "";
    showTyping();

    try {
      const autoUrls = extractUrlsFromText(userMsg, 3);
      const payload = {
        message: `${buildContextPrefix()}\n${userMsg}`,
        conversation_id: activeConversationId,
        attachment_ids: pendingAttachmentIds.map((a) => a.id),
        url_inputs: autoUrls,
      };

      const resp = await fetch("/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (resp.status === 401) {
        window.location.href = "/login";
        return;
      }
      const j = await resp.json();
      hideTyping();
      appendMessage("assistant", j.reply || "(No reply)", inferSentiment(j.reply || ""));
      activeConversationId = j.conversation_id || activeConversationId;
      saveActiveConversation();
      pendingAttachmentIds = [];
      renderAttachmentTray();
      await loadConversations();
      updateGraphFromText(userMsg + " " + (j.reply || ""));
    } catch {
      hideTyping();
      appendMessage("assistant", "Error contacting server.");
    }
  });
}

promptChips.forEach((chip) => {
  chip.addEventListener("click", () => {
    const template = chip.getAttribute("data-template") || "";
    messageInput.value = template;
    messageInput.focus();
  });
});

if (btnReloadHistory) btnReloadHistory.addEventListener("click", async () => { await loadConversations(); await loadHistory(); });
if (btnNewChat) btnNewChat.addEventListener("click", async () => { await newChat(); });
if (btnUpload && fileInput) {
  btnUpload.addEventListener("click", () => {
    if (!activeConversationId) return;
    fileInput.value = "";
    fileInput.click();
  });
  fileInput.addEventListener("change", async () => {
    if (!activeConversationId || !fileInput.files?.length) return;
    const files = Array.from(fileInput.files);
    for (const file of files) {
      const form = new FormData();
      form.append("conversation_id", String(activeConversationId));
      form.append("file", file);
      try {
        const res = await fetch("/upload", {
          method: "POST",
          body: form,
        });
        if (res.status === 401) {
          window.location.href = "/login";
          return;
        }
        const out = await res.json().catch(() => ({}));
        if (!res.ok) {
          if (res.status === 402 || out.code === "UPLOAD_LIMIT_REACHED") {
            window.alert("Free plan allows up to 2 uploads. Upgrade to upload more files.");
            break;
          }
          window.alert(out.error || "Upload failed.");
          continue;
        }
        pendingAttachmentIds.push({
          id: out.id,
          name: out.original_name || file.name,
        });
        renderAttachmentTray();
      } catch {
        window.alert("Upload failed.");
      }
    }
  });
}
if (btnToggleHistory) {
  btnToggleHistory.addEventListener("click", () => {
    const collapsed = !document.body.classList.contains("history-collapsed");
    setHistoryCollapsed(collapsed);
  });
}

if (btnProfile && profileDrawer) btnProfile.addEventListener("click", () => profileDrawer.classList.add("open"));
if (btnCloseProfile && profileDrawer) btnCloseProfile.addEventListener("click", () => profileDrawer.classList.remove("open"));
if (btnSavePrefs && profileDrawer) {
  btnSavePrefs.addEventListener("click", () => {
    prefs.name = (prefName?.value || "").trim();
    prefs.style = prefStyle?.value || "concise";
    prefs.theme = prefTheme?.value || "quantum";
    savePrefs();
    profileDrawer.classList.remove("open");
  });
}

if (toggleCreative) {
  toggleCreative.addEventListener("change", () => {
    prefs.creative = toggleCreative.checked;
    savePrefs();
  });
}

// Speech input
let recognition = null;
let recognizing = false;
const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
if (btnMic && SpeechRecognition) {
  recognition = new SpeechRecognition();
  recognition.lang = "en-US";
  recognition.interimResults = false;
  recognition.continuous = false;
  recognition.onstart = () => { recognizing = true; btnMic.classList.add("active"); };
  recognition.onend = () => { recognizing = false; btnMic.classList.remove("active"); };
  recognition.onerror = () => { recognizing = false; btnMic.classList.remove("active"); };
  recognition.onresult = (event) => {
    let transcript = "";
    for (let i = event.resultIndex; i < event.results.length; i++) {
      if (event.results[i].isFinal) transcript += event.results[i][0].transcript;
    }
    const finalText = transcript.trim();
    if (!finalText) return;
    messageInput.value = messageInput.value ? `${messageInput.value} ${finalText}` : finalText;
  };
  btnMic.addEventListener("click", () => {
    if (!recognition) return;
    if (recognizing) recognition.stop();
    else recognition.start();
  });
} else if (btnMic) {
  btnMic.disabled = true;
}

// Graph
let cy = null;
function ensureGraph() {
  if (cy) return cy;
  cy = cytoscape({
    container: document.getElementById("graph"),
    style: [
      { selector: "node", style: { "background-color": "data(color)", "label": "data(label)", "color": "#cfe8ff", "font-size": 10, "text-outline-color": "#09111a", "text-outline-width": 1 } },
      { selector: "edge", style: { "width": 2, "line-color": "data(color)", "opacity": 0.8, "curve-style": "unbundled-bezier" } },
    ],
    layout: { name: "cose", animate: true, padding: 20 },
  });
  return cy;
}

const topicPalette = { ai: "#00e6ff", neuroscience: "#9b7cff", astrophysics: "#ff3b88", math: "#3cf0a5", code: "#ffd166", ethics: "#ff5c7a", data: "#7be0ff" };
function extractTopics(text) {
  const t = (text || "").toLowerCase();
  const out = [];
  Object.keys(topicPalette).forEach((k) => { if (t.includes(k)) out.push(k); });
  if (/(ml|machine learning|deep learning)/.test(t)) out.push("ai");
  if (/(brain|cortex|neuron|neural)/.test(t)) out.push("neuroscience");
  if (/(space|galaxy|cosmos|universe|black hole)/.test(t)) out.push("astrophysics");
  if (/(algorithm|python|javascript|flask|api)/.test(t)) out.push("code");
  if (/(data|dataset|database|sql)/.test(t)) out.push("data");
  if (/(proof|theorem|calculus|algebra)/.test(t)) out.push("math");
  if (/(ethic|bias|safety|alignment)/.test(t)) out.push("ethics");
  return [...new Set(out)];
}

function updateGraphFromText(text) {
  const topics = extractTopics(text);
  if (!topics.length) return;
  const g = ensureGraph();
  if (!g.getElementById("session").length) g.add({ group: "nodes", data: { id: "session", label: "Session", color: "#7a8faa" } });
  topics.forEach((t) => {
    const id = `t:${t}`;
    if (!g.getElementById(id).length) {
      g.add({ group: "nodes", data: { id, label: t.toUpperCase(), color: topicPalette[t] || "#59f" } });
      g.add({ group: "edges", data: { id: `e:session:${t}`, source: "session", target: id, color: topicPalette[t] || "#59f" } });
    }
  });
  g.layout({ name: "cose", animate: true }).run();
}

async function rebuildGraphFromHistory() {
  const g = ensureGraph();
  g.elements().remove();
  updateGraphFromText("session");
  if (!activeConversationId) return;
  try {
    const res = await fetch(`/history?conversation_id=${encodeURIComponent(activeConversationId)}`);
    if (!res.ok) return;
    const rows = await res.json();
    rows.forEach((r) => updateGraphFromText(r.content || ""));
  } catch {}
}

if (btnRefreshGraph) btnRefreshGraph.addEventListener("click", () => rebuildGraphFromHistory());

async function boot() {
  activeConversationId = loadActiveConversation();
  await loadConversations();
  await loadHistory();
  await rebuildGraphFromHistory();
}
boot();

window.addEventListener("load", () => {
  const overlay = document.getElementById("startupOverlay");
  if (!overlay) return;
  setTimeout(() => overlay.classList.add("hidden"), 450);
});
