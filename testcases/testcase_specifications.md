# TestCase Specifications

## R5 — Alternate and Negative States

R5 保留 R1 Happy Path，使用四個可交易式還原的 Scenario。每項 mutation 與 validator 仍獨立
記錄，一項失敗不連帶改寫同包其他 TC；Scenario 完成後反向還原並驗證原值：

- `PRIVACY-DENIED`：沿用 R1 正向 GAID 的 Settings → Security and privacy → Privacy controls → Ads
  路徑；現代 UI 執行 Delete advertising ID，舊版 UI 開啟 Opt out。動作後重新走同一路徑確認
  Renew/Get new 或 Opt out ON 的停用狀態；驗證 `device.ia` 不可為可用 GAID，且 req/ext
  `device.lat` 必須為 integer `1`。
- `DISPLAY-HIGH`：Dark Mode ON、Font Scale 最大、Brightness 最大、Volume 最大，共用一個 Bid。
- `DISPLAY-LOW`：Brightness 最低與 Volume 靜音，共用一個 Bid；最低有效 brightness raw 1，payload
  對照 `1 ÷ 255 = 0.0039215686`。
- `SYSTEM-ALT`：Battery Saver ON、Timezone `America/New_York`、Location Denied，共用一個 Bid。
  Battery Saver 不得與任何 Brightness TC 放在同一包。Timezone 依抓包當日 DST 動態計算 offset；
  Location 同時收回 precise/approximate permission，Android Permissions 頁須顯示
  Location 為 Not allowed，req/ext 均不得包含 `geo_lat` 或 `geo_lon`，即使值是 0/null 也 FAILED。
- `PRIVACY-DENIED`：永遠最後執行並獨立抓包。

Runner 必須在每個 Scenario 後反向還原原狀態並重新讀值驗證；單項 mutation 失敗仍允許同包其他
TC 擷取與判定。任一 restore 失敗時，後續 Scenario 必須停止並顯示未執行，避免狀態污染。
`PRIVACY-DENIED` 的 `advertising-id-opt-out` 與 `tracking-denied` 是 AIBID-only TestCases。
REEN Static／Dynamic 的執行計畫、結果報告與總 TestCase 目錄不列出這兩條；其餘 R5 Scenario
與 AIBID 共用並照常執行。

iOS R5 每個 TestCase 必須產生同名的 `*-evidence.png` 可視化卡片。卡片固定包含原生設定的
修改前、反面狀態、還原後三階段畫面（某階段無法執行時明確顯示 `NO SCREENSHOT`）、state
read-back、Request／Extended payload、Comparison verdict 與 BLOCKED／FAILED 原因。原始檔
`ios-settings-before.png`、`ios-settings-state.png`、`ios-settings-restored.png`、
`ios-settings-state.json` 與 `bid_decoded.json` 必須保留；只有 payload 或只有通用設定頁截圖
都不足以宣稱反面 Case PASS。若 mutation 在擷取前即不可執行，仍須產生 BLOCKED 證據卡，
不得只留下 `verdicts.json`。

在 AIBID Mediation 一般 R5 中，`PRIVACY-DENIED` 固定產生 BLOCKED verdict，Automation 不得
刪除 GAID。完整 Mediation 與 E2E 結束後，suite 預設以 Standalone 執行獨立 `R5-1`；同一個
run ID 的結果可作為共用 Android SDK Signal Evidence 回填 Mediation 卡片。`R5-1` Renew GAID
後整輪必須結束，不得再送 Mediation request。使用 `--privacy-verification manual` 時不執行
`R5-1`，卡片保持 BLOCKED，等待使用者透過人工覆寫入口填入經覆核的結果。

## E2E campaign continuation

- `E2E-S14`：完成 tracked click 後驗證 Campaign 指定目的地。AIBID 到合法商店／安裝目的地；
  REEN 必須開啟 `TARGET_APP_PACKAGE` 指定的 App。
- `E2E-S15`：以自動保存的 BidObjectId、CID、曝光與點擊時間查詢 MMP Click Action。
- `E2E-S16`：沿用相同 correlation key 查詢歸因認列；AIBID 對 install，REEN 對 re-engagement。

