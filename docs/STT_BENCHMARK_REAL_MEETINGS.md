# STT benchmark on real French meeting audio

> **Why this document exists.** Public ASR leaderboards are dominated by clean,
> English, read-speech corpora. Meeting transcription lives elsewhere: narrowband
> phone/visio recordings, 5 to 26 speakers, overlaps, domain jargon. This is a
> benchmark of STT engines integrated in [TranscrIA](https://github.com/Martossien/transcria)
> on **real French meeting recordings** — with the failure modes that scores alone
> do not show.
>
> **Privacy.** The audio corpus is private and stays private: real meetings cannot
> be published. This document contains **no audio, no transcript excerpt, no name,
> no organisation** — only aggregate metrics, acoustic descriptors and abstract
> descriptions of error classes. Corpus identifiers are neutral (`R01`…, `L01`…).
> The methodology and all scripts are open (`scripts/bench_audio.py`,
> `scripts/score_reference_bench.py`, `scripts/bench_eval.py`), so anyone can
> reproduce the protocol on their own recordings.

## Corpus

Two complementary sets, drawn from **8 distinct real meetings** (works-council-type
sessions, a municipal-council-type session, project reviews, institutional video
calls), recorded with ordinary equipment (dictaphone apps, room mics, visio).

### Set L — ground-truth windows (WER against a human reference)

Eight 5-minute windows spread across a **3.6-hour formal committee meeting**
(26 speakers in the reference), for which a **professionally produced verbatim
transcript** exists. The transcript was aligned and windowed
(`scripts/prepare_reference_windows.py`); hypothesis text is scored against it
(`scripts/score_reference_bench.py`).

| Window | Ref. words | Speakers in window | Character |
|---|---:|---:|---|
| L01 | 535 | 8 | opening, many short turns |
| L02 | 883 | 9 | high speaker variety |
| L03 | 924 | 8 | dense discussion |
| L04 | 1102 | 6 | fast delivery |
| L05 | 1308 | 5 | densest passage |
| L06 | 1176 | — | mid-meeting |
| L07 | 859 | — | Q&A |
| L08 | 776 | — | closing section |

*The human reference itself is a proxy, not a perfect truth — our own reading of it
found occasional transcription errors. Absolute WER values must be read with that
in mind; relative deltas between engines are the meaningful signal.*

### Set R — 60-second windows across 8 meetings (no reference)

18 windows cut from the meetings at different moments, labelled by TranscrIA's
acoustic preflight (deterministic metrics — same code as the
[browser demo](https://huggingface.co/spaces/martossien/transcria-audio-preflight)):

| ID | Dur. | Preflight | SNR (dB) | 99% bandwidth (Hz) | Notes |
|---|---:|---|---:|---:|---|
| R01 | 60 s | suspect | 55.7 | 3016 | narrowband |
| R02 | 60 s | suspect | 42.5 | 3043 | narrowband |
| R03 | 60 s | suspect | 48.5 | 3099 | narrowband |
| R04 | 60 s | suspect | 51.0 | 3282 | narrowband |
| R05 | 60 s | suspect | 34.5 | 2035 | very narrow, low SNR |
| R06 | 60 s | suspect | 42.4 | 1118 | extremely narrow |
| R07 | 60 s | suspect | 53.2 | 500 | pathological bandwidth |
| R08 | 60 s | suspect | n/a | 2806 | dense speech (SNR not computable) |
| R09 | 58 s | suspect | n/a | 2116 | dense speech |
| R10 | 60 s | suspect | 49.0 | 1402 | narrow |
| R11 | 60 s | suspect | 46.1 | 2234 | narrowband |
| R12 | 60 s | suspect | 46.0 | 3004 | narrowband |
| R13 | 48 s | suspect | 42.7 | 2564 | 33% silence |
| R14 | 42 s | suspect | 45.5 | 2556 | 33% silence |
| R15 | 15 s | suspect | 63.0 | 2599 | short institutional visio |
| R16 | 32 s | degrade | n/a | 3200 | **clipping detected** |
| R17 | 60 s | suspect | 45.1 | 3499 | project review |
| R18 | 60 s | suspect | 38.5 | 3121 | project review, low SNR |

Every single window is **narrowband** (99% of energy under ~3.5 kHz): this is what
real meeting audio looks like, and it is far from LibriSpeech.

## Engines and protocol

| Engine | Model | Licence | Serving |
|---|---|---|---|
| `cohere` | Cohere labs ASR (production default) | CC-BY-NC (gated) | in-process, turn-level chunking |
| `whisper` | Whisper large-v3 (faster-whisper) | MIT | in-process, turn-level chunking |
| `granite` | IBM Granite Speech 4.1 2B (experimental) | Apache-2.0 | in-process, turn-level chunking |
| `voxtral` | Mistral Voxtral Mini 3B (experimental) | Apache-2.0 | in-process, turn-level chunking |

All runs: full TranscrIA pipeline (preflight → pyannote diarization → turn-chunked
STT), same GPU pool, deliverable language pinned to French, no session lexicon
(raw engine quality). Matrix runner: `scripts/bench_audio.py --matrix stt`.

## Results — Set L (vs human reference)

8 windows × 4 engines, French pinned, no lexicon. Mean over the 8 windows:

| Engine | WER ↓ | CER ↓ | Word ratio | EN-drift ratio | Mean time/window |
|---|---:|---:|---:|---:|---:|
| **voxtral Mini 3B** | **0.427** | **0.203** | 0.97 | ~0.000 | 130 s |
| whisper large-v3 | 0.437 | 0.204 | 0.96 | 0.000 | 112 s |
| cohere | 0.460 | 0.211 | 1.02 | 0.000 | **84 s** |
| granite 4.1 2B | 0.643 | 0.376 | **0.79** | 0.02–0.07 | 143 s |

Reading the numbers:

- **Absolute WER around 0.43 against an edited human transcript is normal** for
  verbatim ASR on this material: the reference removes fillers, restarts and
  rephrases — every disfluency the engine faithfully writes counts as an "error".
  Compare engines, not absolutes. Best window (clear Q&A): voxtral 0.227 /
  whisper 0.222 / cohere 0.253. Worst (densest): ~0.47–0.67 across engines.
- **Voxtral takes the ground-truth lead on its first day** — best mean WER and
  CER of the four, complete sentences on the densest window, and the domain
  acronyms that granite corrupted are correct in our reading. An Apache-2.0,
  non-gated model from a French lab leading on French meetings is exactly the
  kind of result this corpus was built to detect.
- **Granite's word ratio of 0.79 is the real story**: it silently *drops* about a
  fifth of the words, cutting turns mid-sentence on dense passages. We tested the
  hypothesis that our anti-hallucination generation budget caused it (raising the
  cap 8 → 14 tokens/s): **no change** (WER 0.668 → 0.668 on the densest window).
  The truncation is model-side (early stop on dense narrowband French), not a
  pipeline artifact.
- Granite also keeps a small but non-zero **EN-drift ratio** (2–7 % of unambiguous
  English function words in a French meeting): it occasionally slips into English
  — and once into Spanish — on hard turns. The two other engines, with the
  language pinned, sit at exactly 0.000.

## Results — Set R (LLM judge, median of 3 runs + deterministic signals)

18 windows × 4 engines; each window judged by the arbitration LLM **3 times**,
we aggregate the medians (4 of 72 scores had a run-to-run spread ≥ 2 points —
the median makes the judge usable).

| Engine | Judge score (median of medians, /10) | Words produced | EN-drift | Near-empty windows |
|---|---:|---:|---:|---:|
| whisper large-v3 | 7.6 | 2382 | 0.000 | 1 |
| cohere | 7.5 | 2473 | 0.000 | 1 |
| voxtral Mini 3B | 7.1 | 2378 | 0.001 | 0 |
| granite 4.1 2B | 3.2 | 1920 | 0.059 | 1 |

The top three sit **within the judge's own noise band**: adding a fourth
candidate to the same prompt shifted cohere/whisper by ±0.5 and even swapped
their order relative to the 3-candidate run. Treat "7.6 vs 7.1" as a tie and
granite's 3.2 as the only significant gap.

The "near-empty" window is the same for all three: a 60-second slice whose 99 %
bandwidth is ~500 Hz (essentially rumble). The interesting part is **how** each
engine fails there — see below.

### Where this sits next to our other results

On a *clean bilingual* recording with a session lexicon, granite previously
produced the best raw transcript of the incumbents (it fixed a critical domain term
at the source via its prompt-based keyword biasing, which the logit-boost
biasing of the default engine failed to fix). On *real narrowband meetings
without a lexicon*, granite is clearly behind. Both results are true: **engine
rankings are corpus-dependent**, which is exactly why this benchmark exists.

## What the scores do NOT tell you (human reading)

We read the final SRT files of every suspicious window against the reference.
Error classes actually observed (described abstractly — no verbatim content
from private meetings is reproduced here):

1. **Name hallucination** (granite): a plausible French first name inserted
   where nobody said one. For meeting minutes this is the most dangerous error
   class there is — a WER metric counts it as ~2 errors, a reader may count it
   as an accusation.
2. **Domain-acronym corruption** (all engines, granite most): three-letter
   administrative acronyms consistently rendered as near-miss variants. This is
   precisely the class our session-lexicon biasing targets; the benchmark ran
   without lexicon to measure raw engines.
3. **Language-drift fabrication** (granite): on near-silent/pathological audio,
   whole invented sentences in English and Spanish. cohere and whisper produced
   a single polite word and stopped — honest emptiness beats fluent invention.
   Voxtral sits in between: it fills the same window with short generic French
   phrases — benign, but still words nobody said.
4. **Plausible-sentence invention** (cohere): a fluent, contextually plausible
   French sentence with no acoustic support. Harder to spot than gibberish.
5. **Idiom competition**: on one turn, the experimental engine got a French
   idiom right where the production engine substituted a similar-sounding but
   wrong noun. Rankings are not uniform even within a single window.
6. **Attribution blindness**: text-only WER ignores who said what. One engine
   attributed nearly a whole multi-speaker window to a single speaker while
   scoring a *better* WER than its rivals on that window. cpWER-style scoring
   is future work.
7. **The reference lies a little too**: our reading found transcription errors
   in the professional human reference itself (a similar-sounding verb
   substituted for the correct one). Ground truth is a proxy.

## Bonus round — C++ runtimes and unified models (served, whole-window protocol)

We also pointed the harness at engines served by external runtimes:
[audio.cpp](https://github.com/0xShug0/audio.cpp) (a young ggml-based C++ audio
engine — think "llama.cpp for audio"),
[parakeet.cpp](https://github.com/mudler/parakeet.cpp) (ggml inference for
NVIDIA's Parakeet/Nemotron families, by the LocalAI author — CLI, C API and an
OpenAI-compatible server), [Kroko-ASR](https://huggingface.co/Banafo/Kroko-ASR)
(per-language streaming Zipformer transducers on sherpa-onnx; CC-BY-SA community
models, commercial tiers exist), and Microsoft's
[VibeVoice-ASR](https://huggingface.co/microsoft/VibeVoice-ASR) (9B, MIT, joint
speaker+timestamp+text) via its official vLLM plugin. **Protocol difference,
stated plainly:** these engines transcribed each 5-minute window in one pass
(HTTP or CLI), while our in-pipeline engines used diarization-turn chunking.
Same reference, same scorer, different serving path.

| Engine (runtime) | Mean WER ↓ | Wall/5-min window | GPU footprint | Notes |
|---|---:|---:|---|---|
| Qwen3-ASR-0.6B (audio.cpp, CUDA) | 0.56 *(0.42 excl. one window)* | **7–10 s** | one 24 GB card, small | over-generated ×2 on one chaotic multi-speaker opening; healthy elsewhere |
| Nemotron 3.5 ASR 0.6B (audio.cpp) | 0.51 *(7/8 windows)* | **1–3 s** | one card, small | needs a server restart per request today (session-reuse bug we reported); one window came back empty even so |
| Nemotron 3.5 ASR 0.6B (parakeet.cpp, f16 GGUF, `--lang fr`) | **0.49** *(8/8, zero empty)* | **7–8 s** (CLI, incl. model load) | one card, ~1.4 GB weights | same model, different runtime: no session bug (3 identical back-to-back server replies), no empty windows, 0 % EN drift — best small-model result of the whole test |
| Parakeet TDT 0.6B v3 (parakeet.cpp, auto language) | 0.93 *(8/8, heavy truncation)* | 5 s | one card, small | its automatic language detection drifted to English on ~all windows (11–36 % EN function words) and there is no way to force a language on this model — unusable on narrowband French |
| Kroko-ASR FR Community 128 (sherpa-onnx, **CPU only**, 8 threads) | **0.43** *(8/8, zero empty)* | **10 s** | **no GPU at all**, 155 MB weights | French-dedicated streaming Zipformer; punctuated, cased output; best single-window score of the entire test (0.20); the low-latency FR-64 variant scores the same (0.43) |
| NVIDIA Audex 30B-A3B (official vLLM plugin, TP=8, ASR recipe) | 4.74 *(7/8 windows loop to the token cap)* | 7–50 s | **8 × 24 GB**, 67 GB weights | unified audio LLM, non-commercial license (bench only); with default settings it leaks its English reasoning scratchpad instead of transcribing; with the official recipe (`enable_thinking:false` + placeholder tokens blocked) it produces good French but loops on every real 5-min window except one — on a 30 s excerpt the transcription is excellent, so the failure is length-induced, VibeVoice-style |
| VibeVoice-ASR 9B (vLLM, TP=4) | raw: 4.09 *(8/8 repetition loops)* — with the official auto-recovery client: 0.23–0.48 on 5/8 windows, 3 empty | 20–70 s | 4 × 24 GB | best CER of the whole test on its good windows, plus native speaker+timestamp structure (it under-counted speakers: 3–7 found vs 5–11 in reference) |

What we take away:

- **A 0.6B model at 30–150× real-time within ~5 % WER of our production engine**
  is a real result: the C++-runtime route (tiny VRAM, no Python environment,
  OpenAI-compatible endpoint) is worth watching closely. Since our remote-STT
  client already speaks `/v1/audio/transcriptions`, connecting audio.cpp took
  **zero code** — configuration only.
- **VibeVoice-ASR is the most interesting failure of the test.** Raw serving
  loops on every one of our real narrowband multi-speaker windows (its authors
  ship a recovery client precisely for this); with that client it reaches
  top-tier accuracy on the windows it completes — with speaker attribution and
  timestamps nobody else provides — but it completed only 5 of 8. On short clean
  speech it produced the best transcription of the entire test. One to re-test
  as it matures, not one to deploy today.
- **The most surprising number of the whole benchmark is CPU-only.** A 155 MB
  French-dedicated streaming Zipformer (Kroko-ASR Community, CC-BY-SA) lands at
  0.43 mean WER on 8/8 windows — within noise of our 3B-parameter GPU leader
  (0.427) and ahead of whisper large-v3 (0.437) — at 30× real-time on eight CPU
  threads, with punctuation and casing, zero English drift, zero loops. A
  language-dedicated small model beating language-generalist giants on hard
  real-world audio is a lesson in itself. Caveats: French-only weights (one
  model per language), CC-BY-SA copyleft on the community weights, and a
  vendor whose better models are the paid tier — but as a no-GPU option it
  instantly obsoletes everything else we tested in that class.
- **Same model, two runtimes — the runtime is part of the result.** Nemotron
  3.5 ASR 0.6B scored 0.51 with per-request restarts and an empty window under
  one runtime, and a clean 0.49 on 8/8 with stable repeated serving under
  another (parakeet.cpp). The session-reuse bug we chased is therefore a
  *runtime* bug, not a model defect — benchmark the pair, never the model alone.
- **Automatic language detection is the model-side twin of our trap #1.** The
  multilingual Parakeet TDT v3 silently drifted to English on narrowband French
  meetings (WER 0.93), and unlike our pipeline trap there is no config fix:
  the model exposes no language forcing. Prompt-conditioned models (Nemotron
  `--lang fr`) or explicit language pinning are the only safe options on
  real-world audio.
- **Unified audio LLMs keep failing the same exam.** Audex 30B-A3B is the
  second joint audio-text giant (after VibeVoice-ASR) that transcribes
  clip-sized excerpts beautifully and then loops without recovery on real
  5-minute meeting windows. Both also need their vendor's exact client recipe
  to behave at all (thinking disabled, special tokens blocked, temperature
  escalation…). Until one of them survives a full window, a 155 MB dedicated
  transducer remains the better meeting engine — by three orders of magnitude
  less compute.
- **Deployment traps collected on the way** (all reproducible): a hardcoded
  `max_tokens` in the vendor's own test client silently returning empty results
  on capped-context servers; the audio encoder allocating *outside* vLLM's
  memory budget (OOM at 92 % utilization on 24 GB cards); and a session-reuse
  bug making a streaming ASR model return empty text from the second request on.
  Young runtimes are qualified with the same harness — that is the point of
  having one.

## Traps we fell into (so you don't)

1. **The silent translation benchmark.** Our first Set-L run produced WER ≈ 0.95
   for two engines. Cause: the pipeline resolves the deliverable language from
   the *job owner's UI locale* when no explicit language is set — the bench
   admin's UI was in English, so two engines dutifully **translated** the French
   meeting to English, and the scoring script compared that to a French
   reference without complaint. Fixes: the E2E runner now always pins
   `meeting_context.language`, and the scorer gained an **EN-drift column**
   (ratio of unambiguous English function words) that makes any language slip
   visible at a glance. That column is how you catch this in one look instead
   of one afternoon.
2. **The single-run LLM judge.** The same judge, same prompt, same SRTs can
   reorder engines between two runs. `bench_eval.py --runs 3` now reports the
   median and the min–max spread per engine.
3. **Benchmark scripts age silently.** Our evaluation script only recognised
   old combo-ID patterns and skipped new matrices without failing; its prompt
   file had been archived while the script still referenced it. Both fixed —
   but the lesson is: a bench that runs and says nothing is worse than one
   that crashes.
4. **Narrowband is not doom; pathological bandwidth is.** Every window of the
   corpus is narrowband (≤ 3.5 kHz) and the engines cope. Below ~1 kHz of
   effective bandwidth, they diverge wildly — and the *dangerous* failure mode
   is fluent fabrication, not silence. Our acoustic preflight flags these
   windows before any GPU time is spent, which is the whole point of a
   preflight.

## Limits

- One meeting family per window set; French only; 3 engines.
- WER against an edited human transcript over-penalises verbatim engines
  (fillers, restarts) — treat deltas, not absolutes.
- LLM-as-judge has real run-to-run variance; we report the **median of
  several runs with the spread**, and we read the transcripts ourselves.
- Speaker attribution quality (who said what) is NOT captured by text-only
  WER; a cpWER-style metric is future work.
