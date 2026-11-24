# 巡檢協議（watch 與 stop hook）

worker 完成時會在 board 發 `done` post，並推送通知給 Router。漏發 post 有兩道防線：

1. **stop hook**（第一道，即時）：worker 結束回合時，CLI 呼叫 `hook-stop --as <worker>`。worker 還欠 board 一則 post，或有寄給它的未讀時，就擋下這次結束，並給出要執行的 `post` 或 `read` 指令。
2. **watch**（後備，有延遲）：處理 hook 管不到的情況，包括 worker 停了卻沒發 post、卡在提示畫面、看起來在跑其實已經不動。

## stop hook

| 條件 | 結果 |
|---|---|
| worker 不是 `active`（已發 done、已關閉） | 放行 |
| 有寄給它、還沒讀的 post | 擋下，要它先跑 `read` |
| 最後一則交辦（`brief` 或 `answer`）之後還沒發過 post | 擋下，要它發 `done`；需要決定時發 `ask` |
| 發過 `ask` 在等 Router | 放行，這是合法的等待 |
| 同一個 board 狀態已經擋過一次 | 放行，交給 watch，避免無限擋 |

hook 出錯時一律放行，不會把 worker 卡住。

| ADE + CLI | stop hook |
|---|---|
| hc + claude | 有。`dispatch` 寫出 `stages/<stage>/<worker>.settings.json`，以 `claude --settings` 帶入，不改任何使用者或專案設定。已實測 |
| hc + codex | 沒有。codex 的 hook 寫在 `hooks.json`，需要先在 `config.toml` 信任，superagent 不代為信任 |
| hc + agy | 沒有。agy 沒有可從外部設定的 hook |
| orca | 沒有。CLI 參數由 Orca 組，superagent 帶不進去 |
| native | 沒有。subagent 結束時由 harness 通知 Router |

沒有 stop hook 的組合只靠 watch。


## 誰來跑 watch

| 環境 | 跑法 |
|---|---|
| 有裝 hc（不論 Router 在哪） | `dispatch` 自動啟動 hc 背景 terminal `sa-watch-<repo>`，執行 `watch --auto-nudge --loop`，每 `poll_interval_sec`（預設 180 秒）跑一次。警報以 board post 經 Router 的端點推送（hc 用 `hc send`，Orca 用 `orchestration send`），不需要 LLM 排程 |
| 沒裝 hc | Router 用自己 harness 的排程工具，每 3 分鐘跑一次 `watch --auto-nudge` |

迴圈在 `should_sleep: true` 時自行結束：沒有任何 `active` worker，且連續 `dormant_idle_ticks`（預設 5）次巡檢都是如此。下一次 `dispatch` 會再啟動它。

## 每個 active worker 收集的證據

1. **完成標記**：報告檔最後 20 行（略過空行）有沒有 `expected_marker`。worker 寫了標記卻沒發 post，`watch` 會代它發一則 `done`（帶 `"by": "watch"`），讓 Router 只需要看 board。
2. **後端狀態**：hc 用 `hc get`（`state`、`status.state`、`exit_code`）；Orca 用 `worker-show`。
3. **畫面指紋**：`hc read` 或 `worker-read` 的輸出，濾掉計時、token 進度列等會一直變動的行，再算 sha256。
4. **日誌**：`--watch-log` 指定的檔案，加上階段 `logs/` 下最新的 3 個 `.log`，取 `(mtime, size)`。
5. **子行程 CPU**：以 worker 根 PID 展開整棵子行程樹，加總 CPU 秒數。只有 hc 做得到。

native worker 只檢查第 1 項。

## 判定

| 事件 | 條件 | Tier | watch 的動作 | Router 怎麼處理 |
|---|---|---|---|---|
| `MARKER_WITHOUT_POST` | 報告有標記，但 worker 沒發 post | 0 | 代發 `done` post | 照一般 `done` 處理 |
| `WORKER_EXITED_NO_POST` | 後端 `finished`，沒有 post | 2 | 寫鑑識包、發 alert | 看畫面尾端和 exit code，補收尾或重派 |
| `INTERACTIVE_BLOCK` | 畫面命中權限、信任、登入或額度提示，持續 ≥ `prompt_stall_sec`（120 秒） | 2 | 寫鑑識包、發 alert。**不代按** | 權限問題就改 brief 重派或問使用者；額度問題就換模型或換 CLI |
| `AUTO_NUDGED` | agent 狀態為 `done`／`idle`／`waiting`，沒有 post，無進展 ≥ `state_stall_sec`（180 秒），且還沒催過 | 1 | 推送一次催促，內含 `post` 和 `read` 指令 | 不用處理 |
| `STOPPED_NO_POST` | 同上，但已經催過 | 1 | 寫鑑識包、發 alert | 讀畫面，看 worker 漏了哪一步 |
| `SILENT_HANG` | agent 顯示在工作，但畫面、日誌、子行程 CPU（差值 < 0.5 秒）都沒變，持續 ≥ `silent_hang_sec`（600 秒） | 3 | 寫鑑識包，含完整子行程樹，發 alert | 從鑑識包找出卡住的葉節點行程。可安全重跑的測試，問使用者或在已授權時終止該 PID；涉及外部寫入就交給使用者 |
| `ROUTER_UNREAD` | 給 Router 的 post 超過 `state_stall_sec` 還沒讀 | 1 | 對最新那則再推送一次，同一則只推一次 | 跑 `read --as router` |
| `PROBE_ERROR` | `hc get`／`worker-show` 失敗 | 2 | 發 alert | 用 `diagnose` 確認 worker 還在不在 |

只要畫面、日誌或 CPU 有任一項變化，就算有進展：清掉催促紀錄和告警計數。

> [!CAUTION]
> `watch` 和 Router 一律不代 worker 按權限、信任或登入確認，也不用 `hc send --force`。

## 防重複告警

- 告警簽章：`<worker>:<事件>:<畫面 hash 前 12 碼>`。
- 同一個簽章在 `repeat_cooldown_sec`（600 秒）內不重發。
- 同一個簽章發滿 `max_alerts`（3 次）後就不再發，直到簽章改變。
- 同一輪巡檢的所有警報合成一則 `alert` post，寄給 Router。

## 鑑識包

路徑：`stages/<stage>/watchdog/<worker>_incident_<UTC 時間>.json`。

```json
{
  "timestamp": "2026-11-24T09:30:00Z",
  "worker_id": "impl-01-auth",
  "stage": "01-auth",
  "incident_type": "SILENT_HANG",
  "tier": 3,
  "alert_number": 1,
  "stalled_seconds": 642,
  "ade": "hc",
  "backend_ref": "t110",
  "backend_status": {"backend_state": "live", "agent_status": "working", "status_msg": "", "exit_code": null},
  "screen_tail": ["...最後 40 行..."],
  "watched_logs": [{"path": "...", "exists": true, "size": 53625, "mtime": "...", "tail": ["...最後 20 行..."]}],
  "process_tree": [{"pid": 35192, "ppid": 9904, "name": "cargo", "cpu_sec": 1.05, "cmdline": "..."}],
  "extra": {}
}
```

`diagnose --worker <id>` 會即時產生同樣的內容，不必等 `watch`。
