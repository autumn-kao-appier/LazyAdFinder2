# LazyAdFinder — Claude 專案設定

## 檔案架構（6 個 .py，勿再增加）

```
qa_aos.py          Android runner：佈狀態 → capture → 證據落地 ＋ AOS 的 TC 目錄（AND-xx）
qa_ios.py          iOS runner：同上 ＋ iOS 的 TC 目錄（IOS-xx）
verdict.py         判定與報告的**共用契約**：check 實作、classify()、卡片/CSS/JS 版面
apr_xorenc.py      SDK 的 ae1 加解密（ext_enc / req_enc）
mitmdump_addon.py  mitmdump addon（攔 /v2/sdk/{aos,ios}/ad、impression、全流量）
page.py            跨平台整合頁 ＋ 發佈 gh-pages
```

**邊界原則：按平台垂直分離 runner，但不複製共同語義。**

| 屬於平台（各自一份） | 屬於契約（只有一份） |
|---|---|
| 佈狀態、capture、Appium/adb/idb、證據落地 | check 實作（`run_validator`：regex / int_range / absent / array_timestamp…） |
| **TC 目錄**：欄位路徑、期望值、限制表、批次歸屬、分類 | **判定狀態機 `classify()`** —— PASS / FAIL / BLOCKED |
| bid → 統一欄位樹的 normalize | 報告版面（`render_card` / `CSS` / `js_block`）、`FIELD_SCHEMA`、`CATEGORIES` |

硬規則：

1. `qa_aos.py` 與 `qa_ios.py` **零 import**，兩平台各自可獨立執行完畢。
2. 平台檔只 import `apr_xorenc`（SDK 規格）與 `verdict`（判定/報告契約）。
3. **`verdict.py` 不得偷讀平台的全域表**，平台資料一律用參數傳
   （`classify(tc, has_capture, passed, blocked_tcs)`、`tier_of(check, tc, blocked_tcs)`）。
   忘了傳會拿到空集合、症狀明顯；若改成全域註冊，平台漏註冊就會靜默把 gap 判成產品 FAIL。
4. `page.py` 不 import 平台檔；雙向都用 subprocess 呼叫對方 CLI。
5. `mitmdump_addon.py` 必須獨立（`mitmdump -s` 的載入機制）。

> 為什麼要有第 3 條與這整條邊界：check 實作與版面是**共同語義**，兩平台各留一份就會出現
> 「同一個值在 AOS 判 PASS、在 iOS 判 FAIL」的靜默不一致。2026-08 的 `req_enc` 事故正是
> 同一類：解密被複製成 6 份，SDK 一改只有其中幾份壞掉、AOS 靜默產生 52 條假 FAIL。
> 解密同理只准一個入口 `apr_xorenc.decode_bid()`（`ENC_FIELDS` 加一行即支援新的 `xxx_enc`）。

## 關鍵字「刷」＝啟動整條自刷 QA 流程

使用者打「**刷**」（或「刷 aos」/「刷 ios」）時：

1. 用 **AskUserQuestion** 依序問參數（能從「刷 aos」推斷的跳過該題）：
   - **平台**：AOS / iOS
   - **投放類型**：aibid / reen-static / reen-dynamic
   - **整合模式**：standalone / admob-mediation / applovin-mediation
   - **Test CID**：**一律問自由輸入**。不可把 CID 欄位換成「過去用過的清單」讓人選，
     舊值只能當範例。非互動執行時 `TEST_CID` 不能空。
   - **範圍**：預設 **Signal + E2E 全驗**（AOS：84 Signal TC + 15 E2E TC；iOS：82 + 15）。
     可收窄成 `--signal-only`（跳過 privacy／廣告點擊／landing，快很多）、`--e2e-only`
     （只跑 CURRENT 那一次 capture）、或指定狀態 TC（`AND-04,AND-06`）。
     **收窄時沒驗到的那半會標「本輪未執行 → Blocked」，不可當成通過交付。**
