# manual-rl

Results and the campaign record are in [`RESULTS.md`](RESULTS.md); analysis scripts in [`interp/`](interp/README.md).

Steer a model's rollouts token by token, then train on the corrections.

1. Prompt the model with a problem.
2. It generates one token at a time. At every token a **decider** can accept the model's pick, **force** different tokens, **rewind** to an earlier position and force from there, or stop.
3. When generation ends, a **judge** decides: done (satisfactory), revise (rewind and continue), or abandon.
4. Only once a rollout is done does it go to training. Each round is `episodes_per_round` done rollouts, then one gradient update. Repeat.

Every correction is recorded as a **fork**: at position `pos`, what the model produced (`rejected`) against what it was made to produce (`forced`). After a rewind, `rejected` is the whole stretch the rewind threw away. Forks behind a later rewind are dropped, so every fork's prefix is part of the final rollout.

The decision rules themselves are not built. This crate is the machinery around them.

## Deciders and judges

Two traits, in `src/rollout.rs`:

```rust
trait Decider { fn decide(&mut self, v: &StepView) -> Result<Decision>; }   // Accept | Force(toks) | Rewind{to, force} | Stop
trait Judge   { fn judge(&mut self, v: &RolloutView) -> Result<Verdict>; }  // Done | Abandon | Revise{to, force}
```

What each one sees:
- `StepView`: the prompt (with any extra fields from the prompt file), the completion so far, the model's drawn token, its top-k alternatives, the full probability vector, the forks so far, and encode/decode helpers.
- `RolloutView`: the completion, why generation ended, the forks so far, and how many revisions have happened.

There are three ways to plug in a rule:
- **In Rust:** implement the trait, or wrap a closure in `DecideFn` / `JudgeFn`.
- **Out of process:** `external::External` speaks JSON lines over a child's stdin/stdout. The protocol is documented in `src/external.rs`. A human terminal UI, an LLM client, or a scripted rule in any language can sit behind it. Give the decider and the judge the same command and one process serves both roles.
- **Combined:** `compose.rs` has
  - `FirstOf`: the first decider that intervenes wins, so cheap rules go before an LLM or a person;
  - `AllOf`: every judge must pass the rollout;
  - `Gate`: consult a decider only where a condition holds, e.g. low model confidence;
  - `Script` / `ScriptJudge`: replay a fixed list of decisions or verdicts.

`AcceptAll` and `AcceptAtEnd` are the no-intervention baselines.

## The update (`src/train.rs`)

- **Pairs** (`pair_weight`, default 1): DPO on every fork. The shared prefix is `prompt + completion[..pos]`, the chosen side is the forced tokens, and the rejected side is the model's own continuation. The reference policy is the one that produced the round. This is the GRPO analogue: what it did against what it should have done.
- **SFT** (`sft_weight`, default 0): mean token log-likelihood of each done rollout. This is the "just upweight the good answers" option.
- **Forced** (`forced_weight`, default 0): hard cross-entropy on the forced tokens only, with nothing pushed down ("up only").

Both terms can be on at once.

Memory stays at one sequence. Each minibatch first measures the pair margins without gradients, then backpropagates each sequence on its own, weighted by its exact DPO coefficient `β·σ(−margin)`.

**Which weights move** (`[adapt]`):
- `mode = "lora"`: rank, alpha, and targets. The default targets are all seven projections.
- `mode = "full"`: every weight, from f32 master copies.

The optimizer is AdamW with global-norm clipping.

## Running

```sh
cargo build --release --features cuda                      # CUDA_COMPUTE_CAP=90 on the H200
manual-rl run run.toml                                     # collect → update → checkpoint loop
manual-rl train run.toml --episodes E.jsonl --out DIR      # one update from recorded episodes
manual-rl generate run.toml "prompt"                       # one unsteered sample
manual-rl score run.toml "raw text"                        # per-token log-probs (both paths)
manual-rl ui run.toml [--addr 127.0.0.1:8765]              # steer by hand from a desktop app
```

### Steering by hand (`ui` + `tools/steer_app.py`)

`manual-rl ui` loads the model and serves a small JSON API (`src/serve.rs`). `tools/steer_app.py` is a desktop window for it (Tkinter, standard library only) that runs on your own machine through an ssh tunnel:

```sh
ssh -N -L 8765:localhost:8765 mason@HOST     # tunnel
python3 tools/steer_app.py                   # window
```

