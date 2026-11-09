const state = { documents: [], collections: [], selected: new Set(), materials: [], activeCollection: "", activeMaterial: null };
const $ = (selector, root = document) => root.querySelector(selector);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
const cleanQuizText = (value) => String(value ?? "")
  .replace(/\s*\[\s*S\d+(?:\s*,[^\]]*)?\s*\]/gi, "")
  .replace(/\s*\(\s*S\d+\s*\)/gi, "")
  .replace(/\b(?:according\s+to|as\s+(?:stated|described|explained)\s+in|described\s+in|in|from|per)\s+S\d+\b\s*,?\s*/gi, "")
  .replace(/\bS\d+\b/gi, "")
  .replace(/\(\s*\)/g, "")
  .replace(/\s+([,.;:!?])/g, "$1")
  .replace(/\s{2,}/g, " ")
  .trim();

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { ...(options.body && !(options.body instanceof FormData) ? { "Content-Type": "application/json" } : {}), ...(options.headers || {}) } });
  let data = {};
  try { data = await response.json(); } catch { /* empty body */ }
  if (!response.ok) throw new Error(data.detail || `Request failed (${response.status})`);
  return data;
}

function notice(message, type = "success") {
  const box = $("#notice");
  box.textContent = message;
  box.setAttribute("role", type === "error" ? "alert" : "status");
  box.setAttribute("aria-live", type === "error" ? "assertive" : "polite");
  box.className = `notice ${type === "error" ? "error" : type === "warning" ? "warning" : ""}`;
  box.hidden = false;
  window.clearTimeout(notice.timer);
  notice.timer = window.setTimeout(() => { box.hidden = true; }, 6500);
}

function speakText(text, button) {
  if (!window.speechSynthesis) { notice("Read aloud is not available in this browser.", "warning"); return; }
  if (state.speechButton === button && window.speechSynthesis.speaking) {
    window.speechSynthesis.cancel();
    button.textContent = button.dataset.readLabel || "Read aloud";
    state.speechButton = null;
    return;
  }
  window.speechSynthesis.cancel();
  if (state.speechButton) state.speechButton.textContent = state.speechButton.dataset.readLabel || "Read aloud";
  const utterance = new SpeechSynthesisUtterance(text);
  state.speechButton = button;
  button.dataset.readLabel = button.textContent;
  button.textContent = "Stop reading";
  const reset = () => {
    button.textContent = button.dataset.readLabel || "Read aloud";
    if (state.speechButton === button) state.speechButton = null;
  };
  utterance.onend = reset;
  utterance.onerror = reset;
  window.speechSynthesis.speak(utterance);
}

function setBusy(button, busy, label = "Working…") {
  if (!button) return;
  button.setAttribute("aria-busy", String(busy));
  if (busy) { button.dataset.label = button.textContent; button.textContent = label; button.disabled = true; }
  else { button.textContent = button.dataset.label || button.textContent; button.disabled = false; }
}

async function loadLibrary() {
  const data = await api("/api/library");
  state.documents = data.documents || [];
  state.collections = data.collections || [];
  renderDocuments();
  renderCollections();
  renderScope();
  renderOrphans(data.unregistered_uploads || []);
  $("#library-count").textContent = state.documents.length;
  $("#doc-stat").textContent = state.documents.length;
  $("#chunk-stat").textContent = state.documents.reduce((sum, item) => sum + item.chunk_count, 0);
}

function renderDocuments() {
  const list = $("#document-list");
  const filter = $("#library-filter").value.trim().toLowerCase();
  const docs = state.documents.filter((doc) => doc.original_filename.toLowerCase().includes(filter));
  if (!docs.length) {
    list.innerHTML = `<div class="empty-state">${filter ? "No documents match that filter." : "Your library is ready when you are. Add a PDF or DOCX to begin."}</div>`;
    return;
  }
  list.innerHTML = docs.map((doc) => {
    const selected = state.selected.has(doc.id) ? "checked" : "";
    const ext = doc.file_type.toLowerCase() === "docx" ? "docx" : "pdf";
    const status = doc.duplicate_of ? `<span class="status-badge warning">Duplicate preserved</span>` : doc.indexed ? `<span class="status-badge ready">● ${doc.chunk_count} passages</span>` : `<span class="status-badge">Not indexed</span>`;
    const collectionOptions = state.collections.length
      ? [`<option value="">Add to collection…</option>`, ...state.collections.map((collection) => {
          const included = doc.collections.some((item) => item.id === collection.id);
          return `<option value="${escapeHtml(collection.id)}" ${included ? "disabled" : ""}>${included ? "✓ " : ""}${escapeHtml(collection.name)}${included ? " (added)" : ""}</option>`;
        })].join("")
      : `<option value="" disabled selected>Create a collection first</option>`;
    return `<article class="document-row" data-doc="${escapeHtml(doc.id)}">
      <input type="checkbox" class="select-doc" aria-label="Select ${escapeHtml(doc.original_filename)}" data-select="${escapeHtml(doc.id)}" ${selected}>
      <div class="doc-main"><span class="file-icon ${ext}">${ext.toUpperCase()}</span><div class="doc-copy"><div class="doc-title" title="${escapeHtml(doc.original_filename)}">${escapeHtml(doc.original_filename)}</div><div class="doc-meta">${doc.page_count || 0} pages · ${Number(doc.character_count || 0).toLocaleString()} characters · ${doc.collections.length} collection${doc.collections.length === 1 ? "" : "s"}</div></div></div>
      ${status}
      <button class="tiny-button view-button" data-source="${escapeHtml(doc.id)}">View document</button>
      <details class="row-menu"><summary class="menu-trigger" aria-label="More options for ${escapeHtml(doc.original_filename)}" title="More options">•••</summary><div class="row-menu-content">
        <label class="menu-label">Collection<select class="tiny-select" data-add-collection="${escapeHtml(doc.id)}" aria-label="Add document to collection">${collectionOptions}</select></label>
        ${doc.collections.map((collection) => `<button class="menu-action" data-remove-collection="${escapeHtml(doc.id)}" data-collection-id="${escapeHtml(collection.id)}">Remove from ${escapeHtml(collection.name)}</button>`).join("")}
        ${doc.duplicate_of ? `<div class="menu-note">Search uses the linked original.</div>` : `<button class="menu-action" data-index="${escapeHtml(doc.id)}">${doc.indexed ? "Resume indexing" : "Index document"}</button>`}
        ${doc.has_analysis ? `<button class="menu-action" data-view-analysis="${escapeHtml(doc.id)}">View saved analysis</button>` : `<button class="menu-action" data-analyze="${escapeHtml(doc.id)}">Analyze document</button>`}
        <button class="menu-action menu-danger" data-delete="${escapeHtml(doc.id)}">Delete document</button>
      </div></details>
    </article>`;
  }).join("");
  list.querySelectorAll("[data-select]").forEach((input) => input.addEventListener("change", () => {
    if (input.checked) state.selected.add(input.dataset.select); else state.selected.delete(input.dataset.select);
    renderScope(); renderCollections();
  }));
  list.querySelectorAll("[data-add-collection]").forEach((select) => select.addEventListener("change", async () => {
    const collectionId = select.value;
    if (!collectionId) return;
    select.disabled = true;
    try {
      await api(`/api/collections/${collectionId}/documents/${select.dataset.addCollection}`, { method: "PUT", body: JSON.stringify({ included: true }) });
      const collection = state.collections.find((item) => item.id === collectionId);
      notice(`Added to ${collection?.name || "collection"}.`);
      await loadLibrary();
    } catch (error) { notice(error.message, "error"); select.disabled = false; select.value = ""; }
  }));
  list.querySelectorAll("[data-remove-collection]").forEach((button) => button.addEventListener("click", async () => {
    setBusy(button, true, "Removing…");
    try {
      await api(`/api/collections/${button.dataset.collectionId}/documents/${button.dataset.removeCollection}`, { method: "PUT", body: JSON.stringify({ included: false }) });
      notice("Document removed from collection.");
      await loadLibrary();
    } catch (error) { notice(error.message, "error"); setBusy(button, false); }
  }));
  list.querySelectorAll("[data-index]").forEach((button) => button.addEventListener("click", () => indexOne(button.dataset.index, button)));
  list.querySelectorAll("[data-analyze]").forEach((button) => button.addEventListener("click", () => analyzeOne(button.dataset.analyze, button)));
  list.querySelectorAll("[data-source]").forEach((button) => button.addEventListener("click", () => window.open(`/api/documents/${button.dataset.source}/file`, "_blank", "noopener")));
  list.querySelectorAll("[data-view-analysis]").forEach((button) => button.addEventListener("click", () => viewAnalysis(button.dataset.viewAnalysis, button)));
  list.querySelectorAll("[data-delete]").forEach((button) => button.addEventListener("click", () => deleteOne(button.dataset.delete, button)));
}