2. 把答案設成環境變數，**非互動**執行：
   - **完整範圍** → `python3 qa_aos.py`（不帶參數；需 `APP_PACKAGE` / `APP_ACTIVITY`）。
     依序佈狀態並逐批 capture（CURRENT/CTRL1/CTRL2/CTRL3/SD：深色模式／電量／充電／省電／時區／亮度／
     字級／音量／語系／定位／GAID opt-out／session…），對 fail 自動 retry，合併成一份 round。
     設不起來的狀態（VPN/GAID/SIM/root/AVD）**跳過該批、不卡**。可用 `START_AT`/`STOP_AFTER`
     補跑單一批次。env：`TEST_TYPE`/`TEST_MODE`/`TEST_CID`/`TEST_ROUND`。
   - **補跑指定 TC** → `python3 qa_aos.py AND-04,AND-06`（只做那一次 capture、不佈狀態）
   - **iOS** → `python3 qa_ios.py`（`BUNDLE_ID=com.appier.Random`）
   > ⚠️ 別拿「補跑 TC」去交完整範圍，那 ~34 條狀態類 TC 會全落成「本輪未執行」。
   > 完整範圍就是**不帶參數**。對外只有這兩種用法，沒有別的模式。
   > 我從工具端執行、無法回答互動 stdin；所以由我在對話問參數再帶 env 跑
   > （三個 env 未設且非 TTY 時腳本直接 exit，不會卡在 `input()`）。
3. **刷完自動 `python3 page.py --publish`** —— 重產平台並部署到 GitHub Pages（`gh-pages` 分支）。
   無需另發 claude.ai artifact。

> ⚠️ GitHub Pages 目前是**公開**的（repo public），平台內嵌 IDFA/IDFV/裝置 MAC/GPS/截圖
> 等敏感資料 —— 使用者已知悉並選擇維持公開自動部署。

## 實機執行前必須確認（不可從上一輪推斷）

跑 `qa_aos.py` / `qa_ios.py` 或任何會操作真實裝置的指令前，**先把設定攤出來給使用者確認**：

- 平台、整合模式、投放類型、Test CID、範圍

使用者只說「跑 AOS」/「執行一次」/「跑吧」時，**不可沿用上一輪的設定**。舊設定可以當預設值
提出，但仍必須確認。**唯讀**的裝置／服務檢查（`adb devices`、port 檢查）可以在確認前先做；
**確認完成前不可點擊 ad placement**。

已獲得的常設授權（不必每輪再問）：所有測試點擊，含 privacy icon、真實廣告點擊、開啟 landing
頁／App。round 裡的 CURRENT 批次預設同時開 privacy 與 E2E。

刷廣告**不設次數上限**，刷到指定 CID 命中為止；單獨跑一次 capture 也不設上限。**唯一例外**是
完整 round：每個 capture 有 20 分鐘牆鐘上限（`PHASE_TIMEOUT_SEC`，0＝不限），到點乾淨收尾、
該批標「本輪未執行」，整輪繼續而不卡死（無人值守用）。

## 改 TC 的規則：一次只動一條，不准順手改別的

使用者說要動某一條 TC 時：

1. **只改那一條。** 不順手改其他 TC、不順手重構、不順手改判定邏輯或報告版面，
   即使旁邊那條看起來明顯也有問題 —— 看到了就**講出來**，等使用者決定要不要一起處理。
2. **改動機械上必然牽動別處時，先停下來說明、等確認**，不要自己判斷「這樣比較好」就做。
   典型情況：
   - 把 TC 從某個批次的 scope 移出／移入 → 會改變既有 round 的判定（可能 FAIL→BLOCKED）
   - 改 validator 的 `check` 類型或欄位路徑 → 會動到 `verdict.py` 的共用 check 實作，
     影響所有用同一個 check 的 TC，甚至另一個平台
   - 把 TC 加進／移出 `BLOCKED` / `RD_GAP` → 改變 `BLOCKED_ALL`，影響 `classify()` 與 `tier_of()`
3. **唯一例外**：使用者明確說「這幾條有關聯」時，才可以一起改；範圍仍以使用者講的為限。
4. 改完只回報「這條改了什麼、判定怎麼變」，不要附帶一批未經同意的其他改動。

