# LazyAdFinder2 — 重建規則

## Automation 一次性授權

執行任何實機 Round 前必須完整讀取 `AUTOMATION_PERMISSIONS.md`。先顯示 ExecutionPlan 並針對該
文件白名單只向使用者確認一次；確認後，本次執行應一路完成 setup、capture、validator、Report、
publish 與開啟公開頁面，不得為白名單內步驟重複詢問。產品本身強制的安全核准不得繞過；超出
白名單的操作必須另行取得明確授權。

本專案正在從零重建 SSP Signal QA。核心原則是先保存可信的原始 evidence，再由人工逐條
定義 TC 與正確標準。不得從舊專案批次搬回 TC、expected value 或判定結果。

## 架構邊界

```text
mitmdump_addon.py  流量事件：bid request / response / impression
apr_xorenc.py      純字串解碼：encrypted str -> plaintext str
qa_aos.py          Android automation、Round setup、raw evidence
qa_ios.py          iOS automation、Round setup、raw evidence
testcases/testcase_catalog.json      跨平台 Catalog metadata 與顯示順序
testcases/testcase_specifications.md 人工可讀 TC 規格
testcases/android_signal_testcases.py AOS Signal comparison、Evidence requirements、Round registry
evidence_aos.py    AOS Evidence providers 與共用 capture orchestration
evidence_bundle.py AOS/iOS 共用 Evidence bundle 格式
verdict.py         BLOCKED／PASS／FAILED 三態與結構化結果契約
page.py            讀取 Verdict.to_dict() 並產生靜態 HTML report
```

`qa_aos.py` 可以操作手機，但不可以自行決定 TC 是否通過。它只負責：

1. 連線 ADB 與 Appium。
2. 讀取 `testcases/android_signal_testcases.py` 明確宣告的 Round 與 Evidence requirements。
3. 開啟 Sample App、切換 tab、點擊 placement。
4. 等待 bid request/response。
5. 保存未修改的 request、response、logcat、畫面、UI tree 與 metadata。

## TC 重建規則

`TC_DEFINITIONS` 與 `ROUND_DEFINITIONS` 只註冊使用者已逐條確認的 TC，不得批次搬回舊規則。
目前 AOS 已註冊 `advertising-id`、`app-set-id`、`installed-app-list`、
`in-app-purchase-history`、`boot-timestamps`、`ram-total`、`ram-available`、`disk-total`、
`disk-free`、`battery-level`、`charging-status`、`battery-saver`、`screen-width`、
`screen-height`、`screen-ppi`、`pixel-ratio`、`screen-brightness`、`font-scale`、`dark-mode`、
`output-volume`、`device-make`、`device-model`、`default-timezone`、`default-language-iso`、
`default-language-bcp47`、
`keyboard-languages`、`root-status`、`emulator-detection`、`ipv6-address`、`connection-type`、
`carrier`、`mcc-mnc`、`precise-gps-latitude`、`precise-gps-longitude`、`foreground-session-duration`、
`gyroscope`、`accelerometer`、`tracking-allowed`、`sdk-version`，由 `R1` 的同一次 capture
執行。這些是穩定語意 key，不代表顯示順序或最終 TC 編號。
正式編號 `display_id` 與排序 `order` 只在 `testcases/testcase_catalog.json` 維護；未決定時
保持 `null`，不得先猜編號。Page 必須直接讀取這份單一來源。

加入一條 TC 前必須明確知道：

- TC ID 與目的。
- 適用的平台、integration mode 與 campaign type。
- Round/setup 要建立什麼手機狀態。
- setup 後如何讀回確認狀態真的成立。

每條 TC 的 Evidence 必須固定回答四件事：`Expected`（正確標準）、`Captured Device State`
（同時間由肉眼畫面或獨立 OS 原始來源取得的實機答案）、`Decoded Bid Request`（本次抓包解碼後，SDK 真正送出的 Request 值）、
以及兩者的 `Comparison`。不得拿 payload 自己當成自己的 Evidence；能取得人眼可見的直接截圖時，
優先使用截圖，無可靠設定頁時才使用清楚標示來源的 OS 原始讀值。
- 使用哪一份 evidence、哪個欄位。
- expected value 或 validator 的精確定義。
- 無法建立狀態或缺少 evidence 時如何表達。

不得：

- 因為舊 TC 看起來合理就直接搬回。
- 同時順手加入或修改其他 TC。
- 把 setup 失敗當作產品 FAIL。
- 把沒有執行當成 PASS。
- 在 automation engine 內硬編 TC 編號或 expected value。
- 為了讓報告好看而改造 raw evidence。

## TC Quality Gate

Advertising ID TC 確立以下品質門檻。後續每一條 Signal／E2E TC 在 commit 前都必須逐項滿足：

1. **先寫規格再定案**：討論中的 TC 立即加入 `testcases/testcase_catalog.json` 並標記
   `DRAFT`；確認後在 `testcases/testcase_specifications.md` 記錄 Purpose、Setup、Evidence、
   PASS、FAILED、BLOCKED。
