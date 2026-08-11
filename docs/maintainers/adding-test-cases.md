# 新增 TestCase 指南

這份文件說明在現有架構中新增 AOS Signal 或 E2E TestCase 時，需要修改的位置。

## 新增 AOS Signal TC

一般情況依序修改以下檔案：

| 檔案 | 要加入的內容 |
|---|---|
| [`testcases/testcase_specifications.md`](../../testcases/testcase_specifications.md) | 先寫清楚 TC 的前提、操作方式、Expected、Evidence 與 PASS／FAILED／BLOCKED 條件 |
| [`testcases/testcase_catalog.json`](../../testcases/testcase_catalog.json) | 加入 Report 使用的 key、標題、欄位、優先級、Round、平台適用性與 Evidence 說明 |
| [`testcases/android_signal_testcases.py`](../../testcases/android_signal_testcases.py) | 實作 validator，加入 `TC_DEFINITIONS`，並把 TC key 放進對應的 `ROUND_DEFINITIONS` |
| [`tests/test_campaign_contracts.py`](../../tests/test_campaign_contracts.py) 或對應測試檔 | 驗證 expected／actual 比較、邊界條件與 Catalog／registry 契約 |

如果新 TC 可以沿用現有的 `bid_decoded.json` 或裝置狀態 Evidence，不需要修改 capture 層。
在 `TestCase.evidence` 宣告需要的 Evidence key，Round 會自動去重並共用同一份 capture。

## 新增 Evidence

只有現有 Evidence 不足時，才需要修改以下位置：

| 檔案 | 要加入的內容 |
|---|---|
| [`evidence_aos.py`](../../evidence_aos.py) | 定義新的 Evidence key、capture／materialize function，並註冊到 `EVIDENCE_CAPTURES` |
| [`qa_aos.py`](../../qa_aos.py) | 只有需要新的操作流程、特殊 Scenario 或新的 Round strategy 時才修改 runner |
| [`mitmdump_addon.py`](../../mitmdump_addon.py) | 只有需要攔截目前尚未保存的網路事件時才修改 |

## 新增 AOS E2E TC

Catalog 與規格仍要同步更新。實際流程依類型放在：

- Standalone baseline：[`testcases/e2e/android_e2e_baseline.py`](../../testcases/e2e/android_e2e_baseline.py)
- AdMob mediation 延伸：[`testcases/e2e/android_admob_mediation_extensions.py`](../../testcases/e2e/android_admob_mediation_extensions.py)

## 完成後檢查

至少執行：

```bash
python3 -m unittest tests.test_campaign_contracts
python3 qa_aos.py list-rounds
python3 page.py
```

最後用目標實機跑對應 Round，確認：

- Evidence 檔案完整。
- `verdicts.json` 的 TC key 與判定正確。
- Report 能找到並顯示新 TC。
- 異常路徑會產生正確的 `BLOCKED` 或 `FAILED`，未執行的 TC 不產生 Verdict。