function renderCollections() {
  const target = $("#collection-list");
  if (!state.collections.length) { target.innerHTML = `<div class="muted" style="font-size:10px;padding:4px 1px 10px">No collections yet.</div>`; return; }
  target.innerHTML = state.collections.map((collection) => {
    const allSelectedInCollection = state.selected.size > 0 && [...state.selected].every((id) => state.documents.find((doc) => doc.id === id)?.collections.some((item) => item.id === collection.id));
    return `<div class="collection-card">
    <button type="button" class="collection-name" data-use-collection="${escapeHtml(collection.id)}"><strong>${escapeHtml(collection.name)}</strong><small>${collection.document_count} document${collection.document_count === 1 ? "" : "s"}</small></button>
    <div class="collection-actions"><button class="tiny-button" data-study-collection="${escapeHtml(collection.id)}">Study</button>${state.selected.size ? ` <button class="tiny-button" title="${allSelectedInCollection ? "Remove selected documents" : "Add selected documents"}" data-toggle-collection="${escapeHtml(collection.id)}" data-included="${!allSelectedInCollection}">${allSelectedInCollection ? "Remove selected" : "+ selected"}</button>` : ""}<button class="tiny-button tiny-danger" data-delete-collection="${escapeHtml(collection.id)}">Delete</button></div>
  </div>`;
  }).join("");
  target.querySelectorAll("[data-use-collection]").forEach((node) => node.addEventListener("click", () => {
    state.activeCollection = node.dataset.useCollection; showView("study"); renderScope();
  }));
  target.querySelectorAll("[data-study-collection]").forEach((button) => button.addEventListener("click", () => {
    state.activeCollection = button.dataset.studyCollection; showView("study"); renderScope();
  }));
  target.querySelectorAll("[data-toggle-collection]").forEach((button) => button.addEventListener("click", () => addSelectedToCollection(button.dataset.toggleCollection, button, button.dataset.included === "true")));
  target.querySelectorAll("[data-delete-collection]").forEach((button) => button.addEventListener("click", () => deleteCollection(button.dataset.deleteCollection, button)));
}

async function deleteCollection(id, button) {
  const collection = state.collections.find((item) => item.id === id);
  if (!collection) return;
  const confirmed = window.confirm(
    `Delete the collection “${collection.name}”?\n\n` +
    `${collection.document_count} document${collection.document_count === 1 ? "" : "s"} will be removed from this collection. The documents and uploaded files will stay in your library.`
  );
  if (!confirmed) return;
  setBusy(button, true, "Deleting…");
  try {
    await api(`/api/collections/${id}`, { method: "DELETE" });
    if (state.activeCollection === id) state.activeCollection = "";
    await loadLibrary();
    notice(`Collection “${collection.name}” deleted. Its documents remain in your library.`);
  } catch (error) { notice(error.message, "error"); setBusy(button, false); }
}

function renderScope() {
  updateTopDelete();
  const select = $("#scope-collection");
  const current = state.activeCollection;
  select.innerHTML = `<option value="">Selected documents / whole library</option>` + state.collections.map((item) => `<option value="${escapeHtml(item.id)}" ${item.id === current ? "selected" : ""}>${escapeHtml(item.name)}</option>`).join("");
  const collection = state.collections.find((item) => item.id === current);
  $("#selected-count").textContent = collection ? `${collection.document_count} in collection` : `${state.selected.size} selected`;
  const names = collection ? [] : state.documents.filter((doc) => state.selected.has(doc.id)).map((doc) => doc.original_filename);
  if (!names.length && !collection) { $("#selected-chips").innerHTML = `<span class="muted">No selection means your whole library.</span>`; return; }
  const chips = names.map((name) => `<span class="selected-chip">${escapeHtml(name)}</span>`);
  if (collection) chips.unshift(`<span class="selected-chip">Collection · ${escapeHtml(collection.name)}</span>`);
  $("#selected-chips").innerHTML = chips.join("");
}

