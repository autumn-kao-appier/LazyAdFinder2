# TestCase Specifications

## R5 — Alternate and Negative States

R5 保留 R1 Happy Path。每個裝置狀態 TC 都是可獨立還原的 Scenario，避免一項 mutation 或
Evidence 失敗連帶影響其他 TC；只有同一隱私狀態下的 GAID／LAT 仍共用 `PRIVACY-DENIED`：

- `PRIVACY-DENIED`：沿用 R1 正向 GAID 的 Settings → Security and privacy → Privacy controls → Ads
  路徑；現代 UI 執行 Delete advertising ID，舊版 UI 開啟 Opt out。動作後重新走同一路徑確認
  Renew/Get new 或 Opt out ON 的停用狀態；驗證 `device.ia` 不可為可用 GAID，且 req/ext
  `device.lat` 必須為 integer `1`。
- `DARK-MODE-ENABLED`、`FONT-SCALE-MAXIMUM`、`BRIGHTNESS-MINIMUM`、`VOLUME-MUTED`、
  `BATTERY-SAVER-ENABLED`：各自設定、抓包、判定與還原。最低有效 brightness raw 1，payload
  對照 `1 ÷ 255 = 0.0039215686`。
- `BRIGHTNESS-MAXIMUM`、`VOLUME-MAXIMUM`：分開執行。亮度保存 UI 百分比、display-service float
  與同步後 raw；音量獨立確認 Media volume current=max。任一項失敗不改變另一項 verdict。
- `TIMEZONE-CHANGED`：切換 `America/New_York`，依抓包當日 DST 動態計算 offset，完成後還原。
- `LOCATION-PERMISSION-DENIED`：同時收回 precise/approximate location；Android Permissions 頁須顯示
  Location 為 Not allowed，req/ext 均不得包含 `geo_lat` 或 `geo_lon`，即使值是 0/null 也 FAILED。

Runner 必須在每個 Scenario 後還原原狀態；中途失敗仍保存 Evidence、寫明失敗階段並發布報告。
`PRIVACY-DENIED` 的 `advertising-id-opt-out` 與 `tracking-denied` 是 AIBID-only TestCases。
REEN Static／Dynamic 的執行計畫、結果報告與總 TestCase 目錄不列出這兩條；其餘 R5 Scenario
與 AIBID 共用並照常執行。

## E2E campaign continuation

- `E2E-S14`：完成 tracked click 後驗證 Campaign 指定目的地。AIBID 到合法商店／安裝目的地；
  REEN 必須開啟 `TARGET_APP_PACKAGE` 指定的 App。
- `E2E-S15`：以自動保存的 BidObjectId、CID、曝光與點擊時間查詢 MMP Click Action。
- `E2E-S16`：沿用相同 correlation key 查詢歸因認列；AIBID 對 install，REEN 對 re-engagement。

## Evidence contract

所有 TC 的 Evidence 都必須依序呈現 `Expected`、`Captured Device State`、
`Decoded Bid Request` 與 `Comparison`。Captured Device State 必須獨立於 payload：優先採用
人眼可見的實機設定頁；沒有可靠頁面時才使用同時間的 OS 原始讀值，並明確標示來源。

這是人工可讀的 TC 規格；Catalog metadata 在 `testcase_catalog.json`，AOS 執行與比較邏輯在
`android_signal_testcases.py`。UI hierarchy 只允許執行當下定位，不保存為正式 Evidence。

## Advertising ID (GAID)

- Key: `advertising-id`
- Signal / R1 / AOS / P0
- Field: `device.ia`

目的：確認 SDK 傳送的是 Android Ads 頁面上人眼可見的 GAID。

Evidence：`ads-settings.png`、`ads-settings-state.json`、`bid_raw.json`、
`bid_decoded.json`、`verdicts.json`。

PASS：新版 Ad privacy 頁顯示有效 GAID 與 `Delete advertising ID`（舊版才檢查 Opt out 關閉）；設定頁、req、ext 三份 GAID 都存在、完全相同、是全小寫 UUID，且不是
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

## Installed App List

- Key: `installed-app-list`
- Signal / R1 / AOS / P1
- Field: `device.ext.applist`

目的：確認 Extended payload 的 Installed App List 若有傳送，資料形狀可被正確使用；同時允許
SDK／系統完全拿不到清單，以及使用者裝置經過 Android package visibility 與 Launcher 過濾後
真的沒有任何可回傳 App。不得把某次觀察到的 15 個套件、數量或順序寫死成答案。

