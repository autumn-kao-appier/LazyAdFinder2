# LazyAdFinder2

LazyAdFinder2 是 Appier Ads SDK 的實機 SSP QA 重建專案。TC、正確標準、Evidence 與報告
由人工逐條定義；目前只有 AOS 完成可用的 Signal／E2E 自動化閉環。

## 範例報告

完整測試執行後，會產生像這樣的網頁報告：

[查看 LazyAdFinder 範例報告 →](https://autumn-kao-appier.github.io/LazyAdFinder2/)

## 下載專案

```bash
git clone https://github.com/autumn-kao-appier/LazyAdFinder2.git
cd LazyAdFinder2
```

## 快速使用

1. 安裝 Python 依賴與 Appium：

   ```bash
   python3 -m pip install -r requirements.txt
   npm install -g appium
   appium driver install uiautomator2
   ```

2. 啟動抓包與手機自動化服務：

   ```bash
   mitmdump -s mitmdump_addon.py --listen-port 8081
   appium
   ```

3. 連接 Android 手機，設定 App、Campaign 與 CID，然後執行完整 suite：

   ```bash
   export APP_PACKAGE=com.appier.android.sample
   export APP_ACTIVITY=com.appier.android.sample.MainActivity
   export TEST_CID='<campaign-cid>'

   python3 run_aos_test_suite.py --integration-mode standalone
   ```

4. 開啟本機報告：

   ```bash
   python3 page.py --local
   ```

完整的 Campaign、REEN、Round、capture 與 publish 參數請繼續往下看。

## 安裝與服務

```bash
python3 -m pip install -r requirements.txt
npm install -g appium
appium driver install uiautomator2
```

執行 capture 前先啟動：

```bash
mitmdump -s mitmdump_addon.py --listen-port 8081
appium
```

手機流量路徑為：手機 Wi-Fi proxy → Mac/Charles `:8888` → mitmdump `:8081`。

## Android raw capture

必要設定：

```bash
export APP_PACKAGE=com.appier.android.sample
# APP_ACTIVITY 可省略；runner 會由 Android MAIN/LAUNCHER intent 自動解析。
# 若明確提供，必須與 exported Launcher Activity 相同，內部 Activity 會在 R1 前被拒絕。
export TEST_MODE=standalone
export TEST_TYPE=aibid
export TEST_CID='<campaign-cid>'
```

REEN Static／Dynamic 使用相同的 R1–R5 與 E2E runner。執行 REEN E2E 時另指定最終必須開啟的 App：

```bash
export TEST_TYPE=reen-static   # 或 reen-dynamic
export TARGET_APP_PACKAGE='<promoted-app-package>'
```

REEN 的執行計畫與報告不列 Advertising ID opt-out 與 tracking-denied；其他 R5 裝置狀態 Scenario 仍會執行。
R5 以四包執行：`DISPLAY-HIGH`、`DISPLAY-LOW`、`SYSTEM-ALT`、`PRIVACY-DENIED`。每包只送一次
廣告 request，單項 mutation 與 verdict 仍獨立；Battery Saver 位於 `SYSTEM-ALT`，不與亮度狀態共包。

使用任何 Mediation 模式前，runner 會在第一個廣告操作之前強制詢問：這支手機是否已在
[Google AdMob 登記為 Test Device](https://developers.google.com/admob/android/test-ads)。若尚未登入，請先依
[Appier Google AdMob 登入指南](https://appier.atlassian.net/wiki/x/l4LbNwE) 完成登入與設定。
Runner 會從 Android Ads 設定頁讀取當前 GAID，並以 Chrome 無痕視窗自動開啟
[AdMob Test devices 清單](https://admob.google.com/v2/settings/test-devices/list)，再將 GAID 清楚輸出至終端。
使用者將該值填入 Google AdMob 並儲存後，才確認繼續；不要求另外比對清單，也不以瀏覽器自動化
修改 AdMob 帳號。GAID 無法取得時，runner 必須在第一個廣告請求前停止。
必須明確回答 `y`／`yes` 才會繼續；`--yes` 只略過 Test Scope 確認，不能略過這個安全警告。
完整 suite 只詢問一次，後續 Round 子程序沿用同一次確認。

REEN 有獨立的一鍵入口；Static／Dynamic 共用完全相同的 TestCase 與 runner，但 Evidence 與 Report slot 依 creative type 分開：

```bash
python3 run_reen_test_suite.py static \
  --mode standalone --cid '<cid>' --target-app-package '<package>' \
  --publish

python3 run_reen_test_suite.py dynamic \
  --mode standalone --cid '<cid>' --target-app-package '<package>' \
  --publish
```

完整 AOS suite 使用同一個 runner，只選整合模式：`standalone` 或 `mediation`。`mediation`
目前固定為 Google AdMob，不再提供 AppLovin 第二層選項：

```bash
python3 run_aos_test_suite.py --integration-mode standalone ...
python3 run_aos_test_suite.py --integration-mode mediation ...
```

兩個入口都執行相同 R1–R5 Signal。Standalone 自動追加 E2E-S；Mediation 自動追加
E2E-S 與現有 E2E-M（AdMob）。完整 suite 預設執行 E2E；只有明確指定 `--signal-only`
才只執行 R1–R5。

AIBID Mediation 不會在刪除 GAID 後自動送出 AdMob request。一般 R5 會將兩條 tracking-denied
Privacy TC 記為 BLOCKED；整輪 Mediation／E2E 完成後，預設追加 Standalone `R5-1` 作為共用
SDK Signal Evidence。若要改由人工處理，加入 `--privacy-verification manual`；報告會維持
BLOCKED，並保留人工覆寫入口。

擷取一次符合 CID 的廣告：

```bash
python3 qa_aos.py capture
```

只要取得 request 就保存，不要求 response 200 或 CID 命中：

```bash
python3 qa_aos.py capture --accept-request --max-attempts 1
```

常用選項：

- `--udid`：多台 Android 裝置時指定目標。
- `--trigger-text`：Sample App 的 placement 文字。
- `--tab-text`：覆蓋 integration mode 對應的 tab 文字。
- `--max-attempts`：預設 `20`；`0` 僅供人工明確指定為不限制次數。達上限時 Evidence／Report
  會統計 `NO_BID`、`WRONG_CID`、`SERVER_ERROR`、`REQUEST_REJECTED`、`NETWORK_ERROR`、
  `INVALID_RESPONSE`、`UI_TRIGGER_MISSING`，不得靜默結束。連續 3 次 HTTP 5xx 會提前停止並
  標示 Server error；找不到可點擊版位也必須計入 20 次總上限。
- 每次完整 suite 以 `TEST_RUN_ID` 建立獨立的 Round Evidence 目錄，不得把新結果寫入舊 run。
- Ctrl-C 會先通知當前 Round 保存 `INTERRUPTED` Evidence 與 BLOCKED cards、恢復手機狀態並發布，
  再以中止狀態退出。Activity、裝置、Appium 或 Proxy 等基礎設施失敗會停止後續 Round；單一
  Scenario／TC failure 則仍允許後續測試繼續。
- `--phase-timeout`：整次 capture 的牆鐘上限；`0` 表示不限。
- `--evidence-dir`：evidence 根目錄。

## Round 與 TC

`qa_aos.py` 的 TC 與 Round 是明確註冊表。每次只加入一條人工確認完成的 TC。目前：

```python
TC_DEFINITIONS = {"advertising-id": TestCase(...), ...}
ROUND_DEFINITIONS = {"R1": Round("TRACKING-ALLOWED", (...))}
```

所以：

```bash
python3 qa_aos.py list-rounds
# R1: TRACKING-ALLOWED [advertising-id, app-set-id, installed-app-list, in-app-purchase-history, boot-timestamps, ram-total, ram-available, disk-total, disk-free, battery-level, charging-status, battery-saver, screen-width, screen-height, screen-ppi, pixel-ratio, screen-brightness, font-scale, dark-mode, gyroscope, accelerometer, output-volume, device-make, device-model, default-timezone, default-language-iso, default-language-bcp47, keyboard-languages, root-status, emulator-detection, ipv6-address, connection-type, carrier, mcc-mnc, precise-gps-latitude, precise-gps-longitude, foreground-session-duration, tracking-allowed, sdk-version]
```

只有在人工確認某條 TC 的 setup、證據與正確標準後，才加入定義。Automation engine 不得
自行推測 expected value，也不得把「沒有執行」包裝成測試結果。

穩定 semantic key、未來正式編號 `display_id` 與排序 `order` 都只在
`testcases/testcase_catalog.json` 維護；未決定的編號保持 `null`，Page 直接讀取同一份資料。

Page 會掃描全部歷史 Evidence，但每個平台／模式／類型／semantic key 只呈現最新一次
Verdict；已從 catalog 移除的舊 key 不會被當成額外 TestCase 卡片。

## Raw evidence

每次 capture 會建立一包適合人工審查的 Evidence：

```text
evidence/<round>/<capture>_<timestamp>/
  traffic.log             # 本次 capture 從啟動到結束的裝置 log
  bid_raw.json            # 原始 request；req_enc/ext_enc 不改寫
  bid_decoded.json        # req_enc/ext_enc 分別呼叫 decrypt() 的結果
  screenshot.png          # capture 結束時畫面
  summary.json            # 狀態、CID、creative、時間、裝置與失敗位置
```

Evidence 保留原始觀察；`bid_decoded.json` 是獨立的衍生檔，不回寫 `bid_raw.json`。
若 capture 中途失敗，仍完成同一包 Evidence，並在 `summary.json` 寫入
`result: INTERRUPTED`、`failed_step` 與 `error`。正式 Evidence 不保存 `ui.xml`。AOS 使用
本輪 logcat，iOS 使用本輪 syslog，統一落成 `traffic.log`；Charles/mitmdump 仍負責拆出
原始 bid 與 impression 事件，但不偽造實際未取得的 HAR。

## iOS raw capture（尚未完成完整 TC 覆蓋）

iOS runner 與 AOS 有相同責任，但以 XCUITest／WebDriverAgent、`idevice_id`、
`idevicesyslog` 和 accessibility id 獨立實作。

```bash
export BUNDLE_ID=com.appier.Random
export TEST_MODE=standalone
export TEST_TYPE=aibid
export TEST_CID='<campaign-cid>'

python3 qa_ios.py capture
python3 qa_ios.py list-rounds
```

WDA 需要自動簽名時可設定：

```bash
export XCODE_ORG_ID='<Apple Developer Team ID>'
export WDA_BUNDLE_ID='<unique WDA bundle id>'
```

iOS 可能因 TLS／pinning 只觀察到 impression callback、沒有 bid body。標準 CID capture
允許保存這種 evidence；此時 `summary.json` 仍記錄 impression 身分，但不會假裝產生
`bid_raw.json`／`bid_decoded.json`；
`--accept-request` 則仍要求確實取得 request body。

## Verdict contract

`verdict.py` 目前只定義三種對外結果：

- `BLOCKED`：TC 已開始執行，但因 Round／環境限制、無法取得獨立真值或缺少已確認的正確標準，無法得出 `PASS`／`FAILED`。
- `PASS`：TC 已執行，實際值符合人工確認的正確標準。
- `FAILED`：TC 已執行，實際值不符合正確標準。

尚未開始執行的 TC 不產生 Verdict；Page 根據 Catalog 以「未執行」呈現，不得以
`BLOCKED` 代替未執行狀態。

TC answer key 與 validator 只包含已人工確認的 TC；目前已加入 advertising-id、
app-set-id、installed-app-list、in-app-purchase-history、boot-timestamps、ram-total、
ram-available、disk-total、disk-free、battery-level、charging-status、battery-saver、
screen-width、screen-height、screen-ppi、pixel-ratio、screen-brightness、font-scale、dark-mode、
output-volume、device-make、device-model、default-timezone、default-language-iso、default-language-bcp47、
keyboard-languages、root-status、emulator-detection、ipv6-address、connection-type、carrier、mcc-mnc、
precise-gps-latitude、precise-gps-longitude、foreground-session-duration、
gyroscope、accelerometer、tracking-allowed、sdk-version。`page.py` 只呈現
結構化 `Verdict`，不得自行重算答案。

已取得可比較的 expected／actual 時呼叫 `evaluate(expected=..., actual=...)`，得到 `PASS` 或
`FAILED`；TC 已開始執行，但因 Round／環境限制或缺少可驗證正確標準而無法完成比較時，
才呼叫 `blocked(reason=...)`。尚未開始執行時不寫入 Verdict。

## 下一期改善

- 降低 Android Evidence 擷取對固定 UI 文字與特定畫面結構的依賴，優先使用穩定的系統 API／ADB 狀態來源，
  並對 Android 版本與廠牌 UI 差異提供可明確診斷的 fallback。

## Report

判定層將一個或多個 `Verdict.to_dict()` 寫成 evidence 內的 `verdicts.json`：

```json
{
  "verdicts": [
    {
      "tc": "TC-01",
      "status": "PASS",
      "reason": "",
      "expected": "android",
      "actual": "android",
      "evidence": "bid_raw.json"
    }
  ]
}
```

產生 report：

```bash
python3 page.py --local            # 立即生成並打開本機 report，不等 GitHub Pages
python3 page.py                    # 只生成 report.html
python3 page.py --evidence evidence /path/to/more/evidence --out report.html
```

本地入口固定是 repo 根目錄的 `report.html`；它是生成物，不進 Git。`--local` 與公開頁使用同一份
renderer、Catalog 和 Evidence，只省略 gh-pages publish／部署等待。

Page 只驗證並呈現 `BLOCKED`／`PASS`／`FAILED`，不 import platform runner，也不重新比較
expected/actual。沒有 `verdicts.json` 時會顯示「尚無 TC 判定」。

## 發布

手動發布由 `page.py` 執行：

```bash
python3 page.py --publish          # 產報告並推上 origin 的 gh-pages
python3 page.py --publish --no-open
```

單次 `capture` 不發布。使用 `round` 時，runner 會在整輪結束時自動呼叫一次
`page.py --publish`；即使某個 Step 失敗，也會先發布當下已有的結果，再以非零狀態結束，
並在錯誤訊息標出 Round 與 Step。只有本輪 Evidence folder 與 `verdicts.json` 都完成後才會
發布；若尚未建立本輪 Evidence 就失敗，不會重新發布上一輪舊結果。需要停用 round 的自動
發布時，設定 `AUTO_PUBLISH=0`。

完整流程是：

```text
capture（raw evidence）→ 分別解密需要檢視的 req_enc／ext_enc → 寫入 TC 與判定（verdicts.json）
→ python3 page.py --publish
```

## 專案架構

```text
Android Sample App
       │ Appium / ADB
       ▼
qa_aos.py ──執行 Round──────────────┐
testcases/android_signal_testcases.py ──宣告 Evidence─┤
evidence_aos.py ──操作手機、等待 bid─┤
                                    │
Phone → Charles :8888 → mitmdump :8081
                            │
                            └─ bid request / response / impression
                                    │
                                    ▼
                              raw evidence folder
```

一個 Round 會先將 TC 需要的 Evidence keys 去重，共用同一份 capture，再逐條產生 Verdict。

### 主要檔案

| 檔案 | 用途 |
|---|---|
| `qa_aos.py` | 執行 Android Round 與裝置自動化 |
| `qa_ios.py` | iOS runner、Round 框架與 raw evidence capture；TC 尚未完整覆蓋 |
| `testcases/testcase_catalog.json` | Report 與 TC metadata 的共用來源 |
| `testcases/testcase_specifications.md` | TC 規格、前提與品質限制 |
| `testcases/android_signal_testcases.py` | AOS Signal 比較邏輯、Evidence requirements 與 Round registry |
| `evidence_aos.py` | 取得 AOS 裝置狀態與共用 bid evidence |
| `evidence_bundle.py` | 封裝 AOS／iOS 的 raw 與 decoded evidence |
| `mitmdump_addon.py` | 攔截 bid request、bid response 與 impression callback |
| `apr_xorenc.py` | 解密 `ae1` 字串 |
| `verdict.py` | 定義 `BLOCKED`、`PASS`、`FAILED` 判定格式 |
| `page.py` | 讀取 Verdict 並產生靜態 HTML report |

## 限制與使用但書

### 目前支援範圍

- 目前只有 AOS 完成 Signal／E2E 自動化流程；iOS 尚未完成相同的 TC 覆蓋。
- Android 操作是依開發時使用的實機 UI 設計。不同 Android 版本、廠牌 ROM、系統語言或設定頁版型，可能找不到相同按鈕或畫面。
- 現有結果只代表指定 Sample App、SDK build、Campaign、CID 與測試手機的組合，不代表所有 Publisher App 與裝置。
- 實際覆蓋項目以 `testcases/testcase_catalog.json` 與 `testcases/testcase_specifications.md` 為準。
- 目前只支援單裝置、單一 suite 依序執行，不要同時跑多台手機或多個 capture。

### Automation 卡住時

- 防災警報、來電、鎖定畫面、通知、系統對話框或網路切換，都可能中斷 Automation。
- 目前不會自動關閉這些外力畫面。請先人工排除，再重跑受影響的 Scenario。
- 若畫面沒有繼續，請檢查手機、ADB、Appium、Charles、mitmdump、Proxy、網路與 CID 設定。
- 沒有 bid 或抓包失敗不一定是 SDK 問題，請先查看該輪 Evidence，不要直接判定為 `FAILED`。

### 執行後請確認手機狀態

- 部分 Scenario 會修改追蹤設定、深色模式、字體、亮度、音量、省電模式、時區或權限。
- Runner 會嘗試還原狀態；若中途停止、ADB 斷線或系統 UI 改變，可能無法完整還原。
- 異常中斷後，請人工確認手機狀態再執行下一輪。

### 如何解讀結果

| 情況 | 結果／處理方式 |
|---|---|
| TC 尚未開始 | 顯示未執行，不產生 Verdict |
| TC 已開始，但環境或 Evidence 不足，無法比較 | `BLOCKED` |
| Evidence 完整且符合標準 | `PASS` |
| Evidence 完整但不符合標準 | `FAILED` |
| 執行被外力干擾 | 人工排除後重跑，不直接視為 SDK `FAILED` |

Report 是判定與導覽工具，不能取代人工查看 raw Evidence。每次執行的適用範圍，應以當次
`summary.json`、`traffic.log`、截圖與 Test Run metadata 為準。