function updateTopDelete() {
  const button = $("#top-delete");
  if (!button) return;
  const count = state.selected.size;
  button.disabled = count === 0;
  button.textContent = count ? `Delete selected (${count})` : "Delete selected";
  const indexButton = $("#index-selected");
  if (indexButton) {
    indexButton.disabled = count === 0;
    indexButton.textContent = count ? `Index selected (${count})` : "Index selected";
  }
}

function renderOrphans(files) {
  const panel = $("#unregistered-panel");
  if (!files.length) { panel.hidden = true; panel.innerHTML = ""; return; }
  panel.hidden = false;
  panel.innerHTML = `<h3>Files to add to your library</h3><p>These uploads are preserved in the project folder but do not yet have a database record.</p>` + files.map((file) => `<div class="orphan-item"><span>${escapeHtml(file.filename)} · ${Math.ceil(file.size / 1024)} KB</span><button class="tiny-button" data-register="${escapeHtml(file.filename)}">Register</button></div>`).join("");
  panel.querySelectorAll("[data-register]").forEach((button) => button.addEventListener("click", async () => {
    setBusy(button, true, "Reading…");
    try { const result = await api("/api/uploads/register", { method: "POST", body: JSON.stringify({ filename: button.dataset.register }) }); notice(result.duplicate_of ? "File registered and linked to the existing duplicate." : "File added to your library."); await loadLibrary(); }
    catch (error) { notice(error.message, "error"); setBusy(button, false); }
  }));
}

async function uploadFiles(files) {
  const progress = $("#upload-progress");
  const valid = [...files].filter((file) => /\.(pdf|docx)$/i.test(file.name));
  if (!valid.length) { notice("Choose PDF or DOCX files.", "error"); return; }
  progress.hidden = false;
  let added = 0; let duplicates = 0; let errors = [];
  for (let index = 0; index < valid.length; index += 1) {
    progress.textContent = `Reading ${index + 1} of ${valid.length}: ${valid[index].name}`;
    const form = new FormData(); form.append("file", valid[index]);
    try { const result = await api("/api/upload-document", { method: "POST", body: form }); if (result.duplicate) duplicates += 1; else added += 1; }
    catch (error) { errors.push(`${valid[index].name}: ${error.message}`); }
  }
  progress.hidden = true; progress.textContent = "";
  await loadLibrary();
  const summary = `${added} added${duplicates ? `, ${duplicates} duplicate${duplicates === 1 ? "" : "s"} skipped` : ""}.`;
  notice(errors.length ? `${summary} ${errors.join(" · ")}` : summary, errors.length ? "warning" : "success");
}

async function trackIndexProgress(id, button, isDone, label) {
  while (!isDone()) {
    await new Promise((resolve) => window.setTimeout(resolve, 700));
    if (isDone()) break;
    try {
      const progress = await api(`/api/documents/${id}/index-progress`);
      if (progress.status === "processing") {
        button.textContent = progress.total_chunks
          ? `${label} · ${progress.completed_chunks}/${progress.total_chunks} embeddings`
          : `${label} · preparing passages…`;
      } else if (progress.status === "not_started") {
        button.textContent = `${label} · preparing passages…`;
      }
    } catch {
      button.textContent = `${label} · working…`;
    }
  }
}

async function indexOne(id, button) {
  setBusy(button, true, "Indexing…");
  let done = false;
  const progressTask = trackIndexProgress(id, button, () => done, "Indexing");
  try {
    const result = await api(`/api/documents/${id}/index`, { method: "POST" });
    notice(result.warning ? `Keyword search is ready. Embeddings could not be added: ${result.warning}` : `${result.chunks_indexed} passages indexed${result.embedded_chunks ? " with local embeddings" : " for keyword search"}.`, result.warning ? "warning" : "success");
    await loadLibrary();
  } catch (error) { notice(error.message, "error"); setBusy(button, false); }
  finally { done = true; await progressTask; }
}

async function indexSelected() {
  const docs = [...state.selected];
  if (!docs.length) { notice("Select one or more documents first.", "warning"); return; }
  const button = $("#index-selected"); setBusy(button, true, `Indexing 0/${docs.length}…`);
  const failures = [];
  for (let index = 0; index < docs.length; index += 1) {
    const label = `Indexing ${index + 1}/${docs.length}`;
    button.textContent = `${label}…`;
    let done = false;
    const progressTask = trackIndexProgress(docs[index], button, () => done, label);
    try { const result = await api(`/api/documents/${docs[index]}/index`, { method: "POST" }); if (result.warning) failures.push(result.warning); }
    catch (error) { failures.push(error.message); }
    finally { done = true; await progressTask; }
  }
  setBusy(button, false); await loadLibrary();
  notice(failures.length ? `Keyword indexing finished; some embeddings need attention: ${failures[0]}` : "Selected documents are ready to study.", failures.length ? "warning" : "success");
}

async function analyzeOne(id, button) {
  setBusy(button, true, "Analyzing…");
  $("#study-output").innerHTML = `<div class="loading-inline">Analyzing document sections with your local model…</div>`;
  showView("study");
  try {
    const result = await api(`/api/documents/${id}/analyze`, { method: "POST" });
    const analysis = result.document_analysis || {};
    $("#study-output").innerHTML = `<article class="answer-card"><h3>${escapeHtml(result.filename || "Document analysis")}</h3><p class="answer-text">${escapeHtml(analysis.summary || "Analysis complete.")}</p><div class="source-list">${(analysis.topics || []).map((topic) => `<span class="source-ref">${escapeHtml(topic)}</span>`).join("")}</div></article>`;
    notice("Document analysis saved.");
  } catch (error) { notice(error.message, "error"); $("#study-output").innerHTML = ""; }
  finally { setBusy(button, false); }
}

async function viewAnalysis(id, button) {
  setBusy(button, true, "Loading…"); showView("study");
  try {
    const result = await api(`/api/documents/${id}/analysis`);
    const analysis = result.analysis || {};
    $("#study-output").innerHTML = `<article class="answer-card"><div class="eyebrow">SAVED DOCUMENT ANALYSIS</div><h3>${escapeHtml(state.documents.find((doc) => doc.id === id)?.original_filename || "Document")}</h3><p class="answer-text">${escapeHtml(analysis.summary || "")}</p><div class="source-list">${(analysis.topics || []).map((topic) => `<span class="source-ref">${escapeHtml(topic)}</span>`).join("")}</div>${(analysis.chapters || []).map((chapter) => `<div class="result-card"><h3>${escapeHtml(chapter.chapter_title)} · pp. ${chapter.start_page}–${chapter.end_page}</h3><p>${escapeHtml(chapter.summary)}</p></div>`).join("")}</article>`;
  } catch (error) { notice(error.message, "error"); }
  finally { setBusy(button, false); }
}

