# Railway Deploy Notes — READ BEFORE YOU UPDATE / PUSH

Hard-won lessons from the Week-2 deploy. Follow these and updates won't wipe data
or break the chart-documents feature.

## The two rules that matter most
1. **The database MUST be MariaDB, never MySQL.** OpenEMR's FHIR `DocumentReference`
   query returns **0 documents on MySQL** but works on MariaDB. This silently breaks
   the chart-documents / multimodal-PDF feature with no error. Local is MariaDB 11.8;
   Railway's *managed* database is MySQL — so we run a **MariaDB Docker image**, not
   the `mysql` plugin.
2. **Never trigger a fresh OpenEMR install against the populated DB.** The flex image
   reinstalls (drops+recreates tables = **data wipe**) if it can't find its setup
   sentinel `sites/docker-completed`. The `sites` volume holds that sentinel, so as
   long as the volume exists, redeploys are safe.

## Architecture (what lives where)
- **Project `openemrupdate`** (id `da1523ee…`) — the WORKING stack:
  - `MariaDB` service — image `mariadb:11.8.8`, `mariadb-volume` at `/var/lib/mysql`.
    Public proxy: `tokaido.proxy.rlwy.net:11221` (root). Internal: `mariadb.railway.internal`.
  - `openemr` service — flex image, clones code from GitHub
    `github.com/secretgitaccount/openemr` branch `main`. Has `openemr-volume` at
    `/var/www/localhost/htdocs/openemr/sites` (holds encryption drive-keys, OAuth2
    certs, uploaded PDFs, `sqlconf.php`, and the install sentinel).
    URL: `https://openemr-production-b64d.up.railway.app` (login `admin` / `pass`).
  - There's a leftover `MySQL` service from initial setup — unused, safe to delete.
- **Project `openemr`** (id `0d9f42e5…`, OLD) — holds the **`copilot-agent`** service
  (the submission URL `https://copilot-agent-production-daf5.up.railway.app`). Its own
  openemr/MySQL in that project are broken/abandoned — ignore them.

## Safe ways to update

### Update the AGENT (our Python app) — SAFE, do this freely
The agent is stateless. From `openemr-base-clean/`:
```
railway redeploy --service copilot-agent      # (openemr project is linked here)
```
Or change config: `railway variables --service copilot-agent --set "KEY=VALUE"`.

### Update the OPENEMR PHP code — via GitHub, then redeploy
Railway clones the code from `github.com/secretgitaccount/openemr@main` on each boot.
1. Push new code to that repo (`git push https://github.com/secretgitaccount/openemr.git main:main`).
2. Redeploy openemr: `railway redeploy --service openemr` (link `openemrupdate` first).
   Re-clone takes ~5-10 min. **Data is safe** — the volume sentinel skips install and
   `sqlconf.php` keeps pointing at MariaDB.

### Update DATA (patients / docs) from local
Dump local and load into the Railway **MariaDB** (not MySQL):
```
docker exec development-easy-mysql-1 mariadb-dump -uroot -proot --single-transaction \
  --no-tablespaces --skip-lock-tables --add-drop-table openemr > /tmp/dump.sql
docker exec -i development-easy-mysql-1 mariadb --force \
  -h tokaido.proxy.rlwy.net -P 11221 -u root -p<MARIADB_ROOT_PASS> openemr < /tmp/dump.sql
```
If the dump includes `keys`/`oauth_clients`/`documents`, also re-upload local's
`sites/default/documents/` (drive-keys + certs + PDFs) to the `openemr-volume` via
`railway volume files … upload …` so decryption + PDF byte-fetch still match.

## SMART launch buttons — keep it to ONE
A "SMART Enabled Apps" button = any `oauth_clients` row with **`initiate_login_uri`
set AND `is_enabled=1`**. The agent never creates these itself (its dynamic
registration does not set `initiate_login_uri`), so extra buttons only come from
(a) a DB clone carrying local's SMART client, or (b) hand-registering one.
Keep exactly one enabled launch client — **`0RZF1JAS`** (the same client the agent
authenticates with; the button and the agent must be the same client or you get
`invalid_client`). After any DB clone from local, disable extras:
```sql
UPDATE oauth_clients SET is_enabled=0
WHERE initiate_login_uri IS NOT NULL AND initiate_login_uri != ''
  AND client_id != '0RZF1JASTh5fF9CVZO7Gq2XCz-0hSiLaVdaWRIoqInY';
```
The single SMART client `0RZF1JAS` must have: `initiate_login_uri=<agent>/launch`,
`redirect_uri=<agent>/launch/callback`, scope incl. `launch launch/patient fhirUser`,
`grant_types` incl. `authorization_code`, `is_enabled=1`, `skip_ehr_launch_authorization_flow=1`.

## NEVER do these (they broke us before)
- ❌ Switch the DB to MySQL, or point openemr at the `MySQL` service.
- ❌ Delete `openemr-volume` or `mariadb-volume`. (A stuck `pendingDel` volume in
  Railway is a trap — it mounts nothing yet blocks new volumes. If you hit that,
  make a **new project** rather than fight it.)
- ❌ Run `sql_upgrade.php` or a fresh install against the populated DB.
- ❌ Assume changing `MYSQL_HOST` env re-points an installed openemr — it does NOT.
  The DB host is baked into `sites/default/sqlconf.php` at install time. To change it,
  edit that file on the volume (`railway ssh --service openemr`, then `sed -i` the host).

## If it breaks (data wiped, or docs return 0)
1. Confirm openemr is on MariaDB: `railway ssh --service openemr` →
   `grep -i host sites/default/sqlconf.php` should show `mariadb.railway.internal`.
2. If wiped, reload data into MariaDB (see "Update DATA" above).
3. Confirm drive-keys on the volume: `railway volume files … list /default/documents/logs_and_misc/methods`
   should list `sevena` + `sevenb`.

## Key facts
| Thing | Value |
|---|---|
| OpenEMR URL | https://openemr-production-b64d.up.railway.app (admin/pass) |
| Agent URL (submission) | https://copilot-agent-production-daf5.up.railway.app |
| MariaDB public | tokaido.proxy.rlwy.net:11221 (root) |
| DB engine | **MariaDB 11.8.8** (NOT MySQL — this is the whole point) |
| OpenEMR code source | github.com/secretgitaccount/openemr @ main |
| Agent OAuth clients | read=`0RZF1JAS…`, write=`LSl5Snyh…` (from local, cloned into MariaDB) |
