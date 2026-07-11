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
