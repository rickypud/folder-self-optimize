# Folder Self Optimize

一個給 `Codex` 用的封閉式自我優化器。

白話版：你丟一個資料夾給它，它先把這個資料夾「鎖住」，然後每次只做一小輪修改。  
每輪都會：

1. 在影子工作區改 code
2. 跑你指定的測試
3. 跑你指定的評分
4. 比現在版本好就保留
5. 沒變好就整輪丟掉

所以它不是「放任 AI 亂改」，而是「讓 AI 在籠子裡做小步優化」。

靈感來自 `karpathy/autoresearch`，但這個版本不是只改單一檔案，而是能鎖整個資料夾。

## 它適合做什麼

- 優化既有程式碼
- 清理重複與死碼
- 在固定測試和固定分數下做小步迭代
- 當你的 Codex skill 使用

## 它不適合做什麼

- 幫你從 0 生一個大專案
- 沒有測試也沒有評分，卻想要它自動變神
- 放給它隨便加檔案、加依賴、改架構

## 它到底怎麼保護你

- 不准新增檔案
- 不准刪除檔案
- 不准偷改常見依賴檔和控制檔
- 不准默默吸收你手動改出來的 drift
- 真正目錄預設不直接改
- 每輪先在影子工作區驗證
- 只有 `keep` 才寫回真實資料夾
- 如果 apply 中途 crash，下次啟動先恢復
- 同一個資料夾同時只允許一個 optimizer 在跑

## 先決條件

- Python 3.10+
- 本機已安裝 `codex`
- `codex login` 已完成
- 你手上有一個「現成資料夾」
- 你至少有一個像樣的 `--verify`
- 最好再有一個 `--metric-command`

## 3 分鐘上手

### 1. 先看目前狀態

```bash
python3 scripts/folder_self_optimize.py status /path/to/your/project
```

### 2. 先只看它準備怎麼改

```bash
python3 scripts/folder_self_optimize.py prompt /path/to/your/project
```

### 3. 跑 3 輪最基本閉環

```bash
python3 scripts/folder_self_optimize.py run /path/to/your/project \
  --verify "pytest -q" \
  --iterations 3
```

這樣的效果是：
- 測試要過
- 結構要更簡單
- 沒變好就 rollback

## 真正有用的跑法

如果你只給測試，它只會學會：

`通過測試 + 盡量更短更簡單`

這通常不夠。

真正好的跑法是再加一個評分命令：

```bash
python3 scripts/folder_self_optimize.py run /path/to/your/project \
  --verify "pytest -q" \
  --metric-command "python3 eval_candidate.py" \
  --iterations 5
```

你的 `eval_candidate.py` 可以輸出：

```json
{"score": 0.82}
```

如果分數越低越好：

```bash
python3 scripts/folder_self_optimize.py run /path/to/your/project \
  --verify "pytest -q" \
  --metric-command "python3 eval_candidate.py" \
  --metric-direction lower-is-better \
  --iterations 5
```

## 更強的評分寫法：可以直接否決

如果你不只要分數，還想要「某些條件一踩線就直接淘汰」，輸出 JSON：

```json
{
  "score": 0.82,
  "pass": false,
  "reason": "latency regression"
}
```

或：

```json
{
  "score": 0.82,
  "constraints": [
    {"name": "latency", "pass": false, "reason": "p95 got worse"},
    {"name": "cost", "pass": true}
  ]
}
```

這樣它就不會只看單一分數，而是會被硬性條件攔下來。

## 常用命令

### `status`

看目前鎖住了哪些檔、基線分數是多少、資料夾有沒有 drift。

```bash
python3 scripts/folder_self_optimize.py status /path/to/your/project
```

### `prompt`

只印出下一輪 mutation prompt，不真的執行。

```bash
python3 scripts/folder_self_optimize.py prompt /path/to/your/project
```

### `run`

真的跑閉環。

```bash
python3 scripts/folder_self_optimize.py run /path/to/your/project \
  --verify "pytest -q" \
  --metric-command "python3 eval_candidate.py" \
  --iterations 5
```

### `restore`

把資料夾還原回目前記錄的 baseline。

```bash
python3 scripts/folder_self_optimize.py restore /path/to/your/project
```

## 如果它拒絕跑，通常是這幾種原因

### 1. 你的資料夾已經 drift 了

意思是：你手動改過、或上次殘留了東西，現在和基線不一致。

解法只有兩種：

```bash
python3 scripts/folder_self_optimize.py restore /path/to/your/project
```

或你很確定現在這版要當新起點：

```bash
python3 scripts/folder_self_optimize.py run /path/to/your/project \
  --verify "pytest -q" \
  --rebaseline
```

### 2. 你選的資料夾太大

不要一上來就鎖整個 monorepo。  
先鎖最小可工作的子目錄。

### 3. 你的測試根本不代表品質

如果測試太弱，它就只會學會鑽測試漏洞。  
這不是工具壞掉，是你的 gate 太弱。

## 建議你這樣用

### 最保守

```bash
python3 scripts/folder_self_optimize.py prompt /path/to/project
```

先看 prompt，自己決定要不要放行。

### 一般安全版

```bash
python3 scripts/folder_self_optimize.py run /path/to/project \
  --verify "pytest -q" \
  --iterations 3
```

### 生產級

```bash
python3 scripts/folder_self_optimize.py run /path/to/project \
  --verify "pytest -q" \
  --verify "python3 smoke_test.py" \
  --metric-command "python3 score_candidate.py" \
  --iterations 5 \
  --touch-limit 2 \
  --net-line-limit 80
```

## 當成 Codex Skill 安裝

如果你要把它加進 `~/.codex/skills`：

```bash
mkdir -p ~/.codex/skills
ln -s "$(pwd)" ~/.codex/skills/folder-self-optimize
```

之後你就可以在 Codex 裡叫它：

```text
Use $folder-self-optimize to lock this folder and run a bounded self-optimization loop.
```

## Repo 結構

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

## 最重要的一句

它不是自動賺錢機。  
它只是把 AI 的改動壓進一個更硬、更可回滾、更可評估的閉環裡。

你給的 `verify` 和 `metric` 越真實，它就越像一個有用的 agentic loop。
