"""synthesize_saft_ver.py — syntetisera YTD-saldon (source_kind SAFT_VER) ur
fact_journal_saft för SAF-T-bolag som levererar ENBART årsvis (annual-only).

Bakgrund
--------
Norska SAF-T-bolag levererar månadsvis: varje fil bär sina egna Account-saldon
(YTD per månad) och load_saft.py läser dem rakt av. Två bolag avviker — de
levererar SAF-T som EN helårsfil per räkenskapsår (FY-snapshot vid YYYY12),
inte månadsvis:

  * cid 81  Actas            (Denmark, DKK)
  * cid 52  Prosero Security AS (CENTR)

För deras 2025 finns därför Account-saldon bara i december (202512). Men
GeneralLedgerEntries (journalen) är periodiserad per ValueDate över hela året
och ligger redan komplett i fact_journal_saft. Då blir månads/YTD-basrader
i fact_balances tomma för jan..nov → en YTD-CTE som t.ex. frågar 202504 hittar
ingen bas-källa och returnerar 0.

Den här modulen kumulerar journalens IS-rörelse jan..period → YTD-saldo och
skriver source_kind='SAFT_VER', period_type='ytd'. Det är exakt samma operation
som synthesize_sie_ver() gör för SE-bolag utan #PSALDO — bara att källan är
fact_journal_saft (inte fact_journal_sie) och IS/BS-klassningen följer SAF-T:s
landsregel (saft_parser.statement_type_from_code), inte den svenska 3–8-regeln.

IS-only
-------
Bara resultaträkningskonton syntetiseras. Balansräkningskonton kräver korrekt
ingående balans (IB) för att YTD-kumuleras — P&L-konton startar på 0 varje
räkenskapsår, så ren journal-kumulering ger rätt YTD. Detta speglar SIE_VER.
(BS vore möjligt här via 202412-snapshoten som IB, men hålls utanför scope —
dashboarden behöver Total Sales = IS.)

Idempotens & lager-isolering
----------------------------
DELETE täcker hela FY:t (fy_start..fy_end) för source_kind='SAFT_VER' → idempotent.
INSERT skrivs bara för FY-månader som SAKNAR en riktig SAFT-snapshot (typiskt
jan..nov; december har den auktoritativa Account-snapshoten och rörs aldrig).
Rör aldrig SAFT / IMP / MAN / IMP_ADJ.

Körning
-------
    # dry-run (läser, skriver inget):
    py synthesize_saft_ver.py --year 2025 --company 81 --dry-run
    # skarpt (kräver DATABASE_URL_ETL):
    py synthesize_saft_ver.py --year 2025 --company 81
"""
from __future__ import annotations

import argparse
from datetime import datetime

import db
from load_sie import fy_periods, cumulate_ytd
from shared import log

SOURCE_KIND_SAFT_VER = "SAFT_VER"
SOURCE_KIND_SAFT = "SAFT"
PERIOD_TYPE = "ytd"
STATEMENT_TYPE_IS = "IS"


def _company_meta(con, company_id: int) -> tuple[str, str, str]:
    """(name, country, currency) ur dim_company. Raisar om bolaget saknas."""
    row = con.execute(
        "SELECT name, country, currency FROM dim_company WHERE company_id = %s",
        [company_id],
    ).fetchone()
    if row is None:
        raise SystemExit(f"cid {company_id} saknas i dim_company")
    return row[0], row[1], row[2]


def _account_names(con, company_id: int) -> dict[str, str | None]:
    """account_code → description ur dim_account_map (leaf-konton)."""
    rows = con.execute(
        "SELECT account_code, description FROM dim_account_map "
        "WHERE company_id = %s AND account_code IS NOT NULL",
        [company_id],
    ).fetchall()
    return {code: name for code, name in rows}


def _statement_types(con, company_id: int) -> dict[str, str]:
    """account_code → statement_type ('IS'/'BS') ur de RIKTIGA SAFT-snapshoterna.

    Auktoritativ klassning: exakt det load_saft.py skrev (saft_parser med rätt
    landskod). Att joina mot snapshoten i stället för att re-derivera undviker
    country-kod-fällan ('Denmark' vs 'DK') och garanterar att SAFT_VER-raderna
    klassas identiskt med SAFT-raderna rapporten redan läser.
    """
    rows = con.execute(
        "SELECT account_code, statement_type FROM fact_balances "
        "WHERE company_id = %s AND source_kind = %s "
        "  AND statement_type IS NOT NULL",
        [company_id, SOURCE_KIND_SAFT],
    ).fetchall()
    return {code: st for code, st in rows}


