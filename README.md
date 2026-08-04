# LazyAdFinder2 🎯

LazyAdFinder2 是 Appier Ads SDK 的實機 SSP Signal QA 工具，支援 Android（AOS）與 iOS。它會透過 Appium 操作 sample app、由 Charles 與 mitmdump 攔截 bid 流量、驗證 Signal/E2E TC，並將 request、response、截圖、裝置狀態與 log 整理成 evidence 和 HTML 報告。

> ## ⚠️ 目前是骨架
>
> 本專案是 LazyAdFinder 的**架構重建版**：機械層（佈狀態／capture／證據落地／解密／
> 發佈）照搬且可直接跑，**TC 目錄與判定的通過標準、HTML 版面刻意留空**，準備逐條重建。
>
> 現在跑起來會抓到真 bid、存下完整 evidence，但判定數字一律是 `0 pass / 0 fail / 0 blocked`。
> 哪些留空、以及怎麼從 baseline commit `8307b56` 把原版拉回來，見 `CLAUDE.md`
> 〈骨架狀態〉。本 README 其餘章節描述的是**架構與跑法**，那些都成立。

## 預設測試範圍

**完整 Signal 範圍是預設範圍，以各平台目前程式實作為準** —— 也就是該平台 TC 目錄的完整集合。
**目錄目前是空的，所以完整範圍是 0 條**；填回多少就驗多少。

除非執行時明確指定較小範圍或特定 TC，否則「完整 Signal 範圍」代表所選平台的完整集合。Android 一輪中的 CURRENT 批次預設也會執行 privacy icon、真實廣告點擊與 landing page 驗證。

計數依據是不同 TC ID；有些 TC 會驗證多個欄位，因此 validator 條數不等於 TC 數。

## 功能

- AOS Signal QA：擷取 bid、解開 `ext_enc`、驗證 AND-xx、執行 privacy/E2E 流程。
- iOS Signal QA：擷取 iOS bid 與 impression callback、驗證 IOS-xx。
- 完整 AOS round：自動建立所有需要的互斥裝置狀態，並將 Signal 與 E2E 結果合併成同一個 TC round。
- Evidence 與報告：每次 capture 保存原始證據、重算 round report，並生成整合平台。
- 自動發布：單次 capture 成功後預設更新 GitHub Pages；完整 round 改為整輪結束後發佈一次；可用 `AUTO_PUBLISH=0` 關閉。

線上報告：**尚未設定** —— 本專案還沒有 git remote，`page.py --publish` 會停在取不到 `origin`
（這也保證它不會誤推到 LazyAdFinder 的 Pages）。要發佈得先設好自己的 remote 與 `gh-pages`
分支，並把 `page.PAGES_URL` 改成新網址。

## 流量架構

```text
Phone → Charles :8888 → mitmdump/mitmdump_addon.py :8081 → Internet
             ↓                    ↓
      Charles 人工檢查       bid / impression / traffic capture
```

`mitmdump_addon.py` 辨識：

- Bid：`POST *.apx.appier.net/v2/sdk/aos/ad` 或 `/v2/sdk/ios/ad`
- `200`：有廣告；`204`：no-bid
- Impression win：`apn.c.appier.net/callback/show_cb`
- 其他 Appier 與 mediation network 流量只寫入 traffic log，不會被當成 bid

主要暫存檔位於 `/tmp`：

- `/tmp/appier_bid.json`
- `/tmp/appier_bid_status`
- `/tmp/appier_bid_response.json`
- `/tmp/appier_impression.json`
- `/tmp/appier_traffic.jsonl`

## 需求與安裝

- Python 3
- Charles Proxy
- mitmproxy 12+
- Appium 與對應 driver
- Android：ADB 與開啟 USB debugging 的裝置
- iOS：Xcode、已簽名的 WebDriverAgent；建議安裝 `libimobiledevice`

```bash
python3 -m pip install -r requirements.txt
npm install -g appium
appium driver install uiautomator2
appium driver install xcuitest
brew install libimobiledevice
```

完整的 macOS、Charles 與裝置信任設定見文末〈附錄：權限與裝置設定〉。

手機需將 Wi-Fi proxy 指向 Mac 的 IP、port `8888`，Charles 的 HTTP/HTTPS external proxy 則指向 `127.0.0.1:8081`。Android 也可使用：

```bash
adb shell settings put global http_proxy <MAC_IP>:8888
# 測試後清除
adb shell settings delete global http_proxy
```

## 啟動服務

執行 Signal QA 前，先開兩個 terminal：

```bash
# Terminal 1
mitmdump -s mitmdump_addon.py --listen-port 8081

# Terminal 2
appium
```

