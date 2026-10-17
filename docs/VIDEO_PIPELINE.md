# Research video pipeline

The production target is a clear explanation of one alignment idea in at most **120 seconds**, in landscape and portrait. The intended audience includes alignment researchers and curious enthusiasts. Simplicity means explaining the technical point clearly, while keeping its assumptions and limits.

## What runs

1. **Discovery queue:** the hourly research workflow runs `video_pipeline.py sync`. Stable article and content fingerprints prevent unchanged research from becoming a new job every hour. Uncertain relevance or dates are held for review. Old article versions remain recorded as superseded.
2. **Original source:** the producer retrieves full article text or a scholarly PDF. LessWrong/Alignment Forum use full revision HTML because their default plaintext field can truncate. Abstract-only pages do not count as full papers. Numbered paragraphs, the source URL, and a SHA-256 bind the evidence to the script.
3. **Script:** supply a structured script, use locally signed-in Codex, or explicitly select an API model. Each research claim links to original paragraphs with short exact quote locators. Drafts distinguish empirical findings, theory, and research discussion. The source is data, never instructions to the drafting agent.
4. **Validation:** reject mismatched source hashes, altered paragraph text, unknown citations, absent limitations, oversized scripts, and missing attribution. These are traceability checks. They do not establish that a paraphrase is scientifically correct.
5. **Media:** synthesize each scene separately, measure its audio, animate original diagrams, burn captions, and encode H.264/AAC MP4s. Portrait is composed independently. The renderer measures the final files and rejects anything longer than 120 seconds rather than truncating or unnaturally speeding it up.
6. **Review package:** each complete render is an immutable generation. A failed rerender cannot mix a new script with old video or captions. Local job state and output checksums support safe batch resumption. Nothing posts to a social platform.

## Install

Python 3.11+, Pillow, FFmpeg, FFprobe, and local speech are required for rendering. `pypdf` supports scholarly PDF extraction. The research collector still uses only the Python standard library.

```sh
python3 -m pip install -r requirements-video.txt
```

On macOS, install FFmpeg through your package manager if needed. The renderer uses `say`; `--voice Samantha` selects the default example voice. On Linux, install FFmpeg, eSpeak/eSpeak NG, and DejaVu fonts. The manual GitHub render workflow installs these automatically.

No API key is needed for a supplied script or local narration. Automatic Codex drafting uses the computer's existing Codex sign-in and consumes its usage allowance. Keep that sign-in on the local computer; never copy authentication files into this public repository or GitHub Actions.

## First render

Run these commands from the repository root:

```sh
python3 scripts/video_pipeline.py sync
python3 scripts/video_pipeline.py produce --article-id 70fab32c0c2057e8f247 --script examples/videos/neuron-explanations.json
```

The checked-in example is a source-linked explanation of _Neural networks are not already clean_. It is explicitly a research discussion, not a new experiment. If the article changes, its source hash will no longer match the example; review and update the script against the new source instead of bypassing validation.

Open `artifacts/videos/index.html` for the local studio, or serve it:

```sh
python3 -m http.server 4174 --bind 127.0.0.1 --directory artifacts/videos
```

