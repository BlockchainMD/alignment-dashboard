# Alignment research archive

An hourly discovery and archiving system for newly published public AI alignment research, with a dashboard at [Alignment Daily](https://blockchainmd.github.io/alignment-dashboard/).

**Collection coverage is the priority.** The active collection includes technical preprints, lab reports, workshop and conference papers, and substantive Alignment Forum/LessWrong research posts. Opinion, news, uncertain relevance, and uncertain publication timing remain candidates for review. A small reading shortlist is separate from the full collection. No finite source registry or keyword search establishes that every alignment work published anywhere has been collected; source limits and gaps are visible in the dashboard.

Collection is **forward-only from October 16, 2026 at 7:48:48 p.m. Eastern** (`2026-10-16T23:48:48Z`). The fixed cutoff is persisted in `config/sources.json` and carried into each archive and public manifest. It does not move each hour. Historical backfill and old-page rotation are disabled. Older records already collected remain intact in the canonical repository archive but are excluded from the active public collection and default video exports.

Publication and discovery times are separate. A newly discovered date-only record on the cutoff day, an undated record, or a current-year-only citation enters review with an explicit timing warning. It is not confirmed newly published research. Already-known pre-cutoff undated records stay excluded. Large publisher indexes establish a baseline of existing undated URLs without crawling their old articles; an undated page present at that first baseline has an unavoidable boundary uncertainty.

## Discovery and coverage

The versioned [source registry](config/sources.json) defines the scope:

| Source                              | Discovery route and limits                                                                                                                                                                                                             |
| ----------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| arXiv                               | Cross-category alignment/safety queries bounded by the publication cutoff, with forward catch-up. Keyword recall is incomplete. API outages use explicitly degraded, category-limited RSS discovery without advancing primary cursors. |
| OpenReview                          | Public API v2 papers matching configured phrases. Update-based discovery, publication metadata checks, and visible search-ceiling limits. API v1 venues are not indexed directly.                                                      |
| Alignment Forum and LessWrong       | New public GraphQL posts, with persistent forward cursors and tied-timestamp pagination. Informal research is included; discussion is routed to review.                                                                                |
| Alignment Feed                      | Paginated secondary discovery index with forward catch-up and a bounded seven-day publication overlap for late indexing. Aggregator summaries remain labeled.                                                                          |
| Anthropic Alignment Science         | Publisher index monitored for new links and recent modifications; dates come from article metadata/bylines. Existing undated URLs establish a baseline.                                                                                |
| Redwood Research                    | Public Substack archive with forward catch-up beyond a single page, stopping at the cutoff.                                                                                                                                            |
| FAR.AI, Apollo Research, ARC        | Research-specific sitemaps. New/recently modified pages are fetched; old article rotation is disabled.                                                                                                                                 |
| Transformer Circuits                | Lightweight dated publisher index; explicit publication dates determine admission.                                                                                                                                                     |
| MIRI Publications                   | Official bibliography monitored for newly added citations. Year-only dates require review; existing citations establish a baseline.                                                                                                    |
| METR, MIRI, OpenAI, Google DeepMind | Publisher RSS feeds, bounded by their recent windows. General lab feeds also contain non-research news.                                                                                                                                |

Dedicated alignment sources are ingested broadly. Mixed sources retain ambiguous matches as candidates rather than discarding them. Conservative text and metadata rules route identifiable opinion/news to review, including on dedicated sources. These automatic labels are provisional and are not assessments of evidence quality. The reading view defaults to topic matches; select **All records** or **Candidates to review** to inspect the rest of the active collection.

Known gaps include personal blogs outside the registry, unfamiliar terminology, unindexed or inaccessible material, publications lost during outages longer than a feed window, and work in other languages. A successful fetch means a source was retrieved and parsed, not that its archive is complete. Publisher metadata can also be inaccurate.

## Durable records and recovery

- `data/archive.json` is the authoritative snapshot manifest. It commits source checkpoints together with checksummed metadata chunks under `data/archive/`, so collection growth does not create one file that eventually exceeds GitHub's file-size limit. `scripts.archive_store.load_archive` reads the complete snapshot and also supports the original flat format. It has no automatic age or record-count expiry.
- Publication, discovery, and source-update dates are distinct. Unknown dates are retained and never presented as newly published research.
- Canonical URLs, DOI/arXiv identifiers, and known forum crossposts merge duplicates while preserving provenance, stable IDs, aliases, and metadata revision hashes.
- Outages preserve records and checkpoints. Suspicious empty results, failures, incomplete forward catch-up, and baseline uncertainty remain visible.
- The hosted workflow serializes runs. Local collection also holds a nonblocking per-archive process lock to prevent overlapping commands from overwriting each other's progress.
- Retryable HTTP failures use bounded retries. HTTP validators and a bounded local cache reduce repeated transfers during local runs. Raw responses stay in ignored `.cache/`; fresh GitHub runners refetch them.
- `site/data/edition.json` is a recent-record bootstrap. The complete active public collection is in checksummed, content-addressed chunks referenced by a versioned manifest. Browser search loads the full active collection and reports incomplete loading explicitly.
- Current and previous two public export generations remain in the working tree for in-progress browser loads. Canonical article history is never pruned; Git also retains earlier versions. A browser holding a much older manifest should reload.

Only metadata and bounded source excerpts are published, rather than full articles. Revisions describe changes observed at collection time. Forward discovery does not promise to detect edits to every older article; recent-modification hints can trigger a metadata fetch but never turn an old publication into a new one.

## Hourly operation

The [GitHub Actions workflow](.github/workflows/daily.yml) runs at **17 minutes past every hour**, on pushes to `main`, and on manual dispatch. Each run checks new publications and continues bounded forward catch-up. It does not traverse historical backfill cursors. Records and cursors are committed before GitHub Pages deployment. Runs are serialized, and a rejected data push stops publication.

GitHub schedules are [best effort and can be delayed or dropped](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule). The dashboard warns after three hours without successful collection and shows failures source by source. After publishing the visible health state, the workflow flags a core discovery-source outage or a publisher failure repeated three times. GitHub notification delivery depends on your repository notification settings. This free deployment provides no guaranteed hourly service-level agreement. A strict delivery requirement would need an independently monitored scheduler and durable database/queue; the archive and adapters can be moved there.

The collector requires no model calls, paid research API, or API key. It does not collect X posts. No private notes, credentials, or reading lists are sent to the public repository.

To update immediately, use [Actions → Hourly collection and publish → Run workflow](https://github.com/BlockchainMD/alignment-dashboard/actions). Reload retrieves published data; it does not start collection. Open tabs refresh on return after 15 minutes.

## Local operation

Python 3.11+ and Node.js 22+ are sufficient; there are no application dependencies.

```sh
python3 scripts/collect.py --output site/data/edition.json --backfill-pages 0
python3 -m http.server 4173 --directory site
```

Open [localhost:4173](http://localhost:4173). Collection grows the active archive from the fixed cutoff; running only the server previews existing data. The forward policy overrides historical budgets. Recover one source without resetting other checkpoints:

```sh
python3 scripts/collect.py --source arxiv --backfill-pages 0
```

Corrupt archives stop collection instead of silently resetting history. Restore a known-good `data/archive.json` **and its referenced `data/archive/` files from the same Git commit** before retrying; do not delete the archive to clear a source error. The current and previous two canonical snapshot manifests are also retained under `data/archive/`. A retained recovery manifest can be copied to the authoritative `data/archive.json` location; its chunk references are resolved relative to `data/`.

```sh
python3 -m unittest discover -s tests
node --test tests/*.test.mjs
```

Saved/read state is browser-local. Export/import transfers it between devices. The research archive itself is accessible anywhere without a login.

## Videos of two minutes or less

The working pipeline now captures the original research, creates an evidence-linked script, narrates it, animates explanatory diagrams, and renders both **1920 × 1080 landscape** and **1080 × 1920 portrait** videos. Every output remains a draft for review. Videos are not automatically posted to YouTube or X.

The audience is alignment researchers and anyone interested in alignment. Preserve the necessary technical detail and explain it simply: one idea per scene, a concrete example, defined terms, an explicit limitation, and original credit. The first example explains when a neuron explanation earns confidence; its source is a research discussion, so it is labeled accordingly.

See [the video production guide](docs/VIDEO_PIPELINE.md) for setup, commands, evidence checks, output files, and operation. Start with:

```sh
python3 -m pip install -r requirements-video.txt
python3 scripts/video_pipeline.py produce --article-id 70fab32c0c2057e8f247 --script examples/videos/neuron-explanations.json
```

FFmpeg/FFprobe and a local speech engine are also required. macOS uses its installed `say` voices; Linux uses eSpeak. Optional automatic scripting uses locally signed-in Codex or an explicitly configured OpenAI API model. The hourly collector only updates the discovery queue; it makes no drafting or speech API calls. Complete source snapshots, prompts, and media stay in ignored `artifacts/`.
