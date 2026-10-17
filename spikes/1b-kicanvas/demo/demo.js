// KiCanvas evaluation demo. Plain ES module; state is exposed on window.__demo
// for the Playwright checks in tests/.

const $ = (sel) => document.querySelector(sel);
const projects = await (await fetch("projects.json")).json();

const state = {
  project: null,
  tab: "sch",
  // per tab: { embed, status: "loading"|"ok"|"failed", ms, errors: [] }
  tabs: {},
  selections: [], // every kicanvas:select received on an embed, newest last
};
window.__demo = state;

// ---- load-error detection -------------------------------------------------
// KiCanvas sets `loaded` / fires kicanvas:load even when painting throws
// (spike 1B, Q2), so errors on window while a tab is loading mark it failed.
// Errors after a successful load (e.g. clicking copper) are shown separately.
let loadingTab = null;
function recordError(msg) {
  const t = state.tabs[loadingTab ?? state.tab];
  if (!t) return;
  if (t.status === "loading") t.errors.push(msg);
  else {
    t.runtimeErrors.push(msg);
    showStatus();
  }
}
window.addEventListener("error", (e) => recordError(e.message || String(e.error)));
window.addEventListener("unhandledrejection", (e) =>
  recordError(String(e.reason?.message ?? e.reason)),
);

// ---- shadow-DOM helpers (only for sheet list and "highlight REF") --------
function deepQuery(root, selector) {
  const roots = [root];
  while (roots.length) {
    const r = roots.shift();
    const hit = r.querySelector(selector);
    if (hit) return hit;
    for (const n of r.querySelectorAll("*")) if (n.shadowRoot) roots.push(n.shadowRoot);
  }
  return null;
}
const viewerElement = (embed) =>
  embed?.shadowRoot && deepQuery(embed.shadowRoot, "kc-schematic-viewer, kc-board-viewer");

// ---- selection → side panel ----------------------------------------------
function describe(item) {
  if (!item) return null;
  const kind = item.constructor?.name ?? "item";
  if (kind === "SchematicSheet") {
    const name = item.sheetname ?? item.properties?.get?.("Sheetname")?.text;
    return { ref: name ?? "sheet", kind, meta: `Sheet → ${item.sheetfile ?? "?"} (double-click to enter)` };
  }
  const ref = item.reference ?? null;
  const value = item.value ?? item.properties?.get?.("Value")?.text;
  const extra = [value, item.lib_id ?? item.library_link, item.layer].filter(Boolean);
  return { ref, kind, meta: extra.join(" · ") };
}

function onSelect(tab, e) {
  const d = describe(e.detail?.item);
  state.selections.push({ tab, ref: d?.ref ?? null, kind: d?.kind ?? null, at: performance.now() });
  $("#sel-ref").textContent = d?.ref ?? "—";
  $("#sel-meta").textContent = d ? `${d.kind} · ${d.meta}` : "Nothing selected.";
  const chip = $("#sel-chip");
  chip.hidden = !d?.ref || d.kind === "SchematicSheet";
  chip.textContent = d?.ref ? `@${d.ref}` : "";
}

// ---- embeds ----------------------------------------------------------------
function fmtBytes(n) {
  return n > 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.round(n / 1e3)} kB`;
}

function makeEmbed(tab) {
  const p = state.project;
  const files = tab === "sch" ? p.schematics : p.board ? [p.board] : [];
  const host = $(`#viewer-${tab}`);
  host.replaceChildren();
  const t = (state.tabs[tab] = { embed: null, status: "loading", ms: null, errors: [], runtimeErrors: [], files });
  if (!files.length) {
    t.status = "none";
    showStatus();
    return;
  }
  const embed = document.createElement("kicanvas-embed");
  embed.setAttribute("controls", "basic");
  embed.setAttribute("controlslist", "nooverlay nodownload");
  for (const f of files) {
    const s = document.createElement("kicanvas-source");
    s.setAttribute("src", f);
    embed.appendChild(s);
  }
  embed.addEventListener("kicanvas:select", (e) => onSelect(tab, e));
  t.embed = embed;
  const started = performance.now();
  loadingTab = tab;
  showStatus();
  host.appendChild(embed);

  const poll = setInterval(() => {
    if (state.tabs[tab] !== t) return clearInterval(poll); // superseded
    const v = viewerElement(embed);
    const elapsed = performance.now() - started;
    if (v?.hasAttribute("loaded")) {
      clearInterval(poll);
      t.ms = Math.round(elapsed);
      // Let the first paint/draw run so its exceptions are attributed to us.
      setTimeout(() => {
        if (state.tabs[tab] !== t) return;
        t.status = t.errors.length ? "failed" : "ok";
        if (loadingTab === tab) loadingTab = null;
        if (tab === "sch") wireSheets(embed);
        // The embed's zoom="objects" attribute is not forwarded at this commit;
        // boards otherwise open fitted to the whole drawing sheet.
        if (tab === "pcb" && v.viewer.zoom_to_board) {
          v.viewer.zoom_to_board();
          v.viewer.draw();
        }
        showStatus();
      }, 750);
    } else if (t.errors.length) {
      clearInterval(poll);
      t.status = "failed";
      if (loadingTab === tab) loadingTab = null;
      showStatus();
    } else if (elapsed > 180000) {
      clearInterval(poll);
      t.status = "failed";
      t.errors.push("timeout: not loaded after 180 s");
      showStatus();
    } else if (state.tab === tab) {
      $("#status").textContent = `Loading ${fmtFiles(t)} … ${(elapsed / 1000).toFixed(1)} s`;
    }
  }, 100);
}