### iOS AOS-aligned E2E evidence

iOS E2E 與 AOS 使用相同的證據責任邊界：serving／tracking／mediation 結果由保存的 request、
response、proxy event 與 correlation IDs 判定；Native render、Privacy、CTA 與 Landing 才由
實機畫面與完整操作錄影證明。畫面只能支援 network TC，不能取代缺少的 HTTP event。

每個 iOS E2E TC 必須額外產生同名 `*-evidence.png`，作為 reviewer-facing 摘要入口：

- Baseline 10 條分別保存 init、Appier flow、creative、render、impression、click、landing、privacy
  與兩條 attribution 的 testcase-specific card。
- Mediation 6 條分別保存 pubsetting、GMA routing、GMA→Appier、Google impression、fill result 與
  Google click 的 testcase-specific card。
- 卡片至少包含 Expected、Captured Actual、原 validator artifact、traffic session hash/event count、
  共用 MP4 的 saved/valid/bytes、interaction timeline、相關階段截圖及原 verdict。
- `standalone-privacy` 顯示 ad-before、privacy destination、return-to-ad 三階段；click／landing 顯示
  before-click 與 final destination。缺少階段時明確顯示 `NO SCREENSHOT`。
- Attribution 尚未執行授權的 MMP／backend query 時，仍產出包含 click destination 與 lookup IDs 的
  BLOCKED 卡；不得把「查詢資料已備妥」顯示成 PASS。
- 原始 `e2e-network-evidence.json`、`mediation-network-evidence.json`、`appier-ad-flow.json`、
  `attribution-query.json`、互動截圖、`e2e-interactions.json`、`e2e-interactions.mp4`、raw bodies 與
  `verdicts.json` 必須保留。卡片不取代這些可稽核來源。

renderer 失敗不得改寫 E2E 判定，錯誤記錄於 `evidence-errors.json`；正常產出後才將該 verdict 的
`evidence` 指向 testcase-specific PNG。中途失敗但已建立 evidence bundle 的 E2E round 亦須嘗試
產生 FAILED／BLOCKED 卡片。

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

## Advertising Identifier (IDFA) — iOS

- Key: `advertising-id`
- Signal / R1 / iOS / P0
- Field: `device.ia`

目的：確認 SDK 傳送的 IDFA，與獨立 GetMyIDFA App 顯示的值完全相同。

iOS 原生 Settings 不顯示完整 IDFA；Settings → Privacy & Security → Tracking 只能作為 ATT
授權狀態的可見 Evidence。因此 R1 先開啟 `com.pag3dev.GetMyIDFA`，保存完整 IDFA 畫面與
automation 讀值，再回到 Sample App 保存 ATT 狀態並擷取同輪 Bid。GetMyIDFA 顯示全零、權限提示、
找不到唯一 UUID 或截圖失敗時，本 TC 為 BLOCKED，不得拿 Bid payload 自證。

Evidence：`ios-idfa.png`、`ios-idfa-state.json`、`ios-settings-state.png`、
`bid_raw.json`、`bid_decoded.json`、`verdicts.json`。

PASS：Sample App ATT 為 authorized，GetMyIDFA 顯示非空、非全零 UUID，且該值與 req/ext
`device.ia` 完全相同。獨立 IDFA 已取得但 Bid 缺值、格式錯誤或不一致為 FAILED；GetMyIDFA
畫面、Sample App ATT 狀態或流量無法取得時為 BLOCKED。

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

### iOS current scope

iOS 沿用同一個 semantic key `app-set-id`，但報告顯示平台正確名稱「供應商識別碼（IDFV）」。
目前只檢查解密後的 Extended `device.ifv` 存在，且為 UUID `8-4-4-4-12` 格式；接受
iOS API 常見的大寫 canonical form，也接受等價的小寫格式。Evidence 使用 `app-set-id.json`、
`bid_decoded.json` 與 `verdicts.json`；
這是封包讀取結果，不是獨立來源對答案。

若未來要求可截圖、可由人眼獨立核對的 iOS Evidence，必須請 RD 在 iOS Sample App 增加顯示
該值的 QA 測試入口，保存畫面與 machine-readable 讀值，再要求 Sample App 顯示值完全等於
`device.ifv`。在該入口完成前，報告不得宣稱已獨立證明來源值。