## Android Signal QA

必要環境變數：

```bash
export APP_PACKAGE=com.appier.android.sample
export APP_ACTIVITY=com.appier.android.sample.MainActivity
export TEST_TYPE=aibid                  # aibid / reen-static / reen-dynamic
export TEST_MODE=standalone             # standalone / admob-mediation / applovin-mediation
export TEST_CID='<campaign-cid>'
export TEST_ROUND=R1
```

`qa_aos.py` 是 AOS 的唯一入口。不帶參數就是「跑一輪」，範圍用旗標收窄：

```bash
python3 qa_aos.py                # 跑一輪：Signal + E2E 全驗（預設）
python3 qa_aos.py --signal-only  # 只驗 Signal：跳過 privacy 點擊、廣告點擊與 landing
python3 qa_aos.py --e2e-only     # 只驗 E2E：只跑 CURRENT 那一次 capture
python3 qa_aos.py AND-04         # 補跑這條狀態類 TC
python3 qa_aos.py AND-06,AND-08  # 逗號多選
```

Signal 與 E2E 在**判定層**是分開的（各自的 TC 目錄、判定規則與報告分頁），但在**執行層**
共用同一次 capture——要驗 E2E 就得有一個真的廣告可以點，而那一刻的 bid 同時就是 Signal
的證據。所以沒有「只跑 E2E 不抓 bid」這種模式：`--e2e-only` 仍會抓一次 bid，只是不跑其他
狀態批次、也不補跑失敗的 Signal TC。收窄範圍時，沒驗到的那一半會在報告裡標「本輪未執行 →
Blocked」，不會假裝通過。

互動 terminal 未提供 `TEST_TYPE`、`TEST_MODE`、`TEST_CID` 時，程式會詢問；非互動執行三者都必須設定。runner 會切到 `TEST_MODE` 對應頁籤，再點擊 `TRIGGER_TEXT`（預設 `Native - basic format`）。

常用選項：

| 變數 | 預設 | 用途 |
|---|---:|---|
| `DO_PRIVACY_CLICK` | CURRENT 批次為 `1` | 點 privacy icon 並保存證據 |
| `DO_E2E_FLOW` | CURRENT 批次為 `1` | 真實點擊廣告並驗證 landing |
| `MAX_AD_ATTEMPTS` | `0` | `0` 代表持續刷到指定 CID 命中 |
| `PHASE_TIMEOUT_SEC` | 單次 `0`；round 內 `1200` | 單一 capture 的牆鐘上限（秒）；到點乾淨收尾、不建立 Capture。`0` 代表不限 |
| `AD_RETRY_DELAY` | `2` | 每次重試間隔秒數 |
| `SAVE_ON_BID` | `0` | 設為 `1` 時取得 request 即保存，不要求 200/CID 命中 |
| `DWELL_SEC` | `0` | 觸發廣告前停留秒數 |
| `TEST_ROUND` | `R<YYYYMMDD>` | round 標籤，會成為資料夾名的一段；只保留英數與 `-_`，上限 24 字 |
| `EVIDENCE_DIR` | `./evidence` | evidence 根目錄 |
| `AUTO_PUBLISH` | `1` | 設為 `0` 時不發布 GitHub Pages |

## Android 完整 TC Round

不帶參數執行 `qa_aos.py` 就是完整範圍：排程在 `qa_aos.py`，會建立各 TC 所需的互斥裝置狀態、執行三組 session case 與 CURRENT 批次，再將所有 capture 合併到同一個 `TEST_ROUND` 和同一份 round report。失敗的 Signal TC 會自動嘗試補跑，結束時也會還原標準裝置狀態。每個 capture 都是新的 process 與新的 Appium session，狀態與 logcat 不會互相污染。

CURRENT、CTRL1、CTRL2、CTRL3、SD 僅是程式內部的狀態批次名稱（互斥狀態不能同時成立才要分批），不是獨立 TC round，也不需要分開執行或分開交付報告。

```bash
APP_PACKAGE=com.appier.android.sample \
APP_ACTIVITY=com.appier.android.sample.MainActivity \
TEST_TYPE=aibid \
TEST_MODE=standalone \
TEST_CID='<campaign-cid>' \
python3 qa_aos.py
```

- `START_AT=CTRL2`：維修或補跑時從指定內部狀態批次開始。
- `STOP_AFTER=CTRL3`：除錯時在指定內部狀態批次後停止。
- `MAX_FAILED_RETRIES`：失敗 Signal TC 的自動補跑次數，預設 `1`。
- `PHASE_TIMEOUT_SEC`：round 內每個 capture 的牆鐘上限，預設 `1200`（20 分鐘）。刷不到指定 CID 時該批乾淨收尾、TC 標「本輪未執行」，整輪繼續往下跑而不會卡死；`0` 代表不限。

