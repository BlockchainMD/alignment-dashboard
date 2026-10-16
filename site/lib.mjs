export const DAY = 86400000;
export const TOPICS = ['AI control & oversight', 'Scheming & deception', 'Interpretability', 'Evaluations & risk', 'Alignment methods', 'AI R&D & agents'];
export function safeUrl(value) {
  try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : null; } catch { return null; }
}
export function partitionBriefing(items, generatedAt) {
  const end = Date.parse(generatedAt);
  const dated = items.filter(item => Number.isFinite(Date.parse(item.published_at)) && Date.parse(item.published_at) <= end);
  const recent = dated.filter(item => end - Date.parse(item.published_at) <= 2 * DAY);
  recent.sort((a, b) => b.score - a.score || Date.parse(b.published_at) - Date.parse(a.published_at));
  const top = recent.slice(0, 5);
  const catchup = dated.filter(item => end - Date.parse(item.published_at) > 2 * DAY && end - Date.parse(item.published_at) <= 14 * DAY)
    .sort((a, b) => Date.parse(b.published_at) - Date.parse(a.published_at) || b.score - a.score).slice(0, 3);
  return { top, catchup };
}
export function filterItems(items, { search = '', topic = '', source = '', sort = 'newest', saved = null } = {}) {
  const terms = search.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const result = items.filter(item => (!saved || saved.has(item.id)) && (!topic || item.topics.includes(topic)) && (!source || item.source === source)
    && terms.every(term => [item.title, item.summary, item.source, ...item.topics].join(' ').toLowerCase().includes(term)));
  return result.sort((a, b) => sort === 'relevance' ? b.score - a.score || Date.parse(b.published_at) - Date.parse(a.published_at) : (sort === 'oldest' ? 1 : -1) * (Date.parse(a.published_at) - Date.parse(b.published_at)));
}
export function validateReadingList(value) {
  if (!value || typeof value !== 'object' || (value.version !== undefined && value.version !== 1)) throw new Error('This file is not a supported reading list.');
  if (!Array.isArray(value.saved) || !Array.isArray(value.read)) throw new Error('The reading list needs saved and read entries.');
  const clean = ids => [...new Set(ids.filter(id => typeof id === 'string' && id.length > 0 && id.length <= 256))].slice(0, 5000);
  return { version: 1, saved: clean(value.saved), read: clean(value.read) };
}
export function normalizeEdition(value) {
  if (!value || !Array.isArray(value.items) || !Array.isArray(value.sources) || !Number.isFinite(Date.parse(value.generated_at))) throw new Error('The published edition could not be read.');
  const ids = new Set();
  const items = value.items.filter(item => {
    if (!item || typeof item.id !== 'string' || ids.has(item.id) || typeof item.title !== 'string' || !safeUrl(item.url) || !Number.isFinite(Date.parse(item.published_at))) return false;
    ids.add(item.id); return true;
  }).map(item => ({ ...item, source: String(item.source || 'Research source'), summary: String(item.summary || ''), topics: Array.isArray(item.topics) ? item.topics.filter(t => typeof t === 'string') : [], score: Number.isFinite(item.score) ? item.score : 0 }));
  return { ...value, items };
}
export function editionHealth(edition, now = Date.now()) {
  const failed = edition.sources.filter(source => source.status !== 'ok');
  const last = Date.parse(edition.last_successful_update);
  const stale = !Number.isFinite(last) || now - last > 36 * 3600000;
  return { failed, stale, empty: edition.items.length === 0 };
}
