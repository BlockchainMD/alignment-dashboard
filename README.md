# Alignment research archive

An hourly discovery and historical-archiving system for public AI alignment research, with a dashboard at [Alignment Daily](https://blockchainmd.github.io/alignment-dashboard/).

**Collection coverage is the priority.** The archive retains uncertain candidates, undated records, and old publications. A small reading shortlist is separate from the full collection. No finite source registry or keyword search establishes that every alignment work published anywhere has been collected; coverage and backfill gaps are visible in the dashboard.

## Discovery and coverage

The versioned [source registry](config/sources.json) defines the scope:

| Source | Discovery route and limits |
| --- | --- |
| arXiv | Cross-category alignment/safety queries, incremental updates, and historical pagination by year. Keyword recall is incomplete. If the API is unavailable, category-limited RSS discovery is explicitly degraded and historical progress pauses. |
| OpenReview | Public API v2 paper forums matching configured phrases. Per-query historical progress, update catch-up, and search-ceiling warnings. API v1 venues are not indexed directly. |
| Alignment Forum and LessWrong | Public GraphQL posts, frozen incremental windows, and date-based historical cursors. Informal research and discussion are included. |
| Alignment Feed | Paginated secondary discovery index. Aggregator summaries remain labeled. |
| Anthropic Alignment Science | Research index and progressive article metadata retrieval. Missing publication dates remain unknown. |
| Redwood Research | Public archive pagination beyond the finite RSS window. |
| FAR.AI and Apollo Research | Research-specific sitemap URLs, historical batches, and periodic article rechecks. |
| METR, Transformer Circuits, ARC, MIRI, OpenAI, Google DeepMind | Publisher RSS/Atom feeds. Feed windows do not establish historical completeness; general lab feeds also contain non-research news. |

Dedicated alignment sources are ingested broadly. Mixed sources retain ambiguous matches as candidates rather than discarding them. Labels and keyword scores aid discovery; they are not assessments of evidence quality. The archive can contain commentary and unrelated candidates. Use the review-status filter to narrow the reading view.

Known gaps include personal blogs outside the registry, unfamiliar terminology, unindexed or inaccessible material, older feed-only history, and work in other languages. A successful fetch means a source was retrieved and parsed, not that its archive is complete. Publisher metadata can also be inaccurate.

## Durable records and recovery

- `data/archive.json` is the authoritative snapshot manifest. It commits source checkpoints together with checksummed metadata chunks under `data/archive/`, so historical growth does not create one file that eventually exceeds GitHub's file-size limit. `scripts.archive_store.load_archive` reads the complete snapshot and also supports the original flat format. It has no automatic age or record-count expiry.
- Publication, discovery, and source-update dates are distinct. Unknown dates are retained and never presented as newly published research.
- Canonical URLs, DOI/arXiv identifiers, and known forum crossposts merge duplicates while preserving provenance, stable IDs, aliases, and metadata revision hashes.
- Outages preserve records and checkpoints. Suspicious empty results, failures, incomplete catch-up, and historical backlogs remain visible.
- The hosted workflow serializes runs. Local collection also holds a nonblocking per-archive process lock to prevent overlapping commands from overwriting each other's progress.
- Retryable HTTP failures use bounded retries. HTTP validators and a bounded local cache reduce repeated transfers during local runs. Raw responses stay in ignored `.cache/`; fresh GitHub runners refetch them.
- `site/data/edition.json` is a recent-record bootstrap. The complete public archive is in checksummed, content-addressed chunks referenced by a versioned manifest. Browser search loads the full collected archive and reports incomplete loading explicitly.
- Current and previous two public export generations remain in the working tree for in-progress browser loads. Canonical article history is never pruned; Git also retains earlier versions. A browser holding a much older manifest should reload.

Only metadata and bounded source excerpts are published, rather than full articles. Revisions describe changes observed at collection time; revisit schedules do not guarantee immediate discovery of every historical edit. Some publisher pages are periodically rechecked even when their sitemap modification dates stay unchanged.

## Hourly operation

The [GitHub Actions workflow](.github/workflows/daily.yml) runs at **17 minutes past every hour**, on pushes to `main`, and on manual dispatch. Each run checks recent publications and continues bounded historical backfill. Records and cursors are committed before GitHub Pages deployment. Runs are serialized, and a rejected data push stops publication.

GitHub schedules are [best effort and can be delayed or dropped](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule). The dashboard warns after three hours without successful collection and shows failures source by source. After publishing the visible health state, the workflow flags a core discovery-source outage or a publisher failure repeated three times. GitHub notification delivery depends on your repository notification settings. This free deployment provides no guaranteed hourly service-level agreement. A strict delivery requirement would need an independently monitored scheduler and durable database/queue; the archive and adapters can be moved there.

The collector requires no model calls, paid research API, or API key. It does not collect X posts. No private notes, credentials, or reading lists are sent to the public repository.

To update immediately, use [Actions → Hourly collection and publish → Run workflow](https://github.com/BlockchainMD/alignment-dashboard/actions). Reload retrieves published data; it does not start collection. Open tabs refresh on return after 15 minutes.

## Local operation

Python 3.11+ and Node.js 22+ are sufficient; there are no application dependencies.

```sh
python3 scripts/collect.py --output site/data/edition.json --backfill-pages 2
python3 -m http.server 4173 --directory site
```

Open [localhost:4173](http://localhost:4173). Collection grows the archive; running only the server previews existing data. Increase historical page budgets deliberately while respecting provider rate limits. Recover one source without resetting other checkpoints:

```sh
python3 scripts/collect.py --source arxiv --backfill-pages 2
```

Corrupt archives stop collection instead of silently resetting history. Restore a known-good `data/archive.json` **and its referenced `data/archive/` files from the same Git commit** before retrying; do not delete the archive to clear a source error. The current and previous two canonical snapshot manifests are also retained under `data/archive/`. A retained recovery manifest can be copied to the authoritative `data/archive.json` location; its chunk references are resolved relative to `data/`.

```sh
python3 -m unittest discover -s tests
node --test tests/*.test.mjs
```

Saved/read state is browser-local. Export/import transfers it between devices. The research archive itself is accessible anywhere without a login.

## Foundation for videos of two minutes or less

The archive is the discovery input for a later media pipeline. It does not generate finished scripts, videos, or social posts.

Export JSONL research packets for every record, or one article:

```sh
python3 scripts/export_video_jobs.py
python3 scripts/export_video_jobs.py --article-id ARTICLE_ID --output artifacts/one-video-job.jsonl
```

Packets include stable article IDs, content fingerprints, original links, available author/date/identifier metadata, source excerpts and provenance, an empty script/claims structure, and review steps. Fingerprints ignore collection-time churn so unchanged research does not become a new job every hour. Candidates first need relevance review. The exporter does not retrieve or verify full articles.

The proposed production sequence is:

1. Retrieve and read the original research; preserve page/section references.
2. Draft a plain-language explanation: the question, an everyday example, the finding, and the limitation. Aim for 180–230 spoken words.
3. Check each factual claim against the source, retaining uncertainty and attribution.
4. Create narration, original explanatory visuals, captions, and portrait/landscape versions.
5. Measure the rendered video and enforce a maximum of **120 seconds**.
6. Review the final video and platform-specific post before publication to YouTube or X. Track remote IDs to avoid duplicate posts.

Generation services, voice/style choices, rendering infrastructure, and publishing integrations remain future work. Research collection does not incur those future media-generation costs.