round 期間會自動設 `AUTO_PUBLISH=0`，改為整輪結束後發佈一次 GitHub Pages——一輪有 7～11 個 capture，每個都重產平台並 push 太貴，也會把中間狀態推上線。

裝置狀態由 `qa_aos.py` 以 adb 建立並讀回確認，沒有人工 fallback：某組狀態建不起來（例如 VPN、GAID opt-out）就跳過該批 capture、不中斷整輪，對應的 TC 在報告裡是「本輪未執行 → Blocked」。

## iOS Signal QA

```bash
export BUNDLE_ID=com.appier.Random
export TEST_TYPE=aibid
export TEST_MODE=standalone
export TEST_CID='<campaign-cid>'
export TEST_ROUND=R1

python3 qa_ios.py
python3 qa_ios.py IOS-04 [UDID]
```

runner 會依 integration mode 推斷頁籤與 placement；sample app UI 不同時可用 `TAB` 和 `TRIGGER_LABEL`（或 `AD_LABEL`）覆蓋。iOS 目前 `MAX_AD_ATTEMPTS` 的程式預設是 `150`，設為 `0` 才會無限重試。

iOS bid 由 `qa_ios.py` 驗證；部分項目仍標示待實機校準。iOS round 目錄會加上 `IOS_` 前綴。

## 離線驗證

不碰實機，只重算判定或重產報告：

```bash
# Android
python3 qa_aos.py --inspect /tmp/appier_bid.json
python3 qa_aos.py AND-04 AND-46 --inspect /tmp/appier_bid.json
python3 qa_aos.py --inspect-round evidence/<round-directory>
python3 qa_aos.py --report evidence/<round-directory> --out report.html

# iOS
python3 qa_ios.py --inspect /tmp/appier_bid.json
python3 qa_ios.py --inspect-round evidence/<ios-round-directory>
python3 qa_ios.py --report evidence/<ios-round-directory> --out report.html
```

bid 裡的 `ext_enc`（data-signal）與 `req_enc`（ads SDK 的 req 區塊）都由 `apr_xorenc.py`
以同一套 `ae1` XOR 解碼後才驗證 —— 兩平台共用這一個解密入口。

## Evidence

```text
evidence/
  <MODE>_<TYPE>_CID_<CID>_<ROUND>_<timestamp>/
    round_report.txt
    round_timing.txt
    CURRENT_<timestamp>/
      bid_request.json
      bid_response.json
      device_state.txt
      phone.png
      logcat.txt / ios_syslog.txt
      report.txt
      results.json
      e2e_results.json
```

實際檔案會依平台、是否有 response、是否執行 E2E 與 capture 類型而不同。同一個 round prefix 的後續 capture 會歸入既有目錄；round report 以每個 TC 最新的 capture 結果彙總。

## 報告與發布

建立整合平台：

```bash
python3 page.py
python3 page.py --out artifact-platform.html
python3 page.py --evidence evidence ~/Desktop/LazyAdFinder_evidence
```

平台依下列維度分類 evidence：

- Platform：AOS / iOS
- Integration：Standalone / Mediation
- Campaign：AIBID / REEN-STATIC / REEN-DYNAMIC

發布 GitHub Pages（唯一交付）：

```bash
python3 page.py --publish
```

> ⚠️ 本專案尚未設定 remote，這條指令現在會停在取不到 `origin`。先建好自己的 GitHub repo
> 與 `gh-pages` 分支、加上 `origin`，並把 `page.PAGES_URL` 改成新網址再用。

發佈成功後會**自動開啟線上頁面**。不再產生本地 preview 檔——唯一的交付就是線上頁。
GitHub Pages 重建約需 1–2 分鐘，剛推完看到舊版重整即可。`OPEN_PAGES=0` 可關掉自動開啟。

`page.py --publish` 會 clone `gh-pages`、將最新平台寫成 `index.html`、commit 並 push。此操作需要有效的 GitHub 權限。

發佈前有兩道 sanity gate（與 `page.py --publish` 同一組）：產出的 `index.html` 小於 50 KB，或不含任何「已就緒」卡片（0 個 live report）時中止，不會用退化頁面覆蓋線上的好頁面。

## 主要檔案

全部只有 6 個 Python 檔：

