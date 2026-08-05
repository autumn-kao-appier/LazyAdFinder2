# Limit Ad Tracking Flag (tracking allowed)

- Layer: Signal
- Round: R1
- Platform: AOS
- Category: A. Core Identifiers
- Field: `device.lat`
- Priority: P0
- Status: Implemented

## Purpose

確認 Android Ads 頁面顯示允許個人化廣告時，SDK 不會在 request 宣告限制廣告追蹤。
本 TC 與 Advertising ID 沿用同一次設定與 capture，不重新操作手機或重抓流量。

## Human-visible ground truth

`ads-settings.png` 必須讓人眼直接看到 Ads 頁面上的
`Opt out of Ads Personalization` 開關處於關閉狀態。UI hierarchy 只用於執行當下定位與
讀值，不保存為正式 Evidence。

## Evidence

- `ads-settings.png`：主要人工 Evidence；顯示 opt-out 開關為 OFF。
- `ads-settings-state.json`：執行當下由可見頁面讀取的 switch 狀態，供離線重驗。
- `bid_raw.json`：同一次 capture 的未修改 `req_enc`／`ext_enc`。
- `bid_decoded.json`：檢查 req/ext 各自的 `device.lat`。
- `verdicts.json`：同時保存本輪各 TC 的結構化比較結果。

## PASS

以下條件必須全部成立：

1. 人眼可見的 `Opt out of Ads Personalization` 為關閉。
2. `req.device.lat` 是 JSON 整數 `0`，或欄位真正不存在。
3. `ext.device.lat` 是 JSON 整數 `0`，或欄位真正不存在。

## FAILED

TC 已執行，但 req/ext 任一份出現 `device.lat = 1` 或其他不合法值。`null`、字串
`"0"`、布林 `false` 與其他數字都不是「欄位不存在」，一律 FAILED。

## BLOCKED

設定頁開關無法由人眼 Evidence 確認，或 capture／解碼失敗而無法進行比較。
