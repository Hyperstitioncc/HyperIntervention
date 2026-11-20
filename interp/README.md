# interp/ — analysis scripts behind RESULTS.md

One-off analyses of a forced-token training update on Qwen2.5-7B (base). Each reads checkpoints and evaluation
outputs by absolute path on the GPU host (`/home/mason/...`); usage is in each docstring. Attention in every
patching script is computed with f32 scores (HF's bf16 "eager" attention flips near-tied tokens) and must
register `eager_mask` for the custom attention function (otherwise transformers passes no causal mask).

| script | question |
|---|---|
| `vcheck.py`, `prescreen.py` | vLLM greedy grading of problem lists (base vs instruct; pre-screen for the editor) |
| `hf_bf16_vs_f32.py`, `attncheck.py` | numerics: candle/HF bf16 vs f32 log-probs; eager vs sdpa at a knife-edge token |
| `diverge.py` | where a vLLM rollout leaves an edited (steered) trajectory |
| `pivot.py` | base-wrong / trained-right test problems: first divergent token, causal two-way swap test |
| `acts.py`, `acts2.py` | raw and scale-invariant comparison of MLP neuron firings, base vs trained |
| `swap_test.py` | attention-pattern / MLP-activation swaps (all positions vs the contested position) |
| `fewneurons.py` | top-k neurons by attribution patched into base; random-noise control as donor |
| `shared.py`, `overlap.py`, `pool.py`, `sim.py`, `pool_test.py` | sharing of important neurons across decisions; pools; what predicts overlap |
| `neuron_q.py` | necessity, uniqueness, general fixed sets, and weights-vs-residual mechanism of the key neurons |
| `resid_decomp.py`, `fixed_heads.py` | the residual change the key neurons read, decomposed into upstream heads/neurons |
| `edit_fit.py`, `edit_save.py`, `edit_write.py`, `edit_cmp.py` | mechanism-guided sparse edits and their full MATH-test evaluation |
| `rankcheck.py`, `roundcheck.py` | spectrum of the update vs its gradient; what bf16 rounding leaves of the update |