async function deleteOne(id, button) {
  const document = state.documents.find((item) => item.id === id);
  if (!document) return;
  const confirmed = window.confirm(
    `Delete “${document.original_filename}” from your library?\n\n` +
    "This removes its database record, extracted pages, analysis, search index, " +
    `and saved study sets that use it. Uploaded source file: ${document.original_filename}. ` +
    "The file is kept if another library record still uses it. " +
    "A pre-delete database backup containing the old extracted text is kept under data/backups/. " +
    "The app has no undo for this action."
  );
  if (!confirmed) return;
  setBusy(button, true, "Deleting…");
  try {
    const result = await api(`/api/documents/${id}`, {
      method: "DELETE",
      body: JSON.stringify({ confirm: true }),
    });
    state.selected.delete(id);
    await loadLibrary();
    const detail = result.promoted_duplicate_id
      ? " A duplicate record was preserved as the canonical copy."
      : "";
    const warning = result.warning ? ` ${result.warning}` : "";
    notice(`Document deleted. Database backup saved${detail}.${warning}`, result.warning ? "warning" : "success");
  } catch (error) {
    notice(error.message, "error");
    setBusy(button, false);
  }
}

async function deleteSelected() {
  const ids = [...state.selected];
  if (!ids.length) return;
  const names = ids.map((id) => state.documents.find((doc) => doc.id === id)?.original_filename || id);
  const confirmed = window.confirm(
    `Delete ${ids.length} selected document${ids.length === 1 ? "" : "s"}?\n\n` +
    names.map((name) => `• ${name}`).join("\n") + "\n\n" +
    "This removes the uploaded source files listed above (unless another library record uses the same file), along with extracted pages, analyses, indexes, and saved study sets that reference them. " +
    "A pre-delete database backup containing the old extracted text is kept under data/backups/. " +
    "The app has no undo for this action."
  );
  if (!confirmed) return;
  const button = $("#top-delete");
  setBusy(button, true, "Deleting…");
  let deleted = 0;
  const failures = [];
  for (const id of ids) {
    try {
      await api(`/api/documents/${id}`, {
        method: "DELETE",
        body: JSON.stringify({ confirm: true }),
      });
      state.selected.delete(id);
      deleted += 1;
    } catch (error) {
      failures.push(error.message);
    }
  }
  setBusy(button, false);
  await loadLibrary();
  if (failures.length) {
    notice(`${deleted} deleted. ${failures.length} could not be deleted: ${failures[0]}`, "warning");
  } else {
    notice(`${deleted} document${deleted === 1 ? "" : "s"} deleted. A pre-delete database backup is saved in data/backups.`);
  }
}

async function addSelectedToCollection(collectionId, button, included) {
  const ids = [...state.selected]; if (!ids.length) return;
  setBusy(button, true, "…");
  try { for (const id of ids) await api(`/api/collections/${collectionId}/documents/${id}`, { method: "PUT", body: JSON.stringify({ included }) }); notice(`${included ? "Added to" : "Removed from"} collection.`); await loadLibrary(); }
  catch (error) { notice(error.message, "error"); setBusy(button, false); }
}

function requestScope() {
  return { document_ids: state.activeCollection ? null : state.selected.size ? [...state.selected] : null, collection_id: state.activeCollection || null, limit: 8 };
}

function sourcePills(sources = [], showSourceId = true) {
  return sources.map((source) => `<a class="source-ref source-link" href="${sourceFileUrl(source, source.start_page)}" target="_blank" rel="noopener">${showSourceId ? `${escapeHtml(source.source_id)} · ` : ""}${escapeHtml(source.filename)} · ${source.start_page === source.end_page ? `p. ${source.start_page}` : `pp. ${source.start_page}–${source.end_page}`}</a>`).join("");
}

function sourcePillsForItem(item, content = {}, showSourceId = true) {
  const sourceIds = new Set(Array.isArray(item.source_ids) ? item.source_ids : []);
  const sources = (content.sources || []).filter((source) => sourceIds.has(source.source_id));
  return sources.length
    ? `<div class="source-list" aria-label="Sources for this study item">${sourcePills(sources, showSourceId)}</div>`
    : `<div class="muted item-source-missing">No source link was saved for this item.</div>`;
}

function sourceFileUrl(source, page) {
  const url = `/api/documents/${encodeURIComponent(source.document_id)}/file`;
  return String(source.filename || "").toLowerCase().endsWith(".pdf")
    ? `${url}#page=${Math.max(1, Number(page) || 1)}`
    : url;
}

function renderCitedAnswer(answer, sources = []) {
  const sourceMap = new Map(sources.map((source) => [source.source_id, source]));
  const citationPattern = /\[(S\d+)\s*,\s*pp?\.?\s*(\d+)(?:\s*[-–]\s*(\d+))?\]/gi;
  let html = "";
  let cursor = 0;
  for (const match of answer.matchAll(citationPattern)) {
    const source = sourceMap.get(match[1].toUpperCase());
    html += escapeHtml(answer.slice(cursor, match.index));
    if (source) {
      html += `<a class="inline-citation" href="${sourceFileUrl(source, match[2])}" target="_blank" rel="noopener">${escapeHtml(match[0])}</a>`;
    } else {
      html += escapeHtml(match[0]);
    }
    cursor = match.index + match[0].length;
  }
  return html + escapeHtml(answer.slice(cursor));
}

