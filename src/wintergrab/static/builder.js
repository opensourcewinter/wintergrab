// wintergrab build: pick the parts of a page, get an extraction schema.
// The page is shown in a sandboxed frame without scripts; this script reaches into it (same origin)
// to outline what the pointer is on and to catch clicks. Every change goes to the server as JSON,
// with the token only this page knows. Nothing from the page is ever inserted as HTML.
"use strict";

(() => {
  const token = document.querySelector('meta[name="wg-token"]').content;
  const $ = (id) => document.getElementById(id);
  const TYPES = ["string", "text", "number", "integer", "money", "currency", "url", "date", "datetime",
    "rating", "availability", "email", "phone", "address", "boolean", "quantity", "duration"];
  const NAME = /^[A-Za-z_][A-Za-z0-9_.-]*$/;

  let spec = { name: "record", fields: {} };
  let mode = "field";
  let proposal = null;
  let cards = [];
  let doc = null;
  let editingJson = false;

  // -- talking to the builder ------------------------------------------------------------------ //
  async function api(path, body) {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Wintergrab-Token": token },
      body: JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }

  function say(text, kind) {
    const box = $("message");
    box.textContent = text || "";
    box.className = kind || "";
  }

  function element(tag, text, attrs) {
    const el = document.createElement(tag);
    if (text !== undefined && text !== null) el.textContent = String(text);
    for (const [name, value] of Object.entries(attrs || {})) el.setAttribute(name, value);
    return el;
  }

  // -- the page ---------------------------------------------------------------------------------- //
  const frame = $("page");
  frame.addEventListener("load", () => {
    doc = frame.contentDocument;
    if (!doc) {
      say("The page cannot be shown here.", "error");
      return;
    }
    const style = doc.createElement("style");
    style.textContent =
      "[data-wg-hover]{outline:2px solid #2563eb !important;outline-offset:1px;cursor:crosshair}" +
      "[data-wg-card]{outline:2px dashed #16a34a !important;outline-offset:2px}" +
      "[data-wg-picked]{outline:3px solid #dc2626 !important;outline-offset:1px}";
    (doc.head || doc.documentElement).appendChild(style);
    doc.addEventListener("mouseover", (event) => outline(event.target, true), true);
    doc.addEventListener("mouseout", (event) => outline(event.target, false), true);
    doc.addEventListener("click", pick, true);
    doc.addEventListener("submit", (event) => event.preventDefault(), true);
    mark("data-wg-card", cards);
  });

  function numbered(el) {
    while (el && el.nodeType === 1 && !el.hasAttribute("data-wg")) el = el.parentElement;
    return el && el.nodeType === 1 ? el : null;
  }

  function outline(target, on) {
    const el = numbered(target);
    if (!el) return;
    if (on) el.setAttribute("data-wg-hover", "");
    else el.removeAttribute("data-wg-hover");
  }

  function mark(attribute, ids) {
    if (!doc) return;
    doc.querySelectorAll(`[${attribute}]`).forEach((el) => el.removeAttribute(attribute));
    for (const id of ids || []) {
      const el = doc.querySelector(`[data-wg="${Number(id)}"]`);
      if (el) el.setAttribute(attribute, "");
    }
  }

  async function pick(event) {
    event.preventDefault();
    event.stopPropagation();
    const el = numbered(event.target);
    if (!el) return;
    const id = Number(el.getAttribute("data-wg"));
    mark("data-wg-picked", [id]);
    try {
      if (mode === "field") {
        propose(await api("/api/field", { id, container: spec.container || null }));
      } else if (mode === "card") {
        const card = await api("/api/card", { id });
        spec.container = card.container;
        cards = card.ids;
        mark("data-wg-card", cards);
        showSuggested(card.suggested);
        say(`${card.count} cards: fields you pick in one are read in each.`, "ok");
        setMode("field");
      } else if (mode === "table") {
        applyTable(await api("/api/table", { id }));
      } else if (mode === "next") {
        const next = await api("/api/next", { id });
        spec.next_page = next.selector;
        const guessed = next.detected && next.detected !== next.url ? ` (wintergrab alone would follow ${next.detected})` : "";
        say(`Next page: ${next.url}${guessed}`, "ok");
        setMode("field");
      }
      render();
    } catch (error) {
      say(error.message, "error");
    }
  }

  // -- proposals ------------------------------------------------------------------------------- //
  function uniqueName(name) {
    let base = (name || "field").replace(/[^A-Za-z0-9_.-]/g, "_");
    if (!/^[A-Za-z_]/.test(base)) base = `field_${base}`;
    let candidate = base;
    for (let n = 2; spec.fields[candidate]; n++) candidate = `${base}_${n}`;
    return candidate;
  }

  function typeOptions(select, chosen) {
    select.replaceChildren();
    const known = TYPES.includes(chosen) ? TYPES : [chosen, ...TYPES];
    for (const type of known) select.append(element("option", type, type === chosen ? { value: type, selected: "" } : { value: type }));
  }

  function propose(found) {
    if (!found.candidates.length) {
      say("No selector finds that element on its own: click the element around it.", "error");
      return;
    }
    proposal = found;
    $("p-name").value = uniqueName(found.name);
    typeOptions($("p-type"), found.type);
    $("p-about").textContent = `<${found.tag}> ${found.text || ""}`.slice(0, 160) + (spec.container ? " (in a card)" : "");
    const list = $("p-candidates");
    list.replaceChildren();
    found.candidates.forEach((candidate, index) => {
      const input = element("input", null, { type: "radio", name: "candidate", value: String(index) });
      if (index === 0) input.checked = true;
      const label = element("label");
      const text = element("span");
      text.append(element("code", candidate.selector));
      text.append(document.createTextNode(` ${candidate.matches} match${candidate.matches === 1 ? "" : "es"}`));
      const values = (candidate.values || []).filter((v) => v !== null && v !== "").slice(0, 4);
      text.append(element("span", values.length ? values.map((v) => JSON.stringify(v)).join(", ") : "reads nothing", { class: "values" }));
      label.append(input, text);
      list.appendChild(element("li")).appendChild(label);
    });
    $("proposal").hidden = false;
    $("proposal").scrollIntoView({ block: "nearest" });
    $("p-name").focus();
    say("");
  }

  $("p-add").addEventListener("click", () => {
    if (!proposal) return;
    const chosen = document.querySelector('input[name="candidate"]:checked');
    const candidate = proposal.candidates[Number(chosen ? chosen.value : 0)];
    const name = $("p-name").value.trim();
    if (!NAME.test(name)) {
      say("A field's name starts with a letter or _ and has letters, digits, _ . - only.", "error");
      return;
    }
    const existing = spec.fields[name] || {};
    spec.fields[name] = { ...existing, type: $("p-type").value, selectors: [candidate.selector] };
    closeProposal();
    render();
    say(`Added ${name}.`, "ok");
  });

  $("p-cancel").addEventListener("click", closeProposal);

  function closeProposal() {
    proposal = null;
    $("proposal").hidden = true;
    mark("data-wg-picked", []);
  }

  function showSuggested(suggested) {
    const box = $("suggested");
    box.replaceChildren();
    if (!suggested || !suggested.length) return;
    box.append(element("p", "Found in the cards (click to add):", { class: "muted" }));
    for (const field of suggested) {
      const button = element("button", `+ ${field.name}`, { title: field.selector, type: "button" });
      button.addEventListener("click", () => {
        spec.fields[uniqueName(field.name)] = { type: field.type, selectors: [field.selector] };
        button.remove();
        render();
      });
      box.append(button);
    }
  }

  function applyTable(table) {
    if (table.kind === "records") {
      spec.container = table.container;
      cards = table.ids;
      mark("data-wg-card", cards);
    }
    for (const field of table.fields) {
      spec.fields[uniqueName(field.name)] = { type: field.type, selectors: [field.selector] };
    }
    const what = table.kind === "records" ? `${table.count} rows, a field per column` : "a field per row";
    say(`Table: ${what}. Rename or remove what you do not want.`, "ok");
    setMode("field");
  }

  // -- the specification --------------------------------------------------------------------- //
  function render() {
    $("name").value = spec.name || "record";
    $("container").value = spec.container || "";
    $("card-count").textContent = spec.container ? `(${cards.length || "?"} on this page)` : "(one record per page)";
    $("next").value = spec.next_page || "";
    const body = $("fields");
    body.replaceChildren();
    const names = Object.keys(spec.fields || {});
    $("no-fields").hidden = names.length > 0;
    for (const name of names) {
      const field = spec.fields[name];
      const row = element("tr");
      const nameInput = element("input", null, { "aria-label": "name", spellcheck: "false" });
      nameInput.value = name;
      nameInput.addEventListener("change", () => rename(name, nameInput.value.trim()));
      const type = element("select", null, { "aria-label": "type" });
      typeOptions(type, field.type || "string");
      type.addEventListener("change", () => { field.type = type.value; showJson(); });
      const selector = element("input", null, { "aria-label": "selector", spellcheck: "false" });
      selector.value = (field.selectors || []).join(" | ");
      selector.addEventListener("change", () => {
        const value = selector.value.trim();
        if (value) field.selectors = [value];
        else delete field.selectors;
        showJson();
      });
      const remove = element("button", "×", { title: `Remove ${name}`, type: "button" });
      remove.addEventListener("click", () => { delete spec.fields[name]; render(); });
      for (const cell of [nameInput, type, selector, remove]) row.appendChild(element("td")).appendChild(cell);
      body.append(row);
    }
    showJson();
  }

  function rename(from, to) {
    if (to === from) return;
    if (!NAME.test(to) || spec.fields[to]) {
      say(`Cannot rename ${from} to ${to}: not a name, or taken.`, "error");
      render();
      return;
    }
    const renamed = {};
    for (const [name, field] of Object.entries(spec.fields)) renamed[name === from ? to : name] = field;
    spec.fields = renamed;
    render();
  }

  function showJson() {
    if (!editingJson) $("json").value = JSON.stringify(spec, null, 2);
  }

  $("json").addEventListener("input", () => { editingJson = true; });
  $("apply").addEventListener("click", () => {
    try {
      const parsed = JSON.parse($("json").value);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed) || typeof parsed.fields !== "object") {
        throw new Error("the specification is an object with \"fields\"");
      }
      spec = parsed;
      editingJson = false;
      render();
      say("Applied.", "ok");
    } catch (error) {
      say(`Not applied: ${error.message}`, "error");
    }
  });

  $("name").addEventListener("change", () => { spec.name = $("name").value.trim() || "record"; showJson(); });
  $("container").addEventListener("change", () => {
    const value = $("container").value.trim();
    if (value) spec.container = value;
    else delete spec.container;
    cards = [];
    mark("data-wg-card", []);
    render();
  });
  $("container-clear").addEventListener("click", () => {
    delete spec.container;
    cards = [];
    mark("data-wg-card", []);
    $("suggested").replaceChildren();
    render();
  });
  $("next").addEventListener("change", () => {
    const value = $("next").value.trim();
    if (value) spec.next_page = value;
    else delete spec.next_page;
    showJson();
  });
  $("next-clear").addEventListener("click", () => { delete spec.next_page; render(); });

  // -- testing and saving -------------------------------------------------------------------- //
  function current() {
    if (editingJson) throw new Error("apply the JSON first (or undo your edit)");
    return spec;
  }

  $("test").addEventListener("click", async () => {
    try {
      const result = await api("/api/test", { spec: current() });
      showResults(result);
      say(`${result.records} record(s) read.`, "ok");
    } catch (error) {
      say(error.message, "error");
    }
  });

  $("save").addEventListener("click", async () => {
    try {
      const saved = await api("/api/save", { spec: current() });
      say(`Saved to ${saved.path}. Use it: ${saved.command}`, "ok");
    } catch (error) {
      say(error.message, "error");
    }
  });

  function showResults(result) {
    const names = Object.keys(result.filled);
    const table = $("records");
    table.replaceChildren();
    const head = element("tr");
    for (const name of names) head.append(element("th", `${name} (${result.filled[name]}/${result.records})`));
    table.appendChild(element("thead")).appendChild(head);
    const body = element("tbody");
    for (const row of result.rows) {
      const tr = element("tr");
      for (const name of names) {
        const field = row[name] || {};
        const empty = field.value === null || field.value === undefined || field.value === "";
        const text = empty ? "-" : typeof field.value === "string" ? field.value : JSON.stringify(field.value);
        const title = empty ? "not found" : `${field.method || ""} ${Math.round((field.confidence || 0) * 100)}%`;
        tr.append(element("td", text, { title, class: empty ? "missing" : "" }));
      }
      body.append(tr);
    }
    table.append(body);
    $("summary").textContent = `${result.records} record(s)` + (result.next ? `; next page: ${result.next}` : "");
    $("results").hidden = false;
  }

  // -- modes ----------------------------------------------------------------------------------- //
  function setMode(value) {
    mode = value;
    const radio = document.querySelector(`input[name="mode"][value="${value}"]`);
    if (radio) radio.checked = true;
  }

  document.querySelectorAll('input[name="mode"]').forEach((radio) => {
    radio.addEventListener("change", () => { mode = radio.value; closeProposal(); });
  });

  // -- start ------------------------------------------------------------------------------------ //
  fetch("/api/state").then((r) => r.json()).then((state) => {
    $("url").textContent = state.url;
    $("output").textContent = state.output;
    spec = state.spec;
    if (!spec.fields) spec.fields = {};
    render();
  }).catch((error) => say(`Cannot reach the builder: ${error.message}`, "error"));
})();