## Installed App List

- Key: `installed-app-list`
- Signal / R1 / AOS / P1
- Field: `device.ext.applist`

iOS 不適用：不納入 iOS Round，不產生 iOS verdict 或 Evidence。

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

iOS 比照同一判準：欄位缺少或陣列格式錯誤為 FAILED；合法陣列（包含 `[]`）
因沒有購買流程與獨立 expected product IDs，維持 BLOCKED。Evidence 同樣產生
`in-app-purchase-history.json`。

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

iOS 目前拿不到肉眼可見 Evidence，因此使用解碼後 payload 做技術驗證：`pot` 含
1～5 筆嚴格遞增的正整數 epoch milliseconds 即 PASS；缺值、空陣列、數量超限、
型別或順序錯誤為 FAILED。`boot-timestamps.json` 必須保留這個 Evidence 限制說明。

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

iOS 的 RAM 兩張目前拿不到肉眼可見 Evidence，改用解碼後 payload 結案。`mem_total`
為正整數 bytes 即 PASS；`mem_available` 為正整數 bytes 且不大於 `mem_total` 即 PASS。
缺值、型別錯誤、非正數或數值關係不合法為 FAILED。Evidence 分別使用 `ram-total.json`
與 `ram-available.json`，並保留無可視 Evidence 的限制說明。

### Disk Storage (Total)

- Key: `disk-total`
- Field: `device.ext.disk_total`

iOS 不適用：不納入 iOS Round，不產生 iOS verdict 或 Evidence。

PASS：值為正整數，且與 `df -k /data` 的 1 KiB blocks 換算所得 App data filesystem 總容量
相差不超過 2%。

### Disk Storage (Free)

- Key: `disk-free`
- Field: `device.ext.disk_free`

iOS 不適用：不納入 iOS Round，不產生 iOS verdict 或 Evidence。

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

iOS `battery-level` 在發送 Bid 前從右上角拉下控制中心，保存
`ios-battery-level.png` 並從 accessibility tree 讀取可見百分比到
`ios-battery-level.json`。Payload 值必須為 0～100 的有限數值，且與畫面值差異
不超過 2% 才 PASS；有畫面答案但 payload 錯誤為 FAILED，無法取得控制中心證據為 BLOCKED。

iOS `charging-status` 沿用同一次控制中心觀察，另外保存 `ios-charging-status.png` 與
`ios-charging-status.json`。JSON 必須保留電池 accessibility 原文及解析出的 charging boolean；
`device.charging` 必須是 0／1 或 boolean-compatible 值，且 req/ext（存在時）皆與可見狀態一致。
控制中心沒有明確的電池語意、截圖缺少或無法唯一判讀時為 BLOCKED；有完整可視答案但 payload
缺少、格式錯誤或不一致時為 FAILED。R1 只觀察，不為了測試而插拔電源。

iOS `battery-saver` 在發送 Bid 前開啟原生 Settings 的 Low Power Mode 頁，以非破壞方式讀取
目前 switch，保存 `ios-low-power-mode.png` 與 `ios-low-power-mode.json`。Payload
`device.ext.battery_saver` 必須是 JSON boolean，且與可見開關完全一致。R1 不改變開關；需要驗證
啟用狀態的 mutation 仍由 R5 `battery-saver-enabled` 獨立執行並還原。缺少可視 Evidence 為
BLOCKED；Evidence 完整但 payload 錯誤為 FAILED。
- `screen-width` / `device.sw`、`screen-height` / `device.sh`：正整數 pixels，等於直向 capture
  的 Android display size。Evidence 卡保留真實手機截圖並直接標出原圖 1080×2424 pixels，
  分別對照 payload width／height；`wm size` 保存精確 OS 答案。
- `screen-ppi` / `device.ppi`：正整數，等於 `wm density` 的 Android logical density DPI；這不是
  面板物理 PPI。若裝置型號已有官方規格 mapping，再以 ±5% 比對 logical density 與官方物理
  PPI，作為合理性檢查；未收錄型號只略過輔助檢查，不 BLOCK。Pixel 10a 官方值為 422.2 PPI。
