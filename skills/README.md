# skills/

Versionskontrollerad källkod för Cowork/Claude Desktop-skills som hör till detta
projekt. Själva `.skill`-bundlen (zip) är en byggartefakt och checkas inte in —
den byggs från källan här och laddas upp i Cowork.

## fte-ytd

YTD-dashboard-skill (Mercur-validering, status-dots, FTE) som körs i Cowork mot
samma prod-Postgres som `finance-warehouse`-MCP:n. Versionshistorik i `fte-ytd/SKILL.md`.

**Bygg `.skill`-bundlen** (forward slashes, ingen `__pycache__` — viktigt för
icke-Windows-loaders som Cowork):

```bash
cd skills
py - <<'PY'
import zipfile, os
root, dest = "fte-ytd", "fte-ytd-v1.4.skill"
files = []
for dp, dn, fn in os.walk(root):
    dn[:] = [d for d in dn if d != "__pycache__"]
    files += [os.path.join(dp, f) for f in fn if not f.endswith(".pyc")]
with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
    for p in sorted(files):
        z.write(p, p.replace("\\", "/"))
print("wrote", dest)
PY
```
