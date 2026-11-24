# ADE 對照：hc、Orca、native

每個角色在 `config.json` 的 `roles.<role>.ade` 指定 worker 跑在哪個 ADE。角色由 `set-role` 建立，沒給的欄位沿用 `kinds.write` 或 `kinds.review` 的預設。`dispatch` 只讀設定檔決定怎麼啟動，Router 不用記。

| | `hc`（herdr-core） | `orca` | `native` |
|---|---|---|---|
| 啟動 | `hc run --bg --outlive-parent` | `orca-ide orchestration worker-start` | Router 用自己 harness 的 subagent 工具 |
| `backend_ref` | hc 位址（如 `t110`） | dispatch id | 無 |
| 推送通知給它 | `hc send <ref> <text>` | `orchestration send --to dispatch:<id>` | 無，subagent 結束時 harness 會通知 Router |
| `watch` 巡檢 | `hc get` + `hc read` + 子行程 CPU | `worker-show` + `worker-read` | 只檢查完成標記 |
| 收尾 | `hc kill` → `hc rm` | `worker-stop` | 無 |
| 可用 CLI | claude、codex、agy | claude、codex、agy（Orca 叫 `antigravity`） | harness 自己的模型 |

## Adaptor

每個 ADE 是 `scripts/sa/adaptors/` 下的一支 adaptor，實作 `base.py` 的 `Adaptor` 介面。`dispatch`、`watch`、`post`、`close-worker` 只呼叫這個介面，不判斷是哪個 ADE。

| 方法／屬性 | 用途 | hc | orca | native |
|---|---|---|---|---|
| `detect_router()` | Router 是否跑在這個 ADE 上，回傳端點 | `HC_TERMINAL_ID` | `ORCA_TERMINAL_HANDLE` | 無 |
| `validate(model, effort)` | 這個 ADE 不接受的組合 | 無 | 有 effort 沒 model | 無 |
| `launch(LaunchRequest)` | 回傳要執行的指令，或給 Router 的 subagent prompt | `hc run …` | `worker-start …` | `native_spawn` |
| `parse_ref(stdout, wid)` | 從啟動輸出取出 `backend_ref` | `address` | dispatch id | 無 |
| `send(ref, text)` | 推送通知 | `hc send` | `orchestration send` | 不支援 |
| `probe(worker)` | 給 `watch` 的狀態、畫面、子行程 | `hc get` + `hc read` + CPU | `worker-show` + `worker-read` | 不支援 |
| `stop(ref)` | 收尾 | `kill` + `rm` | `worker-stop` | 無 |
| `can_push`／`can_probe`／`needs_pinned_model`／`creates_worktree`／`passes_cli_args` | 能力旗標 | 是／是／是／否／是 | 是／是／是／是／否 | 否／否／否／否／否 |

新增 ADE：寫一支 `<名稱>_adaptor.py` 實作這個介面，在 `adaptors/__init__.py` 的 `ADAPTORS` 加一行；Router 也可能跑在上面時，再加進 `ROUTER_ORDER`。`--ade` 的選項和 `set-role` 都會自動接受新名稱。畫面判斷用 `sa/screen.py` 的 `probe_result()`，回傳格式就會和其他 adaptor 一致。

## Router 端點

`init` 依環境變數判斷 Router 在哪裡，寫進 `members.json` 的 `router`。Router 重開或換 terminal 後要再跑一次 `init` 更新端點。

| 環境 | 判斷依據 | 推送方式 | Router 收到的樣子 |
|---|---|---|---|
| hc | `HC_TERMINAL_ID` | `hc send <id> <text>` | 通知原文直接出現在對話中，內含 `read` 指令 |
| Orca | `ORCA_TERMINAL_HANDLE`（`term_<uuid>`） | `orchestration send --to term_<uuid>` | 只會出現一行提示：`You have 1 orchestration message. Run orca orchestration check --terminal term_…`。要跑 `check` 才看得到通知原文，再照原文跑 `read` |
| 都沒有 | | 無推送 | Router 要定期跑 `read --as router`；沒裝 hc 時還要自己排程 `watch` |

