# Indie Founder Revenue MCP

<!-- mcp-name: io.github.charlie-morrison/indie-founder-mcp -->

[![charlie-morrison/indie-founder-mcp MCP server](https://glama.ai/mcp/servers/charlie-morrison/indie-founder-mcp/badges/score.svg)](https://glama.ai/mcp/servers/charlie-morrison/indie-founder-mcp)

A Model Context Protocol server that aggregates revenue across **Lemon Squeezy + Gumroad + Polar + Stripe** into a single Claude-queryable view. A solo founder can ask Claude *"what's my MRR across all my stores?"* — and Claude answers from one tool call instead of four browser tabs.

**Status:** v0.2 — all four adapters shipped. LS + Gumroad live-validated against production; Polar + Stripe offline-validated (live test requires your own org/account). Freemium: 3 tools free, 3 paid (see [Pricing](#pricing)).

## Tools exposed to Claude

| Tool | Tier | What it does |
|------|------|--------------|
| `summary_mrr(days=30)` | **Free** | Combined MRR / ARR across every connected store. Per-provider breakdown. |
| `recent_orders(limit=20)` | **Free** | Last N orders across all stores, normalized to one schema. |
| `health()` | **Free** | Per-adapter healthcheck — useful before the others. |
| `top_customers(limit=10, days=30)` | Paid | Highest-revenue customers in window, ranked by gross USD. |
| `refund_signal(days=7, alert_multiplier=2.0)` | Paid | Current N-day refund ratio vs prior N-day window. Fires `alert` when ratio jumps ≥2x. Zero-case safe. |
| `export_csv_for_tax(year, quarter)` | Paid | CSV export for Ukrainian FOP 3rd-group quarterly filing. NBU USD/UAH exchange rate per-order from `bank.gov.ua`. |

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
        "LS_STORE_ID": "optional_store_id_filter",
        "GUMROAD_ACCESS_TOKEN": "optional_gumroad_token",
        "POLAR_API_TOKEN": "optional_polar_token",
        "STRIPE_API_KEY": "optional_stripe_secret_key",
        "INDIE_FOUNDER_MCP_LICENSE_KEY": "optional_license_key_for_paid_tier"
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
1. **Multi-source aggregation** — one query surface across LS + Gumroad + Polar + Stripe (all four shipped in v0.2).
2. **Summarized indie-founder daily view** — MRR, top customer, refund signal, churn risk — not raw API parroting.
3. **CSV export for Ukrainian FOP tax filing** — third-section single-tax filers need a specific NBU-rate-anchored format.
4. **Freemium with paid tier** — Lemon Squeezy license keys gate the analytics + tax-export tools; the daily-check tools stay free forever.

## Production validation

Built and validated on the author's own `charliemorrison.lemonsqueezy.com` store (store ID 344362) and live Gumroad account:

- `health` → `{"lemonsqueezy": true, "gumroad": true}` against each provider's `/users/me`-equivalent.
- `recent_orders` → returns the real $17 *Social Media AI Mastery* LS order from `johannaakoenig@gmail.com` on 2026-05-07, normalized into the same schema as Gumroad orders.
- `summary_mrr` → `$0` MRR (correct — no subscriptions converted yet on these stores; the tool's plumbing across active/trialing/past_due statuses is exercised).
- `top_customers(limit=5, days=90)` → ranks customers by lifetime USD across all wired providers.
- `refund_signal(days=7)` → zero-case `alert=False` for healthy young stores.
- `export_csv_for_tax(2026, 2)` → one row, `2026-05-07,lemonsqueezy,8274123,johannaakoenig@gmail.com,Social Media AI Mastery,17.00,0.00,17.00,43.8528,745.50,745.50` — NBU rate fetched live from `bank.gov.ua`.

Polar + Stripe adapters are offline-validated (24 fixture tests across all four adapters pass). Live validation against those two requires the user's own Polar org / Stripe account — the adapter wires automatically when the corresponding env var is set.

## Pricing

| Tier | Price | Tools |
|------|-------|-------|
| **Free** | $0 | `summary_mrr`, `recent_orders`, `health` — the daily-check loop. Unlimited stores. |
| **Paid** | **$19/mo** or **$190/yr** (2 months free) | All Free tools **plus** `top_customers`, `refund_signal`, `export_csv_for_tax` (analytics + Ukrainian FOP CSV export). |

Buy a license: **<https://charliemorrison.lemonsqueezy.com>** → drop the key into `INDIE_FOUNDER_MCP_LICENSE_KEY` and restart your MCP host.

Free tools never check the license — there's no rug-pull. The paid tier is just the analytics + export surface for people who want them.

## License & monetization stack

- Code: MIT (open server; paid features gated by a Lemon Squeezy license key at runtime).
- License keys: Lemon Squeezy License API (`/v1/licenses/validate`), 6-hour validation cache.
- Payouts: LS → Wise USD (proven — $17 cleared on the dogfood store).

## Build plan

See [`PLAN.md`](./PLAN.md) for the staged build sequence. v0.2 ships all four adapters + license gate + 6 tools (3 free, 3 paid).

## Why this exists

I was opening four dashboard tabs (Lemon Squeezy, Gumroad, Polar, Stripe) every morning to check MRR and recent orders, then re-doing the same math in a spreadsheet for FOP tax filing. So I asked Claude to consolidate it. This is the result — and now Claude does the morning check itself.

---

Built by [Charlie Morrison](https://github.com/charlie-morrison). Issues + PRs welcome.