> 為什麼：TC 的定義又多又互相牽連，一次混多條改動時，判定數字一起變，就分不出
> 「這條修好了」和「順手弄壞了別的」。逐條對的價值就在於每次只有一個變因。

## 判定原則：報告只由「本次 scope」決定 pass / fail / block

報告永遠對照**完整 TC 目錄**呈現，但每條 TC 的狀態由本次 run 實際做了什麼決定。
判定狀態機在 `qa_aos.py` 的 `classify()`：

1. **有 eligible capture（這輪有做）**：值符合 → **PASS**；值不符 → **FAIL**。
   FAIL **失敗一次就算**，不看次數（別把單次 mismatch 降級成 block，否則真失敗如
   AND-67 `sdk_version=None` 會被藏掉）。
2. **無 capture（這輪沒做）** → **BLOCKED**。

> signal 判定**只有這三種**。`classify()` 產不出 PENDING／MANUAL，報告端也不該再有那兩條軌。
> E2E 的 `observe` 算 PASS、`pending`/`backend`/`gated`/`na_*` 算 BLOCKED —— 權威是 `E2E_SCORE`，
> **stdout 摘要必須跟報告 scorecard 同一套映射**（曾有兩套並存，終端印 E2E 4 pass、報告寫 7 pass）。

**BLOCKED 的定位非常窄——只有「清楚的限制」或「這輪根本沒做」：**

- **RD/硬體限制**（`BLOCKED` ∪ `RD_GAP`，恆 block，即使抓到空值也不算 FAIL）：
  - RD 沒做：SDK 未實作、值恆 null/[]（AND-42/43 感應器、AND-51 impression_history、
    AND-38 Not in this Release、AND-61 latency、AND-14 vpn…）
  - 硬體不可得：沒 SIM（AND-39/41/64）、需 AVD（AND-12）、需非 root 機（AND-10）
- **本輪未執行**：這輪沒佈該狀態／沒跑該情境（狀態類 TC 只跑了 CURRENT 批次；AND-47 未跑 SD）。
  **這不是缺證據**——是這輪沒做。跑 `python3 qa_aos.py`（完整 round）它們就會變 PASS/FAIL。
- **整合模式/投放目的不適用**：E2E 依 `TEST_MODE`/`TEST_TYPE` 自動判 `na_mode`/`na_type` → BLOCKED
  （例：standalone 輪的 mediation-only TC-02/03/08/16）。權威在 `qa_aos.py` 的 E2E 目錄區段
  （`E2E_TCS` 的 `modes`/`types` 欄位 + `evaluate()`）；**不要**在報告區段另立硬編 standalone 清單。
  REEN 輪 opt-out signal TC 走 `TYPE_NA_REEN`（標 N/A，計入 Blocked tile）。

> 平台「總覽卡」層級：**完全沒 round 的 cell** 標「未執行 / No run」（`page.render_card`），
> 不可顯示成 0/0/84 Blocked——那會跟真的 blocked 混淆。

> **Signal 與 E2E 的分工**：判定層分開（`VALIDATORS`＋`classify()` vs `evaluate()`＋`E2E_SCORE`，
> 報告也是兩個分頁）；執行層**共用同一次 capture** —— E2E 要有真廣告可點，那一刻的 bid 同時
> 就是 Signal 證據；且 E2E 的 init 只在 app 冷啟首次載入時發送、E2E 點擊會把 app 帶去外部
> Chrome 再回來（見 commit fc05c08）。所以**不要把 runner 拆成 Signal／E2E 兩支入口**，
> 範圍用旗標表達。

## 命名契約

**兩層都用到「輪」這個詞，別搞混：**

| 層級 | 是什麼 | 名字 |
|---|---|---|
| 外層資料夾 | **一次交付**（一次 Run 的全部產出，彙總成一份報告） | `TEST_ROUND` 標籤，預設 `R<YYYYMMDD>` |
| 內層 capture | **批次**（一次佈狀態＋抓一次 bid） | `CURRENT` / `CTRL1` / `CTRL2` / `CTRL3` / `SD` |

批次語意與**強制順序**：

