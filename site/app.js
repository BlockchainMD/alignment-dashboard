import {
  safeUrl,
  partitionBriefing,
  filterItems,
  validateReadingList,
  canonicalizeReading,
  normalizeEdition,
  editionHealth,
} from "./lib.mjs";

const $ = (id) => document.getElementById(id);
const STORAGE_KEY = "alignment-daily-reading-v1";
let edition = null;
let view = "library";
let archiveLoaded = false;
let archiveLoading = false;
let archiveError = "";
let visibleCount = 50;
let reading = { version: 1, saved: [], read: [] };
let storageWorks = true;
let toastTimer;
let lastFetchAt = 0;
try {
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored) reading = validateReadingList(JSON.parse(stored));
} catch {
  storageWorks = false;
}
try {
  const theme = localStorage.getItem("alignment-daily-theme");
  if (["light", "dark"].includes(theme))
    document.documentElement.dataset.theme = theme;
  else if (matchMedia("(prefers-color-scheme: dark)").matches)
    document.documentElement.dataset.theme = "dark";
} catch {
  /* use the default theme */
}

function node(tag, className, text) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text !== undefined) result.textContent = text;
  return result;
}
function date(value, options = {}) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf())
    ? "Date unavailable"
    : new Intl.DateTimeFormat("en-US", {
        timeZone: "America/Detroit",
        month: "short",
        day: "numeric",
        ...options,
      }).format(parsed);
}
function toast(message) {
  $("toast").textContent = message;
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ($("toast").hidden = true), 4200);
}
function persist() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(reading));
    storageWorks = true;
    return true;
  } catch {
    storageWorks = false;
    toast(
      "Browser storage is unavailable. Export your reading list before leaving.",
    );
    return false;
  }
}
function link(url, label, className) {
  const a = node("a", className, label);
  const safe = safeUrl(url);
  if (safe) {
    a.href = safe;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
  }
  return a;
}
function bookmarkIcon() {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", "M6 4h12v17l-6-4-6 4z");
  svg.append(path);
  return svg;
}
function renderArticle(item, index) {
  const saved = reading.saved.includes(item.id),
    read = reading.read.includes(item.id);
  const article = node("article", `article${read ? " is-read" : ""}`);
  article.append(
    node("span", "article-number", String(index + 1).padStart(2, "0")),
  );
  const body = node("div", "article-content");
  const meta = node("div", "article-header");
  meta.append(
    node("span", "source-name", item.source),
    node("span", "meta-separator"),
  );
  const dayOnly =
    item.date_precision === "day" &&
    /^\d{4}-\d{2}-\d{2}$/.test(item.published_date || "");
  if (item.published_at) {
    const time = node(
      "time",
      "",
      dayOnly
        ? date(`${item.published_date}T00:00:00Z`, {
            timeZone: "UTC",
            year: "numeric",
          })
        : date(item.published_at, { year: "numeric" }),
    );
    time.dateTime = dayOnly ? item.published_date : item.published_at;
    time.title = dayOnly
      ? `Publisher date: ${item.published_date} (time not supplied)`
      : `${date(item.published_at, { year: "numeric", hour: "numeric", minute: "2-digit" })} ET`;
    meta.append(time);
  } else
    meta.append(
      node(
        "span",
        "",
        item.publication_year
          ? `${item.publication_year} · exact date unknown`
          : "Publication date unknown",
      ),
    );
  if (item.relevance === "candidate")
    meta.append(node("span", "tag candidate-tag", "Candidate · needs review"));
  if (read) meta.append(node("span", "read-indicator", "Read"));
  const heading = node("h3", "article-title");
  const title = link(item.url, item.title);
  title.append(node("span", "external-arrow", "↗"));
  heading.append(title);
  body.append(meta, heading);
  if (item.authors?.length)
    body.append(
      node(
        "p",
        "author-line",
        item.authors
          .map((a) => (typeof a === "string" ? a : a.name || ""))
          .filter(Boolean)
          .slice(0, 8)
          .join(", ") + (item.authors.length > 8 ? " et al." : ""),
      ),
    );
  const summary =
    item.summary.length > 460
      ? item.summary.slice(0, 457).replace(/\s+\S*$/, "") + "…"
      : item.summary;
  if (summary) {
    body.append(node("p", "article-summary", summary));
    body.append(
      node(
        "span",
        "summary-label",
        item.summary_kind === "aggregator summary"
          ? "Aggregator summary · not independently verified"
          : "Publisher excerpt · may be truncated",
      ),
    );
  }
  const bottom = node("div", "article-bottom");
  const tags = node("div", "tags");
  for (const topic of item.topics.slice(0, 3))
    tags.append(node("span", "tag", topic));
  const readButton = node(
    "button",
    "read-button",
    read ? "✓ Read · mark unread" : "Mark as read",
  );
  readButton.type = "button";
  readButton.setAttribute("aria-pressed", String(read));
  readButton.dataset.focusKey = `read-${item.id}`;
  readButton.addEventListener("click", () => {
    toggleReading("read", item.id);
  });
  bottom.append(tags, readButton);
  body.append(bottom);
  if (item.provenance?.length || item.doi || item.arxiv_id) {
    const details = node("details", "provenance-details");
    details.append(
      node(
        "summary",
        "",
        `Sources & record details${item.provenance?.length > 1 ? ` · ${item.provenance.length} appearances` : ""}`,
      ),
    );
    const list = node("ul");
    for (const origin of item.provenance || []) {
      const li = node("li");
      li.append(
        link(
          origin.article_url || origin.url,
          origin.name || origin.source_id || "Source",
        ),
      );
      list.append(li);
    }
    if (item.doi) {
      const li = node("li");
      li.append(link(`https://doi.org/${item.doi}`, `DOI: ${item.doi}`));
      list.append(li);
    }
    if (item.first_seen)
      list.append(
        node(
          "li",
          "",
          `First collected: ${date(item.first_seen, { year: "numeric" })}`,
        ),
      );
    if (item.updated_at)
      list.append(
        node(
          "li",
          "",
          `Source update: ${date(item.updated_at, { year: "numeric" })}`,
        ),
      );
    if (item.revision_count > 0 || item.revisions?.length > 0)
      list.append(
        node(
          "li",
          "",
          "The source metadata has changed since initial collection.",
        ),
      );
    details.append(list);
    body.append(details);
  }
  const save = node("button", "save-button");
  save.type = "button";
  save.dataset.active = String(saved);
  save.title = saved ? "Remove from saved" : "Save for later";
  save.setAttribute("aria-label", `${saved ? "Unsave" : "Save"} ${item.title}`);
  save.setAttribute("aria-pressed", String(saved));
  save.append(bookmarkIcon());
  save.dataset.focusKey = `saved-${item.id}`;
  save.addEventListener("click", () => {
    toggleReading("saved", item.id);
    toast(
      saved
        ? "Removed from your saved research."
        : "Saved for later in this browser.",
    );
  });
  article.append(body, save);
  return article;
}
function toggleReading(kind, id) {
  const focusKey = document.activeElement?.dataset.focusKey;
  reading = canonicalizeReading(reading, edition.items);
  reading[kind] = reading[kind].includes(id)
    ? reading[kind].filter((value) => value !== id)
    : [...reading[kind], id].slice(-5000);
  persist();
  renderArticles();
  updateCounts();
  if (focusKey) {
    const target = document.querySelector(
      `[data-focus-key="${CSS.escape(focusKey)}"]`,
    );
    if (target) target.focus({ preventScroll: true });
    else $("main").focus({ preventScroll: true });
  }
}
function empty(title, message, browse = false) {
  const box = node("div", "empty-state");
  box.append(
    node("span", "empty-icon", "✳"),
    node("h3", "", title),
    node("p", "", message),
  );
  if (browse) {
    const a = node("a", "", "Browse the research library ↗");
    a.href = "#library";
    box.append(a);
  }
  return box;
}
function updateCounts() {
  const total = edition?.stats?.total_items ?? edition?.items.length;
  $("library-count").textContent =
    total === undefined ? "—" : total.toLocaleString();
  $("saved-count").textContent = reading.saved.length;
  if (edition) {
    $("metric-total").textContent = total.toLocaleString();
    $("metric-relevant").textContent = (
      edition.stats?.relevant_items ??
      edition.items.filter((i) => i.relevance !== "candidate").length
    ).toLocaleString();
    $("metric-candidates").textContent = (
      edition.stats?.candidate_items ??
      edition.items.filter((i) => i.relevance === "candidate").length
    ).toLocaleString();
    $("metric-sources").textContent =
      `${edition.sources.filter((s) => s.status === "ok").length}/${edition.sources.length}`;
  }
}
function renderArticles() {
  if (!edition) return;
  $("load-more").hidden = true;
  const libraryView = ["library", "saved"].includes(view);
  $("archive-status").hidden = !libraryView;
  $("archive-status").classList.toggle("error", !!archiveError);
  $("archive-status").textContent =
    archiveError ||
    (archiveLoading
      ? "Loading the full research archive… Search results are partial until loading completes."
      : archiveLoaded
        ? "Full collected archive loaded. Candidates are retained for review; historical source backfill continues."
        : "Showing recent records. Loading the historical archive when available.");
  $("articles").replaceChildren();
  $("catchup").replaceChildren();
  $("catchup-section").hidden = true;
  $("edition-end").hidden = view !== "briefing";
  let items;
  if (view === "briefing") {
    const { top, catchup } = partitionBriefing(
      edition.items,
      edition.generated_at,
    );
    items = top;
    $("section-title").firstChild.textContent = "Worth your attention ";
    $("section-caption").textContent = "Past 48 hours · up to 5 reads";
    if (catchup.length) {
      $("catchup-section").hidden = false;
      catchup.forEach((item, index) =>
        $("catchup").append(renderArticle(item, index)),
      );
    }
    if (!items.length)
      $("articles").append(
        empty(
          "A quiet edition.",
          "No new matching research was collected in this edition’s 48-hour window. Check source status for coverage, or read a little further back.",
          true,
        ),
      );
  } else {
    items = filterItems(edition.items, {
      search: $("search").value,
      topic: $("topic-filter").value,
      source: $("source-filter").value,
      relevance: $("relevance-filter").value,
      sort: $("sort").value,
      saved: view === "saved" ? new Set(reading.saved) : null,
    });
    $("section-title").firstChild.textContent =
      view === "saved" ? "Your reading list " : "All collected research ";
    $("section-caption").textContent =
      view === "saved"
        ? "Saved in this browser"
        : `${Math.min(visibleCount, items.length).toLocaleString()} of ${items.length.toLocaleString()} records shown`;
    $("load-more").hidden = visibleCount >= items.length;
    if (!items.length)
      $("articles").append(
        empty(
          view === "saved" && !reading.saved.length
            ? "Make room for a good read."
            : "No matching records.",
          view === "saved" && !reading.saved.length
            ? "Use the bookmark beside an article to save it for a quieter moment."
            : "Try another topic, source, review status, or search. Check the loading status above for archive availability.",
          view === "saved",
        ),
      );
  }
  $("section-count").textContent = String(items.length);
  (view === "briefing" ? items : items.slice(0, visibleCount)).forEach(
    (item, index) => $("articles").append(renderArticle(item, index)),
  );
}
function renderSources() {
  $("source-list").replaceChildren();
  for (const source of edition.sources) {
    const row = node("article", "source-row");
    const body = node("div");
    const h = node("h3");
    h.append(link(source.url, source.name || source.id));
    body.append(h);
    body.append(
      node(
        "p",
        "",
        `Last checked: ${date(source.checked_at, { year: "numeric", hour: "numeric", minute: "2-digit" })} ET · ${source.item_count ?? 0} records returned`,
      ),
    );
    const coverage = source.coverage || {};
    const notes = [
      coverage.description,
      coverage.scope,
      coverage.history_limit,
      coverage.backfill_complete === true
        ? "Available historical pages traversed; source scope still applies."
        : coverage.has_more || coverage.backfill_complete === false
          ? "Historical coverage is incomplete. See source notes below."
          : "Historical completeness is not verified.",
    ];
    if (coverage.pages_fetched !== undefined || coverage.pages !== undefined)
      notes.push(
        `${coverage.pages_fetched ?? coverage.pages} page(s) checked this run.`,
      );
    if (coverage.degraded)
      notes.push(
        "Fallback discovery active; the primary archive route is unavailable.",
      );
    if (coverage.next_year)
      notes.push(
        `Historical cursor: ${coverage.next_year}, offset ${coverage.next_offset || 0}.`,
      );
    if (coverage.total_available !== undefined)
      notes.push(`${coverage.total_available} results reported by the source.`);
    body.append(
      node(
        "p",
        "source-coverage",
        notes.filter((n) => typeof n === "string").join(" "),
      ),
    );
    const warnings = [
      ...new Set([...(source.warnings || []), ...(coverage.warnings || [])]),
    ];
    for (const warning of warnings)
      body.append(
        node(
          "p",
          "source-error",
          typeof warning === "string"
            ? warning
            : warning.message || "Source coverage warning.",
        ),
      );
    if (source.status !== "ok") {
      if (source.error || source.status !== "degraded")
        body.append(
          node(
            "p",
            "source-error",
            source.error ||
              (source.status === "warning"
                ? "The source returned an unusual result; inspect the coverage warnings."
                : "Could not retrieve this source."),
          ),
        );
      body.append(
        node(
          "p",
          "",
          source.last_success_at
            ? `Last successful retrieval: ${date(source.last_success_at, { year: "numeric", hour: "numeric", minute: "2-digit" })} ET. Earlier research remains in the library.`
            : "No successful retrieval has been recorded yet.",
        ),
      );
    }
    const status = node(
      "span",
      `source-status${source.status === "ok" ? "" : " error"}`,
      source.status === "ok"
        ? "● Available"
        : source.status === "degraded"
          ? "◐ Fallback active"
          : source.status === "warning"
            ? "◌ Needs attention"
            : "◌ Unavailable",
    );
    row.append(body, status);
    $("source-list").append(row);
  }
}
function fillFilters() {
  const fill = (id, values) => {
    const select = $(id),
      selected = select.value;
    while (select.options.length > 1) select.remove(1);
    for (const value of [...new Set(values)].sort()) {
      const option = node("option", "", value);
      option.value = value;
      select.append(option);
    }
    select.value = [...select.options].some((o) => o.value === selected)
      ? selected
      : "";
  };
  fill(
    "topic-filter",
    edition.items.flatMap((item) => item.topics),
  );
  fill(
    "source-filter",
    edition.sources
      .map((s) => s.name)
      .concat(edition.items.map((item) => item.source)),
  );
}
function renderHealth() {
  const { failed, stale, overdue } = editionHealth(edition);
  const messages = [];
  if (failed.length)
    messages.push(
      `${failed.length} of ${edition.sources.length} sources need attention or are using fallback discovery. Previously collected records are retained.`,
    );
  if (stale)
    messages.push(
      "The hourly archive may be out of date. No successful collection has been recorded in over three hours.",
    );
  else if (overdue.length > failed.length)
    messages.push(
      `${overdue.length} sources have not completed a successful check in over three hours.`,
    );
  if (!storageWorks)
    messages.push(
      "Browser storage is unavailable. Export your reading list to keep it.",
    );
  $("notice").replaceChildren();
  $("notice").hidden = !messages.length;
  if (messages.length) {
    $("notice").append(document.createTextNode(messages.join(" ") + " "));
    const a = node("a", "", "View sources & status ↗");
    a.href = "#sources";
    $("notice").append(a);
  }
  $("update-label").textContent =
    `${stale ? "Last update" : "Updated"} ${date(edition.last_successful_update || edition.generated_at, { hour: "numeric", minute: "2-digit" })} ET`;
  $("update-dot").classList.toggle("warning", !!failed.length || stale);
  $("edition-date").textContent = date(edition.generated_at, {
    weekday: "long",
    month: "long",
    day: "numeric",
    year: "numeric",
  }).toUpperCase();
}
const views = {
  briefing: [
    "A little signal.\nA clearer perspective.",
    "The research that moves AI safety forward.\nA focused daily read, with the original sources close at hand.",
    "YOUR DAILY BRIEFING",
  ],
  library: [
    "The alignment\nresearch archive.",
    "Broad discovery. Persistent history. Hourly updates.\nExplore original research and review uncertain matches.",
    "EXPLORE THE FULL COLLECTION",
  ],
  saved: [
    "Good research.\nOn your own time.",
    "A small shelf for the ideas you want to return to.\nSaved locally, ready when you are.",
    "SAVED FOR LATER",
  ],
  sources: [
    "Coverage you\ncan inspect.",
    "A transparent look at discovery, backfill, and source health.\nSee what has been checked and where gaps remain.",
    "SOURCE COVERAGE",
  ],
};
function navigate() {
  if (location.hash === "#main") return;
  const next = location.hash.slice(1);
  view = Object.hasOwn(views, next) ? next : "library";
  visibleCount = 50;
  for (const a of document.querySelectorAll("nav [data-view]")) {
    if (a.dataset.view === view) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  const [title, description, eyebrow] = views[view];
  $("view-title").replaceChildren();
  title.split("\n").forEach((line, i) => {
    if (i) $("view-title").append(document.createElement("br"));
    $("view-title").append(document.createTextNode(line));
  });
  $("view-description").textContent = description;
  $("view-description").style.whiteSpace = "pre-line";
  $("section-eyebrow").textContent = eyebrow;
  $("research-view").hidden = view === "sources";
  $("sources-view").hidden = view !== "sources";
  $("filters").hidden = !["library", "saved"].includes(view);
  $("reading-tools").hidden = view !== "saved";
  $("edition-meta").hidden = view === "sources";
  document.title = `${view === "briefing" ? "Reading shortlist" : view === "library" ? "Research archive" : view === "saved" ? "Saved research" : "Source coverage"} — Alignment Daily`;
  if (edition) {
    renderArticles();
    if (["library", "saved"].includes(view)) loadArchive();
  }
}
function dataUrl(path) {
  const root = new URL("./data/", location.href);
  const url = new URL(path, location.href);
  if (
    url.origin !== root.origin ||
    !url.pathname.startsWith(root.pathname) ||
    !url.pathname.endsWith(".json")
  )
    throw new Error("The archive contains an invalid data path.");
  return url;
}
async function loadArchive() {
  if (!edition || archiveLoaded || archiveLoading) return;
  const current = edition;
  if (!current.library_index?.manifest) {
    archiveLoaded = true;
    renderArticles();
    return;
  }
  archiveLoading = true;
  archiveError = "";
  renderArticles();
  try {
    const manifestUrl = dataUrl(current.library_index.manifest);
    manifestUrl.searchParams.set("t", current.generated_at);
    const response = await fetch(manifestUrl, { cache: "no-store" });
    if (!response.ok) throw new Error(`Manifest HTTP ${response.status}`);
    const manifestText = await response.text();
    if (current.library_index.sha256) {
      if (!globalThis.crypto?.subtle)
        throw new Error(
          "Use a secure browser connection to verify archive integrity.",
        );
      const hash = [
        ...new Uint8Array(
          await crypto.subtle.digest(
            "SHA-256",
            new TextEncoder().encode(manifestText),
          ),
        ),
      ]
        .map((v) => v.toString(16).padStart(2, "0"))
        .join("");
      if (hash !== current.library_index.sha256)
        throw new Error("Archive manifest checksum mismatch.");
    }
    const manifest = JSON.parse(manifestText);
    if (
      !Array.isArray(manifest.chunks) ||
      !Number.isInteger(manifest.total_items)
    )
      throw new Error("Archive manifest is invalid.");
    if (
      current.library_index.total_items !== undefined &&
      manifest.total_items !== current.library_index.total_items
    )
      throw new Error("Archive manifest belongs to a different collection.");
    const merged = new Map();
    let offset = 0;
    async function worker() {
      while (offset < manifest.chunks.length) {
        const chunk = manifest.chunks[offset++];
        const result = await fetch(dataUrl(chunk.url));
        if (!result.ok) throw new Error(`Archive chunk HTTP ${result.status}`);
        const text = await result.text();
        if (chunk.sha256) {
          if (!globalThis.crypto?.subtle)
            throw new Error(
              "Use a secure browser connection to verify archive integrity.",
            );
          const hash = [
            ...new Uint8Array(
              await crypto.subtle.digest(
                "SHA-256",
                new TextEncoder().encode(text),
              ),
            ),
          ]
            .map((v) => v.toString(16).padStart(2, "0"))
            .join("");
          if (hash !== chunk.sha256)
            throw new Error("Archive chunk checksum mismatch.");
        }
        const raw = JSON.parse(text);
        const rows = Array.isArray(raw) ? raw : raw.items;
        if (
          !Array.isArray(rows) ||
          (Number.isInteger(chunk.count) && rows.length !== chunk.count)
        )
          throw new Error("Archive chunk is incomplete.");
        const checked = normalizeEdition({
          generated_at: current.generated_at,
          sources: current.sources,
          items: rows,
        });
        if (checked.items.length !== rows.length)
          throw new Error("Archive chunk contains invalid records.");
        for (const item of checked.items) merged.set(item.id, item);
      }
    }
    await Promise.all(
      Array.from({ length: Math.min(4, manifest.chunks.length) }, worker),
    );
    if (merged.size !== manifest.total_items)
      throw new Error("Archive record count does not match its manifest.");
    if (edition !== current) return;
    current.items = [...merged.values()];
    archiveLoaded = true;
    const canonicalReading = canonicalizeReading(reading, current.items);
    if (JSON.stringify(canonicalReading) !== JSON.stringify(reading)) {
      reading = canonicalReading;
      persist();
    }
    fillFilters();
    updateCounts();
  } catch (error) {
    if (edition === current)
      archiveError = `Historical archive could not be fully loaded (${error.message}). Showing recent records only; results are incomplete. Reload to retry.`;
  } finally {
    if (edition === current) {
      archiveLoading = false;
      renderArticles();
    }
  }
}
async function loadEdition() {
  if ($("reload-button").disabled) return;
  $("reload-button").disabled = true;
  lastFetchAt = Date.now();
  try {
    const response = await fetch(`./data/edition.json?t=${Date.now()}`, {
      cache: "no-store",
    });
    if (!response.ok)
      throw new Error(`The edition request returned ${response.status}.`);
    edition = normalizeEdition(await response.json());
    archiveLoaded = false;
    archiveLoading = false;
    archiveError = "";
    visibleCount = 50;
    fillFilters();
    updateCounts();
    renderSources();
    renderHealth();
    renderArticles();
    if (["library", "saved"].includes(view)) loadArchive();
  } catch (error) {
    $("notice").hidden = false;
    $("notice").textContent =
      `The latest edition could not be loaded. ${edition ? "The edition already on screen is retained. " : ""}Check your connection and try Reload. ${error.message}`;
    if (!edition) {
      $("articles").replaceChildren(
        empty(
          "Your research desk is temporarily offline.",
          "The published edition could not be loaded. Try again in a moment.",
        ),
      );
      $("update-label").textContent = "Edition unavailable";
      $("update-dot").classList.add("warning");
    }
  } finally {
    $("reload-button").disabled = false;
  }
}
for (const id of [
  "search",
  "topic-filter",
  "source-filter",
  "relevance-filter",
  "sort",
])
  $(id).addEventListener(id === "search" ? "input" : "change", () => {
    visibleCount = 50;
    renderArticles();
  });
$("load-more").addEventListener("click", () => {
  visibleCount += 50;
  renderArticles();
});
$("reload-button").addEventListener("click", async () => {
  await loadEdition();
});
$("theme-button").addEventListener("click", () => {
  const theme =
    document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem("alignment-daily-theme", theme);
  } catch {
    /* a temporary preference is still useful */
  }
});
$("export-button").addEventListener("click", () => {
  const blob = new Blob(
    [
      JSON.stringify(
        { ...reading, exported_at: new Date().toISOString() },
        null,
        2,
      ),
    ],
    { type: "application/json" },
  );
  const url = URL.createObjectURL(blob);
  const a = node("a");
  a.href = url;
  a.download = "alignment-daily-reading-list.json";
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast("Reading list exported.");
});
$("import-input").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  try {
    if (file.size > 2000000)
      throw new Error("Please choose a reading list under 2 MB.");
    const imported = validateReadingList(JSON.parse(await file.text()));
    reading = canonicalizeReading(
      {
        version: 1,
        saved: [...new Set([...reading.saved, ...imported.saved])].slice(-5000),
        read: [...new Set([...reading.read, ...imported.read])].slice(-5000),
      },
      edition?.items || [],
    );
    const saved = persist();
    updateCounts();
    renderArticles();
    if (saved) toast("Reading lists combined and saved in this browser.");
  } catch (error) {
    toast(`Could not import: ${error.message}`);
  } finally {
    event.target.value = "";
  }
});
window.addEventListener("hashchange", navigate);
function refreshOnReturn() {
  if (
    document.visibilityState === "visible" &&
    Date.now() - lastFetchAt > 15 * 60000
  )
    loadEdition();
  else if (edition) renderHealth();
}
window.addEventListener("focus", refreshOnReturn);
document.addEventListener("visibilitychange", refreshOnReturn);
window.addEventListener("storage", (event) => {
  if (event.key === STORAGE_KEY) {
    try {
      reading = event.newValue
        ? validateReadingList(JSON.parse(event.newValue))
        : { version: 1, saved: [], read: [] };
      updateCounts();
      renderArticles();
    } catch {
      /* ignore malformed changes from other tabs */
    }
  }
});
navigate();
loadEdition();
