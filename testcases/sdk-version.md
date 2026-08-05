# SDK Version (sdk_version)

- Layer: Signal
- Round: R1
- Platform: AOS
- Category: D. App State - Format
- Field: `app.sdk_version`
- Priority: P1
- Status: Implemented

## Purpose

確認 bid request 宣告的 Appier SDK 版號，等於本次 Sample App build 使用的 SDK 版號。
本 TC 沿用 R1 已取得的 capture，不需要額外設定、操作手機或重抓流量。

## Independent answer key

目前人工確認的 project default 是 `2.2.0`。執行不同 SDK build 時，Round 必須用
`EXPECTED_SDK_VERSION` 明確提供該 build 的版號，不能從待測 request 反推 expected。

```bash
EXPECTED_SDK_VERSION=2.2.0 python3 qa_aos.py round R1
```

## Evidence

- `sdk-build-info.json`：人眼可讀的 expected build version 與 request actual value。
- `bid_raw.json`：同一次 capture 的未修改暗文。
- `bid_decoded.json`：同一次 capture 解碼後的 `req.plaintext.app.sdk_version`。
- `verdicts.json`：本 TC 的結構化比較結果；Page 以資訊卡呈現 expected／actual。

`ext.plaintext` 目前沒有 `app`，因此本 TC 只檢查定義欄位所在的 req payload，不拿其他
request 欄位互相證明。

## PASS

`req.plaintext.app.sdk_version` 存在、非空，且完全等於本輪 expected SDK version。

本次 answer key 為 `2.2.0`。

## FAILED

TC 已取得並解碼 request，但 `app.sdk_version` 缺少、為空或與 expected build version 不符。

## BLOCKED

Round 未取得／解碼 request，因而無法執行比較。BLOCKED 不得偽裝成產品 FAILED。
