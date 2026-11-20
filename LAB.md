# Legal work: Harvey's LAB

A second task, run once so far: legal work product from Harvey's Legal Agent Benchmark (LAB,
github.com/harveyai/harvey-labs, MIT, commit `7f9b1ee`). The judge here is the same kind of model as in the MATH run;
a separate model, the grader, scores the student's answers against each task's rubric.

## Result

One round, 2026-11-20. Student: Qwen3-30B-A3B-Instruct-2507, 32-bit weights. The judge, Claude Sonnet 5.5, corrected
the student's own answers to 48 pool tasks and kept 41 corrections. The untrained student was trained on them (Adam,
learning rate 4e-7, until every forced token passed 50% probability or 10 steps, the end-of-answer token held, a bf16
forward pass over 32-bit weights) and both students answered the 50 validation tasks, one greedy answer each.

| Validation, 50 tasks | Untrained | Trained | Change (paired standard error) |
|---|---|---|---|
| Share of criteria passed | 47.5% | 55.4% | **+7.9** (2.6); 36 tasks up, 10 down |

The 50 tasks are listed in `validation-tasks.txt`. One of the 50 trained answers repeated itself until the 16,384-token
limit, and a cut-off answer fails every criterion. The untrained student never ran to the limit (0 of 190 answers
checked). The verdict and answer files hold exactly these 50.

## Reproducing

Needs, beyond the MATH setup: a checkout of harvey-labs at `7f9b1ee` (`LAB`, default `$WORK_DIR/harvey-labs`),
pandoc 3.11 (`$LAB/lab_core/sandbox/install_pandoc.sh`), the parser's Python packages (markitdown, pandas, pdfplumber)
in the environment, the student's weights in `$WORK_DIR/models/qwen3-30b-a3b-instruct-2507`, and one node of 8 GPUs of
180 GB.

```bash
sbatch $WORK_DIR/repo/slurm/lab_reproduce.sbatch
```

It builds the tasks and the split, takes the 50 published validation tasks from it, rebuilds the 41
corrections from [`reports/lab/corrections.jsonl.gz`](reports/lab/) (it stops if any prompt does
not come out token for token as it was), trains, answers the 50 tasks, grades them and prints the comparison above.
`BASE=again` answers and grades the untrained student too instead of using its published verdicts.

## The conversations

The 48 conversations were run by `tools/lab_collect.py` (the job: `slurm/lab_session.sbatch`): one student server per
GPU of the node, and a fix-only session (`tools/lab_session.py`) on one pool task each — the same instructions for
every conversation, `experiments/lab/instructions.md`, byte-identical to the first message each was given. Each
conversation keeps its own run directory `w<k>`: `state.json` (its messages and budgets, resumed where a job restarts)
and `agent.jsonl` (its every turn); the corrections they keep all land in one `collected.jsonl`, their fix scores in
`summary.json`. The transcripts above are rendered from those records with `tools/analysis/transcript.py`.

The round's launch settings: judge `anthropic/claude-sonnet-5.5`, temperature 0.3, a reply limit of 64,000 tokens
with a 12,000-token reasoning budget (a reply that came back all reasoning was retried without it), 600 turns a
conversation, a generation budget of 400,000 units (50 a call, one a token written), a training budget of 600,000,000
units and 12 validation looks (a fix-only session trains and validates nothing), no answer checks, no spending cap;
the student served in bf16 over its 32-bit weights, one greedy answer of at most 16,384 tokens; every token put into
an answer held to the probability floor of 0.001. `slurm/lab_session.sbatch` carries all of these as its defaults.

## The data

| File | Contents |
|---|---|
| `corrections.jsonl.gz` | the 41 corrections: each task's id, the corrected answer's tokens and forks; the prompt is rebuilt from the task |
| `validation-tasks.txt` | the 50 validation tasks of the result |
| `untrained-verdicts.jsonl.gz`, `trained-verdicts.jsonl.gz` | the grader's verdict and reasoning for every criterion |
| `trained-answers.jsonl.gz` | the trained student's answers, and how each ended |
| `transcripts/w<k>.md` | each of the 48 correcting conversations, the judge's replies in order: its reasoning, every action (forks, edits, previews of what the student allows, documents read, undos, discards) and what each returned; rendered with `tools/analysis/transcript.py` |
| `transcripts/index.csv` | each conversation's task, its turns, the corrections it kept, and how many of that task's criteria the answer passed before and after its corrections |

In the 48 conversations Sonnet 5.5 took 2,596 turns: 915 previews of what the student allows at a point, 246 edits,
154 forks, 60 continuations, 89 document reads, 24 undos and 35 discards, and kept 41 corrections in 38 of them.
