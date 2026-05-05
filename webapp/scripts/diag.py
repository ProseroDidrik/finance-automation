"""Diagnostik för report_pnl row-explosion."""
import duckdb

con = duckdb.connect("data/finance.duckdb", read_only=True)
COMPANY = 76
PERIOD = "202603"
PREV = "202602"

con.execute("""
CREATE TEMP VIEW pnl_tree AS
WITH RECURSIVE walk(account_id, parent_id, depth, sort_path) AS (
  SELECT account_id, parent_id, 0, account_id
  FROM dim_account_map WHERE account_id = 'P&L'
  UNION ALL
  SELECT m.account_id, m.parent_id, t.depth + 1, t.sort_path || '/' || m.account_id
  FROM dim_account_map m JOIN walk t ON m.parent_id = t.account_id
) SELECT * FROM walk
""")
print("pnl_tree rows:        ", con.execute("SELECT COUNT(*) FROM pnl_tree").fetchone()[0])
print("  distinct account_id:", con.execute("SELECT COUNT(DISTINCT account_id) FROM pnl_tree").fetchone()[0])
print(con.execute("SELECT depth, COUNT(*) AS n FROM pnl_tree GROUP BY depth ORDER BY depth").df().to_string(index=False))

con.execute(f"""
CREATE TEMP VIEW raw_balances AS
SELECT fb.company_id, fb.period, fb.account_code, fb.amount, fb.period_type
FROM fact_balances fb JOIN dim_company c ON c.company_id = fb.company_id
WHERE fb.company_id = {COMPANY}
  AND fb.period IN ('{PERIOD}', '{PREV}')
  AND fb.source_kind = CASE c.country
       WHEN 'Sweden' THEN 'SIE' WHEN 'Norway' THEN 'SAFT' ELSE 'INL' END
""")
print()
print("raw_balances rows:", con.execute("SELECT COUNT(*) FROM raw_balances").fetchone()[0])
print(con.execute("SELECT period, COUNT(*) AS n FROM raw_balances GROUP BY period").df().to_string(index=False))

# Hur ofta finns dubbletter (company, account_code) inom samma period?
print()
print("Duplicates in raw_balances per (period, account_code):")
print(con.execute("""
SELECT period, account_code, COUNT(*) AS n
FROM raw_balances GROUP BY period, account_code HAVING COUNT(*) > 1
LIMIT 20
""").df().to_string(index=False))

con.execute(f"""
CREATE TEMP VIEW leaf_amounts AS
SELECT m.account_id AS leaf_node_id, m.parent_id AS group_node_id,
       cur.amount AS amount_ytd,
       cur.amount - COALESCE(prev.amount, 0) AS amount_month
FROM raw_balances cur
JOIN dim_account_map m ON m.company_id = cur.company_id AND m.account_code = cur.account_code
JOIN pnl_tree t ON t.account_id = m.parent_id
LEFT JOIN raw_balances prev
  ON prev.company_id=cur.company_id AND prev.account_code=cur.account_code AND prev.period='{PREV}'
WHERE cur.period='{PERIOD}'
""")
print()
print("leaf_amounts rows:        ", con.execute("SELECT COUNT(*) FROM leaf_amounts").fetchone()[0])
print("  distinct leaf_node_id:  ", con.execute("SELECT COUNT(DISTINCT leaf_node_id) FROM leaf_amounts").fetchone()[0])
print()
print("Top duplicates in leaf_amounts:")
print(con.execute("""
SELECT leaf_node_id, COUNT(*) AS n FROM leaf_amounts GROUP BY leaf_node_id HAVING COUNT(*) > 1 ORDER BY n DESC LIMIT 5
""").df().to_string(index=False))

# Hur kommer dubbletter till? Kolla dim_account_map
print()
print("dim_account_map rows for company 76:")
print(con.execute("SELECT COUNT(*) FROM dim_account_map WHERE company_id = 76").fetchone()[0])
print("  distinct account_code:", con.execute("SELECT COUNT(DISTINCT account_code) FROM dim_account_map WHERE company_id = 76").fetchone()[0])
print("  distinct (account_code) with multiple rows:")
print(con.execute("""
SELECT account_code, COUNT(*) AS n
FROM dim_account_map WHERE company_id = 76
GROUP BY account_code HAVING COUNT(*) > 1 LIMIT 10
""").df().to_string(index=False))
