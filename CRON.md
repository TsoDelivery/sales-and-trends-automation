# Cron Schedule

The sales-and-trends script runs monthly via cron:

```
0 6 1 * * cd /Users/tsora/work/sales-and-trends-automation && /opt/homebrew/bin/node scripts/write-sales-trends.mjs --month $(date -v-1m +%Y-%m) --write >> /tmp/sales-trends-cron.log 2>&1
```

This runs at 6:00 AM on the 1st of every month, writing the **previous month's** data.

`date -v-1m +%Y-%m` computes the previous month (e.g. on Aug 1 it resolves to `2026-07`).

To install:
```bash
crontab -e
# Add the line above
```

Logs: `/tmp/sales-trends-cron.log`