| 檔案 | 行數 | 用途 | 骨架狀態 |
|---|---:|---|---|
| `qa_aos.py` | 3763 | Android runner：佈狀態、capture、證據落地 ＋ AOS 的 TC 目錄（AND-xx） | TC 目錄空、HTML 版面骨架；其餘照搬 |
| `qa_ios.py` | 1491 | iOS runner：同上 ＋ iOS 的 TC 目錄（IOS-xx） | 同上 |
| `verdict.py` | 470 | **判定與報告的共用契約**：check 實作、`classify()`、卡片/CSS/JS 版面 | check 通過標準與版面留空（`CHECKS` 詞彙表與 `classify()` 保留） |
| `apr_xorenc.py` | 220 | SDK 的 `ae1` 加解密（`ext_enc` / `req_enc`） | 照搬，可用 |
| `mitmdump_addon.py` | 189 | mitmproxy addon，攔 bid、impression 與 E2E traffic | 照搬，可用 |
| `page.py` | 785 | 跨平台整合頁 ＋ 發佈 `gh-pages` | 照搬（`--publish` 需先設 remote） |

架構原則：**按平台垂直分離 runner，但不複製共同語義。**

- 屬於平台（各自一份）：佈狀態、capture、證據落地、**TC 目錄**（欄位路徑／期望值／限制表／
  批次歸屬／分類）、bid 的 normalize
- 屬於契約（只有一份）：check 實作、判定狀態機 `classify()`、報告版面

規則：兩個 runner 之間零 import，各自可獨立跑完；平台檔只 import `apr_xorenc` 與 `verdict`；
`verdict.py` 不得偷讀平台全域表，平台資料一律用參數傳；`page.py` 不 import 平台檔，雙向都用
subprocess 呼叫對方 CLI；`mitmdump_addon.py` 必須獨立（`mitmdump -s` 的載入機制）。

## 實機執行規則

任何會操作真實裝置的 runner 執行前，都必須先確認：

- Platform：AOS / iOS
- Integration mode：standalone / AdMob mediation / AppLovin mediation
- Campaign type：AIBID / REEN static / REEN dynamic
- Test CID
- Scope：預設 Signal + E2E 完整範圍（AOS 84 + 15；iOS 82 + 15），也可 `--signal-only`／`--e2e-only`／指定 TC

讀取裝置與服務狀態可以先做；未完成上述確認前，不應點擊 ad placement。

---

## 附錄：權限與裝置設定

（原 本附錄；讓自動測試不被系統彈窗卡住）

這份清單只列**這台 Mac + 這支測試 iPhone**需要「按一次同意」的地方。全部都是
**一次性**設定——按過一次之後，之後每次跑 `qa_aos.py` / `qa_ios.py` /
`page.py` 都不會再跳出任何系統詢問。

判斷方式：下面每一項都附了「怎麼確認目前狀態」的指令，跑了才知道哪幾項還沒弄。

---

### A. Mac 系統設定

#### A1. Full Disk Access（必要 — 目前缺）

**現象**：讀取 `~/Desktop/...` 之類的資料夾時噴
`PermissionError: [Errno 1] Operation not permitted`（`evidence` 若指到
Desktop/Documents 以外的位置就會踩到；這次 `~/Desktop/LazyAdFinder_evidence`
已經踩到）。

**原因**：跑這些腳本的實際 App 是 **Terminal**（process tree：
`Terminal → login → zsh → …`），macOS 對 Desktop/Documents/Downloads/iCloud
等資料夾的存取是**按 App 授權**，不是按使用者；Terminal 目前只有部分資料夾權限，
且這種存取被拒絕時是**靜默失敗**（不會跳出可以點的對話框，因為背景 script 沒有
UI 可以觸發詢問），只能靠這條指令先手動開權限。

**設定步驟**（一次）：
1. 系統設定 → 隱私權與安全性 → **完整磁碟取用權限**（Full Disk Access）
2. 加入 **Terminal**（若清單沒有，點左下角 `+`，路徑
   `/System/Applications/Utilities/Terminal.app`）
3. 打勾啟用 → 完全關閉並重開 Terminal（Full Disk Access 生效需要重啟 App）

**確認指令**：
```bash
ls ~/Desktop >/dev/null 2>&1 && echo OK || echo DENIED
```

> 用的是 iTerm2 / VS Code 內建終端機而不是 Terminal.app？把對應的 App
> （`iTerm.app` / `Visual Studio Code.app` 等）加進同一個 Full Disk Access
> 清單，道理一樣。

---

#### A2. Local Network（建議先開，避免未來卡住）

**現象**：手機透過 Wi-Fi 連到 Mac 的 mitmdump（8081）/ Charles（8888）時，
macOS 第一次可能跳「Terminal 想要在區域網路上查找並連接裝置」的詢問。

