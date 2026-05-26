# Indie Founder Revenue MCP

<!-- mcp-name: io.github.charlie-morrison/indie-founder-mcp -->

A Model Context Protocol server that aggregates revenue across **Lemon Squeezy + Gumroad + Polar + Stripe** into a single Claude-queryable view. A solo founder can ask Claude *"what's my MRR across all my stores?"* — and Claude answers from one tool call instead of four browser tabs.

**Status:** v0.1 — Lemon Squeezy adapter live-validated against production. Gumroad / Polar / Stripe adapters land in v0.2.

## Tools exposed to Claude

| Tool | What it does |
|------|--------------|
| `summary_mrr(days=30)` | Combined MRR / ARR across every connected store. Per-provider breakdown. |
| `top_customers(limit=10, days=30)` | Highest-revenue customers in window, ranked by gross USD. |
| `refund_signal(days=7, alert_multiplier=2.0)` | Compares current N-day refund ratio to prior N-day ratio. Fires `alert` when ratio jumps ≥2x. Zero-case safe. |
| `recent_orders(limit=20)` | Last N orders across all stores, normalized to one schema. |
| `export_csv_for_tax(year, quarter)` | CSV export for Ukrainian FOP 3rd-group quarterly filing. Fetches NBU USD/UAH exchange rate per-order from `bank.gov.ua`. |
| `health()` | Per-adapter healthcheck — useful before the others. |

## Install (Claude Desktop / Claude Code / Cursor)

Until the PyPI release lands, install from this Git repo via [`uvx`](https://docs.astral.sh/uv/):

```json
{
  "mcpServers": {
    "indie-founder": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/charlie-morrison/indie-founder-mcp@main",
        "indie-founder-mcp"
      ],
      "env": {
        "LS_API_TOKEN": "your_lemon_squeezy_token_here",
        "LS_STORE_ID": "optional_store_id_filter"
      }
    }
  }
}
```

Drop this into `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows), then restart Claude Desktop.

Get a Lemon Squeezy API token from <https://app.lemonsqueezy.com/settings/api>.

### Local clone install

```bash
git clone https://github.com/charlie-morrison/indie-founder-mcp.git
cd indie-founder-mcp
pip install -e .
indie-founder-mcp  # runs stdio transport
```

## What makes this different from the 3 existing LS MCPs

Three open-source LS-only MCPs already exist on GitHub. All expose raw CRUD API calls. None monetized, none aggregated.

This MCP differs:
1. **Multi-source aggregation** — one query surface across LS + Gumroad + Polar + Stripe (v0.1 ships LS; rest follow in v0.2).
2. **Summarized indie-founder daily view** — MRR, top customer, refund signal, churn risk — not raw API parroting.
3. **CSV export for Ukrainian FOP tax filing** — third-section single-tax filers need a specific NBU-rate-anchored format. Ships in v0.1.
4. **Freemium with paid tier** (v0.2) — LS license keys gate historical data + multi-store views.

## v0.1 ↔ Production validation

Built and validated on the author's own `charliemorrison.lemonsqueezy.com` store (store ID 344362):

- `health` → `{"lemonsqueezy": true}` against `/users/me`.
- `recent_orders` → returns the real $17 *Social Media AI Mastery* order from `johannaakoenig@gmail.com` on 2026-05-07.
- `summary_mrr` → `$0` MRR (correct — no subscriptions converted yet on this store; the tool's plumbing across active/trialing/past_due statuses is exercised).
- `top_customers(limit=5, days=90)` → ranks customers by lifetime USD in window.
- `refund_signal(days=7)` → zero-case `alert=False` for healthy young stores.
- `export_csv_for_tax(2026, 2)` → one row, `2026-05-07,lemonsqueezy,8274123,johannaakoenig@gmail.com,Social Media AI Mastery,17.00,0.00,17.00,43.8528,745.50,745.50` — NBU rate fetched live from `bank.gov.ua`.

## Pricing (planned for v0.2)

- **Free:** 1 connected store, last 30d of data, basic tools.
- **Paid:** $9/mo or $79/yr — all stores, full history, CSV exports, daily summary alerts.

## License & monetization stack

- Code: MIT (open server; v0.2 will gate premium features behind a license key).
- License keys: Lemon Squeezy License API (already configured).
- Payouts: LS → Wise USD (proven — $17 cleared on the dogfood store).

## Build plan

See [`PLAN.md`](./PLAN.md) for the staged build sequence. v0.1 ships the LS adapter + 6 tools + tax CSV. v0.2 adds Gumroad / Polar / Stripe + license gate.

## Why this exists

I was opening four dashboard tabs (Lemon Squeezy, Gumroad, Polar, Stripe) every morning to check MRR and recent orders, then re-doing the same math in a spreadsheet for FOP tax filing. So I asked Claude to consolidate it. This is the result — and now Claude does the morning check itself.

---

Built by [Charlie Morrison](https://github.com/charlie-morrison). Issues + PRs welcome.
