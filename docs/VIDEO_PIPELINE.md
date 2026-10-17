# Research video pipeline

The production target is a clear explanation of alignment research in at most **120 seconds**, in landscape and portrait. The intended audience includes alignment researchers and curious enthusiasts. Every substantive idea is taught in three consecutive passes: **technical language → approximately eighth-grade language → the simplest faithful explanation**. Simplicity preserves the idea's assumptions, uncertainty, and limits.

## What runs

1. **Discovery queue:** the hourly research workflow runs `video_pipeline.py sync`. Stable article and content fingerprints prevent unchanged research from becoming a new job every hour. Uncertain relevance or dates are held for review. Old article versions remain recorded as superseded.
2. **Original source:** the producer retrieves full article text or a scholarly PDF. LessWrong/Alignment Forum use full revision HTML because their default plaintext field can truncate. Abstract-only pages do not count as full papers. Numbered paragraphs, the source URL, and a SHA-256 bind the evidence to the script.
3. **Script:** supply a structured script, use locally signed-in Codex, or explicitly select an API model. Choose 2–3 substantive concepts. Each gets three adjacent scenes in technical/plain/simple order, with the same concept title and source-linked claims. Limitations are taught at all three levels too. Drafts distinguish empirical findings, theory, and research discussion. The source is data, never instructions to the drafting agent.
4. **Validation:** reject missing or reordered teaching levels, inconsistent claims within a group, claims outside a complete group, absent sourced limitations, mismatched source hashes, altered paragraph text, unknown citations, oversized scripts, and missing attribution. These are structural and traceability checks. They do not establish that the three phrasings have equivalent scientific meaning; that remains part of human review.
5. **Media:** synthesize each scene separately, measure its audio, animate original diagrams, burn captions, and encode H.264/AAC MP4s. The shared editorial design uses bundled Manrope typography, deep teal and ivory scenes, restrained lime/aqua/coral accents, progressive diagrams, concept anchors, and three-level indicators. Portrait is composed independently with reserved caption and phone-interface space. Short pauses separate levels and longer pauses separate concepts. The renderer measures final files and rejects anything longer than 120 seconds rather than truncating or unnaturally speeding it up.
6. **Review package:** each complete render is an immutable generation. A failed rerender cannot mix a new script with old video or captions. Local job state and output checksums support safe batch resumption. Nothing posts to a social platform.

## Install

