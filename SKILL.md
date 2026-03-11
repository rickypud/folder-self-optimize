---
name: folder-self-optimize
description: Lock a target folder or project subtree into a fixed file whitelist and run bounded self-optimization loops with keep-or-discard evaluation, shadow-workspace verification, crash-safe rollback, and optional Codex-driven mutation. Use when a user wants autonomous or semi-autonomous iterative improvement on an existing code folder without letting the agent add files, add dependencies, silently absorb drift, or expand architecture.
---

# Folder Self Optimize

Use `scripts/folder_self_optimize.py` to build a folder-scoped optimization cage inspired by `karpathy/autoresearch`.

The core pattern is the same:
- Fix the editable scope.
- Fix the evaluation gate.
- Run one small mutation at a time.
- Keep only improvements.
- Roll back everything else.

The difference is that this skill generalizes from `one editable file + one metric` to `one locked folder + verify commands + optional custom score`, while keeping the real target directory stable until a candidate is accepted.

## Use It Correctly

Do not treat "tests pass" as the whole objective unless that is truly enough.

Closed-loop self-correction gets close to "agentic loop" behavior only when the acceptance gate is real:
- `--verify` should catch regressions.
- `--metric-command` should score what you actually care about.
- `--metric-direction lower-is-better` is for loss, Brier score, drawdown, latency, error rate.
- No metric command means the script falls back to structural simplification: smaller, flatter, cleaner code that still verifies.
- If your metric needs hard vetoes, return JSON with `pass: false`, `veto: true`, or failing `constraints`.

If you only provide tests, the loop will optimize toward "passes tests and is simpler". That is useful, but it is not the same as optimizing toward profit, calibration, latency, or model quality.

## Workflow

1. Pick the narrowest folder that should evolve.
2. Define the gate.
3. Lock the folder.
4. Run bounded iterations.
5. Keep only scored improvements.
6. Restore immediately on drift or failure.

## Define The Gate

Prefer explicit commands over implied intent.

- `--verify "<command>"` is repeatable.
- `--metric-command "<command>"` must print either:
  - plain numeric output, or
  - JSON with a numeric `score` field
- Optional JSON fields:
  - `pass: false` to hard-fail the candidate
  - `veto: true` to hard-fail the candidate
  - `reason: "..."` to explain the veto
  - `constraints: [{"name": "...", "pass": false, "reason": "..."}]` for multi-objective gates
- Higher score is better unless you set `--metric-direction lower-is-better`.

Examples:

```bash
python3 scripts/folder_self_optimize.py run src/service \
  --verify "pytest -q" \
  --iterations 5
```

```bash
python3 scripts/folder_self_optimize.py run trading_v4 \
  --verify "python3 program_1_feeder.py --once" \
  --verify "python3 program_2_debate.py --once" \
  --verify "python3 program_3_kelly.py --once" \
  --verify "python3 program_4_state_machine.py --status" \
  --metric-command "python3 score_pipeline.py" \
  --iterations 8
```

```bash
python3 scripts/folder_self_optimize.py run train_loop \
  --verify "pytest tests/test_train_loop.py -q" \
  --metric-command "python3 eval_candidate.py" \
  --metric-direction lower-is-better \
  --iterations 10
```

## Commands

`status`
- Inspect the current lock, baseline, verify commands, and score mode.
- Show whether the target drifted from the stored baseline.

`prompt`
- Print the next mutation prompt without running `codex exec`.
- Use this when you want the current Codex session to make the patch manually.

`run`
- Execute the closed loop.
- The script will:
  - save a baseline outside the target folder
  - create a shadow workspace for each mutation
  - generate one bounded prompt per iteration
  - optionally invoke `codex exec`
  - reject new files, deleted files, protected-file edits, and oversized diffs
  - verify and score the candidate in a shadow copy, not the real target folder
  - write back to the real target folder only when the candidate is accepted
  - leave a recovery marker if a crash happens during apply

`restore`
- Put the folder back to the saved baseline immediately.

## Guardrails

- The whitelist is the current file set under the target folder.
- New files and deleted files are rejected.
- Dependency files and common control/config paths are protected by default.
- Each iteration has touched-file and net-new-line limits.
- State lives outside the repo in `~/.codex/state/folder-self-optimize/`.
- The tool refuses to silently absorb target drift. Use `restore` or pass `--rebaseline` explicitly.
- A process lock prevents two optimizer runs from mutating the same folder at once.
- Use `--relock` only when you intentionally want to redefine the allowed file set.
- Use `--rebaseline` when the current folder contents should become the new starting point.

## Recommended Defaults

- Start with `--iterations 3`.
- Keep `--touch-limit` small.
- Keep `--net-line-limit` small.
- Add a custom metric command whenever "better" means more than "passes and simpler".
- Prefer metric JSON with veto-capable constraints for production loops.
- Use `prompt` mode first if you want to inspect the exact mutation brief before letting it run unattended.

## Resource

- Controller: `scripts/folder_self_optimize.py`
