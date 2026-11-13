# Human actions

- [ ] Get past the ISP DNS block on data.alpaca.markets (blocks: daily job bars/dividends/articles/calendar, Phase 1 exit criteria)
  Biznet SafeSurf rewrites every plain DNS answer for data.alpaca.markets (even queries sent to 1.1.1.1) to its block page 202.169.44.80. The real IP (34.86.145.125) is reachable, so encrypted DNS fixes it. Pick one:
  1. Tailscale (already running on this Mac): open https://login.tailscale.com/admin/dns, under "Global nameservers" click "Add nameserver" > "Cloudflare", and turn on "Override DNS servers". Tailscale sends queries to known public resolvers over DNS-over-HTTPS.
  2. Or turn off SafeSurf for your Biznet line (MyBiznet app or Biznet support).
  3. Or run the Mac on a VPN while the app is up.
  Then restart the app: `docker compose restart app`
  Verify: `dig +short data.alpaca.markets` prints a non-202.169.44.80 address, `docker compose exec app python -c "import httpx; print(httpx.get('https://data.alpaca.markets/v2/stocks/SPY/bars').status_code)"` prints 401, and `job_runs` gets `ok` rows for bars/dividends/articles on 2026-11-04 onward.

- [ ] Keep `docker compose up -d` running for a week (blocks: Phase 1 exit criteria "unattended for a week, catch-up exercised at least once")
  1. Do the DNS step above first.
  2. Leave Docker Desktop running; sleeping the laptop is fine, the hourly tick catches up on wake.
  Verify: every trading session over 7 days has all 6 steps `ok` in `job_runs`, with at least one session run late by catch-up.
