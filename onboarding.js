"use strict";
const $ = id => document.getElementById(id);
const fields = ["name", "owner", "purpose", "preset", "tone", "length", "humor", "extra", "identity", "host", "model", "num_ctx"];
const titles = ["Welcome", "Name", "Personality", "Brain", "Try it", "Telegram", "Ready"];
let step = 0, initial = null, customIdentity = false, pairingTimer = null;
let candidate = null, paired = false, pendingPair = false, busy = false;
let generation = 0, pendingIdentity = Promise.resolve();

async function api(action, data = {}) {
  const response = await fetch("/setup/" + action, {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(data)
  });
  const result = await response.json();
  if (!response.ok || result.error) throw new Error(result.error || "Setup request failed.");
  return result;
}
function draft() {
  const value = Object.fromEntries(fields.map(id => [id, $(id).value]));
  value.num_ctx = Number(value.num_ctx);
  value.custom_identity = customIdentity;
  return value;
}
async function saveDraft(target = step) {
  await pendingIdentity;
  await api("draft", {step: target, draft: draft()});
}
function error(message) { $("error").textContent = message; $("error").hidden = !message; }
async function run(fn) {
  if (busy) return;
  busy = true; error(""); $("busy").hidden = false;
  document.querySelectorAll("button").forEach(b => b.disabled = true);
  try { await fn(); }
  catch (e) { error(e.message || "Request failed. Is BarrelShell still running?"); }
  finally {
    busy = false; $("busy").hidden = true;
    document.querySelectorAll("button").forEach(b => b.disabled = false);
  }
}
function render() {
  document.querySelectorAll("section[data-step]").forEach(s => s.hidden = Number(s.dataset.step) !== step);
  $("steps").replaceChildren();
  titles.forEach((title, index) => {
    const item = document.createElement("span");
    item.textContent = `${index + 1}. ${title}`;
    if (index === step) { item.className = "current"; item.setAttribute("aria-current", "step"); }
    $("steps").append(item);
  });
  $("back").hidden = step === 0;
  $("next").textContent = step === 0 ? "Let’s begin" : step === 6 ? "Start chatting" : step === 5 ? "Continue / use local chat" : "Save & continue";
  if (step === 6) {
    $("summary").textContent = `${$("name").value || "Your Barrel"}\nPersonality: identity.md\nModel: ${$("model").value}\nOllama: ${$("host").value}\nLocal chat: ready\nTelegram: ${initial.telegram_enabled ? "already enabled" : paired ? "paired — restart required" : "not linked (optional)"}`;
    $("final-restart").hidden = !(paired || initial.restart_required);
  }
}
async function generateIdentity() {
  const version = ++generation;
  const result = await api("identity-preview", draft());
  if (version === generation && !customIdentity) $("identity").value = result.identity;
}

$("next").onclick = () => run(async () => {
  await pendingIdentity;
  if (step === 1 && !$("name").value.trim()) throw new Error("Give your Barrel a name.");
  if (step === 2) {
    await api("save-identity", {identity: $("identity").value, replace: $("replace").checked});
    initial.identity = $("identity").value;
    $("replace-wrap").hidden = false;
    $("replace").checked = false;
  }
  if (step === 3) await api("save-backend", draft());
  if (step === 5 && pendingPair) throw new Error("Confirm the Telegram account or cancel pairing before continuing.");
  if (step === 6) { await api("finish"); location.href = "/"; return; }
  await saveDraft(step + 1);
  step++; render(); window.scrollTo(0, 0);
});
$("back").onclick = () => run(async () => { await saveDraft(step - 1); step--; render(); });
$("later").onclick = () => run(async () => {
  await saveDraft();
  // Pairing is ephemeral; never leave an owner-registration poller behind.
  if (pendingPair) await cancelPair();
  await api("dismiss"); location.href = "/";
});
$("generate").onclick = () => run(async () => {
  if ($("identity").value && !confirm("Replace the preview with a personality generated from the choices above? This does not save the file yet.")) return;
  customIdentity = false; await generateIdentity(); await saveDraft();
});
$("identity").oninput = () => { generation++; customIdentity = true; };
for (const id of ["name", "owner", "purpose", "preset", "tone", "length", "humor", "extra"]) {
  $(id).addEventListener("change", () => {
    if (!customIdentity) pendingIdentity = generateIdentity().catch(e => error(e.message));
  });
}
$("check").onclick = () => run(async () => {
  const result = await api("models", draft());
  $("models").replaceChildren();
  result.models.forEach(name => { const option = document.createElement("option"); option.value = name; $("models").append(option); });
  $("connection").textContent = "✓ Ollama is reachable. " + (result.models.length ? `${result.models.length} installed model(s); choose one below.` : "No models installed yet. Use the pull command below.");
  if (result.models.length && !result.models.includes($("model").value)) $("model").value = result.models[0];
});
$("test").onclick = () => run(async () => {
  $("test-result").textContent = "Testing actual model generation…";
  try {
    const result = await api("test-model", draft());
    $("test-result").textContent = `✓ Model returned a response (${result.seconds}s)\n${result.reply}`;
    await saveDraft();
  } catch (e) { $("test-result").textContent = "✗ No successful response yet."; throw e; }
});
async function preview() {
  const result = await api("preview-chat", {...draft(), message: $("preview-message").value});
  $("preview-result").textContent = `${result.reply}\n\n(${result.seconds}s · isolated preview)`;
}
$("preview-send").onclick = () => run(preview);
document.querySelectorAll(".prompt").forEach(button => button.onclick = () => run(async () => {
  $("preview-message").value = button.dataset.prompt; await preview();
}));

