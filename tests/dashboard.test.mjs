import { test } from 'node:test';
import assert from 'node:assert/strict';
import { safeUrl, partitionBriefing, filterItems, validateReadingList, normalizeEdition, editionHealth, DAY } from '../site/lib.mjs';
const generated_at = '2026-10-16T12:17:00Z';
const item = (id, age, score = 1) => ({ id, title: `Research ${id}`, url: `https://example.org/${id}`, source: 'Lab', summary: 'AI control research', topics: ['AI control & oversight'], score, published_at: new Date(Date.parse(generated_at) - age * DAY).toISOString() });
test('daily edition is finite, excludes future items, and keeps catch-up separately dated', () => {
  const items = [...Array.from({ length: 12 }, (_, i) => item(`new${i}`, 1, i)), item('future', -1, 100), item('older', 4), item('ancient', 60)];
  const { top, catchup } = partitionBriefing(items, generated_at);
  assert.equal(top.length, 5); assert.equal(top[0].id, 'new11'); assert.deepEqual(catchup.map(i => i.id), ['older']);
});
test('library combines saved, text, source and topic filters without mutating the archive', () => {
  const items = [item('one', 1), { ...item('two', 2), source: 'Other' }, { ...item('three', 3), topics: ['Interpretability'] }];
  const found = filterItems(items, { search: 'control research', source: 'Lab', topic: 'AI control & oversight', saved: new Set(['one', 'two']) });
  assert.deepEqual(found.map(i => i.id), ['one']); assert.equal(items[0].id, 'one');
});
test('source URLs cannot execute script or carry embedded credentials', () => { for (const url of ['javascript:alert(1)', 'data:text/html,x', 'https://name:secret@example.com', 'not a url']) assert.equal(safeUrl(url), null); assert.equal(safeUrl('https://arxiv.org/abs/2601.12345'), 'https://arxiv.org/abs/2601.12345'); });
test('reading list import bounds entries and rejects incompatible formats', () => {
  assert.throws(() => validateReadingList({ version: 2, saved: [], read: [] })); assert.throws(() => validateReadingList({ saved: 'broken', read: [] }));
  assert.deepEqual(validateReadingList({ saved: ['abc', 'abc', null, 5], read: ['def'], version: 1 }), { version: 1, saved: ['abc'], read: ['def'] });
});
test('invalid and duplicate rows cannot create unsafe article links', () => {
  const data = normalizeEdition({ generated_at, sources: [], items: [item('one', 1), item('one', 2), { ...item('bad', 1), url: 'javascript:alert(1)' }, { ...item('missing', 1), published_at: 'nonsense' }] });
  assert.deepEqual(data.items.map(i => i.id), ['one']);
});
test('failed update cannot disguise an old successful edition as fresh', () => {
  const edition = { generated_at, last_successful_update: '2026-10-12T12:17:00Z', sources: [{ status: 'error' }], items: [item('retained', 4)] };
  const health = editionHealth(edition, Date.parse(generated_at)); assert.equal(health.stale, true); assert.equal(health.failed.length, 1); assert.equal(health.empty, false);
});