async function doSearch(button) {
  const query = $("#study-query").value.trim(); if (!query) { notice("Type a search phrase first.", "warning"); return; }
  setBusy(button, true, "Searching…"); $("#study-output").innerHTML = `<div class="loading-inline">Finding relevant passages in your selected files…</div>`;
  try {
    const result = await api("/api/search", { method: "POST", body: JSON.stringify({ ...requestScope(), query }) });
    $("#study-output").innerHTML = result.results.length ? `<div class="eyebrow">${result.mode === "hybrid" ? "KEYWORD + SEMANTIC MATCHES" : "KEYWORD MATCHES"}</div>` + result.results.map((item) => `<article class="result-card"><h3>${escapeHtml(item.filename)} · ${item.start_page === item.end_page ? `p. ${item.start_page}` : `pp. ${item.start_page}–${item.end_page}`}</h3><p>${escapeHtml(item.excerpt)}</p></article>`).join("") : `<div class="empty-state">No matching passages. Index the selected documents or try different words.</div>`;
  } catch (error) { $("#study-output").innerHTML = ""; notice(error.message, "error"); }
  finally { setBusy(button, false); }
}

async function doAsk(button) {
  const question = $("#study-query").value.trim(); if (!question) { notice("Type a question first.", "warning"); return; }
  setBusy(button, true, "Thinking…"); $("#study-output").innerHTML = `<div class="loading-inline">Retrieving source passages and asking your local model…</div>`;
  try {
    const result = await api("/api/ask", { method: "POST", body: JSON.stringify({ ...requestScope(), question }) });
    $("#study-output").innerHTML = `<article class="answer-card"><div class="eyebrow">LOCAL AI · ${escapeHtml(result.retrieval_mode.toUpperCase())} RETRIEVAL</div><p class="answer-text">${renderCitedAnswer(result.answer, result.sources)}</p>${result.citation_warning ? `<div class="notice warning">${escapeHtml(result.citation_warning)}</div>` : ""}<div class="source-list">${sourcePills(result.sources)}</div></article>`;
    if (result.embedding_warning) notice(`Answered with keyword retrieval. ${result.embedding_warning}`, "warning");
  } catch (error) { $("#study-output").innerHTML = ""; notice(error.message, "error"); }
  finally { setBusy(button, false); }
}

async function generateMaterial(type, button) {
  const topic = $("#material-topic").value.trim(); setBusy(button, true, "Creating…");
  const labels = { quiz: "quiz questions", flashcards: "flashcards", study_guide: "a study guide", concept_map: "a concept map" };
  $("#study-output").innerHTML = `<div class="loading-inline">Preparing source passages and generating ${labels[type]} locally…</div>`;
  try {
    const result = await api("/api/study-materials/generate", { method: "POST", body: JSON.stringify({ ...requestScope(), material_type: type, topic, count: type === "quiz" ? Number($("#quiz-count").value) : 10, quiz_format: $("#quiz-format").value, difficulty: $("#quiz-difficulty").value }) });
    const count = type === "study_guide" ? `${result.material.content.sections.length} sections` : type === "concept_map" ? `${result.material.content.nodes.length} concepts` : `${result.material.content.items.length} ${type === "quiz" ? "questions" : "flashcards"}`;
    $("#study-output").innerHTML = `<article class="answer-card"><h3>${escapeHtml(result.material.title)}</h3><p>${count} saved to Study materials.</p><div class="source-list">${sourcePills(result.material.content.sources)}</div><button class="button button-light" id="open-generated">Open set</button></article>`;
    $("#open-generated").addEventListener("click", () => { showView("materials"); loadMaterials(result.material.id); });
    notice("Study set saved.");
  } catch (error) { $("#study-output").innerHTML = ""; notice(error.message, "error"); }
  finally { setBusy(button, false); }
}

async function loadMaterials(selectId = null) {
  try {
    const data = await api("/api/study-materials"); state.materials = data.materials || [];
    const list = $("#materials-list");
    list.innerHTML = state.materials.length ? state.materials.map((item) => `<div class="material-row"><button type="button" class="material-item ${item.id === selectId ? "selected" : ""}" data-material="${escapeHtml(item.id)}"><strong>${escapeHtml(item.title)}</strong><small>${item.material_type === "quiz" ? "Quiz" : item.material_type === "study_guide" ? "Study guide" : item.material_type === "concept_map" ? "Concept map" : "Flashcards"} · ${new Date(item.created_at).toLocaleDateString()}</small></button><button type="button" class="material-delete" data-delete-material="${escapeHtml(item.id)}" aria-label="Delete ${escapeHtml(item.title)}" title="Delete saved set">×</button></div>`).join("") : `<div class="empty-state">No saved sets yet. Generate a study guide, concept map, flashcards, or a quiz from your library.</div>`;
    list.querySelectorAll("[data-material]").forEach((node) => node.addEventListener("click", () => renderMaterial(state.materials.find((item) => item.id === node.dataset.material))));
    list.querySelectorAll("[data-delete-material]").forEach((button) => button.addEventListener("click", () => deleteStudyMaterial(button.dataset.deleteMaterial, button)));
    const selected = state.materials.find((item) => item.id === selectId) || state.materials[0];
    if (selected) renderMaterial(selected);
    else { state.activeMaterial = null; $("#material-detail").innerHTML = `<div class="empty-state">No saved sets yet. Generate a study guide, concept map, flashcards, or a quiz from your library.</div>`; }
  } catch (error) { notice(error.message, "error"); }
}

async function deleteStudyMaterial(id, button) {
  const material = state.materials.find((item) => item.id === id);
  if (!material) return;
  const kind = material.material_type === "quiz" ? "quiz" : material.material_type === "study_guide" ? "study guide" : material.material_type === "concept_map" ? "concept map" : "flashcard set";
  const confirmed = window.confirm(`Delete the saved ${kind} “${material.title}”? Your documents will remain in the library.`);
  if (!confirmed) return;
  setBusy(button, true, "…");
  try {
    await api(`/api/study-materials/${id}`, { method: "DELETE" });
    const nextId = state.activeMaterial?.id === id ? null : state.activeMaterial?.id;
    await loadMaterials(nextId);
    notice("Saved study set deleted. Your documents remain in the library.");
  } catch (error) { notice(error.message, "error"); setBusy(button, false); }
}