- Pick a problem and the model answers it. Low-probability tokens are tinted orange.
- Click a token to see the sampler's probabilities there. Clicking another candidate forks the answer at that token and regenerates the rest. Clicking the token already there reinforces it. Unselect restores exactly what was there before, including picks made later on the old continuation.
- **Apply gradient** trains on every pick across problems: each pick is a one-token fork. The picks are appended to `episodes.jsonl` (so `train` can replay them under other settings), and the app checkpoints and regenerates. With `forced_weight = 1` and `pair_weight = 0` this is "up only": hard CE on the picked tokens.
- A reinforcement is a fork whose forced and rejected tokens are equal. Its DPO term is exactly zero.

`src/ui.html` is a browser version of the same client, served at `/`.

`train` replays the same recorded corrections through different settings: LoRA vs full, DPO vs SFT, other learning rates. Human corrections are the expensive part, so this lets you answer the open questions without collecting new ones.

Minimal `run.toml`:

```toml
model = "/path/to/hf-model-dir"        # Qwen2 / Qwen3 / Llama / Mistral, safetensors
run_dir = "runs/first"
prompts = "prompts.jsonl"              # {"id": .., "prompt": .., any other fields}
episodes_per_round = 1                 # update after every done rollout
# template = "<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n"   (default)
# dtype = "bf16"                       # or "f32"

[episode.sampling]
temperature = 1.0
max_new_tokens = 1024

[adapt]
mode = "lora"                          # or "full"

[update]
beta = 0.1
sft_weight = 0.0
# lr = 5e-5                            # default: 5e-5 LoRA, 1e-6 full (untuned)

[decider]
kind = "external"
command = ["python3", "my_ui.py"]

[judge]
kind = "external"
command = ["python3", "my_ui.py"]      # same command: one process serves both
```

## What a run writes, and resuming after preemption

- `episodes.jsonl`: every episode, flushed as it ends and tagged with its round.
- `metrics.jsonl`: one line per update with pair loss, margin before and after, the fraction of pairs that improved, SFT log-likelihood, and gradient norm.
- `ckpt-NNNNN/`: written atomically, with `LATEST` pointing at the newest.
  - For resuming: f32 masters and Adam state.
  - For use elsewhere: a PEFT adapter under LoRA, or a HuggingFace model directory under full.

Rerunning the same command after a preemption restores the newest checkpoint and picks up the episodes already collected for the round in flight. No steered rollout is lost.

## Checked (2026-11-06, H200, Qwen2.5-0.5B-Instruct)

- **Numerics:** in f32, both forward paths match HuggingFace transformers to within 1e-4 (training path) and 8e-4 (sampling path) nats/token. In bf16, both ours and HF's own bf16 drift up to ~0.7 nats/token from f32 on this small model; that is bf16 rounding, not a bug.
- **Adapter export:** a saved adapter loaded with HF PEFT gives the same log-probs as our model, within 8e-5.
- **End-to-end:** a scripted decider (`tools/smoke_decider.py`, a test fixture) forced " banana" mid-answer. Under LoRA the pair margin went from −29.9 to +5.6 over 4 rounds, and every update raised every pair's margin.
- **Full mode and resume:** full-weight mode trains. Killing a run between its update and its checkpoint, then restarting, retrains that round without collecting any episode twice.
- **Tests:** `cargo test --release` runs 12 tests on a tiny random model on CPU. They cover cache vs full-sequence agreement (including truncation), checkpointed gradients vs whole-graph autograd, forcing, rewinds, judge revisions, LoRA and full training, SFT-only training, checkpoint round-trip, and the external protocol.

## Limits

- **Batch size 1:** sampling is one rollout at a time (75 tok/s on the 0.5B model). That's fine with a person or an LLM in the loop, but slow for a scripted decider over many prompts.
- **Training memory:** gradients are checkpointed by layer, so only one layer's activations are alive at a time. Inside that layer, attention is naive (no flash attention), so memory still grows with the square of sequence length. On the H200 with Qwen2.5-7B LoRA, a training pass fits 4,096 tokens (prompt + completion) and runs out of memory at 8,192. A rollout longer than the limit fails at the update; the episode itself is kept on disk.
- **Full-weight checkpoints are large:** 6.5 GB per checkpoint for 0.5B, because f32 masters and Adam state are saved for resuming. `keep_checkpoints` bounds disk use.
- **Untuned defaults:** the learning rates and β are placeholders.
