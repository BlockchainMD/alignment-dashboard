# Alignment Dashboard

A daily reading desk for AI alignment and safety research, built for a quick morning scan and a short list of articles to return to. The site is static HTML, CSS, and JavaScript; a Python standard library collector updates its public data file.

The intended public home is [BlockchainMD/alignment-dashboard](https://github.com/BlockchainMD/alignment-dashboard), with the dashboard served at [blockchainmd.github.io/alignment-dashboard](https://blockchainmd.github.io/alignment-dashboard/).

## What it collects

Sources are declared in [`config/sources.json`](config/sources.json). The collector reads public feeds and research pages, normalizes article titles, links, publication dates, and source excerpts, removes duplicate links, and scores relevance with explicit alignment and safety keywords. It does not call an AI model or generate summaries. Any excerpt is source text, and a relevance score is a browsing aid rather than an assessment of research quality.

The initial sources are Alignment Feed, arXiv, Alignment Forum, METR, Anthropic Alignment Science, and Redwood Research. The library retains up to 90 days of dated, relevant research.

This version has no X/Twitter collection and requires no API key, paid service, or model subscription. Open the original article to read it in full and verify its claims.

## Freshness and source health

The edition records when collection was attempted and shows the result for each source. A successful fetch means the source responded and its content could be parsed; it does not mean that the source published something today. Publication dates come from the source. Items without a reliable publication date are omitted rather than assigned the collection time.

When a source fails, previously collected items from that source are retained within the library's retention period, and source health reports the error. The dashboard keeps the source's last successful fetch time. A partial outage still publishes the available material. If every source fails, the workflow publishes the failure state and retained records, then marks the Actions run failed so the outage is visible. These are public sources and can change their feed formats, block requests, or become unavailable.

## Saved reading

Saved articles and reading state live in your browser's local storage. They persist on that browser and site address, but do not automatically sync between devices or browsers. Use **Export** to keep a JSON backup and **Import** to restore it elsewhere. Clearing site data removes the local copy. The public repository and deployed site do not receive your reading list.

## Run locally

Use Python 3.11 or newer to collect and serve the site; no Python packages need to be installed.

```sh
python3 scripts/collect.py --output site/data/edition.json
python3 -m http.server 4173 --directory site
```

Open [localhost:4173](http://localhost:4173). Serving over HTTP is needed for the browser to load the edition JSON. To preview the existing edition without fetching sources, run only the server command.

Run the checks with Python and Node.js 22 or newer:

```sh
python3 -m unittest discover -s tests
node --test tests/*.test.mjs
```

## Daily refresh and GitHub Pages

The [`Refresh and publish dashboard`](.github/workflows/daily.yml) workflow collects the edition, commits `site/data/edition.json` when it changes, and publishes `site/` through the official GitHub Pages actions. Keeping the edition in Git lets future runs retain article history and source health across runner restarts. The workflow uses the repository's built-in token with permission to update repository contents and deploy Pages; it needs no personal access token or repository secrets. Repository or organization rules must permit that token to push the generated data to `main`.

For initial hosting, push this project to the public repository, then select **Settings → Pages → Build and deployment → Source → GitHub Actions**. Enable Actions if GitHub prompts for it. Pushes to `main` run the workflow; manual runs and scheduled runs publish only from the repository's default branch. The intended default branch is `main`.

Refresh is scheduled for approximately **8:17 a.m. America/Detroit** each day. Two UTC schedules cover daylight saving time, and a timezone check selects the correct one. A queued run uses its intended schedule hour so an ordinary start delay does not silently skip collection. GitHub [can delay or drop scheduled jobs during heavy load and disables public-repository schedules after 60 days without repository activity](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule); this is a daily digest rather than a guaranteed delivery service.

To refresh on demand, open the repository's **Actions → Refresh and publish dashboard → Run workflow**, choose `main`, and run it. Reload the dashboard after the deployment completes. Reloading the web page alone only reloads the latest published edition.

Collection and deployment runs are serialized. If someone updates the default branch during collection, the older run stops before writing or deploying, and the newer push run rebuilds from the updated branch. A rejected data push or a failed test also stops publication.

## Change the coverage

Edit the source configuration to add or remove sources and adjust the collector's keyword rules to tune relevance. Review the source health display after a change, then use the manual workflow to collect and publish. Keep feed use modest and preserve the original source links and attribution.
