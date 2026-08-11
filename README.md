# LazyAdFinder2

LazyAdFinder2 是 Appier Ads SDK 的實機 SSP QA 重建專案。TC、正確標準、Evidence 與報告
由人工逐條定義；目前 AOS R1 已有七條 Signal TC。

## 目前邊界

```text
Android sample app
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

一個 Round 先將 TC 所需的 Evidence keys 去重，只 capture 一包，再逐條產生 Verdict。

## 檔案責任

| 檔案 | 責任 |
|---|---|
| `qa_aos.py` | Android automation engine；讀 registry 並執行 Round |
| `qa_ios.py` | iOS automation、Round 執行框架、raw evidence 擷取 |
| `testcases/testcase_catalog.json` | Page 與 TestCase Catalog 的跨平台 metadata 單一來源 |
| `testcases/testcase_specifications.md` | 所有 TC 的人工可讀規格與品質限制 |
| `testcases/android_signal_testcases.py` | AOS Signal 比較邏輯、Evidence requirements 與 Round registry |
| `evidence_aos.py` | 所有 AOS Evidence providers；負責去重、手機狀態與共用 bid capture |
| `evidence_bundle.py` | AOS/iOS 共用 Evidence bundle 格式與 raw/decoded 檔案封裝 |
| `mitmdump_addon.py` | 攔截 bid request、bid response 與 impression callback |
| `apr_xorenc.py` | `ae1` 密文字串進、UTF-8 明文字串出 |
| `verdict.py` | `BLOCKED`／`PASS`／`FAILED` 三態與結構化判定結果契約 |
| `page.py` | 讀取結構化 Verdict，統計三態並產生靜態 HTML report |

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
export APP_ACTIVITY=com.appier.android.sample.MainActivity
export TEST_MODE=standalone
export TEST_TYPE=aibid
export TEST_CID='<campaign-cid>'
```

REEN Static／Dynamic 使用相同的 R1–R5 與 E2E runner。執行 REEN E2E 時另指定最終必須開啟的 App：

```bash
export TEST_TYPE=reen-static   # 或 reen-dynamic
export TARGET_APP_PACKAGE='<promoted-app-package>'
```

REEN 的 R5 只跳過 Advertising ID opt-out 與 tracking-denied；其他裝置狀態 Scenario 仍會執行。

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
- `--max-attempts`：`0` 表示不限制次數。
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

## iOS raw capture

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
