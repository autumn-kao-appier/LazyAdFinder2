# LazyAdFinder2

LazyAdFinder2 是 Appier Ads SDK 的實機 SSP QA 重建專案。目前先建立乾淨的 automation
與 raw evidence 層；TC 目錄、正確標準、判定與報告將由人工逐條重新定義。

## 目前邊界

```text
Android sample app
       │ Appium / ADB
       ▼
qa_aos.py ──點 placement、等待 bid──┐
                                    │
Phone → Charles :8888 → mitmdump :8081
                            │
                            └─ bid request / response / impression
                                    │
                                    ▼
                              raw evidence folder
```

目前不產生 PASS、FAIL、BLOCKED，也不宣稱執行了任何 TC。

## 檔案責任

| 檔案 | 責任 |
|---|---|
| `qa_aos.py` | Android automation、Round 執行框架、raw evidence 擷取 |
| `qa_ios.py` | iOS automation、Round 執行框架、raw evidence 擷取 |
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

`qa_aos.py` 目前刻意保留空目錄：

```python
TC_DEFINITIONS = {}
ROUND_DEFINITIONS = {}
```

所以：

```bash
python3 qa_aos.py list-rounds
# No rounds defined.
```

只有在人工確認某條 TC 的 setup、證據與正確標準後，才加入定義。Automation engine 不得
自行推測 expected value，也不得把「沒有執行」包裝成測試結果。

## Raw evidence

成功 capture 會建立：

```text
evidence/<round>/MANUAL_<timestamp>/
  bid_request.json
  bid_response.json       # 有 response body 時
  bid_status.txt
  impression.json         # 有 impression callback 時
  logcat.txt
  phone.png
  ui.xml
  metadata.json
```

Evidence 保留原始觀察；判定層未來從 evidence 讀取，不回寫或改造原始 request。

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
允許保存這種 evidence，並在 `metadata.json` 明確記錄 `request_available: false`；
`--accept-request` 則仍要求確實取得 request body。

## Verdict contract

`verdict.py` 目前只定義三種對外結果：

- `BLOCKED`：Round／環境限制導致 TC 沒有執行。
- `PASS`：TC 已執行，實際值符合人工確認的正確標準。
- `FAILED`：TC 已執行，實際值不符合正確標準。

TC answer key 與 validator 仍為空；加入 TC 時才逐條補上。`page.py` 未來只呈現結構化
`Verdict`，不得自行重算答案。

已執行的 TC 呼叫 `evaluate(expected=..., actual=...)`，比較後必然得到 `PASS` 或
`FAILED`；只有因 Round／環境限制而根本沒執行，才呼叫 `blocked(reason=...)`。

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
      "evidence": "bid_request.json"
    }
  ]
}
```

產生 report：

```bash
python3 page.py
python3 page.py --evidence evidence /path/to/more/evidence --out report.html
```

Page 只驗證並呈現 `BLOCKED`／`PASS`／`FAILED`，不 import platform runner、不重新比較
expected/actual，也不負責發布。沒有 `verdicts.json` 時會顯示「尚無 TC 判定」。