function fmtFiles(t) {
  const bytes = t.files.reduce((a, f) => a + (state.project.bytes[f] ?? 0), 0);
  return `${t.files.length} file${t.files.length > 1 ? "s" : ""}, ${fmtBytes(bytes)}`;
}

function showStatus() {
  const t = state.tabs[state.tab];
  const el = $("#status");
  const banner = $("#banner");
  banner.hidden = true;
  el.className = "meta";
  if (!t) return;
  if (t.status === "none") {
    el.textContent = "No file for this tab in this project.";
    banner.className = "banner info";
    banner.textContent = "This project has no " + (state.tab === "sch" ? "schematic." : "board.");
    banner.hidden = false;
  } else if (t.status === "loading") {
    el.textContent = `Loading ${fmtFiles(t)} …`;
  } else if (t.status === "ok") {
    el.className = "meta ok";
    el.textContent = `Rendered ${fmtFiles(t)} in ${(t.ms / 1000).toFixed(2)} s`;
  }
  if (t.status === "ok" && t.runtimeErrors.length) {
    banner.className = "banner warn";
    banner.textContent = `KiCanvas error after load (${t.runtimeErrors.length}×): ${t.runtimeErrors.at(-1)}` +
      (state.tab === "pcb" ? " — likely net highlight on KiCad 11 nets-by-name (not patched)." : "");
    banner.hidden = false;
  }
  if (t.errors.length) {
    el.className = "meta bad";
    el.textContent = `Render error (${t.errors.length}): ${t.errors[0]}`;
    banner.className = "banner";
    banner.textContent = `KiCanvas failed to render this ${state.tab === "sch" ? "schematic" : "board"}: ${t.errors[0]}`;
    banner.hidden = false;
  }
}

// ---- hierarchical sheets ----------------------------------------------------
function wireSheets(embed) {
  const project = embed.project; // patches/embed-project-getter.patch
  const row = $("#sheet-row");
  const sel = $("#sheet");
  const pages = [...project.pages()].filter((p) => p.type === "schematic");
  row.hidden = pages.length < 2 || state.tab !== "sch";
  sel.replaceChildren(
    ...pages.map((p) => {
      const depth = p.sheet_path ? p.sheet_path.split("/").length - 2 : 0;
      const o = new Option(`${"  ".repeat(Math.max(depth, 0))}${p.name || p.filename}`, p.project_path);
      return o;
    }),
  );
  const sync = () => (sel.value = project.active_page?.project_path ?? "");
  sync();
  project.addEventListener("change", sync);
  sel.onchange = () => project.set_active_page(sel.value);
}

// ---- UI wiring --------------------------------------------------------------
function setTab(tab) {
  state.tab = tab;
  for (const b of document.querySelectorAll(".tabs button"))
    b.setAttribute("aria-selected", String(b.dataset.tab === tab));
  for (const v of document.querySelectorAll(".viewer")) v.hidden = v.dataset.tab !== tab;
  if (!state.tabs[tab]) makeEmbed(tab);
  $("#sheet-row").hidden = tab !== "sch" || $("#sheet").options.length < 2;
  showStatus();
  saveHash();
}

function setProject(id) {
  state.project = projects.find((p) => p.id === id) ?? projects[0];
  $("#project").value = state.project.id;
  state.tabs = {};
  $("#sheet").replaceChildren();
  for (const v of document.querySelectorAll(".viewer")) v.replaceChildren();
  onSelect(null, { detail: { item: null } });
  state.selections = [];
  setTab(state.tab);
}

function saveHash() {
  history.replaceState(null, "", `#${encodeURIComponent(state.project.id)}/${state.tab}`);
}

$("#project").append(
  ...projects.map((p) => {
    const size = p.board ? ` · PCB ${fmtBytes(p.bytes[p.board])}` : "";
    return new Option(`${p.name} (${p.schematics.length} sheet file${p.schematics.length > 1 ? "s" : ""}${size})`, p.id);
  }),
);
$("#project").onchange = (e) => setProject(e.target.value);
for (const b of document.querySelectorAll(".tabs button")) b.onclick = () => setTab(b.dataset.tab);

$("#theme").textContent = document.documentElement.dataset.theme === "light" ? "Dark" : "Light";
$("#theme").onclick = () => {
  const light = document.documentElement.dataset.theme === "light";
  localStorage.setItem("kc:prefs:theme", JSON.stringify({ val: light ? "witchhazel" : "kicad" }));
  location.reload(); // KiCanvas reads the preference only at module load
};

$("#find").onsubmit = (e) => {
  e.preventDefault();
  const ref = $("#find-ref").value.trim();
  const v = viewerElement(state.tabs[state.tab]?.embed)?.viewer;
  if (!ref || !v) return;
  v.select(ref);
  if (!v.selected) $("#sel-meta").textContent = `${ref} not found on this ${state.tab === "sch" ? "sheet" : "board"}.`;
};

function fromHash() {
  const [hashId, hashTab] = decodeURIComponent(location.hash.slice(1)).split(/\/(?=sch$|pcb$)/);
  state.tab = hashTab === "pcb" ? "pcb" : "sch";
  setProject(hashId);
}
await customElements.whenDefined("kicanvas-embed");
window.addEventListener("hashchange", fromHash);
fromHash();