- `pixel-ratio` / `device.pxratio`：等於 logical density ÷ 160；Pixel 10a 為 420 ÷ 160 = 2.625。

iOS 四條沿用 AOS「Bid 前獨立抓系統狀態、Bid 後合併 payload、每條產一張比較卡」的模式，
共用 `ios-display-status.json` 與 `ios-display-source.png`。R1 必須保持直向，Bid 前以 XCUITest
保存 logical width／height points，以 `ideviceinfo ProductType` 對應附 Apple 官方 URL 的 native
resolution 與 physical PPI。未收錄 ProductType、非直向、缺少可視截圖或任一直接來源無法取得時，
四條皆不得從 payload 自證，應為 BLOCKED。

- iOS `screen-width`／`screen-height`：Request `sw/sh` 分別等於 XCUITest logical points；Extended
  `sw/sh` 分別等於 Apple native pixels。WDA PNG dimensions 僅作 supporting observation，不作唯一答案。
- iOS `screen-ppi`：Extended `ppi` 直接等於 Apple physical PPI；Request 允許缺少，若存在亦須相等。
  iOS 不套用 AOS logical density 對 physical PPI 的 ±5% 規則。
- iOS `pixel-ratio`：expected 分別由 `native_width ÷ logical_width` 及
  `native_height ÷ logical_height` 推導；兩軸須在 0.000001 內一致，req/ext `pxratio` 亦須一致。
  Evidence 卡的角色等同 AOS `wm density ÷ 160` 公式卡。
- iOS `screen-brightness` / `device.ext.screen_bright`：Bid 前以 read-only 方式開啟原生
  Display & Brightness 頁，將頁面往下捲到滑桿完整位於 screenshot viewport 內，再保存截圖與
  accessibility 百分比。Expected 為百分比 ÷ 100，
  payload 必須是 0～1 的有限數值並在 0.01 內相等；Request 若存在亦須相等。R1 不改變亮度。
  缺少畫面或無法讀取滑桿為 BLOCKED；畫面完整但 payload 錯誤為 FAILED。
- iOS `font-scale` / `device.ext.fontscale`：Bid 前以 read-only 方式開啟原生 Accessibility 的
  Larger Text 頁，保存目前 Dynamic Type 滑桿狀態。該畫面是可視 Evidence，但滑桿位置不是
  API 定義的 exact multiplier，不得直接把百分比映射成例如 `1.24`。在獨立 API bridge／QA probe
  經 review 前，合法正數且 req/ext 一致仍為 BLOCKED；缺值、非正數或不一致為 FAILED。
- iOS `dark-mode` / `device.ext.darkmode`：Bid 前以 read-only 方式開啟原生 Display & Brightness
  的 Appearance 區塊，同時保存 Light／Dark 畫面與 accessibility selected state。Light 對應
  `false`、Dark 對應 `true`；payload 必須是 JSON boolean 且與唯一可見選擇完全一致，Request
  若存在亦須一致。無法唯一解析選取狀態或缺少畫面為 BLOCKED；Evidence 完整但 payload 錯誤為 FAILED。
- `screen-brightness` / `device.ext.screen_bright`：原生 Display 頁顯示感知百分比；同時由
  `dumpsys display` 保存該狀態的 float brightness，並由 `BrightnessSynchronizer` 保存
  float ↔ legacy integer 對照。SDK 值驗證 integer ÷ 255，容許 1/255 誤差；不得直接宣稱
  UI 70% 等於 38÷255。
- `font-scale` / `device.ext.fontscale`：與 Android `system font_scale` 相等；設定頁輔助人眼確認。
- `dark-mode` / `device.ext.darkmode`：JSON boolean，與可見 Dark theme 開關及 UI night mode 一致。
- AOS／iOS `gyroscope`、`accelerometer`：P2 / BLOCKED / Not In Scope。本輪沒有感測器動作與
  reviewed expected samples；即使 payload 是空陣列或帶有樣本，也只觀察、不判 PASS。

### iOS AOS-aligned visual cards

