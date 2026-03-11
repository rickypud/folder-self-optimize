# Folder Self Optimize

`Folder Self Optimize` is a closed-loop optimizer for `Codex`.

Give it an existing folder, lock the file set, define a verification gate, optionally define a scoring command, and let it iterate in bounded keep-or-discard loops.

中文補充：它不是放任 AI 亂改，而是把 AI 關進一個小籠子裡，一輪一輪試，變好才保留，沒變好就回退。

## What It Does

For each iteration, it will:

1. Create a shadow workspace from the current accepted baseline.
2. Ask Codex to make one bounded mutation.
3. Reject changes that add files, delete files, touch protected files, or exceed diff limits.
4. Run your verification commands in another shadow workspace.
5. Run your scoring command if you provided one.
6. Keep the candidate only if it is strictly better.
7. Roll back everything else automatically.

The real target folder stays untouched until a candidate is accepted.

中文補充：真正的資料夾預設不直接被測試和亂改，只有 `keep` 的版本才會寫回去。

## What It Is Good For

- Iterative cleanup of an existing code folder
- Safe-ish autonomous improvement under a fixed test gate
- Small, repeated refactors with rollback
- Codex skill workflows

## What It Is Not Good For

- Generating an entire large project from scratch
- Optimizing without meaningful tests or metrics
- Unbounded architectural exploration
- Production governance by itself

中文補充：如果你沒有像樣的 `verify` 和 `metric`，它最多只會學會「過測試 + 變短」，不會神奇地學會你的真正目標。

## Core Safety Model

- No new files
- No deleted files
- Protected dependency and control files are blocked by default
- Drift is not silently absorbed
- One optimizer process per target folder
- Crash-safe apply markers
- Shadow-workspace verification
- Automatic keep or rollback

## Requirements

- Python 3.10+
- `codex` installed locally
- `codex login` already completed
- An existing target folder
- At least one useful `--verify` command
- Preferably a real `--metric-command`

## Quick Start

### 1. Inspect the current lock state

```bash
python3 scripts/folder_self_optimize.py status /path/to/project
```

### 2. Preview the next mutation prompt

```bash
python3 scripts/folder_self_optimize.py prompt /path/to/project
```

### 3. Run a small loop

```bash
python3 scripts/folder_self_optimize.py run /path/to/project \
  --verify "pytest -q" \
  --iterations 3
```

This means:

- tests must pass
- the code should get simpler
- non-improving candidates are discarded

中文補充：這是最基本版閉環，但還不夠強。真正有用的版本通常要再加一個評分命令。

## Real-World Usage

### Basic loop

```bash
python3 scripts/folder_self_optimize.py run /path/to/project \
  --verify "pytest -q" \
  --iterations 5
```

### Better loop with a custom metric

```bash
python3 scripts/folder_self_optimize.py run /path/to/project \
  --verify "pytest -q" \
  --metric-command "python3 eval_candidate.py" \
  --iterations 5
```

### Lower-is-better metric

```bash
python3 scripts/folder_self_optimize.py run /path/to/project \
  --verify "pytest -q" \
  --metric-command "python3 eval_candidate.py" \
  --metric-direction lower-is-better \
  --iterations 5
```

### Stop early when the loop is clearly stuck

```bash
python3 scripts/folder_self_optimize.py run /path/to/project \
  --verify "pytest -q" \
  --metric-command "python3 eval_candidate.py" \
  --iterations 20 \
  --max-no-improve-streak 5
```

中文補充：`--max-no-improve-streak` 的意思是「連續 5 輪都沒進步就收工」，避免空轉。

## Metric Output Formats

### Plain numeric output

```json
0.82
```

### JSON with a score

```json
{"score": 0.82}
```

### JSON with hard veto

```json
{
  "score": 0.82,
  "pass": false,
  "reason": "latency regression"
}
```

### JSON with multi-objective constraints

```json
{
  "score": 0.82,
  "constraints": [
    {"name": "latency", "pass": false, "reason": "p95 got worse"},
    {"name": "cost", "pass": true}
  ]
}
```

If any veto or failed constraint appears, the candidate is rejected even if the score looks better.

中文補充：這就是避免 reward hacking 的關鍵。不要只給單一分數，能 veto 的條件越多越穩。

## Commands

### `status`

Show the current baseline, verification setup, and whether the real target folder drifted.

```bash
python3 scripts/folder_self_optimize.py status /path/to/project
```

### `prompt`

Print the next bounded Codex mutation prompt without actually running Codex.

```bash
python3 scripts/folder_self_optimize.py prompt /path/to/project
```

### `run`

Run the full closed loop.

```bash
python3 scripts/folder_self_optimize.py run /path/to/project \
  --verify "pytest -q" \
  --metric-command "python3 eval_candidate.py" \
  --iterations 5
```

### `restore`

Restore the target folder back to the saved baseline.

```bash
python3 scripts/folder_self_optimize.py restore /path/to/project
```

### `report`

Render a human-readable summary of the current baseline and recent loop history.

```bash
python3 scripts/folder_self_optimize.py report /path/to/project
```

Write the report to a file:

```bash
python3 scripts/folder_self_optimize.py report /path/to/project \
  --output /tmp/fso-report.md
```

中文補充：`report` 是給人看的，不是給模型看的。你可以很快知道它最近到底是在進步，還是在白跑。

## Common Failure Cases

### 1. The target folder drifted

You changed files manually, or a previous run left the folder different from the stored baseline.

Fix it by restoring:

```bash
python3 scripts/folder_self_optimize.py restore /path/to/project
```

Or explicitly accept the current tree as the new starting point:

```bash
python3 scripts/folder_self_optimize.py run /path/to/project \
  --verify "pytest -q" \
  --rebaseline
```

### 2. The target folder is too large

Do not start with an entire monorepo if you can avoid it. Lock the smallest useful subtree first.

### 3. The tests do not represent quality

If your tests are weak, the loop will optimize toward weak tests.

中文補充：不是工具壞，是 gate 太弱。

## Recommended Operating Modes

### Conservative

```bash
python3 scripts/folder_self_optimize.py prompt /path/to/project
```

Preview the prompt first.

### Safe default

```bash
python3 scripts/folder_self_optimize.py run /path/to/project \
  --verify "pytest -q" \
  --iterations 3
```

### Stronger production-style loop

```bash
python3 scripts/folder_self_optimize.py run /path/to/project \
  --verify "pytest -q" \
  --verify "python3 smoke_test.py" \
  --metric-command "python3 score_candidate.py" \
  --iterations 8 \
  --touch-limit 2 \
  --net-line-limit 80 \
  --max-no-improve-streak 4
```

## Use It as a Codex Skill

```bash
mkdir -p ~/.codex/skills
ln -s "$(pwd)" ~/.codex/skills/folder-self-optimize
```

Then invoke it from Codex with:

```text
Use $folder-self-optimize to lock this folder and run a bounded self-optimization loop.
```

## Repository Layout

```text
.
├── README.md
├── LICENSE
├── SKILL.md
├── agents/
│   └── openai.yaml
└── scripts/
    └── folder_self_optimize.py
```

## Origin

This project is inspired by `karpathy/autoresearch`, but adapted from:

- one editable file
- one metric

to:

- one locked folder
- multiple verification commands
- optional multi-objective scoring
- crash-safe keep or rollback semantics

## The One Sentence Version

This is not a magic money machine.  
It is a stricter, more recoverable, more measurable loop for AI-driven code mutation.

中文總結：它不是保證成功，它只是把 AI 改 code 這件事，收斂成一個比較不容易失控的閉環。