def synthesize_saft_ver(con, company_id: int, fy_start: str, fy_end: str,
                        now: datetime, dry_run: bool) -> dict:
    """Syntetisera SAFT_VER-rader (IS YTD-saldon) ur fact_journal_saft.

    Returnerar en summary-dict för loggning/verifiering. Inom open transaction
    (anroparen committar). Vid dry_run görs ingen DELETE/INSERT.
    """
    name, country, currency = _company_meta(con, company_id)
    periods = fy_periods(fy_start, fy_end)

    # Vilka FY-månader har redan en RIKTIG SAFT-snapshot? Dem rör vi inte.
    snapshot_periods = {
        r[0] for r in con.execute(
            "SELECT DISTINCT period FROM fact_balances "
            "WHERE company_id = %s AND source_kind = %s "
            "  AND period BETWEEN %s AND %s",
            [company_id, SOURCE_KIND_SAFT, fy_start, fy_end],
        ).fetchall()
    }
    target_periods = [p for p in periods if p not in snapshot_periods]

    # Månadsrörelse per (konto, period) ur journalen. Läser public.fact_journal_saft
    # (etl-rollen har access; mcp_readonly skulle nekas — använd reporting-vyn i
    # ad-hoc-verifiering). Tecknet bevaras (SIE-konvention, intäkt negativ).
    rows = con.execute(
        "SELECT account_code, period, SUM(amount) AS amount "
        "FROM fact_journal_saft "
        "WHERE company_id = %s AND period BETWEEN %s AND %s "
        "GROUP BY account_code, period",
        [company_id, fy_start, fy_end],
    ).fetchall()

    # IS-filter via snapshotens auktoritativa statement_type (se _statement_types).
    st_by_code = _statement_types(con, company_id)
    journal_is = [
        (code, p, float(amount))
        for code, p, amount in rows
        if st_by_code.get(code) == STATEMENT_TYPE_IS
    ]
    unclassified = sorted({code for code, _p, _a in rows
                           if code not in st_by_code})

    ytd = cumulate_ytd(journal_is, periods)            # (code, period, ytd_amount)
    names = _account_names(con, company_id)

    # Behåll bara perioder vi faktiskt ska skriva (saknar SAFT-snapshot).
    target_set = set(target_periods)
    ytd_target = [(code, p, amt) for code, p, amt in ytd if p in target_set]

    # --- verifiering: full-FY IS-kumulering vs riktig SAFT-snapshot vid fy_end ---
    cum_fy_is = sum(amt for code, p, amt in ytd if p == fy_end)
    snap_fy_is = con.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM fact_balances "
        "WHERE company_id = %s AND source_kind = %s AND period = %s "
        "  AND statement_type = %s",
        [company_id, SOURCE_KIND_SAFT, fy_end, STATEMENT_TYPE_IS],
    ).fetchone()[0]

    if not dry_run:
        # Idempotens: rensa hela FY:t för SAFT_VER innan INSERT.
        con.execute(
            "DELETE FROM fact_balances "
            "WHERE company_id = %s AND source_kind = %s "
            "  AND period BETWEEN %s AND %s",
            [company_id, SOURCE_KIND_SAFT_VER, fy_start, fy_end],
        )
        idx_per_period: dict[str, int] = {}
        insert_rows = []
        for code, p, amount in ytd_target:
            idx_per_period[p] = idx_per_period.get(p, 0) + 1
            insert_rows.append((
                company_id, p, PERIOD_TYPE, code, names.get(code),
                amount, currency, STATEMENT_TYPE_IS, SOURCE_KIND_SAFT_VER,
                f"synthesize_saft_ver:{fy_start[:4]}", idx_per_period[p], now,
            ))
        if insert_rows:
            con.executemany(
                "INSERT INTO fact_balances "
                "(company_id, period, period_type, account_code, account_name, "
                " amount, currency, statement_type, source_kind, source_file, "
                " row_index, loaded_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                insert_rows,
            )

    return {
        "company_id": company_id, "name": name, "country": country,
        "currency": currency,
        "snapshot_periods": sorted(snapshot_periods),
        "target_periods": target_periods,
        "is_accounts": len({code for code, _p, _a in journal_is}),
        "unclassified": unclassified,
        "rows_written": len(ytd_target),
        "cum_fy_is": cum_fy_is, "snap_fy_is": float(snap_fy_is),
        "ytd_by_period": {p: round(sum(a for c, q, a in ytd if q == p), 2)
                          for p in periods},
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--year", required=True, help="Räkenskapsår, t.ex. 2025")
    ap.add_argument("--company", type=int, nargs="+", required=True,
                    help="BolagsID (en eller flera)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Beräkna och skriv ut, men skriv inget till DB")
    args = ap.parse_args()

    fy_start, fy_end = f"{args.year}01", f"{args.year}12"
    now = datetime.now()
    mode = "[DRY RUN]" if args.dry_run else ""
    log("START", "synthesize_saft_ver", f"FY {args.year}  {mode}")

    con = db.connect(read_only=args.dry_run, role="etl")
    try:
        for cid in args.company:
            s = synthesize_saft_ver(con, cid, fy_start, fy_end, now, args.dry_run)
            verb = "skulle skriva" if args.dry_run else "skrev"
            log("INFO", cid,
                f"{s['name']} ({s['country']}/{s['currency']}): {verb} "
                f"{s['rows_written']} SAFT_VER-rader över "
                f"{len(s['target_periods'])} mån ({s['is_accounts']} IS-konton). "
                f"Snapshot-mån (orörda): {','.join(s['snapshot_periods']) or '-'}")
            if s["unclassified"]:
                log("WARN", cid,
                    f"{len(s['unclassified'])} journal-konton utan snapshot-"
                    f"statement_type (droppas): {','.join(s['unclassified'][:10])}")
            ytd = s["ytd_by_period"]
            apr = f"{args.year}04"
            if apr in ytd:
                log("INFO", cid,
                    f"YTD {apr} IS (journal-kum) = {ytd[apr]:,.0f} {s['currency']}")
            # full-FY-verifiering mot riktig snapshot
            if s["snap_fy_is"]:
                pct = 100.0 * s["cum_fy_is"] / s["snap_fy_is"]
                log("INFO", cid,
                    f"Full-FY IS: journal-kum {s['cum_fy_is']:,.0f} vs "
                    f"SAFT-snapshot {s['snap_fy_is']:,.0f} = {pct:.1f}%")
        if not args.dry_run:
            con.commit()
    finally:
        con.close()
    log("DONE", "synthesize_saft_ver", "")


if __name__ == "__main__":
    main()
