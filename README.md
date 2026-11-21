# MF and IPO Tracker (India)

Open `index.html` in a browser. It reads `data/data.js`, which `update_data.py` writes.
The data currently in the folder is **synthetic demo data**. Replace it with live data:

```
python update_data.py
```

Needs Python 3.8+ and internet access. No packages to install. Takes a few minutes (about 150 funds).
Quick test: `python update_data.py --max-per-bucket 10`

## What it tracks

- Direct-growth equity funds in Large, Mid, Small, Flexi and Large & Mid cap categories.
- Latest NAV from AMFI (`NAVAll.txt`), full NAV history from mfapi.in.
- Indicators: RSI(14), MACD(12,26,9), 50/200-day averages, 52-week high, 1M to 5Y returns, volatility, Sharpe, Sortino, max drawdown.
- Call (Strong Buy / Buy / Hold / Reduce / Avoid) = 60% peer-ranked quality (3Y CAGR, Sharpe, drawdown, 1Y return within the same cap bucket) + 40% technical score. Rules live in `tech_score`, `build_funds` and `call_for`; edit the weights there.

## Automatic updates (free)

1. Push this folder to a GitHub repo.
2. Settings > Pages > deploy from the main branch. Your tracker is then a public website.
3. `.github/workflows/update.yml` refreshes the data every weekday at 7 pm IST and commits it.

## IPOs

There is no official free API for GMP, subscription and financials, and sites like Investorgain do not offer one,
so the IPO tab is driven by `data/ipo_input.csv`. Add one row per IPO (open/close/listing dates as YYYY-MM-DD),
run `python update_data.py`, and each IPO is scored on GMP, subscription (QIB/HNI/retail), valuation versus peers,
growth, ROCE, leverage and fresh-issue share. Rows with under half the key fields get "Insufficient data".
If you hold a licensed data feed, add a loader next to `load_ipos` that returns the same fields.

## Limits

- Funds younger than 3 years get no call.
- NAV history does not include expense ratio, AUM, holdings or portfolio P/E. Check the AMC factsheet.
- Scores are screening aids, not investment advice.
