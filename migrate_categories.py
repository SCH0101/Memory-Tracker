"""One-time migration: remap chart categories to the 10 analytical sections."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backend.paths import DB_PATH

db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row

NEW_CATS = [
    "Bit Supply vs Bit Demand Growth",
    "Inventory",
    "Contract Pricing Trend",
    "Spot Pricing & Spot-Contract Spread",
    "HBM Demand & Capacity",
    "Wafer Starts by Supplier & Node",
    "Capex / WFE / Cleanroom Expansion",
    "End-Market Demand x Memory Content",
    "Utilization Rates",
    "Yield & Cycle-Time Commentary",
    "General Memory",
]

# old subcategory name -> new category name
REMAP = {
    # Bit Supply vs Bit Demand Growth
    "DRAM bit growth": "Bit Supply vs Bit Demand Growth",
    "DRAM supply/demand": "Bit Supply vs Bit Demand Growth",
    "NAND supply/demand": "Bit Supply vs Bit Demand Growth",
    "HBM supply/demand": "Bit Supply vs Bit Demand Growth",
    "DRAM supply allocation": "Bit Supply vs Bit Demand Growth",
    "DDR bit demand": "Bit Supply vs Bit Demand Growth",
    "NAND bit growth": "Bit Supply vs Bit Demand Growth",
    "Supply/demand dynamics": "Bit Supply vs Bit Demand Growth",
    "DRAM TAM": "Bit Supply vs Bit Demand Growth",
    "NAND TAM": "Bit Supply vs Bit Demand Growth",
    "Memory demand composition": "Bit Supply vs Bit Demand Growth",

    # Inventory
    "DRAM inventory": "Inventory",
    "NAND inventory": "Inventory",
    "Inventory cycle": "Inventory",

    # Contract Pricing Trend
    "DRAM pricing": "Contract Pricing Trend",
    "NAND pricing": "Contract Pricing Trend",
    "DDR4 pricing": "Contract Pricing Trend",
    "DDR5 pricing": "Contract Pricing Trend",
    "DDR4 vs DDR5": "Contract Pricing Trend",
    "Mobile memory pricing": "Contract Pricing Trend",
    "Specialty DRAM pricing": "Contract Pricing Trend",
    "Memory pricing trends": "Contract Pricing Trend",
    "HBM pricing": "Contract Pricing Trend",
    "HBM vs DRAM pricing": "Contract Pricing Trend",
    "Mobile eMCP pricing": "Contract Pricing Trend",
    "DRAM downturn cycles": "Contract Pricing Trend",
    "DRAM downturn severity": "Contract Pricing Trend",
    "DDR DRAM general": "Contract Pricing Trend",
    "DDR5 adoption": "Contract Pricing Trend",
    "ASP to CPI correlation": "Contract Pricing Trend",
    "Smartphone ASP vs CPI": "Contract Pricing Trend",
    "PC ASP inflation impact": "Contract Pricing Trend",

    # HBM Demand & Capacity
    "HBM TAM": "HBM Demand & Capacity",
    "HBM capacity": "HBM Demand & Capacity",
    "HBM roadmap": "HBM Demand & Capacity",
    "HBM cost structure": "HBM Demand & Capacity",
    "HBM market share": "HBM Demand & Capacity",
    "HBM operating margins": "HBM Demand & Capacity",
    "HBM profitability": "HBM Demand & Capacity",
    "HBM vs DRAM margins": "HBM Demand & Capacity",
    "AI accelerator market share": "HBM Demand & Capacity",
    "AI chip composition": "HBM Demand & Capacity",

    # Wafer Starts by Supplier & Node
    "Capacity by geography": "Wafer Starts by Supplier & Node",
    "NAND capacity": "Wafer Starts by Supplier & Node",
    "Wafer starts": "Wafer Starts by Supplier & Node",
    "DRAM market share": "Wafer Starts by Supplier & Node",
    "NAND market share": "Wafer Starts by Supplier & Node",

    # Capex / WFE / Cleanroom Expansion
    "DRAM capex": "Capex / WFE / Cleanroom Expansion",
    "NAND capex": "Capex / WFE / Cleanroom Expansion",
    "Capital spending": "Capex / WFE / Cleanroom Expansion",
    "Cloud capex trends": "Capex / WFE / Cleanroom Expansion",
    "Cloud FCF outlook": "Capex / WFE / Cleanroom Expansion",
    "Carrier capex intensity": "Capex / WFE / Cleanroom Expansion",

    # End-Market Demand x Memory Content
    "DRAM demand by market": "End-Market Demand x Memory Content",
    "NAND demand by market": "End-Market Demand x Memory Content",
    "Memory BOM exposure": "End-Market Demand x Memory Content",
    "Server CPU TAM": "End-Market Demand x Memory Content",
    "Data center demand composition": "End-Market Demand x Memory Content",

    # Utilization Rates
    "DRAM capacity utilization": "Utilization Rates",

    # Yield & Cycle-Time Commentary
    "Technology roadmap": "Yield & Cycle-Time Commentary",

    # General Memory (catch-all)
    "General Memory": "General Memory",
    "Overall memory market": "General Memory",
    "Memory revenue": "General Memory",
    "Memory revenue composition": "General Memory",
    "DRAM operating margin": "General Memory",
    "NAND operating margins": "General Memory",
    "DRAM profitability": "General Memory",
    "NAND profitability": "General Memory",
    "Memory dividend yields": "General Memory",
    "Equity analyst ratings": "General Memory",
    "Manufacturing input cost inflation": "General Memory",
    "Supply chain cost inflation": "General Memory",
    "Supply chain cost pressures": "General Memory",
    "DRAM stock performance": "General Memory",
    "NAND stock performance": "General Memory",
    "Memory stock performance": "General Memory",
}

# 1) Create new categories
for cat_name in NEW_CATS:
    existing = db.execute("SELECT id FROM categories WHERE name = ?", (cat_name,)).fetchone()
    if not existing:
        db.execute("INSERT INTO categories (name) VALUES (?)", (cat_name,))
        print(f"  Created: {cat_name}")
    else:
        print(f"  Exists:  {cat_name} (id={existing['id']})")
db.commit()

# 2) Build lookup
cat_ids = {}
for r in db.execute("SELECT id, name FROM categories"):
    cat_ids[r["name"]] = r["id"]

# 3) Remap subcategories (merge duplicates)
moved = 0
merged = 0
unmapped = []
for r in db.execute(
    "SELECT s.id, s.name, s.category_id as old_cat_id, c.name as old_cat FROM subcategories s JOIN categories c ON s.category_id = c.id"
):
    new_cat = REMAP.get(r["name"])
    if not new_cat:
        unmapped.append(f'{r["old_cat"]} > {r["name"]}')
        new_cat = "General Memory"
    new_cat_id = cat_ids.get(new_cat)
    if not new_cat_id:
        continue
    # Check if a sub with the same name already exists in the target category
    existing = db.execute(
        "SELECT id FROM subcategories WHERE category_id = ? AND name = ? AND id != ?",
        (new_cat_id, r["name"], r["id"]),
    ).fetchone()
    if existing:
        # Merge: move all charts from this sub to the existing one, then delete this sub
        db.execute("UPDATE charts SET subcategory_id = ? WHERE subcategory_id = ?", (existing["id"], r["id"]))
        db.execute("DELETE FROM subcategories WHERE id = ?", (r["id"],))
        merged += 1
        print(f"  Merged duplicate: {r['name']} (old sub {r['id']} -> {existing['id']})")
    else:
        db.execute("UPDATE subcategories SET category_id = ? WHERE id = ?", (new_cat_id, r["id"]))
        moved += 1
db.commit()
print(f"\nRemapped {moved} subcategories, merged {merged} duplicates")
if unmapped:
    print(f"Unmapped (defaulted to General Memory):")
    for u in unmapped:
        print(f"  - {u}")

# 4) Delete old empty categories
for old_name in ["DRAM", "DDR DRAM", "HBM", "NAND"]:
    cnt = db.execute(
        "SELECT COUNT(*) as c FROM subcategories WHERE category_id = (SELECT id FROM categories WHERE name = ?)",
        (old_name,),
    ).fetchone()["c"]
    if cnt == 0:
        db.execute("DELETE FROM categories WHERE name = ?", (old_name,))
        print(f"Deleted empty category: {old_name}")
    else:
        print(f"Kept {old_name} ({cnt} subcategories still linked)")
db.commit()

# 5) Verify
print("\n=== NEW CATEGORY STRUCTURE ===")
for r in db.execute("""
    SELECT c.name as cat, COUNT(DISTINCT s.id) as subs, COUNT(ch.id) as charts
    FROM categories c
    LEFT JOIN subcategories s ON s.category_id = c.id
    LEFT JOIN charts ch ON ch.subcategory_id = s.id AND ch.quality_status = 'approved'
    GROUP BY c.id ORDER BY c.name
"""):
    print(f"  {r['cat']:42s}  {r['subs']} subs, {r['charts']} charts")

db.close()
print("\nDone.")
