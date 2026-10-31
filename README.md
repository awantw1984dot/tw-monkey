# 投信的猴子

台股族群層級的投信資金追蹤。每個交易日收盤後，GitHub Actions 自動抓證交所／櫃買中心的行情與三大法人資料，產生族群報表到 GitHub Pages。

- 最新報表：https://awantw1984dot.github.io/tw-monkey/
- 歷史報表：https://awantw1984dot.github.io/tw-monkey/archive.html

## 檔案

| 檔案 | 用途 |
|---|---|
| `tw_backfill.py` | 抓官方每日資料（上市／上櫃行情、三大法人、產業別） |
| `monkey_report.py` | 計算族群指標、標籤，輸出 HTML 報表 |
| `cloud_daily.py` | 雲端流程：抓資料 → 合併進 `store/` → 產生報表 |
| `concept_groups.csv` | 自訂概念族群（光通訊、HVDC），直接編輯即可 |
| `store/` | 最近約 110 個交易日的精簡資料（每月一檔）與產業分類 |
| `docs/` | GitHub Pages 網頁 |

## 執行

- 平日由 cron-job.org 觸發 `daily.yml`（workflow_dispatch），GitHub 自身 18:47 排程為備援
- 手動執行：Actions → 投信的猴子 每日更新 → Run workflow；第一次建檔把天數設為 170

## 標籤規則（2026/10/31 以一年資料回測校正）

只在族群投信 5 日淨買超 ≥ 5 億元時判斷：
- 主動佈局：擴散度 20–50% 且有效買超 ≥ 3 檔
- 過熱：擴散度 ≥ 50%
- 買超集中：擴散度 < 20% 或不到 3 檔
- 擴散度 ＝ 有效買超檔數（5 日淨買超 ≥ 500 萬元）÷ 近 60 日投信有交易的檔數

標籤是過濾條件，不是進場訊號；近半年回測效果明顯變弱。