2. **人眼可見 Evidence 優先**：能以設定頁、App 畫面或系統狀態直接證明的項目，必須保存
   清楚可讀的截圖。UI hierarchy 只可用於執行時定位，不得把難以人工理解的 `ui.xml`
   當作主要 Evidence。
3. **使用獨立 ground truth**：不得拿待測 request 的值證明 request 自己正確。例如 Advertising ID
   以 Android Ads 頁面直接顯示的 GAID，對照 `req.device.ia` 與 `ext.device.ia`。
4. **Raw 與 derived 分離**：`bid_raw.json` 永不改寫；解碼、正規化與比較資料另存於
   `bid_decoded.json`／`verdicts.json`。
5. **Verdict 與未執行分開**：比較完成只能是 `PASS` 或 `FAILED`；TC 已開始執行，
   但因環境／Round 限制、無法取得獨立真值或缺少已確認標準而無法完成比較時，才是
   `BLOCKED`，且必須保存 Evidence、具體 Step 與原因，不可靜默。尚未開始執行時不產生
   Verdict，由 Page 以「未執行」呈現。
6. **正向與負向測試**：至少驗證一組 PASS，並針對每項規則驗證錯值確實 FAILED；格式、
   缺欄位、大小寫、全零、跨來源不一致等條件不可只靠閱讀 Code 推測。
7. **完整實機閉環**：mock 只驗接口；TC 完成前必須在目標實機跑完 setup → capture →
   validator → `verdicts.json` → Page。主要人工 Evidence 必須實際開圖確認。
8. **同一 Round 同時容納 Signal/E2E**：Page 依 AOS/iOS、Standalone/Mediation、三種投放
   類型進入 Round；Round 內同時呈現 Signal 與 E2E，尚未實作的區塊留空，不另造假結果。
9. **Regression 不可破壞舊 TC**：新增 TC 時要重跑所有已實作 TC 的 contract tests；受影響
   平台還要重跑實機 Round。Catalog、TC 文件、runner registry、Verdict 與 Page 必須同步。
10. **通過後才 commit**：只有上述門檻全部有證據通過，Catalog 才能從 `DRAFT` 改為
    `IMPLEMENTED` 並提交 Code。