The studio is then at [localhost:4174](http://localhost:4174). A local server provides reliable video seeking and caption tracks. The `artifacts/` directory is deliberately outside the published dashboard.

Use `--preview` for 1280 × 720 and 720 × 1280 iteration. Use `--format landscape` or `--format portrait` to render only one format. `--rate` sets the requested natural speaking rate; excessive total duration fails validation.

## Automatic scripts

With Codex installed and signed in locally:

```sh
python3 scripts/video_pipeline.py produce --article-id ARTICLE_ID --codex-cli-defaults
python3 scripts/video_pipeline.py batch --limit 1 --codex-cli-defaults
```

`--codex-cli-defaults` explicitly asks the child CLI to ignore its user config for that invocation, while retaining its saved sign-in. This is useful when the desktop app's configured model is not supported by the standalone CLI. Omit the flag to inherit the CLI's normal settings. Runs are ephemeral and read-only, use a strict output schema, and are bounded by a timeout. Failed model requests are not automatically retried.

Batch mode handles at most one job by default. It skips a finished job only when the requested formats/settings and the current script/source/output hashes match. Failures remain visible and do not stop unrelated jobs within the chosen limit. Review candidates are excluded from automatic batch generation.

For a hosted API instead, configure `OPENAI_API_KEY` in the process environment and explicitly choose the model:

```sh
python3 scripts/video_pipeline.py produce --article-id ARTICLE_ID --provider openai --model YOUR_MODEL
```

This option makes a paid API request. The model must support Responses Structured Outputs. The pipeline sends the bounded source context with `store: false`, validates the response, and does not silently retry an ambiguous paid request. No model API key is configured by the repository.

For drafting in an existing chat or editor, prepare an offline prompt:

```sh
python3 scripts/video_pipeline.py prepare --article-id ARTICLE_ID
```

The job directory contains `source.json`, `packet.json`, and `draft-prompt.txt`. Save the returned JSON as a script, then pass `--script PATH`. Drafts need 5–7 scenes, a target of 180–225 spoken words, source-linked claims, limitations, and exact credits. Short evidence locators total no more than 25 words; paragraph references carry the surrounding context.

`--include-candidate` explicitly selects a candidate for individual draft review; it does not classify the article as confirmed research or enable publication. `--refresh-source` recaptures the source. `--redraft` requests a new generated script. A changed source requires a newly validated script.

## Outputs

Each job lives under `artifacts/videos/JOB_ID/`. Complete media generations contain:

- Landscape and portrait MP4s, with original animated diagrams and burned captions.
- Posters/thumbnails, combined narration WAV, SRT and VTT captions.
- The exact script, a measured timeline, output dimensions, durations, checksums, and voice disclosure in a production manifest.
- A review page with playable videos, original research links, and limitations.

Job metadata and `publish-package.json` hold the proposed title, description, credits, limits, and review requirements. Publishing remains unapproved and remote platform IDs remain empty.

Caption chunks use measured scene audio and proportional phrase timing. They are not word-level forced alignment; listen to the full video and check pronunciation/timing. The local voice is a working baseline. The renderer also accepts supplied per-scene audio through its Python API for a different narration service or a recorded voice.

## GitHub rendering

The **Render a research video for review** workflow is manual. Select an active article ID and a source-linked script already committed in the repository. It renders both formats with Linux speech and retains a downloadable review artifact for 14 days. It does not invoke a paid model or publish to YouTube/X. Complete article snapshots and source-bearing prompts are excluded from uploaded artifacts.

The hourly **Hourly collection and publish** workflow updates the research and video discovery queue only. It continues to publish source health even when a final health step flags an upstream outage. Video production does not hold up research collection with model calls or rendering.

## Quality bar

Before sharing a render, review the original source and the video together:

- The hook states a real question rather than a sensational promise.
- Necessary terms are defined when introduced; each scene develops one idea.
- Analogies are clearly illustrative and preserve the relevant relationship.
- Experimental results, theoretical claims, and commentary are distinguished.
- Every factual assertion is supported in context; exact quote matching alone is insufficient.
- Meaningful assumptions and limitations stay attached to the result.
- Captions and diagrams remain readable on a phone, and the final duration is at most 120 seconds.

Source capture uses public HTTP(S), rejects private addresses and unsafe redirects, bounds downloads, and stores complete research only under ignored local artifacts. Keep original figure reuse, music, extra narration providers, and social-account integrations as deliberate later additions.

## Verification

```sh
python3 -m unittest discover -s tests
node --test tests/*.test.mjs
python3 -m unittest discover -s tests -p 'test_video_*.py'
```

The tests cover source extraction and boundaries, evidence binding, draft parsing, queue versioning, candidate holds, rendering timing/layout helpers, safe resume, and failed rerenders. Actual sample renders are additionally inspected visually and measured with FFprobe.

Provider references: [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode), [Responses Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses).