function renderMaterial(material, reviewItems = null) {
  if (!material) return;
  state.activeMaterial = material;
  const detail = $("#material-detail");
  $("#materials-list").querySelectorAll(".material-item").forEach((node) => node.classList.toggle("selected", node.dataset.material === material.id));
  const items = material.content.items || [];
  if (material.material_type === "study_guide") {
    const content = material.content;
    detail.innerHTML = `<h2>${escapeHtml(material.title)}</h2><button type="button" class="tiny-button read-aloud" id="read-guide">Read guide aloud</button><p class="guide-overview">${escapeHtml(content.overview || "")}</p><h3>Key ideas</h3>${(content.sections || []).map((section) => `<article class="guide-section"><h4>${escapeHtml(section.heading)}</h4><p>${escapeHtml(section.summary)}</p>${sourcePillsForItem(section, content)}</article>`).join("")}<h3>Key terms</h3>${(content.key_terms || []).map((term) => `<article class="guide-section"><strong>${escapeHtml(term.term)}</strong><p>${escapeHtml(term.definition)}</p>${sourcePillsForItem(term, content)}</article>`).join("")}<h3>Check your understanding</h3>${(content.review_questions || []).map((item, index) => `<details class="guide-question"><summary>${index + 1}. ${escapeHtml(item.question)}</summary><p>${escapeHtml(item.answer)}</p>${sourcePillsForItem(item, content)}</details>`).join("")}`;
    $("#read-guide").addEventListener("click", (event) => speakText([content.overview, ...(content.sections || []).flatMap((section) => [section.heading, section.summary]), ...(content.key_terms || []).flatMap((term) => [term.term, term.definition])].filter(Boolean).join(". "), event.currentTarget));
    return;
  }
  if (material.material_type === "concept_map") {
    const content = material.content;
    const nodes = content.nodes || [];
    const nodeList = new Map(nodes.map((node) => [node.id, node]));
    detail.innerHTML = `<h2>${escapeHtml(material.title)}</h2><p class="guide-overview">Explore each concept. Its explanation links back to the source pages.</p><div class="concept-map-layout"><div class="concept-node-list" aria-label="Concepts">${nodes.map((node) => `<button type="button" class="concept-node" data-concept="${escapeHtml(node.id)}">${escapeHtml(node.label)}<span>View concept</span></button>`).join("")}</div><section class="concept-detail" id="concept-detail" aria-live="polite"><p class="muted">Choose a concept to see its explanation and sources.</p></section></div><h3>How ideas connect</h3><div class="concept-relations">${(content.edges || []).map((edge) => { const from = nodeList.get(edge.from); const to = nodeList.get(edge.to); return from && to ? `<div class="concept-relation"><strong>${escapeHtml(from.label)}</strong><span>${escapeHtml(edge.relationship)} →</span><strong>${escapeHtml(to.label)}</strong></div>` : ""; }).join("")}</div>`;
    const showConcept = (id) => {
      const node = nodes.find((item) => item.id === id);
      if (!node) return;
      detail.querySelectorAll("[data-concept]").forEach((button) => button.classList.toggle("selected", button.dataset.concept === id));
      $("#concept-detail").innerHTML = `<h3>${escapeHtml(node.label)}</h3><p>${escapeHtml(node.summary)}</p>${sourcePillsForItem(node, content)}`;
    };
    detail.querySelectorAll("[data-concept]").forEach((button) => button.addEventListener("click", () => showConcept(button.dataset.concept)));
    if (nodes.length) showConcept(nodes[0].id);
    return;
  }
  if (material.material_type === "flashcards") {
    let index = 0; let reveal = false;
    const draw = () => {
      const item = items[index];
      detail.innerHTML = `<h2>${escapeHtml(material.title)}</h2><div class="material-subtitle">Flashcard ${index + 1} of ${items.length} · activate the card to ${reveal ? "show question" : "reveal answer"}</div><div class="flashcard" id="flashcard" role="button" tabindex="0" aria-label="${reveal ? "Show question" : "Reveal answer"}"><small>${reveal ? "ANSWER" : "QUESTION"}</small><p>${escapeHtml(reveal ? item.back : item.front)}</p></div>${sourcePillsForItem(item, material.content)}<div class="flash-controls"><button class="tiny-button" id="card-prev">← Previous</button><button type="button" class="tiny-button read-aloud" id="read-card">Read aloud</button><span>${index + 1} / ${items.length}</span><button class="tiny-button" id="card-next">Next →</button></div>`;
      $("#read-card").addEventListener("click", (event) => { event.stopPropagation(); speakText(reveal ? item.back : item.front, event.currentTarget); });
      $("#flashcard").addEventListener("click", () => { reveal = !reveal; draw(); });
      $("#flashcard").addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); reveal = !reveal; draw(); } });
      $("#card-prev").addEventListener("click", () => { index = (index - 1 + items.length) % items.length; reveal = false; draw(); });
      $("#card-next").addEventListener("click", () => { index = (index + 1) % items.length; reveal = false; draw(); });
    };
    draw(); return;
  }
  const quizItems = reviewItems || items;
  const isReview = Boolean(reviewItems);
  let answered = 0;
  let correct = 0;
  const missedItems = [];
  const title = isReview ? `Review missed answers · ${material.title}` : material.title;
  detail.innerHTML = `<h2>${escapeHtml(title)}</h2><div class="material-subtitle">${quizItems.length} ${isReview ? "missed questions" : "questions"} · answer each question to see its explanation</div><div class="quiz-progress" id="quiz-progress" role="status" aria-live="polite">0 of ${quizItems.length} answered · 0 correct</div>` + quizItems.map((item, index) => {
    const kind = item.type || "multiple_choice";
    const cleanQuestion = cleanQuizText(item.question);
    const question = kind === "fill_blank" ? escapeHtml(cleanQuestion).replace(/____/g, "<span class=\"blank-marker\">your answer</span>") : escapeHtml(cleanQuestion);
    const answerControls = kind === "multiple_choice"
      ? item.choices.map((choice, choiceIndex) => `<button class="quiz-choice" data-q="${index}" data-choice="${choiceIndex}">${escapeHtml(cleanQuizText(choice))}</button>`).join("")
      : kind === "multi_select"
        ? `${item.choices.map((choice, choiceIndex) => `<button type="button" class="quiz-choice" data-multi-choice="${index}" data-choice-index="${choiceIndex}" aria-pressed="false">${escapeHtml(cleanQuizText(choice))}</button>`).join("")}<button type="button" class="tiny-button" data-submit-multi="${index}">Check selection</button>`
      : kind === "fill_blank"
        ? `<label class="sr-only" for="fill-answer-${index}">Your answer</label><input class="quiz-text-answer" id="fill-answer-${index}" data-fill-answer="${index}" autocomplete="off"><button type="button" class="tiny-button" data-check-fill="${index}">Check answer</button><div data-fill-result="${index}" role="status" aria-live="polite"></div>`
        : `<label class="sr-only" for="short-answer-${index}">Your answer</label><textarea class="quiz-text-answer" id="short-answer-${index}" data-short-answer="${index}" rows="2" placeholder="Write your answer before revealing the answer key"></textarea><button type="button" class="tiny-button" data-reveal-answer="${index}">Show answer key</button><div data-short-result="${index}" role="group" aria-label="Self-check your answer"></div>`;
    return `<div class="quiz-item"><strong>${index + 1}. ${question}</strong>${item.hint ? `<button type="button" class="quiz-hint-toggle" data-hint-toggle="${index}" aria-expanded="false">Show hint</button><p class="quiz-hint" data-hint="${index}" hidden>${escapeHtml(cleanQuizText(item.hint))}</p>` : ""}${sourcePillsForItem(item, material.content, false)}${answerControls}<div class="muted" data-explanation="${index}" role="status" aria-live="polite" style="font-size:10px;margin-top:7px"></div></div>`;
  }).join("");
  detail.querySelectorAll("[data-hint-toggle]").forEach((button) => button.addEventListener("click", () => {
    const hint = detail.querySelector(`[data-hint="${button.dataset.hintToggle}"]`);
    const show = hint.hidden;
    hint.hidden = !show;
    button.textContent = show ? "Hide hint" : "Show hint";
    button.setAttribute("aria-expanded", String(show));
  }));
  const finishIfDone = () => {
    if (answered !== quizItems.length) return;
    const result = document.createElement("p");
    result.className = "quiz-result";
    result.setAttribute("role", "status");
    result.textContent = `You got ${correct} of ${quizItems.length} correct.`;
    detail.append(result);
    if (!isReview && missedItems.length) {
      const review = document.createElement("button");
      review.type = "button"; review.className = "button button-primary quiz-retry";
      review.textContent = `Review ${missedItems.length} missed ${missedItems.length === 1 ? "question" : "questions"}`;
      review.addEventListener("click", () => renderMaterial(material, missedItems));
      detail.append(review);
    }
    const retry = document.createElement("button");
    retry.type = "button"; retry.className = "button button-light quiz-retry";
    retry.textContent = isReview ? "Try missed questions again" : "Try this quiz again";
    retry.addEventListener("click", () => renderMaterial(material, isReview ? quizItems : null));
    detail.append(retry);
    if (isReview) {
      const fullQuiz = document.createElement("button");
      fullQuiz.type = "button"; fullQuiz.className = "button button-light quiz-retry";
      fullQuiz.textContent = "Back to full quiz";
      fullQuiz.addEventListener("click", () => renderMaterial(material));
      detail.append(fullQuiz);
    }
  };
  const recordAnswer = (index, isCorrect) => {
    const item = quizItems[index];
    answered += 1;
    if (isCorrect) correct += 1; else missedItems.push(item);
    $("#quiz-progress").textContent = `${answered} of ${quizItems.length} answered · ${correct} correct`;
    const explanation = detail.querySelector(`[data-explanation="${index}"]`);
    explanation.textContent = cleanQuizText(item.explanation) || (isCorrect ? "Correct." : "Review the source passage.");
    finishIfDone();
  };
  detail.querySelectorAll("[data-choice]").forEach((button) => button.addEventListener("click", () => {
    const item = quizItems[Number(button.dataset.q)]; const selected = Number(button.dataset.choice);
    detail.querySelectorAll(`[data-q="${button.dataset.q}"]`).forEach((choice) => {
      choice.disabled = true;
      choice.setAttribute("aria-pressed", String(choice === button));
      if (Number(choice.dataset.choice) === item.correct_index) choice.classList.add("correct");
      else if (choice === button) choice.classList.add("incorrect");
    });
    recordAnswer(Number(button.dataset.q), selected === item.correct_index);
  }));
  detail.querySelectorAll("[data-multi-choice]").forEach((button) => button.addEventListener("click", () => {
    const pressed = button.getAttribute("aria-pressed") === "true";
    button.setAttribute("aria-pressed", String(!pressed));
    button.classList.toggle("selected", !pressed);
  }));
  detail.querySelectorAll("[data-submit-multi]").forEach((button) => button.addEventListener("click", () => {
    const index = Number(button.dataset.submitMulti); const item = quizItems[index];
    const choices = [...detail.querySelectorAll(`[data-multi-choice="${index}"]`)];
    const selected = choices.filter((choice) => choice.getAttribute("aria-pressed") === "true").map((choice) => Number(choice.dataset.choiceIndex));
    if (!selected.length) { choices[0]?.focus(); return; }
    const expected = item.correct_indices || [];
    const isCorrect = selected.length === expected.length && selected.every((value) => expected.includes(value));
    choices.forEach((choice) => {
      const value = Number(choice.dataset.choiceIndex);
      choice.disabled = true;
      if (expected.includes(value)) choice.classList.add("correct");
      else if (selected.includes(value)) choice.classList.add("incorrect");
    });
    button.disabled = true;
    recordAnswer(index, isCorrect);
  }));
  const normalizeAnswer = (value) => String(value || "").toLocaleLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim().replace(/\s+/g, " ");
  detail.querySelectorAll("[data-check-fill]").forEach((button) => button.addEventListener("click", () => {
    const index = Number(button.dataset.checkFill); const item = quizItems[index];
    const field = detail.querySelector(`[data-fill-answer="${index}"]`);
    if (!field.value.trim()) { field.focus(); return; }
    const isCorrect = normalizeAnswer(field.value) === normalizeAnswer(item.answer);
    field.disabled = true; button.disabled = true;
    detail.querySelector(`[data-fill-result="${index}"]`).textContent = `${isCorrect ? "Correct." : `Answer: ${cleanQuizText(item.answer)}.`} ${cleanQuizText(item.explanation)}`;
    recordAnswer(index, isCorrect);
  }));
  detail.querySelectorAll("[data-reveal-answer]").forEach((button) => button.addEventListener("click", () => {
    const index = Number(button.dataset.revealAnswer); const item = quizItems[index];
    const field = detail.querySelector(`[data-short-answer="${index}"]`);
    if (!field.value.trim()) { field.focus(); return; }
    button.disabled = true; field.disabled = true;
    const result = detail.querySelector(`[data-short-result="${index}"]`);
    result.innerHTML = `<p>Answer key: ${escapeHtml(cleanQuizText(item.answer))}. Compare it with what you wrote.</p><button type="button" class="tiny-button" data-self-grade="${index}" data-correct="true">I got it</button> <button type="button" class="tiny-button" data-self-grade="${index}" data-correct="false">I need to review</button>`;
    result.querySelectorAll("[data-self-grade]").forEach((grade) => grade.addEventListener("click", () => {
      result.querySelectorAll("[data-self-grade]").forEach((choice) => { choice.disabled = true; });
      recordAnswer(index, grade.dataset.correct === "true");
    }));
  }));
}