**設定步驟**：
1. 系統設定 → 隱私權與安全性 → **區域網路**
2. 找到 Terminal，打勾啟用

**確認指令**：目前這次操作沒有實際觸發到（可能已授權，或這次連線模式沒用到
mDNS），但這是 mitmproxy 類工具常見的第一次執行提示，建議先開起來一次解決。

---

#### A3. 防火牆允許連入連線（目前不影響 — 防火牆本身是關的）

**現象**：如果之後有人把 Mac 的「應用程式防火牆」打開，`mitmdump` 第一次
監聽 port 收到外部連線（手機打過來）時，會跳「是否允許 mitmdump/python3
接受連入連線」的對話框。

**目前狀態**：
```bash
/usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate
## → Firewall is disabled. (State = 0)   ← 目前是關的，不會卡
```

**若之後防火牆被打開**，設定步驟：
1. 系統設定 → 網路 → 防火牆 → 選項
2. 加入 `python3`（`/opt/homebrew/bin/python3`）與 `mitmdump`
   （`~/Library/Python/3.14/bin/mitmdump`），設為「允許連入連線」

---

### B. iPhone 裝置設定（每一支實測手機各自要弄一次）

#### B1. Trust This Computer（已完成，此裝置免動作）

**現象**：USB 接上 Mac 第一次會在手機螢幕跳「信任這台電腦？」，要輸入
裝置密碼確認。沒信任的話 `idevice*` 系列工具會整個連不上。

**確認指令**：
```bash
idevicepair -u <UDID> validate
## → SUCCESS: Validated pairing with device <UDID>   ← 已通過
```

若換一支新手機測試，記得先用資料線接上、螢幕解鎖後點「信任」再開始跑腳本。

#### B2. 信任開發者憑證 / WebDriverAgent（已完成，此裝置免動作）

**現象**：第一次裝 Appium 自動簽的 WebDriverAgentRunner，手機會顯示
「未信任的開發者」，要去 設定 → 一般 → VPN 與裝置管理 手動信任。

**確認方式**：這支手機上已經裝著
`com.facebook.WebDriverAgentRunner.xctrunner`（跑過的痕跡），代表已經信任過。
換新裝置或新的簽名憑證（`XCODE_ORG_ID` 換人/換 Team）時要重新走一次
`README.md`「iOS 裝置」章節的手動簽名流程。

#### B3. Charles 憑證完全信任（已完成，此裝置免動作）

**現象**：裝了 Charles CA（`chls.pro/ssl`）後，iOS 15+ 還要多一步手動開關，
不然裝了憑證也不會生效：

設定 → 一般 → 關於本機 → 憑證信任設定 → 針對 Charles Proxy CA 開啟「完全信任」

**確認方式**：這次 Charles 接上正確 proxy 後有成功解密一般流量（非 pinned
主機），代表這支手機已經完成這步。

#### B4. App Tracking Transparency 授權彈窗（已在程式碼處理，非權限設定）

**現象**：全新安裝 / 重置過的 app 第一次要 IDFA 時，系統會跳「允許『追蹤』
您的活動嗎？」的彈窗——這**不是**要你去系統設定裡預先開，而是每個 app
第一次要 IDFA 時都會問一次（跟前面幾項「設定裡打勾一次就好」不同類）。

**這次改法**：`qa_ios.py` 已加上 Appium 的 `autoAcceptAlerts` capability，
啟動 session 時如果跳出任何系統彈窗（含 ATT）會自動接受，不需要人在旁邊點。
✅ 這項不需要你做任何操作，已經是程式碼層面解決。

---

### 目前狀態總結（2026-07-20 這次盤點）

| 項目 | 狀態 | 需要你做的事 |
|---|---|---|
| A1 Full Disk Access | ❌ 缺 | 系統設定加 Terminal，重開 Terminal |
| A2 Local Network | 未確認會不會卡 | 建議順手開一次 |
| A3 防火牆例外 | ✅ 不影響（防火牆本來就關） | 無 |
| B1 Trust This Computer | ✅ 已配對 | 無（新手機才要重做） |
| B2 信任開發者憑證 | ✅ 已信任 | 無（新手機才要重做） |
| B3 Charles 憑證完全信任 | ✅ 已生效 | 無（新手機才要重做） |
| B4 ATT 彈窗 | ✅ 程式碼已自動處理 | 無 |

**只剩 A1 需要你手動點一次**，弄完之後整條 `qa_ios.py` →
`qa_ios.py` → `page.py` 流程就不會再被任何系統對話框
擋住。
