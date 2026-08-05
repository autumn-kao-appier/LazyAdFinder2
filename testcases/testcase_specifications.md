# TestCase Specifications

這是人工可讀的 TC 規格；Catalog metadata 在 `testcase_catalog.json`，AOS 執行與比較邏輯在
`android_signal_testcases.py`。UI hierarchy 只允許執行當下定位，不保存為正式 Evidence。

## Advertising ID (GAID)

- Key: `advertising-id`
- Signal / R1 / AOS / P0
- Field: `device.ia`

目的：確認 SDK 傳送的是 Android Ads 頁面上人眼可見的 GAID。

Evidence：`ads-settings.png`、`ads-settings-state.json`、`bid_raw.json`、
`bid_decoded.json`、`verdicts.json`。

PASS：Opt out 關閉；設定頁、req、ext 三份 GAID 都存在、完全相同、是全小寫 UUID，且不是
全零。已執行但任一條件不符為 FAILED；設定頁或流量無法取得才是 BLOCKED。

## Vendor ID (App Set ID)

- Key: `app-set-id`
- Signal / R1 / AOS / P0
- Field: `device.ifv`

目的：確認 SDK 從 Google Play services App Set ID API 取得值，並放入 Extended payload 的
`device.ifv`。App Set ID 可能因移除 App、長期未使用、恢復原廠
設定或 scope 變更而重置，所以不得把某次觀察值寫死成答案。

目前 Evidence：`app-set-id.json` 顯示解密後的實際值，並保留 `bid_raw.json`、
`bid_decoded.json`、`verdicts.json`。

目前 PASS：`ext.plaintext.device.ifv` 存在、非空，且符合全小寫 UUID `8-4-4-4-12` 格式。
已執行但缺值或格式不符為 FAILED；沒有取得／解開 Extended payload 才是 BLOCKED。

品質限制：目前 Evidence 是單純抓包並解密 `device.ifv`，驗證的是「有抓到且格式合理」，
不是完整的來源對答案。若需要可截圖、可由人眼獨立核對的證據，需請 RD 在 Sample App 增加
顯示 App Set ID 與 scope 的測試入口；Evidence 保存該畫面截圖與讀值，並要求畫面值完全等於
`ext.device.ifv`。在此能力完成前，不得宣稱本 TC 已獨立證明 Google API 原值。

## Limit Ad Tracking Flag (tracking allowed)

- Key: `tracking-allowed`
- Signal / R1 / AOS / P0
- Field: `device.lat`

目的：確認 Ads 頁顯示允許個人化廣告時，SDK 不會宣告限制廣告追蹤。與 GAID 沿用同一次
設定與 capture。

Evidence：`ads-settings.png`、`ads-settings-state.json`、`bid_raw.json`、
`bid_decoded.json`、`verdicts.json`。

PASS：人眼可見的 Opt out 為 OFF，req/ext 的 `device.lat` 各自必須是 JSON 整數 `0` 或欄位
真正不存在。`null`、字串、布林或其他數字均為 FAILED；設定頁或 payload 無法取得才是
BLOCKED。

## SDK Version

- Key: `sdk-version`
- Signal / R1 / AOS / P1
- Field: `app.sdk_version`

目的：確認 request 宣告的 SDK 版號等於本次 Sample App build 使用的版號。預設人工確認值為
`2.2.0`；測試其他 build 時必須用 `EXPECTED_SDK_VERSION` 提供獨立答案，不得從 request 反推。

Evidence：`sdk-build-info.json`、`bid_raw.json`、`bid_decoded.json`、`verdicts.json`。

PASS：`req.plaintext.app.sdk_version` 存在、非空，且完全等於 expected build version。不符為
FAILED；request 無法取得／解碼才是 BLOCKED。
