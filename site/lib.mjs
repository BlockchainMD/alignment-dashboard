export const DAY = 86400000;
export const TOPICS = [
  "AI control & oversight",
  "Scheming & deception",
  "Interpretability",
  "Evaluations & risk",
  "Alignment methods",
  "AI R&D & agents",
];
export function safeUrl(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) &&
      !url.username &&
      !url.password
      ? url.href
      : null;
  } catch {
    return null;
  }
}
export function partitionBriefing(items, generatedAt) {
  const end = Date.parse(generatedAt);
  const dated = items.filter(
    (item) =>
      item.relevance !== "candidate" &&
      Number.isFinite(Date.parse(item.published_at)) &&
      Date.parse(item.published_at) <= end,
  );
  const recent = dated.filter(
    (item) => end - Date.parse(item.published_at) <= 2 * DAY,
  );
  recent.sort(
    (a, b) =>
      b.score - a.score ||
      Date.parse(b.published_at) - Date.parse(a.published_at),
  );
  const top = recent.slice(0, 5);
  const catchup = dated
    .filter(
      (item) =>
        end - Date.parse(item.published_at) > 2 * DAY &&
        end - Date.parse(item.published_at) <= 14 * DAY,
    )
    .sort(
      (a, b) =>
        Date.parse(b.published_at) - Date.parse(a.published_at) ||
        b.score - a.score,
    )
    .slice(0, 3);
  return { top, catchup };
}
export function filterItems(
  items,
  {
    search = "",
    topic = "",
    source = "",
    relevance = "",
    sort = "newest",
    saved = null,
  } = {},
) {
  const terms = search.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const result = items.filter(
    (item) =>
      (!saved ||
        saved.has(item.id) ||
        item.aliases?.some((id) => saved.has(id))) &&
      (!topic || item.topics.includes(topic)) &&
      (!source ||
        item.source === source ||
        item.provenance?.some((p) => p.name === source)) &&
      (!relevance || item.relevance === relevance) &&
      terms.every((term) =>
        [
          item.title,
          item.summary,
          item.source,
          item.doi || "",
          ...(item.authors || []).map((a) =>
            typeof a === "string" ? a : a.name || "",
          ),
          ...item.topics,
        ]
          .join(" ")
          .toLowerCase()
          .includes(term),
      ),
  );
  const stamp = (item, key = "published_at") => {
    const parsed = Date.parse(item[key]);
    if (Number.isFinite(parsed)) return parsed;
    // A year-only record belongs in its known year when sorting, while the UI
    // and record keep the exact publication date explicitly unknown.
    if (key === "published_at" && Number.isInteger(item.publication_year))
      return Date.UTC(item.publication_year, 0, 1);
    return 0;
  };
  return result.sort((a, b) =>
    sort === "relevance"
      ? b.score - a.score || stamp(b) - stamp(a)
      : sort === "discovered"
        ? stamp(b, "first_seen") - stamp(a, "first_seen")
        : sort === "updated"
          ? stamp(b, "updated_at") - stamp(a, "updated_at")
          : sort === "oldest"
            ? (stamp(a) || Infinity) - (stamp(b) || Infinity)
            : stamp(b) - stamp(a),
  );
}
export function validateReadingList(value) {
  if (
    !value ||
    typeof value !== "object" ||
    (value.version !== undefined && value.version !== 1)
  )
    throw new Error("This file is not a supported reading list.");
  if (!Array.isArray(value.saved) || !Array.isArray(value.read))
    throw new Error("The reading list needs saved and read entries.");
  const clean = (ids) =>
    [
      ...new Set(
        ids.filter(
          (id) => typeof id === "string" && id.length > 0 && id.length <= 256,
        ),
      ),
    ].slice(0, 5000);
  return { version: 1, saved: clean(value.saved), read: clean(value.read) };
}
export function canonicalizeReading(value, items) {
  const byAlias = new Map();
  for (const item of items)
    for (const alias of item.aliases || []) byAlias.set(alias, item.id);
  return {
    ...value,
    saved: [...new Set(value.saved.map((id) => byAlias.get(id) || id))],
    read: [...new Set(value.read.map((id) => byAlias.get(id) || id))],
  };
}
export function normalizeEdition(value) {
  if (
    !value ||
    !Array.isArray(value.items) ||
    !Array.isArray(value.sources) ||
    !Number.isFinite(Date.parse(value.generated_at))
  )
    throw new Error("The published edition could not be read.");
  const ids = new Set();
  const items = value.items
    .filter((item) => {
      if (
        !item ||
        typeof item.id !== "string" ||
        ids.has(item.id) ||
        typeof item.title !== "string" ||
        !safeUrl(item.url) ||
        (item.published_at !== null &&
          !Number.isFinite(Date.parse(item.published_at)))
      )
        return false;
      ids.add(item.id);
      return true;
    })
    .map((item) => ({
      ...item,
      source: String(item.source || "Research source"),
      summary: String(item.summary || ""),
      topics: Array.isArray(item.topics)
        ? item.topics.filter((t) => typeof t === "string")
        : [],
      relevance: item.relevance || "relevant",
      authors: Array.isArray(item.authors) ? item.authors : [],
      score: Number.isFinite(item.score) ? item.score : 0,
    }));
  return { ...value, items };
}
export function editionHealth(edition, now = Date.now()) {
  const failed = edition.sources.filter((source) => source.status !== "ok");
  const last = Date.parse(edition.last_successful_update);
  const stale = !Number.isFinite(last) || now - last > 3 * 3600000;
  const overdue = edition.sources.filter(
    (source) =>
      !Number.isFinite(Date.parse(source.last_success_at)) ||
      now - Date.parse(source.last_success_at) > 3 * 3600000,
  );
  return { failed, stale, overdue, empty: edition.items.length === 0 };
}