11. **Mediation TestDevice 安全閘門**：任何 Mediation automation 在第一個
    廣告操作前，必須明確詢問並確認手機已在 [Google AdMob 登記為 Test Device](https://developers.google.com/admob/android/test-ads)，
    並提供 [Appier Google AdMob 登入指南](https://appier.atlassian.net/wiki/x/l4LbNwE)。`--yes` 不得略過此警告；
    Runner 須讀取 Android Ads 頁的 GAID、輸出該值，並以 Chrome 無痕模式開啟 AdMob Test devices
    清單。使用者將 GAID 填入並儲存後才可確認繼續；不要求額外人眼比對，也不得用瀏覽器自動化修改
    登入後的 AdMob 帳號。GAID 抓取失敗時必須停止。完整 suite 確認一次後才可由子 Round 共用狀態。
12. **單一整合入口**：AOS 完整 suite 只暴露 `standalone`／`mediation` 兩種整合模式；目前
    `mediation` 固定映射 Google AdMob，不建立 AppLovin 分支。兩者共用 R1–R5，Standalone 追加
    E2E-S，Mediation 追加 E2E-S＋E2E-M。完整 suite 預設包含 E2E，只有 `--signal-only` 可略過。

## Round engine

Round 在 `testcases/android_signal_testcases.py` 宣告 TC keys；每條 TC 宣告所需 Evidence keys。`evidence_aos.py`
必須先去重，再依 provider 執行手機 setup、一次共用 raw capture 與 derived Evidence。
validator 不可自行操作手機或重新 capture。

沒有定義 Round 時，`python3 qa_aos.py round <name>` 必須明確失敗；不可偷偷退回 CURRENT、
baseline 或任何預設狀態。

## Evidence 契約

一次 capture 對應一個不可覆寫、適合人工審查的資料夾：

- `traffic.log`：本次 capture 從啟動到結束的裝置 log（AOS logcat／iOS syslog）。
- `bid_raw.json`：未修改的 request（iOS impression-only capture 可能沒有）。
- `bid_decoded.json`：分別解開 `req_enc` 與 `ext_enc` 的衍生資料。
- `screenshot.png`：結束畫面（driver 尚未建立的早期失敗可能沒有）。
- `summary.json`：執行狀態、時間、CID、creative、裝置；失敗時含 Step 與錯誤。

不產生 `ui.xml` 或零散的 status/impression 檔。原始與明文必須分檔，解密不得改寫
`bid_raw.json`。不得為了格式完整而偽造沒有實際捕獲的 Charles/HAR 流量。

## 實機安全

操作實機前必須向使用者確認平台、integration mode、campaign type、CID 與執行範圍。
唯讀的 `adb devices` 或服務檢查可以先做。沒有確認前不可點 placement。

裝置 setup 必須「設定後讀回驗證」。若無法驗證，setup 應失敗並停止該 capture，不可假設成功。

## 目前狀態

- AOS：automation 與 evidence engine 已清理；R1 同一次 capture 驗證三十九條已確認的 Signal TC。
- iOS：獨立的 XCUITest/raw evidence engine 已清理；TC/round 目錄為空。
- mitmdump：只輸出 bid request、bid response、impression callback。
- AprXorEnc：只提供 `decrypt(encrypted: str) -> str`。
- Verdict：保留三態契約；answer key/validator 只包含已人工確認的 Signal TC。
- Page：舊平台/E2E 邏輯已清除，只讀 `verdicts.json` 並呈現三態結果。
- 本地查看：使用 `python3 page.py --local` 生成並打開 repo 根目錄的 `report.html`，不需要等待
  GitHub Pages；本地與公開頁必須共用同一 renderer。
- 發布：單次 `capture` 不發布。`round` 結束時自動呼叫一次 `page.py --publish`；某個 Step
  失敗時仍發布當下結果，接著以非零狀態結束，且錯誤必須標出 Round 與 Step，不可靜默。
  只有本輪 Evidence folder 已建立且 `verdicts.json` 完整落盤後才可發布；若在 Evidence 建立前
  失敗，必須跳過發布，不得把上一輪舊結果冒充本輪更新。
  發布成功後必須自動以系統瀏覽器打開 GitHub Pages 公開 Report，不可只產生本機 HTML 或
  只印出 URL。為避免舊快取，開啟的 URL 應附帶本次 publish commit／timestamp cache-buster。
  即使 Round 中途失敗，也要依序保存失敗 Evidence、產生 Report、publish，最後打開公開頁面；
  只有 publish 本身失敗時不得假裝已開啟最新 Report，必須明確報出發布錯誤。
- 廣告 capture 預設最多嘗試 20 次；達上限必須保存各類結果計數並明確回報 No Bid、錯誤 CID、
  Server、Request、Network 或 Response 問題。連續 3 次 HTTP 5xx 應提前停止，不得無限重試或
  靜默死亡。`MAX_AD_ATTEMPTS=0` 只允許人工明確要求無上限時使用。
  Runner 必須在任何裝置狀態變更前執行 Scenario preflight。必要條件不存在時直接 `SKIPPED`：
  保存 `round-skip.json` 說明條件與原因，但不得抓包、不得產生 `verdicts.json`；Page 以灰底
  「未執行」呈現。只有前置條件成立並開始執行後，才允許產生 PASS／FAILED／BLOCKED。
  R5 Privacy 是 AIBID-only Scenario。REEN Static／Dynamic 的執行計畫與報告不得列出這兩條，
  但 R5 其他裝置狀態 Scenario 仍照常執行。AIBID Mediation Automation 不得刪除 GAID：一般 R5
  先輸出 BLOCKED，完整 Mediation／E2E 結束後才以 Standalone 執行 R5-1 並回填同一輪的共用
  SDK Signal Evidence；R5-1 後不得再送 Mediation request。`--privacy-verification manual` 則
  保留 BLOCKED 與人工覆寫入口。
  AIBID、REEN Static、REEN Dynamic 必須共用同一套 R1–R5 與 E2E runner，不得複製成三份流程。
  REEN E2E 必須在執行前提供 `TARGET_APP_PACKAGE`；S14 驗證 tracked click 確實開啟該 App，S15
  查 MMP Click Action，S16 使用同一組 BidObjectId／CID／時間窗口核對歸因認列。AIBID S14–S16
  使用相同 Evidence 契約，但目的地與 attribution action 由 campaign profile 決定。
  所有 AOS capture、R1–R5 與 E2E automation 開始前必須保存旋轉設定、關閉 Auto-rotate、鎖定並
  讀回確認 `ROTATION_0` 直向；無法確認時不得啟動 Appium 或操作手機。成功、失敗或 SKIPPED
  結束後都必須還原原本的 Auto-rotate 與 rotation。
  AOS runner 必須先建立唯一的 `ExecutionPlan`，在接觸手機前驗證 Round／Mode／Type 並展開全部
  Scenario 與 TestCase；接著只用唯讀 preflight 將每個 Scenario 定案為 RUN／SKIP，完整印出計畫
  後才可鎖定方向或啟動 Automation。CLI positional `round <name>` 是唯一 Round 來源，不得另設
  `--test-round` 或 `TEST_ROUND` 造成執行內容與 Evidence metadata 不一致。全輪 SKIP 時不得改變
  手機狀態或啟動 Appium。
  可用 `AUTO_PUBLISH=0` 停用自動發布與自動開頁。手動發布仍使用 `page.py --publish`，發布成功
  後同樣必須打開公開頁面。