其餘原本只有 JSON 的 iOS TC，也必須產生同名 `*-evidence.png`，但卡片只能可視化 AOS 已採用的
證據範圍，不得把 payload 自己包裝成獨立裝置答案：

- R1 `app-set-id`、`in-app-purchase-history`、`boot-timestamps`、`ram-total`、`ram-available`
  顯示 wire-format／數值關係、Actual 與 verdict，並明確標示 `NO INDEPENDENT SCREEN`。IDFV／IAP
  與 AOS 同為 payload contract；boot／RAM 在 iOS 沒有對等於 `/proc/uptime`、`/proc/meminfo` 的
  harness source，因此卡片不提升其獨立性。
- R1 `gyroscope`、`accelerometer` 固定顯示 `NOT IN SCOPE` 與 BLOCKED 原因；卡片只保存設計決策，
  不代表曾執行感測器動作。
- R2 `impression-history`、`network-latency` 將同一次 Automation 的第一則曝光、第二包 Bid 與
  proxy event／probe verdict 整理成卡片；畫面是 supporting capture，因果答案仍來自原始事件檔。
- R3 五條 lifecycle TC 各產一張卡，並列 START、CONTINUOUS、BACKGROUND、TERMINATED 四個 capture
  及 `ios-lifecycle-sequence.json` 的 values/rule。原始各步 Bid 必須保留。
- R4 六條 IPv6 TC 各產一張卡，並列 LAUNCH、WI-FI SWITCH、RECOVERY、DEBOUNCE、SLOW NETWORK
  最多五個 capture，以及每步 decoded IPv6／conntype。`r4-network-sequence.json` 與各步 bundle
  仍是可稽核原始來源；卡片不取代 Appier probe 契約。

renderer 失敗不得改寫 Signal verdict；錯誤記錄於 `evidence-errors.json`，正常卡片產出後才把該
verdict 的 `evidence` 指向 testcase-specific PNG。

## Audio, Device Identity, Timezone and Language

- `output-volume` / `device.ext.volume`：Android Media volume 的 current ÷ max，正規化為 0～1；
  容許一個音量級距誤差。Sound & vibration 的 Media volume 滑桿是人眼 Evidence。
- iOS `output-volume`：Bid 前以 read-only 方式保存控制中心的媒體音量滑桿及 accessibility
  百分比，不使用 Sounds & Haptics 的鈴聲音量。Expected 為百分比 ÷ 100，payload 必須在 0～1
  且於 0.01 內相等；Request 若存在亦須相等。無法唯一讀取滑桿或缺少截圖為 BLOCKED，
  Evidence 完整但 payload 錯誤為 FAILED。
- `device-make` / `device.make`：req/ext 均須等於 `ro.product.manufacturer`。
- iOS `device-make`：Bid 前保存原生 Settings > General > About 的可見 Model Name，並將同輪
  `ideviceinfo ProductType` 對應至 Apple-hosted 官方機型規格。三者一致時建立 manufacturer
  expected=`Apple`；Extended `device.make` 必須精確相等，Request 若存在亦須相等。未收錄
  ProductType、About 讀不到唯一 Model Name、兩者不一致或缺少截圖時為 BLOCKED。
- `device-model` / `device.model`、`device.hwv`：req/ext 的 model 與 hwv 均須等於
  `ro.product.model`；About phone 是人眼 Evidence。
- iOS `device-model`：與 device-make 共用 About／ProductType／Apple 官方 mapping。Extended
  `device.model` 必須等於可見且官方的 Model Name，Extended `device.hwv` 必須等於 ProductType；
  Request 欄位若存在亦須相等。Request `device.model` 允許不傳。
- `default-timezone` / `device.utcoffset`：req/ext 均須等於 capture 當下 `date +%z` 轉換的
  UTC offset 分鐘數。答案隨系統時區改變，不固定為 480。
- `default-language-iso` / `device.lang` / System Language Code：只驗 Settings 第一順位系統語言的 ISO-639-1 語言部分；例如 English (Japan) → `en`。
- `default-language-bcp47` / `device.langb` / System Language and Region Tag：驗 Settings 第一順位系統語言＋地區的完整 BCP 47；例如 English (Japan) → `en-JP`，req/ext 均須完全相同。答案不得由 payload 反推或固定寫死。