Use Python 3.12 for the neural narrator, plus Pillow, FFmpeg and FFprobe. Kokoro ONNX currently supports Python 3.10–3.13; the complete pipeline needs at least 3.11. `pypdf` supports scholarly PDF extraction. The research collector still uses only the Python standard library and does not load speech dependencies.

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements-video.txt -r requirements-voice.txt
.venv/bin/python scripts/video_voice.py setup
source .venv/bin/activate
```

If you do not use `uv`, create a Python 3.12 virtual environment and install both requirements files with its pip. Install FFmpeg through your package manager. Linux also needs DejaVu fonts. The manual GitHub render workflow installs these tools and the neural narrator automatically.

The setup command explicitly downloads checksum-pinned model and voice files into ignored `.cache/kokoro/`. Normal rendering never downloads a model or silently changes providers. Allow roughly 1 GB of free disk space for the voice runtime and model. An explicit smaller-model option is described below.

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

Use `--preview` for 1280 × 720 and 720 × 1280 iteration. Use `--format landscape` or `--format portrait` to render only one format. Excessive measured total duration fails validation; speech is never automatically accelerated to squeeze a long script under the limit.

## Visual direction and teaching format

`config/video-design.json` is the reusable visual profile. `scripts/video_design.py` composes the title, persistent concept cue, teaching-level badge, diagram, captions, and source disclosure into separate safe regions. `scripts/video_diagrams.py` animates six original visual types: focused networks, explicitly illustrative analogies, toy comparisons, intervention flows, bounded takeaways, and source credits. Technical scenes can show more structure; plain and simple scenes reduce visual complexity while retaining the same conceptual relationship.

Motion reveals steps and changes focus throughout a scene. Probability diagrams use normalized illustrative distributions and carry an explicit toy-data notice. They never depict measurements from the paper. Other diagrams also remain illustrative. No copied article figures, stock footage, or generated raster images are needed.

The design profile, both drawing modules, and the fonts actually used enter the render fingerprint and production manifest. A style or font change therefore regenerates the video. The bundled regular/bold Manrope files make macOS and Linux typography consistent; their license and upstream provenance are in `assets/fonts/`.

New scripts require `teaching_format: "three_levels"`. Every substantive scene has `concept_id`, `concept_title`, and `explanation_level` (`technical`, `plain`, or `simple`). A concept is exactly one contiguous triple, with identical nonempty `claim_ids` in all three scenes. A source-credit scene may use null teaching metadata and no claims, but must not introduce a research assertion. Teach fewer ideas if the three passes cannot fit comfortably in two minutes.

Older scripts without this structure must be rewritten or regenerated before a new production run. Previously completed media remain available. The drafting validator's explicit legacy-reading option is not enabled by the production pipeline.

## Narration and voice auditions

The default is **Kokoro `af_heart`**, a warm American English neural voice, at a natural speed factor of `0.96`. Voice preference is subjective: audition the same passage before choosing the series voice. `af_bella` offers another feminine voice; `am_michael` offers a masculine alternative.

```sh
python scripts/video_voice.py sample --voice af_heart --text "A pattern can suggest an explanation. But does that explanation predict what the network will do?" --output artifacts/voice-heart.wav
python scripts/video_voice.py sample --voice af_bella --text "A pattern can suggest an explanation. But does that explanation predict what the network will do?" --output artifacts/voice-bella.wav
python scripts/video_voice.py sample --voice am_michael --text "A pattern can suggest an explanation. But does that explanation predict what the network will do?" --output artifacts/voice-michael.wav
python scripts/video_pipeline.py produce --article-id 70fab32c0c2057e8f247 --script examples/videos/neuron-explanations.json --voice af_heart --speech-speed 0.96
```

`config/video-voice.json` defines the production delivery profile: voice, natural speed, sentence/clause and scene pauses, pronunciation replacements, and audio mastering. Kokoro uses voice and speed controls; it does not accept free-form acting instructions. Scripts provide the conversational rhythm through short sentences, meaningful punctuation, and one idea at a time. The goal is a thoughtful person explaining an interesting result, with restrained emphasis and room to think.

Technical acronyms can have pronunciation replacements without changing the displayed script or captions. Review author names individually. Every synthesized scene is mastered to a consistent **−16 LUFS** target with a **−1.5 dB true-peak** ceiling before the renderer measures it. Pauses and measured speech determine the timeline for both formats.

For limited disk space, explicitly install the smaller quantized model and select it for rendering:

```sh
python scripts/video_voice.py setup --model int8
ALIGNMENT_KOKORO_MODEL=int8 python scripts/video_pipeline.py produce --article-id 70fab32c0c2057e8f247 --script examples/videos/neuron-explanations.json
```

Model identity, voice, speed, pronunciation rules, mastering settings, and implementation hashes participate in render reuse. Changing the voice regenerates the video instead of reusing old narration. `ALIGNMENT_KOKORO_DIR` can select another local asset directory. Model weights remain local and are excluded from review packages.

For a lightweight system-voice preview, explicitly request `--speech-provider system`. macOS then uses `say` (`--voice Samantha`); Linux requires eSpeak/eSpeak NG. `--rate 155` is a system-speech words-per-minute setting; neural narration uses `--speech-speed` instead. A missing neural dependency or model is an actionable error, never a silent fallback to a different voice.

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

The job directory contains `source.json`, `packet.json`, and `draft-prompt.txt`. Save the returned JSON as a script, then pass `--script PATH`. Drafts need 7–11 scenes: 2–3 complete concept triples plus optional opening/closing credit scenes. Target 180–225 spoken words, with source-linked claims, limitations, and exact credits. Short evidence locators total no more than 25 words; paragraph references carry the surrounding context.

`--include-candidate` explicitly selects a candidate for individual draft review; it does not classify the article as confirmed research or enable publication. `--refresh-source` recaptures the source. `--redraft` requests a new generated script. A changed source requires a newly validated script.

## Outputs

Each job lives under `artifacts/videos/JOB_ID/`. Complete media generations contain:

- Landscape and portrait MP4s, with original animated diagrams and burned captions.
- Posters/thumbnails, combined narration WAV, SRT and VTT captions.
- The exact script, concept/teaching levels, measured timeline and pauses, output dimensions, durations, checksums, voice disclosure, and versioned visual profile in a production manifest.
- A review page with playable videos, original research links, and limitations.

Job metadata and `publish-package.json` hold the proposed title, description, credits, limits, and review requirements. Publishing remains unapproved and remote platform IDs remain empty.

Caption chunks use measured scene audio and proportional phrase timing. They are not word-level forced alignment; listen to the full video and check pronunciation/timing, especially around expressive pauses. The standalone renderer also accepts supplied per-scene audio through `--audio-dir` for a recorded narrator or another narration service.

## GitHub rendering

The **Render a research video for review** workflow is manual. Select an active article ID and a source-linked script already committed in the repository. It renders both formats with the pinned neural narrator and retains a downloadable review artifact for 14 days. It does not invoke a paid model or publish to YouTube/X. Complete article snapshots and source-bearing prompts are excluded from uploaded artifacts.

The hourly **Hourly collection and publish** workflow updates the research and video discovery queue only. It continues to publish source health even when a final health step flags an upstream outage. Video production does not hold up research collection with model calls or rendering.

## Quality bar

Before sharing a render, review the original source and the video together:

- The hook states a real question rather than a sensational promise.
- Necessary technical terms are retained in the first pass and clearly defined in the following plain-language pass; each concept develops one idea.
- Every substantive idea is explained in technical language, then approximately eighth-grade language, then the simplest faithful language. All three preserve its scope, conditions, uncertainty, and limitations.
- Analogies are clearly illustrative and preserve the relevant relationship.
- Experimental results, theoretical claims, and commentary are distinguished.
- Every factual assertion is supported in context; exact quote matching alone is insufficient.
- Meaningful assumptions and limitations stay attached to the result.
- The narrator sounds conversational, technical words and names are pronounced correctly, and pauses make the argument easier to follow.
- Captions preserve all spoken words, diagram labels remain readable on a phone, and the final duration is at most 120 seconds.

Source capture uses public HTTP(S), rejects private addresses and unsafe redirects, bounds downloads, and stores complete research only under ignored local artifacts. Keep original figure reuse, music, additional hosted narration providers, and social-account integrations as deliberate later additions.

## Verification

```sh
python3 -m unittest discover -s tests
node --test tests/*.test.mjs
python3 -m unittest discover -s tests -p 'test_video_*.py'
```

The tests cover source extraction and boundaries, evidence binding, ordered three-level teaching groups, draft parsing, queue versioning, candidate holds, layout/caption bounds, semantic diagram selection, illustrative distributions, motion, visual/voice fingerprinting, safe resume, and failed rerenders. Actual sample keyframes and both completed video formats are additionally inspected and measured with FFprobe.

Provider references: [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode), [Responses Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses).

Voice references: [Kokoro ONNX runtime](https://github.com/thewh1teagle/kokoro-onnx) (MIT), [Kokoro model and voice guide](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md) (model weights Apache 2.0). These notices travel with the production profile; the narration remains disclosed as synthetic.