Evidence：Round 開始時開啟 Android Settings 的所有 App 清單、稍微向下滑動並保存
`installed-apps-settings.png`，作為人眼可見的裝置狀態；`installed-app-list.json` 將抓包解密
結果整理成 collection status、package count 與完整清單，並保留 `bid_raw.json`、
`bid_decoded.json`、`verdicts.json`。設定頁使用 App 顯示名稱，而 SDK 受 Android package
visibility 與 Launcher 過濾影響，因此截圖是輔助 Evidence，不與 payload 做完整一對一比對。

PASS 有三種合法狀態：

1. `UNAVAILABLE`：`device.ext.applist` 欄位不存在。
2. `EMPTY`：欄位存在且為空陣列 `[]`。
3. `CAPTURED`：欄位是非空陣列，每項皆為唯一且格式合法的 package-name 字串。

FAILED：欄位存在但為 `null`、非陣列，或陣列包含非字串、空字串或重複套件。TC 已開始執行，
但沒有取得／解開 Extended payload，因而無法完成比較時是 BLOCKED；尚未開始執行時不產生 Verdict。

## In App Purchase History

- Key: `in-app-purchase-history`
- Signal / R1 / AOS / P1
- Field: `device.ext.iaphistory`

目的：確認 Extended payload 送出 BillingClient 查得的 in-app product IDs 與 subscription
product IDs 合併去重陣列。這不是包含時間、金額的完整交易歷史。

欄位缺少、`null`、非陣列、空字串、非字串或重複值為 FAILED。合法陣列（包含 `[]`）只能
證明欄位形狀成立；目前 Sample App 沒有購買流程或獨立 expected product IDs，無法驗證內容
正確性，因此結果為 BLOCKED。待 RD 增加測試商品、購買入口與可核對答案後，才判定
PASS／FAILED。

Evidence：`in-app-purchase-history.json` 顯示欄位狀態、數量及 product IDs，並保留 raw、decoded
與 verdict。SDK 的非同步 Billing 查詢失敗也可能維持空陣列，所以不得把 `[]` 判成 PASS。

## System Boot Timestamps

- Key: `boot-timestamps`
- Signal / R1 / AOS / P1
- Field: `device.ext.pot`

目的：確認 SDK 送出最多五筆 power-on timestamp。`pot` 是 epoch milliseconds 的開機時間，
不是 uptime 時長。

PASS：欄位存在且含 1～5 筆正整數；數列嚴格遞增且不得晚於 capture。Round 在抓包前以裝置
epoch time 減 `/proc/uptime` 獨立算出本次開機時間，`pot` 最後一筆必須在 ±120 秒內相符。
欄位缺少、空陣列、格式／順序錯誤或最新值不符為 FAILED；無法取得必要 payload 或獨立裝置
時間才是 BLOCKED。

Evidence：`boot-time-calculation.png` 上半部是 About phone → Uptime 的原生畫面隱私裁切，只
保留 Uptime 卡；含 IP、Wi-Fi MAC、Bluetooth address 的完整截圖不得寫入 Evidence。下半部
列出「裝置目前時間 − Uptime = 推算開機時間」，再與最新 `pot` 比較並顯示毫秒誤差。
`boot-timestamps.json` 保存精確計算資料。歷史舊值無法由當下系統狀態逐筆還原，只驗格式與順序。

## Resource Status: RAM and Disk

這一組共用一次送出 request 前的 Android 系統採樣與設定頁截圖，但維持四條獨立 Verdict，讓
單一欄位失敗不會掩蓋其他欄位結果。所有 payload 值的單位都是 bytes。

### RAM Status (Total)

- Key: `ram-total`
- Field: `device.ext.mem_total`

PASS：值為正整數，且與 `/proc/meminfo` 的 `MemTotal × 1024` 相差不超過 2%。

### RAM Status (Available)

- Key: `ram-available`
- Field: `device.ext.mem_available`

PASS：值為正整數且不大於同包 payload 的 `mem_total`；與 request 前立即讀取的
`MemAvailable × 1024` 相差不超過 `max(RAM total 10%, 512 MiB)`。這是動態值，不要求逐 byte
相等。

### Disk Storage (Total)

- Key: `disk-total`
- Field: `device.ext.disk_total`

PASS：值為正整數，且與 `df -k /data` 的 1 KiB blocks 換算所得 App data filesystem 總容量
相差不超過 2%。

### Disk Storage (Free)

- Key: `disk-free`
- Field: `device.ext.disk_free`

PASS：值為正整數且不大於同包 payload 的 `disk_total`；與 request 前讀取的 `/data` 可用容量
相差不超過 `max(Disk total 2%, 512 MiB)`。capture 本身會寫檔，所以不要求逐 byte 相等。

