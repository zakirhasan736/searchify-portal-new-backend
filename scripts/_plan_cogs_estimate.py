"""Estimate DataForSEO COGS if one user fully uses each Searchify plan."""

# Published DFS rates (approx, post Jul 2026 where noted)
LABS_TASK_CHEAP = 0.0012
LABS_TASK_OTHER = 0.012
LABS_ITEM = 0.00012
BL_REQ = 0.024
BL_ROW = 0.000036
SERP_LIVE = 0.002
ONPAGE_PAGE = 0.00015  # ~$0.000125 + ~20%
# LLM Responses live: $0.0006 + LLM tokens + web_search. Typical mini+search ~$0.02–$0.06
VIS_EACH = 0.04


def kw_cost(tracked, ideas_seeds=3, ranked_rows=150, suggestion_limit=40):
    ranked = LABS_TASK_OTHER + ranked_rows * LABS_ITEM
    overview = LABS_TASK_CHEAP + tracked * LABS_ITEM
    suggestions = ideas_seeds * (LABS_TASK_CHEAP + suggestion_limit * LABS_ITEM)
    return ranked + overview + suggestions


def bl_cost(live_rows=200, lost_rows=50):
    # summary + live list + lost list (matches research.backlinks)
    return 3 * BL_REQ + (live_rows + lost_rows) * BL_ROW


def audit_cost(pages):
    return pages * ONPAGE_PAGE + 0.01


plans = {
    "Starter": dict(price=29, tracked=10, kw=10, bl=5, vis=20, comp=10, audits=2, audit_pages=100),
    "Growth": dict(price=79, tracked=50, kw=40, bl=20, vis=100, comp=40, audits=8, audit_pages=300),
    "Agency": dict(price=199, tracked=200, kw=150, bl=60, vis=400, comp=150, audits=30, audit_pages=1000),
}

print("Per-unit estimates")
for name, p in plans.items():
    print(f"  {name} keyword refresh: ${kw_cost(p['tracked']):.4f}")
print(f"  Backlink refresh: ${bl_cost():.4f}")
print(f"  Visibility check: ${VIS_EACH:.4f}")
print(f"  Competitor SERP: ${SERP_LIVE:.4f}")
for name, p in plans.items():
    pages = p["audit_pages"]
    print(f"  {name} audit ({pages} pages): ${audit_cost(pages):.4f}")

print()
print("FULL USE monthly DataForSEO COGS (1 user maxing quotas)")
for name, p in plans.items():
    kw = p["kw"] * kw_cost(p["tracked"])
    bl = p["bl"] * bl_cost()
    vis = p["vis"] * VIS_EACH
    comp = p["comp"] * SERP_LIVE
    aud = p["audits"] * audit_cost(p["audit_pages"])
    total = kw + bl + vis + comp + aud
    margin = p["price"] - total
    pct = 100 * total / p["price"]
    left = 100 * margin / p["price"]
    print(f"{name} charge ${p['price']}/mo")
    print(f"  keywords ${kw:.2f} | backlinks ${bl:.2f} | visibility ${vis:.2f} | competitors ${comp:.2f} | audits ${aud:.2f}")
    print(f"  TOTAL COGS ${total:.2f} | left vs price ${margin:.2f} ({left:.0f}%) | COGS {pct:.0f}% of price")
    print()

print("Visibility sensitivity (largest cost driver)")
for vis_u in (0.02, 0.04, 0.08):
    print(f"  @ ${vis_u}/check:")
    for name, p in plans.items():
        total = (
            p["kw"] * kw_cost(p["tracked"])
            + p["bl"] * bl_cost()
            + p["vis"] * vis_u
            + p["comp"] * SERP_LIVE
            + p["audits"] * audit_cost(p["audit_pages"])
        )
        pct = 100 * total / p["price"]
        print(f"    {name}: ${total:.2f} COGS / ${p['price']} ({pct:.0f}%)")
