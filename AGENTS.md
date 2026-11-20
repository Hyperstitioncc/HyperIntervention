# manual-rl — steer-then-train on Qwen2.5-7B (MATH)

Scientific owner: the person running the manual-RL campaign (the user who asked for this tree); the
question it owns is in [`RESULTS.md`](RESULTS.md): does training on a handful of forced tokens — corrections
an editor made to the model's own rollouts — improve held-out math, and what does that update look like inside
the model.

Runtime door: the `manual-rl` binary (`cargo build --release --features cuda`, then `manual-rl ui | train |
run | generate | score`; see [`README.md`](README.md)). Collection is `tools/steer_vllm.py` (vLLM steering server,
the `ui` API) driven by `tools/llm_editor.py`; offline training + evaluation is `tools/batch_train.py`, which
calls `tools/eval_watch.py one`. `interp/` holds the analysis scripts behind the interpretability results; they
read checkpoints and result files by absolute path on the GPU host and are evidence, not a supported runtime.

Verifier: `cargo test --release` (CPU; checks the model against its own math, the loss terms, resume, the LoRA
export). Then rebuild with `--features cuda` — `cargo test --release` overwrites `target/release/manual-rl`
without CUDA. Model numerics were checked against HuggingFace with `tools/hf_compare.py` (f32 exact; bf16 at the
HF bf16 noise floor after the attention-score fix) and `interp/hf_bf16_vs_f32.py`.

Not here: GPU-host launch scripts and queues (they were ad-hoc shell on the host), collected episodes, and
checkpoints. The collected corrections (`collected.jsonl` per editor run) are the durable data; they live with
the host's `~/mathx/` run directories.