四條各有獨立的人眼 Evidence：`mem-total-evidence.png`、`mem-available-evidence.png`、
`disk-total-evidence.png`、`disk-free-evidence.png`。RAM 圖直接保留 `/proc/meminfo` 的原始
`MemTotal`／`MemAvailable` 行並列出 `kB × 1024 = bytes`；Pixel Settings 沒有即時 Total／
Available 頁面，不拿 App 平均用量冒充答案。Disk 圖上半部保留 Android Storage 的 Total／Used
原生畫面；Free 圖明列 `Total − Used ≈ Free`。下半部才使用同一時間的精確 OS bytes 與 SDK
對答案。`resource-status.json` 保存讀值、來源與容差。

任一欄位缺少、型別錯誤、關係不合法或超出容差為 FAILED；獨立系統讀值或 payload 無法取得，
導致 TC 未真正執行時才是 BLOCKED。

## Battery and Display Status

六條皆為 Signal / R1 / AOS / P1，分成兩次共用 Evidence capture。

- `battery-level` / `device.batterylevel`：整數 0～100；與 Android Battery 頁及 `dumpsys battery`
  的 level 相差不超過 2%。Evidence：`battery-settings.png`。
- `charging-status` / `device.charging`：整數 0/1；任一 AC／USB／Wireless／Dock powered 時為
  1。100% 顯示 Full／Charged 但仍接電源也視為 charging=1。Evidence：同一 Battery 原生頁。
- `battery-saver` / `device.ext.battery_saver`：JSON boolean，與 Battery Saver 原生開關及
  `settings get global low_power` 完全一致。舊表 `disk.ext.batterysaver` 是錯誤路徑。
- `screen-width` / `device.sw`、`screen-height` / `device.sh`：正整數 pixels，等於直向 capture
  的 Android display size。Evidence 卡保留真實手機截圖並直接標出原圖 1080×2424 pixels，
  分別對照 payload width／height；`wm size` 保存精確 OS 答案。
- `screen-ppi` / `device.ppi`：正整數，等於 `wm density` 的 Android logical density DPI；這不是
  面板物理 PPI。若裝置型號已有官方規格 mapping，再以 ±5% 比對 logical density 與官方物理
  PPI，作為合理性檢查；未收錄型號只略過輔助檢查，不 BLOCK。Pixel 10a 官方值為 422.2 PPI。
- `pixel-ratio` / `device.pxratio`：等於 logical density ÷ 160；Pixel 10a 為 420 ÷ 160 = 2.625。
- `screen-brightness` / `device.ext.screen_bright`：原生 Display 頁顯示感知百分比；同時由
  `dumpsys display` 保存該狀態的 float brightness，並由 `BrightnessSynchronizer` 保存
  float ↔ legacy integer 對照。SDK 值驗證 integer ÷ 255，容許 1/255 誤差；不得直接宣稱
  UI 70% 等於 38÷255。
- `font-scale` / `device.ext.fontscale`：與 Android `system font_scale` 相等；設定頁輔助人眼確認。
- `dark-mode` / `device.ext.darkmode`：JSON boolean，與可見 Dark theme 開關及 UI night mode 一致。
- `gyroscope`、`accelerometer`：P2 / BLOCKED / Not In Scope。本輪沒有感測器動作與獨立正確
  樣本，即使 payload 是空陣列也不判 PASS。

## Audio, Device Identity, Timezone and Language

- `output-volume` / `device.ext.volume`：Android Media volume 的 current ÷ max，正規化為 0～1；
  容許一個音量級距誤差。Sound & vibration 的 Media volume 滑桿是人眼 Evidence。
- `device-make` / `device.make`：req/ext 均須等於 `ro.product.manufacturer`。
- `device-model` / `device.model`、`device.hwv`：req/ext 的 model 與 hwv 均須等於
  `ro.product.model`；About phone 是人眼 Evidence。
- `default-timezone` / `device.utcoffset`：req/ext 均須等於 capture 當下 `date +%z` 轉換的
  UTC offset 分鐘數。答案隨系統時區改變，不固定為 480。
- `default-language-iso` / `device.lang` / System Language Code：只驗 Settings 第一順位系統語言的 ISO-639-1 語言部分；例如 English (Japan) → `en`。
- `default-language-bcp47` / `device.langb` / System Language and Region Tag：驗 Settings 第一順位系統語言＋地區的完整 BCP 47；例如 English (Japan) → `en-JP`，req/ext 均須完全相同。答案不得由 payload 反推或固定寫死。

共用 `device-context` capture 會保留 Sound、About phone、Date & time、Languages 四個原生頁面，
並將 OS 精確值、換算式與 decoded bid 組合成各自可翻頁的 Evidence 卡。

