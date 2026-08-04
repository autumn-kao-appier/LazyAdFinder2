# LazyAdFinder2 — 重建規則

本專案正在從零重建 SSP Signal QA。核心原則是先保存可信的原始 evidence，再由人工逐條
定義 TC 與正確標準。不得從舊專案批次搬回 TC、expected value 或判定結果。

## 架構邊界

```text
mitmdump_addon.py  流量事件：bid request / response / impression
apr_xorenc.py      純字串解碼：encrypted str -> plaintext str
qa_aos.py          Android automation、Round setup、raw evidence
qa_ios.py          iOS automation、Round setup、raw evidence
verdict.py         BLOCKED／PASS／FAILED 三態與結構化結果契約
page.py            讀取 Verdict.to_dict() 並產生靜態 HTML report
```

`qa_aos.py` 可以操作手機，但不可以自行決定 TC 是否通過。它只負責：

1. 連線 ADB 與 Appium。
2. 執行明確宣告的 Round setup。
3. 開啟 Sample App、切換 tab、點擊 placement。
4. 等待 bid request/response。
5. 保存未修改的 request、response、logcat、畫面、UI tree 與 metadata。

## TC 重建規則

`TC_DEFINITIONS` 與 `ROUND_DEFINITIONS` 預設保持空白。每次只加入使用者正在人工確認的 TC。

加入一條 TC 前必須明確知道：

- TC ID 與目的。
- 適用的平台、integration mode 與 campaign type。
- Round/setup 要建立什麼手機狀態。
- setup 後如何讀回確認狀態真的成立。
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

## Round engine

Round 是一組依序執行的 `RoundStep`。每個 step 只能：

1. 執行 setup。
2. setup 成功後進行一次 raw capture。

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

- AOS：automation 與 raw evidence engine 已清理；TC/round 目錄為空。
- iOS：獨立的 XCUITest/raw evidence engine 已清理；TC/round 目錄為空。
- mitmdump：只輸出 bid request、bid response、impression callback。
- AprXorEnc：只提供 `decrypt(encrypted: str) -> str`。
- Verdict：舊 validator 與報告版面已清除，只保留三態契約；TC answer key 尚未加入。
- Page：舊平台/E2E 邏輯已清除，只讀 `verdicts.json` 並呈現三態結果。
- 發布：單次 `capture` 不發布。`round` 結束時自動呼叫一次 `page.py --publish`；某個 Step
  失敗時仍發布當下結果，接著以非零狀態結束，且錯誤必須標出 Round 與 Step，不可靜默。
  可用 `AUTO_PUBLISH=0` 停用。手動發布仍使用 `page.py --publish`。