- `hc send` 會等該 terminal 裡使用者還沒送出的輸入，最多約 60 秒。
- Orca 的 `check` 不帶參數時，會把讀到的訊息標成已讀。只想看不標已讀，用 `check --peek`。
- 不要用 Orca 的 `terminal send` 對 agent 發訊息，那是給 shell 用的。

## hc

- 執行檔找 `hc`，找不到再找 `herdr-core`。
- worker 一律加 `--outlive-parent`。沒有這個旗標，Router 的 terminal 一關，worker 跟著被殺。
- `config.json` 的 `herdr_space` 預設為 `"router"`：worker 會帶 Router terminal 的 `herdr.space` label，出現在 Router 所在的 Herdr 視窗。不帶 `herdr.tab`，每個 worker 開一個新分頁。要固定別的 space 就填該 space 的 ID；填 `null` 則不加 label。
- Router 在 Orca、worker 用 hc 時，Router 沒有 `herdr.space` 可以跟隨，`"router"` 等同 `null`：worker 照常執行，但不會出現在 Herdr 視窗，只能用 `hc ls`、`hc read` 看。要在視窗裡看到，把 `herdr_space` 填成固定的 space ID。
- claude worker 以 `--add-dir` 取得 store 的寫入權。store 所在目錄沒被 claude 信任過時，worker 會停在「Adding a new directory … one you trust?」提示，watch 會回報 `INTERACTIVE_BLOCK`。預設 store 在 agent-home 底下，只要 agent-home 已信任就不會出現；用 `SUPERAGENT_HOME` 改到別處時，先在那個目錄開一次 claude 並信任它。
- 讀畫面用 `hc read`，不要用 `hc attach`。
- 卡在權限或信任提示時，不要用 `hc send --force` 代按。

## Orca

- 執行檔順序：`ORCA_CLI_COMMAND` → `orca-dev`（有 `ORCA_DEV_REPO_ROOT` 時）→ Linux 上不在 Orca terminal 裡時用 `orca-ide` → `orca`。Linux 上單打 `orca` 通常是 GNOME 螢幕閱讀器。
- 派工指令：`worker-start --environment local --spec-file <prompt> --worktree path:<dir>|new-child --agent claude|codex|antigravity [--model <id> [--effort <level>]]`。Orca 規定要有 `--model` 才能給 `--effort`。
- Orca worker 結束時，Orca 自己也會送 `worker_done` 給它的 coordinator。superagent 一律以 board 上的 `done` post 為準。
- Router 不在 Orca 時（例如在 hc 上派 Orca worker），Orca 仍會把它自己的 `worker_done` 寄給某個 Orca terminal。實測時寄到了同一個 workspace 裡另一個 agent 的 terminal。那個 agent 不用回覆，也不用處理。
- Orca 依模型能力檢查 effort：claude haiku 帶任何 `--effort` 都被拒（實測 low、medium、high）。這類模型的角色要設成 `effort=none`，代表模型沒有 effort 設定，算已指定，但不會帶進指令。直接用 claude CLI 時，haiku 接受 `--effort low`，但是否真的有作用無法從外部確認。
- 不要輪詢 `orchestration check` 來等 worker，監控交給 `watch`。

## CLI 參數

`model` 和 `effort` 必須寫在角色設定裡。任一欄空著，`dispatch` 回 `ROLE_UNPINNED`；確定要吃 CLI 當天的預設值才加 `--allow-cli-default`。

| CLI | model | effort | 其他 |
|---|---|---|---|
| `claude` | `--model <id>` | `--effort <level>` | `--permission-mode auto`、`--add-dir <store>`；在 hc 上另加 `--settings <worker>.settings.json` 掛 stop hook |
| `codex` | `-m <id>` | `-c model_reasoning_effort="<level>"` | `--no-daemon -s workspace-write -c sandbox_workspace_write.network_access=true -a never`、`--add-dir <store>` |
| `agy` | `--model <id>` | `--effort <level>` | `-i`、`--add-dir <store>` |

worker 的 prompt 只有一句「讀 `<stage>/<worker>.prompt.md` 並照做」。prompt 檔列出 brief、報告路徑和 `post`、`read` 的完整指令。`--add-dir` 讓 worker 能寫入 `.superagent/<repo>/` 下的報告。