## Keyboard, Integrity, Network, Location and Session

- `keyboard-languages`：Gboard Languages 畫面與 enabled subtype BCP 47 tags 必須和
  `device.input_lang` 陣列順序一致。
- `root-status`：Magisk 畫面與 `su -c id` 是獨立答案；`device.ext.jailbreak` 必須是相同 boolean。
- `emulator-detection`：由 qemu/product hardware properties 判斷；實體 Pixel 應為 false。
- `connection-type`：Android active default transport 必須和 req/ext `device.conntype` 一致。
- `carrier`、`mcc-mnc`：本 QA device 無 active SIM 時必須為空字串；若日後插入 SIM，需由
  cellular round 定義 populated value，不能沿用空字串規則。
- `ipv6-address`：AOS 會呼叫 Appier 自有的 IPv6 endpoint
  `https://adx6.apx.appier.net/v2/sdk/net` 取得公網 IPv6，不依賴 SIM，也不是讀取手機 local IP。
  同一輪必須保存 endpoint request／HTTP 200 response，並驗證 response `ipv6` 是合法 IPv6、且與
  decoded `ext.device.ipv6` 完全相同。若目前網路無法連到 IPv6-only endpoint，屬環境前提不足而
  BLOCKED；endpoint 已成功回覆但 payload 缺值、格式錯誤或不一致則 FAILED。IPv4 依需求排除。
- AOS／iOS `R4` 共用六條判定、五步同一 App session 的 IPv6 refresh 契約：IPv6 Address、冷啟動、Wi-Fi A→B、
  斷線恢復、快速 A→B→A→B、slow-network A→B。兩個平台各自以原生 runner 實作；程式負責
  每步等待、送廣告 request、保存 payload 與比較，操作者只負責 Wi-Fi／hotspot／throttle
  checkpoint。AOS 每一步另須保存 Appier adx6 net probe response，並與 ext.device.ipv6 相等。
- `R4` 第一個 capture 若確認測試網路沒有合法 IPv6，六條全部 BLOCKED（環境前提不足）。一旦
  IPv6 環境成立，已執行步驟缺值、格式錯、保留舊 IP、request 被阻擋或 App crash 都是 FAILED。
- `precise-gps-latitude`、`precise-gps-longitude`：R1 先授予 Sample App 定位權限，再以 Android
  fused last-known location 與 accuracy 作為獨立答案。正確 payload 路徑是
  `ext.device.geo_lat` / `ext.device.geo_lon`；兩點距離須在 `max(accuracy, 200m)` 內。
  `device.lat` 是 tracking flag，不能當緯度。
- `network-latency`：R1 必須保存 SDK 對
  `https://cr.adsappier.com/4QGDNtuHG/icon/Info.svg` 的 HEAD 200 response，且解碼後
  `device.ext.latency` 必須是大於 0 的整數毫秒值。缺 probe 或欄位即 FAILED；不得用 bid RTT 代替。
- `foreground-session-duration`：BLOCKED。需 SampleApp 提供獨立 session start timestamp，並先
  確認 `user.session_duration` 單位；只檢查 payload 是正數不構成正確性驗證。

## Advertising Tracking Allowed

- Key: `tracking-allowed`
- Signal / R1 / AOS / P0
- Field: `device.lat`

目的：確認 Ads 頁顯示允許個人化廣告時，SDK 不會宣告限制廣告追蹤。與 GAID 沿用同一次
設定與 capture。

Evidence：`tracking-allowed.png`、`ads-settings-state.json`、`bid_raw.json`、
`bid_decoded.json`、`verdicts.json`。

PASS：人眼可見的 Advertising ID 為可用狀態（新版顯示有效 GAID 與 `Delete advertising ID`；舊版 Opt out 為 OFF），代表「允許追蹤」。`device.lat` 的名稱是 Limit Ad
Tracking，語意相反，所以 req/ext 各自必須是 JSON 整數 `0`（未限制）或欄位真正不存在。
`null`、字串、布林或其他數字均為 FAILED；設定頁或 payload 無法取得才是 BLOCKED。

## SDK Version

- Key: `sdk-version`
- Signal / R1 / AOS / P1
- Field: `app.sdk_version`

目的：先從 request 擷取 SDK 版號，再由 reviewer 在報告中輸入本次 Sample App build 應使用的
Expected 版號。Expected 不得從 request 反推。

Evidence：`sdk-build-info.json`、`bid_raw.json`、`bid_decoded.json`、`verdicts.json`。

未輸入 Expected 時為 BLOCKED。輸入後，`req.plaintext.app.sdk_version` 存在、非空且完全相等
為 PASS；不相等或缺值為 FAILED。