| 批次 | 是什麼 | 關鍵約束 |
|---|---|---|
| `CURRENT` | 手機**當下的真實狀態**，不佈也不還原；每次跑到的狀態都可能不同 | **必須排最前面**。排在 CTRL 之後會繼承 CTRL2 的 GAID opt-out／VPN on／暗色殘留，那是實驗殘骸不是使用者狀態。也因此 `CURRENT_TCS` 只能收「不隨裝置狀態改變」的欄位 |
| `CTRL1` | 受控：預設／低／允許（GAID opt-in、100% 不充電、VPN off、台北、淺色/最暗/小字/靜音/給定位） | |
| `CTRL2` | 受控：相反／高／拒絕（GAID opt-out、0%、省電、VPN on、紐約、深色/最亮/大字/最大音量/拒定位） | REEN 輪跳過 opt-out 兩條（與投遞互斥） |
| `CTRL3` | 受控：充電中／UTC | |
| `SD` | **Session Duration**（`user.session_duration` 三情境，AND-47-1/2/3） | 不是狀態批次而是行為序列：必須抓兩個 bid 對照（A → 動作 → B）才驗得出累進/重置，故產出 3 個 capture、6 個 bid；情境 2 會 force-stop 殺 App，不能與別的批次共用 capture |

> **期望值假設狀態的 TC 不可掛在 `CURRENT`。** `AND-01`（`device.ia` 需合法 UUID）與
> `AND-75`（`device.lat` 需為 0）假設 GAID opt-in，已移出 CURRENT、只在 CTRL1 驗（CTRL1
> 會 `ensure_tracking(True)`）。留在 CURRENT 的話，手機平常是 opt-out 就每輪多兩條假 FAIL。
> `AND-40`（`conntype=wifi`）與 `AND-73`（`country=tw`）仍屬環境假設，改連線方式或地區時
> 會亮紅燈——那是有用的訊號，刻意保留。

capture 資料夾名＝批次名：`CURRENT_<ts>`／`CTRL1_<ts>`／`CTRL2_<ts>`／`CTRL3_<ts>`／
`AND-47-{1,2,3}_<ts>`（SD 的 capture 按 TC 命名），補跑加 `_RETRY<n>`，補跑指定 TC 為
`AND-04+AND-06_<ts>`。

**歷史名稱都要繼續認得**（`LEGACY_BATCH` / `batch_prefixes()`），否則舊 round 會對不上、
那些 TC 全被誤判 BLOCKED：`CTRL1/2/3 ← R1/R2/R3 ← M1/M2/M3`、
`CURRENT ← AUTO ← BASELINE/baseline_`。`declared()` 也要認 `CURRENT`/`AUTO`/`BASELINE`
三種宣告值。`baseline_` 前綴對**所有** label 都接受（舊 baseline-only round 只有那一個
capture）。

round 標籤 `TEST_ROUND` 未設時是 `R<YYYYMMDD>`，會被消毒成只含英數與 `-_`（上限 24 字）。

## 已知落差

- **iOS 沒有 round 排程**：`qa_ios.py` 目前只做單次 capture，所以 iOS「完整範圍 82 條」實際
  跑不出來，只覆蓋不需佈狀態的欄位；狀態類 TC 要逐條人工佈。待補（AOS 的 CURRENT/CTRL1/CTRL2/CTRL3/SD
  對應實作）。
- **沒有測試**。目前唯一的回歸機制是「拿既有 evidence 重算，判定數字必須不變」。動任何判定或
  報告邏輯前，先跑一次全部 round 的重算並比對逐條 `(tc, field, passed, actual)`。
- 三條長期真失敗待與 test plan／RD 確認：`AND-67 ext.app.sdk_version`（路徑在任何座標系都不
  存在）、`AND-44 device.ext.boottime`（SDK 未送）、`AND-48 user.session_duration`（期望 <5 但
  harness 本身就先花 ~5.5s，測試設計矛盾）。

## 慣例

- `artifact-*.html` 與 `evidence/` 是生成物／測試資料，已 gitignore，不進 repo。
- 開發直接在 `main`；平台部署在 `gh-pages`。
- 環境設定（Mac Full Disk Access、iPhone 信任、Charles 憑證）見 `README.md` 附錄。
