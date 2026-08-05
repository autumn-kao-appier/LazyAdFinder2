# Advertising ID (GAID)

- Layer: Signal
- Round: R1
- Platform: AOS
- Category: A. Core Identifiers
- Field: `device.ia`
- Priority: P0
- Status: Implemented

## Purpose

確認 SDK 傳送的是這台 Android 裝置畫面上實際可見的 GAID，而不只是格式相似的 UUID。

## Setup

1. 開啟 `com.google.android.gms.settings.ADS_PRIVACY`。
2. 找到 `Opt out of Ads Personalization`，若開啟則關閉。
3. 讀回確認 switch 為 `checked=false`。
4. 找到畫面上的 `Your advertising ID:`。
5. 保存包含完整 GAID 與 opt-out 開關的 `ads-settings.png`。
6. 啟動 Sample App，進行一次 AIBID capture。

UI hierarchy 只用於執行當下定位與讀值，不保存為 Evidence。

## Evidence

- `ads-settings.png`：主要人工 Evidence；畫面直接顯示 GAID 與 opt-out 開關。
- `ads-settings-state.json`：執行當下由可見頁面讀取的 GAID／switch 狀態，供離線重驗。
- `bid_raw.json`：未修改的 `req_enc`／`ext_enc`。
- `bid_decoded.json`：兩份解密後的 `device.ia`。
- `screenshot.png`：廣告完成顯示畫面。
- `traffic.log`、`summary.json`：本輪時間線與環境摘要。
- `verdicts.json`：結構化比較結果。

## PASS

以下條件必須全部成立：

1. `Opt out of Ads Personalization` 為關閉。
2. 設定頁 GAID、`req.plaintext.device.ia`、`ext.plaintext.device.ia` 都存在。
3. 三者完全相同。
4. 三者符合全小寫 UUID `8-4-4-4-12` 格式。
5. 值不是 `00000000-0000-0000-0000-000000000000`。

## FAILED

TC 已執行，但任一 PASS 條件不成立，包括缺欄位、全零、大寫、格式錯誤或三者不相等。

## BLOCKED

Round 因環境限制未能執行，例如 Ads 頁無法開啟、GAID 沒有出現在可見畫面、無法讀回
personalization switch，或 capture 環境不可用。BLOCKED 不得偽裝成產品 FAILED。

## Verified run

2026-08-04 在 Pixel 10a / Android 16 實機完成 R1，設定頁、req 與 ext 三方 GAID 相同，
結果為 PASS。