function stopPolling() { if (pairingTimer) clearInterval(pairingTimer); pairingTimer = null; }
async function pollPair() {
  try {
    const result = await api("telegram-status");
    if (result.phase === "candidate") {
      candidate = result.candidate;
      $("tg-status").textContent = `✓ Token validated: @${result.username}\n✓ Private pairing message received\n\nIs this you?\n${candidate.name} ${candidate.username ? "(@" + candidate.username + ")" : ""}\nNumeric user ID: ${candidate.id}`;
      $("tg-confirm").hidden = false; stopPolling();
    } else if (["error", "expired", "idle"].includes(result.phase)) {
      $("tg-status").textContent = result.error || "Pairing expired or was cancelled. Start again for a new link.";
      stopPolling(); pendingPair = false;
      $("pairing").hidden = true; $("tg-confirm").hidden = true;
    } else if (result.phase === "confirmed") {
      stopPolling(); paired = true; pendingPair = false;
      $("restart-note").hidden = false;
    } else {
      $("tg-status").textContent = "✓ Token validated\nWaiting for you to press Start in the private Telegram chat…";
    }
  } catch (e) { $("tg-status").textContent = e.message + " You can cancel and retry."; }
}
$("tg-start").onclick = () => run(async () => {
  const token = $("token").value.trim();
  $("token").value = ""; // Never persist the token in browser storage or draft state.
  const result = await api("telegram-start", {token});
  pendingPair = true;
  $("pair-link").href = result.link;
  $("tg-bot").textContent = `Token validated: @${result.username}`;
  $("pairing").hidden = false; $("tg-cancel").hidden = false;
  await pollPair(); stopPolling(); pairingTimer = setInterval(pollPair, 2000);
});
$("tg-confirm").onclick = () => run(async () => {
  const result = await api("telegram-confirm", {user_id: candidate.id});
  paired = true; pendingPair = false; stopPolling();
  $("tg-confirm").hidden = true; $("pairing").hidden = true; $("tg-cancel").hidden = true;
  $("tg-status").textContent = "✓ Your account authorized\n" + (result.delivery ? "✓ Test reply sent to Telegram" : "⚠ Credentials saved, but test delivery failed. Check Telegram connectivity after restart.");
  $("restart-note").hidden = false;
});
async function cancelPair() {
  await api("telegram-cancel"); stopPolling(); pendingPair = false; candidate = null;
  $("pairing").hidden = true; $("tg-confirm").hidden = true; $("tg-cancel").hidden = true;
  $("tg-status").textContent = "Pairing cancelled. Saved credentials, if any, are unchanged.";
}
$("tg-cancel").onclick = () => run(cancelPair);

async function init() {
  const response = await fetch("/setup/status");
  initial = await response.json();
  if (!response.ok || initial.error) throw new Error(initial.error || "Cannot load setup.");
  const state = initial.state;
  $("host").value = initial.host; $("model").value = initial.model; $("num_ctx").value = initial.num_ctx;
  $("identity").value = initial.identity;
  customIdentity = Boolean(initial.identity);
  if (initial.identity) {
    const match = initial.identity.match(/You are ([^,\n]+)/);
    if (match) $("name").value = match[1];
  }
  if (state.draft) {
    for (const id of fields) {
      if (state.draft[id] !== undefined && (!state.completed || !["identity", "host", "model", "num_ctx"].includes(id))) {
        $(id).value = state.draft[id];
      }
    }
    customIdentity = state.completed ? Boolean(initial.identity) : Boolean(state.draft.custom_identity);
  }
  step = Number.isInteger(state.step) && state.step >= 0 && state.step <= 6 ? state.step : 0;
  // Returning via Settings starts at welcome; a deferred incomplete wizard resumes.
  if (state.completed) step = 0;
  $("existing").hidden = !initial.existing;
  $("replace-wrap").hidden = !initial.identity;
  $("tg-existing").hidden = !initial.telegram_enabled;
  $("tg-new").hidden = initial.telegram_enabled;
  $("restart-note").hidden = !initial.restart_required;
  paired = initial.restart_required;
  if (!$("identity").value) { customIdentity = false; await generateIdentity(); }
  // A refresh can resume the server's temporary pairing state (not its secret link).
  const pair = await api("telegram-status");
  if (["waiting", "candidate"].includes(pair.phase)) {
    pendingPair = true; $("tg-cancel").hidden = false;
    await pollPair();
    if (pair.phase === "waiting") pairingTimer = setInterval(pollPair, 2000);
  }
  render();
}
run(init);
