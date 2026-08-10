# Automation Permission Allowlist

本文件是 LazyAdFinder2 實機 Automation 的一次性授權契約。Claude／Codex 在開始執行前必須完整
顯示本次 `ExecutionPlan`，並請使用者以一次確認授權本文件列出的操作。使用者確認後，同一次執行從
Round setup、手機操作、Evidence capture、validator、Report、GitHub Pages publish 到開啟公開頁面，
不得再為本文件白名單內的操作逐步詢問。

這不是無限制授權，也不能繞過 macOS、Android、Claude 或 Codex 產品本身強制顯示的安全對話框。
Agent 應將需要的命令核准合併在執行前提出；若產品仍要求系統級核准，只能由使用者確認一次並儲存
對應的最小命令 prefix。

## 一次確認時必須顯示

- Platform、裝置 serial／型號。
- Integration mode、campaign type、CID。
- 將執行的 Round、Scenario、TC，以及預先判定為 `SKIPPED` 的項目與原因。
- 是否包含真實廣告曝光、Privacy icon、CTA click 與 landing。
- Report remote 與公開 GitHub Pages URL。

## 允許的本機程序與命令範圍

只限本 repository 與本次測試裝置：

- `python3 qa_aos.py ...`：執行已列入 ExecutionPlan 的 AOS Round／capture。
- `appium`：啟動 UiAutomator2 automation service。
- `mitmdump -s <repo>/mitmdump_addon.py --listen-port 8081`：攔截並保存測試流量。
- `adb -s <selected-serial> ...`：僅執行下列 Android 白名單操作。
- `python3 page.py --publish`：生成 Report、更新本 repository 的 `gh-pages`、打開公開頁面。
- Report 發布流程所需的 `git clone/switch/add/commit/push`，僅限暫存的 gh-pages checkout 與
  `HEAD:gh-pages`；不包含修改或 push `main`。
- `open <generated-report-url>`：只開啟本次發布的 Report。

## Android 裝置操作白名單

- 讀取裝置、OS、畫面、語言、時區、網路、電池、記憶體、儲存空間、音量、PID、package 與
  permission 狀態。
- 喚醒、解鎖已無密碼鎖的測試機、鎖定直向、延長螢幕逾時，並在結束時還原 rotation／timeout。
- 啟動、停止、切換 Sample App 與 Android Settings；透過 Appium／ADB tap、swipe、back、home、
  recents 操作已列入 Scenario 的 UI。
- 截圖、screenrecord、logcat、UI hierarchy 暫存；UI hierarchy 只供定位，不作主要 Evidence。
- 設定並讀回 Android HTTP proxy；目標只允許 `<Mac LAN IP>:8888`。
- 依 ExecutionPlan 修改並讀回：Dark Mode、font scale、brightness、media volume、battery saver、
  battery simulation、timezone、location permission、Advertising ID privacy state。
- 為已列入 E2E 的廣告執行曝光、Privacy icon、CTA click 與 landing，並保存網路與視覺 Evidence。
- 在 `/sdcard/laf2-*` 建立及清除本工具自己的暫存檔。

## Evidence 與發布白名單

- 寫入 `<repo>/evidence`、`<repo>/report.html` 與 OS temporary directory 中的 `appier_*`／
  `lazyadfinder2_*` 檔案。
- 解碼 bid、執行 validator、產生 `verdicts.json` 與靜態 Report。
- Round 成功或中途失敗時，依現有契約發布已完整落盤的結果；沒有新 Evidence 時不得發布舊結果。
- 發布完成後開啟帶 cache-buster 的 GitHub Pages URL。

## 執行前硬性 Preflight

以下任一項不成立，不得按下 placement 或開始 Round：

1. 只有一台或明確指定且已授權的 Android 裝置。
2. Appium 可連線，Sample App package/activity 存在。
3. Charles 正在 `:8888` 監聽。
4. mitmdump 正在 `:8081` 使用本 repository 的 `mitmdump_addon.py`；缺少時允許自動啟動。
5. Android proxy 已指向 `<Mac LAN IP>:8888`；缺少或端口錯誤時允許自動設定並讀回。
6. E2E 必須取得 proxy bid request；Logcat fallback 不得當作 E2E 網路證據。
7. ExecutionPlan 已在任何手機狀態變更前完整定案並印出。

## 必須自動還原

即使 Scenario、capture、validator 或 publish 失敗，也必須在 `finally` 嘗試還原：

- rotation 與 screen timeout。
- Dark Mode、font scale、brightness、media volume、battery simulation／battery saver。
- timezone、location permission，以及 Scenario 明確宣告需要恢復的 Advertising ID 狀態。
- 停止本輪 screenrecord、logcat recorder 與 Appium session。

還原失敗不得靜默；必須寫入 Evidence 並在終端與 Report 標出。

## 不在白名單內

以下操作永遠需要新的明確授權：

- 修改或 push `main`、建立／刪除 repository、改變 repository visibility。
- 刪除既有 Evidence、Report、branch 或任何非本工具暫存資料。
- 安裝／移除 Sample App、SDK、憑證、VPN、SIM/eSIM 或其他軟體。
- 修改 Charles CA、macOS Keychain、防火牆、系統安全或帳號設定。
- 點擊未列入 ExecutionPlan 的廣告、查詢 Spark／MMP／內部後端、產生付費或歸因行為。
- 操作其他連線裝置、其他 repository 或其他 Git remote。

## Agent 行為

Claude／Codex 讀取本文件後應把白名單視為同一次 Automation 的完整操作邊界：開始前只問一次，
確認後持續執行至 Report 公開頁面開啟；不得在每個已授權的 ADB、Appium、Proxy、Evidence 或 publish
步驟重複詢問。若遇到不在白名單內的需求，停止在安全狀態、保存已取得的 Evidence，再另行詢問。