共用 `device-context` capture 會保留 Sound、About phone、Date & time、Languages 四個原生頁面，
並將 OS 精確值、換算式與 decoded bid 組合成各自可翻頁的 Evidence 卡。

iOS R1 使用另一份 read-only `ios-system-context` capture 保存 Date & Time、Language & Region、
Keyboards、Wi-Fi、Cellular、VPN & Device Management、Location Services 七個原生頁面。Timezone
以 `ideviceinfo` 的 IANA timezone 在 capture 當下換算（含 DST）；`Locale` 正規化為 BCP 47，
並與原生語言頁共同建立 `device.lang`／`device.langb` 的 expected。鍵盤則只接受已 review 的
可見名稱到 tag 對照，保留畫面順序；遇到無法映射的鍵盤就 BLOCKED，不從 payload 補答案。

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

iOS 對應規則：

- `root-status` 保留卡片但 BLOCKED。原生 Settings 與 physical ProductType 無法證明「沒有 jailbreak」；
  要 PASS 必須另有已 review 的 integrity probe。
- `emulator-detection` 以 libimobiledevice 可取得的 iPhone／iPad／iPod ProductType 證明是實機；
  req/ext `device.ext.emulator` 必須為 JSON boolean `false`。
- `connection-type` 以 Settings > Wi-Fi 的已勾選網路作為 expected=`wifi`；`carrier`／`mcc-mnc`
  在 Settings > Cellular 清楚顯示 No SIM 時只接受空字串或欄位不存在。active SIM 需另訂精確契約。
- `vpn-status` 以 VPN & Device Management 的 Connected／Not Connected 分別對應 wire 字串
  `"1"`／`"0"`；畫面不明確時 BLOCKED。
- `connection-type-cellular` 在目前無 active SIM 的 R1 為 BLOCKED／Not executable；不可用 Wi-Fi
  capture 宣稱 cellular 結果。
- `precise-gps-latitude`／`precise-gps-longitude` 保留 Location Services 與 payload 卡片，但
  BLOCKED。系統頁不顯示精確座標；需 Sample App 增加獨立 coordinate QA surface 才能比較。
- `last-foreground-times`／`last-background-times` 保留 payload 可視化卡片但 BLOCKED；需 Sample App
  輸出獨立 callback timeline 才能逐筆核對，不能只靠陣列格式判 PASS。
- `force-gdpr-override`／`coppa-applies` 保留 Actual 卡片但 BLOCKED；需 Sample App 提供可視且可控的
  configured input。Request 不能證明自身輸入設定。

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

iOS 沿用相同的反向 LAT 語意，但獨立來源改為原生
Settings > Privacy & Security > Tracking 的 Sample App 開關，以及同輪 GetMyIDFA 顯示的非零
IDFA。R1 僅觀察、不替使用者開啟追蹤：開關未開或可視來源缺失時，`tracking-allowed` 前提未成立，
結果為 BLOCKED。開關為 authorized 且可見 IDFA 完整後，req/ext `device.ia` 必須與可見值完全相同，
req/ext `device.lat` 各自只能是 JSON 整數 `0` 或欄位不存在；boolean、字串、`null` 或其他數字為 FAILED。

## SDK Version

- Key: `sdk-version`
- Signal / R1 / AOS / P1
- Field: `app.sdk_version`

目的：先從 request 擷取 SDK 版號，再由 reviewer 在報告中輸入本次 Sample App build 應使用的
Expected 版號。Expected 不得從 request 反推。

Evidence：`sdk-build-info.json`、`bid_raw.json`、`bid_decoded.json`、`verdicts.json`。

未輸入 Expected 時為 BLOCKED。輸入後，`req.plaintext.app.sdk_version` 存在、非空且完全相等
為 PASS；不相等或缺值為 FAILED。

iOS 的 `sdk-version` 與 `argus-sdk-version` 同樣不可從 payload 反推 Expected。R1 會把 Actual
與缺少 reviewer/build-manifest expected 的原因做成獨立可視化卡片，並維持 BLOCKED；等 reviewer
提供本次 build 的 Ads SDK／Argus SDK 版號後，才可做精確相等判定。
