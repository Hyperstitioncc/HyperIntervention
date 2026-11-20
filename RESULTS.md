# manual-rl results (2026-11-06 → 11-09)

Model: Qwen2.5-7B (base) and Qwen2.5-7B-Instruct, MATH (Hendrycks). Editor: DeepSeek v4.1 flash via
OpenRouter, steering token by token with top-20 candidate forks (`tools/llm_editor.py`,
`experiments/math/editor_prompt.md`); the editor sees the reference solution. Training: "up only" hard CE on the
forced tokens (`forced_weight = 1`), dense full weights, whole batch per step, AdamW unless stated. Eval: vLLM
greedy on the full 5000-problem MATH test (+1500 train), paired against the same model's base run, with a
random-direction control of the same Frobenius norm as the trained update (`tools/eval_watch.py`). Grader:
`math_verify` with a TeX-shorthand fix (`\frac\pi 2`) and a canonical-string fallback.

## Instrument bugs found (each changed earlier conclusions)

- **candle bf16 attention scores** (`src/model.rs`): QKᵀ was computed in bf16 before the f32 softmax. bf16
  log-probs were ~15x noisier than HF bf16 (mean |Δlogp| 0.18 vs 0.013 on base; max 3.5). Fixed by f32 scores
  (now 0.0145). Every collection before 11-08 21:50 ran on the noisy model; on base it was "already right" on
  17% of random problems vs 63% in vLLM, and it explained most of the "vLLM leaves the edited trajectory before
  the first edit" finding (110/117).
- **HF eager attention has the same flaw** in bf16: at a knife-edge token base gives p(LM)=0.93 / p(LN)=0.04
  vs f32 0.51 / 0.47. All patching in `interp/` uses f32 scores and registers `eager_mask` (a custom
  attention function otherwise receives no causal mask).
- **bf16 training forward / bf16 export erase most of the update.** Trainable f32 masters are cast to bf16 for
  the forward pass and the export. At lr ~1e-6 the accumulated per-weight change is below half a bf16 step for
  most weights: only 13–18% of entries change, all of them small weights (median |w| 0.0018 vs 0.0107 overall),
  each by ~one bf16 step. Every result below except the f32 A/B is on this quantised update.
- Grader: unboxed base answers are read by their last number ("So, there are 52 Fridays in 2007" → 2007):
  ~35–40 of base's test answers are false negatives; trained models box ~97% and lose this penalty (~+30 of
  the base gains below are this artifact).

## Training results (bf16 update; test vs base, control in parentheses)

- Instruct (selected near-miss / hard / random problems, broken attention): live lr 1e-5 rounds hurt (−68, p≈.004);
  batch runs at lr 1e-6 were within noise (+17, +3, +9 vs controls +11, −17, 0).
- **Base, random problems, fixed attention:** 93 solves / 185 forced tokens: 5 ep +105 (+27), 10 ep +137 (+6).
  410 solves / 686 tokens, lr × epochs sweep:

  | lr \ epochs | 3 | 5 | 10 |
  |---|---|---|---|
  | 5e-7 | +32 | +122 | **+207** |
  | 1e-6 | +145 | **+198** | +60 |
  | 2e-6 | **+214** | +77 | −64 |

  The optimum sits at lr·epochs ≈ 5–6e-6 (≈ +4 points, z ≈ 7.6); controls within ±20. The 93-solve batch at
  matched lr·epochs scores lower (+65…+105 at 5e-6), and both runs move the weights equally (relative change
  7.7e-5 vs 7.3e-5), so the gain from more solves is not a step-size artifact.
- Non-distinction: these runs cannot separate "the edits teach reasoning" from "the edits move base into its
  boxed, exact, step-by-step mode" — base boxes 85% of answers, trained 97% — beyond the grader artifact above.

## Inside the update (93 solves × 10 epochs, bf16; 82 causally pivotal test tokens, 57 with an MLP effect)

Frame: one forward pass at the first token where base and trained diverge on a base-wrong / trained-right test
problem; selection = swapping that token flips the outcome both ways (most such tokens are lexical near-ties:
"To"/"Let", `\(`/`$`). Measured quantity: log p(trained token) − log p(base token).

- Swaps (gap 1.23 nats): MLP activations at all positions carry ~95%; at the contested position only ~68%;
  attention patterns ~70% (not additive — patterns read the changed prefill).
- Per decision a few neurons suffice and are necessary: top-10 of 530k by attribution recover 80% (35/57
  flips), reverting them in trained kills 41/54; a same-norm random donor gets +0.05 vs +0.93. Not unique:
  disjoint rank bands 51–100 or 101–200 also flip ~35 — many push both ways, near cancellation, the top few tip it.
- Key neurons fire differently because the residual they read changed (~100%), not their own gate/up rows (~0%).
  That residual change splits ~evenly between attention heads and earlier neurons, layers 18–27; attention head
  L19 h2 is in 34/57 decisions' top-10 upstream components.
- Neuron sets are output-choice specific: tokens deciding the same pair share 8.3/15 neurons vs 0.5 otherwise.
  Union of top-10 sets: 324 neurons over 57 decisions (233 seen once; Chao1 ≥ ~900). A fixed pool of 300–1000
  matches free choice for choices seen in the pool's half, not for novel choices.
- Sparse reader edits (fit gate/up rows of K target neurons to trained firings) do not stick: on the full test
  (excluding fit problems) 100 neurons −194, 1000 neurons −112, random 1000 neurons −82; the targeted problems
  improve barely above the random edit (15/29, 14/29 vs 10/29 — knife-edge problems flip under any perturbation).
- **The diffuse picture is mostly the optimizer and bf16, not the signal.** Gradient of the 185-token loss:
  9–163 directions hold 90% of its energy per matrix; the bf16 AdamW update needs 1700–2750 (Gaussian:
  1800–2800); cosine(update, −gradient) 0.10–0.30.

## Running / open

- f32 A/B (`batch-base-f32.toml`, `batch-base-f32-sgdm.toml`): f32 training, export and vLLM eval; AdamW lr 1e-6
  vs SGD-momentum lr ∈ {1e-2, 3e-3, 3e-2, 1e-3} on the 410-solve batch, 3 and 5 epochs. Prediction: the f32
  update keeps the gradient's low rank under SGDM, and its test effect concentrates into fewer neurons.
- Scaling with more solves (518 → ~1300 as the key-limit retries land) at the precision/optimizer the A/B picks.
- LoRA A/B arm (replay of the same `collected.jsonl`).
