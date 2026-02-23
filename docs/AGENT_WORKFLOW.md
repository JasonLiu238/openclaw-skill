# Agent Workflow

## 1. 建立 task 檔
在 `.agent/inbox/` 放入 JSON，例如 `.agent/inbox/T0001.json`：

```json
{
  "id": "T0001",
  "title": "修正登入流程測試",
  "repo_root": ".",
  "workdir": ".",
  "branch": "agent/T0001",
  "prompt_text": "請修正登入流程並更新測試。\n\nAcceptance:\n- pytest -q"
}
```

欄位說明：
- `id`: 任務唯一識別碼
- `title`: commit 訊息摘要
- `repo_root`: Git repo 根目錄
- `workdir`: 執行 `codex exec` 與 Acceptance 命令的目錄（相對於 `repo_root`）
- `branch`: 任務分支名稱
- `prompt_text`: 交給 Codex CLI 的完整提示

## 2. 執行 runner
在 repo 根目錄執行：

```bash
python3 scripts/codex_runner.py
```

Runner 會：
- 掃描 `.agent/inbox/*.json`（略過 `*.result.json`）
- 切換或建立 task 指定分支
- 執行 `codex exec --json "<prompt_text>"`
- 將 Codex JSONL 輸出寫入 `.agent/logs/<id>.codex.jsonl`
- 將所有 shell 命令與輸出記錄到 `.agent/logs/<id>.runner.log`（Acceptance 另寫到 `.agent/logs/<id>.tests.log`）
- 解析 error 事件決定狀態
- 自動執行 `Acceptance:` 區塊內的 shell 命令，寫入 `.agent/logs/<id>.tests.log`
- 若有變更則 commit，並輸出 `.agent/outbox/<id>.result.json`

## 3. 到 outbox 看結果
結果檔位於：

- `.agent/outbox/<id>.result.json`

常見欄位：
- `status`: `needs_review` 或 `blocked`
- `branch`: 任務分支
- `commit`: commit hash（若有）
- `changed_files`: 本次變更檔案
- `test_commands` / `test_ok`: 驗收命令與結果
- `errors`: 失敗原因

## 4. 回滾方式
若要丟棄尚未 commit 的變更：

```bash
git reset --hard
git checkout <原本分支>
```

若要移除 runner 產生的 task 分支（請先切離該分支）：

```bash
git checkout <原本分支>
git branch -D agent/T0001
```