function showView(name) {
  document.querySelectorAll(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${name}`));
  document.querySelectorAll(".nav-item").forEach((button) => {
    const active = button.dataset.view === name;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  const titles = { library: "Your library", study: "Ask your library", materials: "Study materials" };
  $("#page-title").textContent = titles[name] || "Study Desk";
  if (name === "materials") loadMaterials();
}

async function checkLocalAI() {
  try {
    const status = await api("/api/ollama-status");
    const dot = $("#ollama-dot");
    dot.classList.toggle("online", status.connected && status.model_available);
    dot.classList.toggle("warning", !status.connected || !status.model_available || !status.embedding_model_available);
    if (!status.connected) {
      $("#ollama-label").textContent = "Ollama offline";
      $("#ollama-detail").textContent = "Start Ollama to use local AI. Indexed keyword search remains available.";
    } else if (!status.model_available) {
      $("#ollama-label").textContent = "Qwen model unavailable";
      $("#ollama-detail").textContent = `Install ${status.model} in Ollama to ask questions or generate study materials.`;
    } else if (!status.embedding_model_available) {
      $("#ollama-label").textContent = "Qwen ready · embeddings unavailable";
      $("#ollama-detail").textContent = `Keyword search is available. Install ${status.embedding_model} for semantic search.`;
    } else {
      $("#ollama-label").textContent = "Local AI ready";
      $("#ollama-detail").textContent = `${status.model} · semantic search ready`;
    }
  } catch {
    $("#ollama-dot").classList.remove("online");
    $("#ollama-dot").classList.add("warning");
    $("#ollama-label").textContent = "Local AI status unavailable";
    $("#ollama-detail").textContent = "Could not check Ollama. Try again shortly.";
  }
}

async function downloadLibraryBackup(button) {
  setBusy(button, true, "Preparing…");
  try {
    const result = await api("/api/library/backup", { method: "POST" });
    const link = document.createElement("a");
    link.href = result.download_url; link.download = ""; document.body.append(link); link.click(); link.remove();
    notice("Backup downloaded with your database and uploaded documents.");
  } catch (error) { notice(error.message, "error"); }
  finally { setBusy(button, false); }
}

async function restoreLibraryBackup(file) {
  if (!file) return;
  const confirmed = window.confirm(
    `Restore “${file.name}”?\n\n` +
    "This replaces the current library database and uploaded documents with the backup. " +
    "The app first creates a safety backup of your current library."
  );
  if (!confirmed) { $("#restore-file").value = ""; return; }
  const button = $("#restore-choose");
  setBusy(button, true, "Restoring…");
  const form = new FormData(); form.append("file", file);
  try {
    const result = await api("/api/library/restore", { method: "POST", body: form });
    state.selected.clear(); state.activeCollection = ""; state.materials = []; state.activeMaterial = null;
    await loadLibrary();
    showView("library");
    notice(`Restored ${result.restored_documents} documents and ${result.restored_uploads} uploaded files. A safety backup of the previous library is in data/backups.`);
  } catch (error) { notice(error.message, "error"); }
  finally { setBusy(button, false); $("#restore-file").value = ""; }
}

document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
$("#top-upload").addEventListener("click", () => $("#file-input").click());
$("#top-delete").addEventListener("click", deleteSelected);
$("#hero-ask").addEventListener("click", () => showView("study"));
$("#hero-add").addEventListener("click", () => $("#file-input").click());
$("#choose-files").addEventListener("click", (event) => { event.stopPropagation(); $("#file-input").click(); });
$("#file-input").addEventListener("change", async (event) => { await uploadFiles(event.target.files); event.target.value = ""; });
$("#drop-zone").addEventListener("click", (event) => { if (!event.target.closest("button")) $("#file-input").click(); });
$("#drop-zone").addEventListener("dragover", (event) => { event.preventDefault(); $("#drop-zone").classList.add("dragging"); });
$("#drop-zone").addEventListener("dragleave", () => $("#drop-zone").classList.remove("dragging"));
$("#drop-zone").addEventListener("drop", async (event) => { event.preventDefault(); $("#drop-zone").classList.remove("dragging"); await uploadFiles(event.dataTransfer.files); });
$("#library-filter").addEventListener("input", renderDocuments);
$("#index-selected").addEventListener("click", indexSelected);
$("#add-collection").addEventListener("click", async () => { const name = window.prompt("Collection name"); if (!name?.trim()) return; const description = window.prompt("Short description (optional)") || ""; try { await api("/api/collections", { method: "POST", body: JSON.stringify({ name: name.trim(), description }) }); notice("Collection created."); await loadLibrary(); } catch (error) { notice(error.message, "error"); } });
$("#backup-download").addEventListener("click", (event) => downloadLibraryBackup(event.currentTarget));
$("#restore-choose").addEventListener("click", () => $("#restore-file").click());
$("#restore-file").addEventListener("change", (event) => restoreLibraryBackup(event.target.files[0]));
$("#scope-collection").addEventListener("change", (event) => { state.activeCollection = event.target.value; renderScope(); });
$("#ask-button").addEventListener("click", (event) => doAsk(event.currentTarget));
$("#search-button").addEventListener("click", (event) => doSearch(event.currentTarget));
$("#flashcards-button").addEventListener("click", (event) => generateMaterial("flashcards", event.currentTarget));
$("#quiz-button").addEventListener("click", (event) => generateMaterial("quiz", event.currentTarget));
$("#guide-button").addEventListener("click", (event) => generateMaterial("study_guide", event.currentTarget));
$("#concept-map-button").addEventListener("click", (event) => generateMaterial("concept_map", event.currentTarget));
$("#study-query").addEventListener("keydown", (event) => { if ((event.ctrlKey || event.metaKey) && event.key === "Enter") doAsk($("#ask-button")); });

loadLibrary().catch((error) => notice(error.message, "error"));
checkLocalAI();
window.setInterval(checkLocalAI, 30000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) checkLocalAI(); });
