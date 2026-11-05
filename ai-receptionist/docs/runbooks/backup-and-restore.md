# Runbook: backups, point-in-time recovery, and restore drills

Appointments, callbacks and call records are the product. This runbook says:
- how much of them we can lose (RPO);
- how long a recovery takes (RTO);
- how to restore;
- how to prove the backups work before we need them.

Commands assume Postgres 16. `scripts/...` paths are relative to `apps/api`, and `infra/...` paths to the repo's `ai-receptionist/` folder.

## Targets

| | Target | Measured (drills below) |
|---|---|---|
| **RPO**, data lost if the server is destroyed | ≤ 60 s (`archive_timeout`) | **52.1 s**, restoring from the offsite bucket after a crash |
| **RPO**, recovering from a bad write such as a deletion or a bad migration | 0: restore to just before it | **0**: replay stopped at the transaction before the DELETE |
| **RTO**, restore and replay until the copy accepts connections | scales with data size | **4.3–4.8 s** from the bucket for 165 MB |
| **RTO**, until callers are served again | ≤ 30 min (proposed; confirm) | not drilled: the cutover below is unrehearsed |

The 30-minute end-to-end target is a proposal for the product owner to confirm. It covers noticing the problem, choosing the target time, restoring, verifying and repointing the services. Only the restore and verify steps have been timed.

## How it works

**pgBackRest** keeps the backups in an S3-compatible bucket in another region. That can be AWS S3, Cloudflare R2, Backblaze B2 or MinIO.

- **Full backup** weekly, plus a **block-level incremental** daily. An incremental uploads only the blocks that changed.
- **The WAL archive** holds every change since: Postgres hands each finished 16 MB segment to `pgbackrest archive-push`.
- **Restoring** downloads the newest backup before the target and replays WAL up to the chosen moment, or to the end.
- **Encryption.** Everything is encrypted on the database host (AES-256-CBC) and compressed (zstd) before upload. The bucket and its provider only ever hold ciphertext.

What this gives you:
- **RPO** is whatever WAL hasn't reached the bucket yet. A segment is shipped when it fills, or after `archive_timeout` (60 s), whichever comes first. At most a minute of writes lives only on the server.
- **Replay time** grows with the WAL since the last backup, and daily incrementals keep it to under a day's worth.
- **The recovery window** is `BACKUP_RETENTION_DAYS`, 14 by default. You can restore to any moment in it. pgBackRest expires older backups and WAL after each backup.

### Set it up (self-managed Postgres)

On the database host, as root:

```bash
apt install pgbackrest            # 2.50 on Ubuntu 24.04; any 2.46+ supports block incrementals
# Secrets come from the host's secret store, never from the repo or the shell history:
export PGBACKREST_REPO1_S3_BUCKET=... PGBACKREST_REPO1_S3_ENDPOINT=... \
       PGBACKREST_REPO1_S3_REGION=... PGBACKREST_REPO1_S3_KEY=... \
       PGBACKREST_REPO1_S3_KEY_SECRET=... PGBACKREST_REPO1_CIPHER_PASS=...
# R2, B2 and MinIO also need: PGBACKREST_REPO1_S3_URI_STYLE=path
infra/pgbackrest/setup.sh
```

`setup.sh` (re-runnable) does the following:
1. Writes `/etc/pgbackrest/pgbackrest.conf`. It holds no secrets and is safe to read.
2. Writes `conf.d/secrets.conf`, mode `640 root:postgres`.
3. Creates the stanza.
4. Points `archive_command` at `pgbackrest archive-push`.
5. Runs `pgbackrest check`, which archives a segment and reads it back from the bucket.
6. Takes the first full backup.

Then schedule the backups and the monitoring, as the `postgres` user:

```cron
17 3 * * *    postgres  /opt/receptionist/infra/pgbackrest/backup.sh >> /var/log/pgbackrest/cron.log 2>&1
*/5 * * * *   postgres  /opt/receptionist/infra/pgbackrest/backup-status.sh || <alert>
```

`backup-status.sh` exits with 0 for OK, 1 for WARNING and 2 for CRITICAL. It reports CRITICAL when:
- the bucket doesn't answer within 30 s;
- the stanza is unhealthy;
- the newest backup is over 26 h old;
- WAL archiving is failing.

It warns when WAL is queuing up.

### The bucket

- **Another region** from the database, ideally another provider. The point is surviving the loss of the database's whole site.
- **A key that can reach this bucket only.** It needs read, write, list and delete. pgBackRest deletes what retention has expired.
- **Protect it from a compromised database host.** That host holds a key that can delete.
  - Turn on **bucket versioning**, with a lifecycle rule that removes noncurrent versions after 30 days.
  - Don't give the host's key permission to delete versions (`s3:DeleteObjectVersion`, or the provider's equivalent). Its deletes then only add a delete marker, and anything deleted, by an attacker or by a bad command, is recoverable for a month.
  - Don't use S3 Object Lock in compliance mode on this bucket: it would also block pgBackRest's own expiry.

### The cipher passphrase

- **Without it, the backups can't be read, by anyone.** Checked in the drill: with the wrong passphrase, pgBackRest can't even parse its own index.
- **Keep a copy outside the database host and outside the bucket's account.** A password manager entry owned by two people works.
- **It can't be changed for an existing repository.** To rotate it:
  1. Set a new `PGBACKREST_REPO1_PATH` and passphrase, and re-run `setup.sh`. That creates a new repository and takes a new full backup.
  2. Keep the old path and passphrase until the old repository's backups are past the retention window.
  3. Then delete the old repository.

### When the bucket is unreachable

Commits never wait for the bucket: `archive-async` pushes WAL from a local spool. During an outage:
- WAL queues in `pg_wal`;
- `backup-status.sh` goes CRITICAL;
- archiving catches up by itself once the bucket is back.

If the outage lasts until 4 GiB of WAL is queued (`archive-push-queue-max`), pgBackRest **drops** WAL so the disk can't fill and stop Postgres. Postgres keeps serving, but the recovery window has a gap. Take a full backup as soon as the bucket returns.

That trades the recovery window for availability. It's the right trade for a live phone line, provided the alert is answered long before the 4 GiB mark.

### Managed Postgres

Most managed providers don't allow `archive_command`, so pgBackRest can't run against them. That leaves two options:
- use the provider's own PITR, with at least 7 days of retention, and drill restores the provider makes;
- run Postgres yourself, as above.

**Railway: TBD.** Whether Railway's Postgres offers PITR is unverified, because its docs were unreachable from the environment this was written in. If it only takes daily snapshots, the RPO is up to 24 hours of appointments. On Railway, running pgBackRest means running Postgres as your own service: an image with pgvector and pgBackRest, a volume, and a scheduled job for `backup.sh`. That image hasn't been built.

## Restoring

**Never restore over the live cluster.** Restore next to it, verify, then switch. The broken cluster is evidence, and for a single-business accident it's also the source of everything written since the target (see "Choosing what to restore").

### 1. Choose the target

- **After a bad write:** a moment just before it. Find it in the API logs or in Postgres's logs, and allow a little margin. Everything committed after the target is left out, including legitimate writes from other businesses.
- **After losing the server:** `latest`, meaning everything in the bucket.

### 2. Before restoring, take a manifest if the source is still up

This is optional:

```bash
python scripts/verify_restore.py snapshot --url "$ADMIN_DATABASE_URL" > before.json
```

After a bad write it describes the damaged state, not the target. Use it to see what changed. It's not a pass or fail test.

### 3. Restore into a new data directory

On any host with pgBackRest's config and secrets. Nothing is needed from the old server:

```bash
sudo -u postgres infra/pgbackrest/restore.sh "2026-11-05 01:24:23+00" /var/lib/postgresql/16/restored 5433
sudo -u postgres infra/pgbackrest/restore.sh latest /var/lib/postgresql/16/restored 5433
```

The script:
- refuses to touch an existing directory;
- starts the copy with **archiving off**, so it never writes a second timeline into the repository;
- listens on the local socket only;
- prints the restore time;
- fails, with the log, if the bucket can't reach the target.

A managed provider does the equivalent from its console. For a plain WAL archive without pgBackRest, use `infra/postgres/pitr-restore.sh`, which needs a base backup and archive directory.

### 4. Verify before anything points at it

```bash
python scripts/verify_restore.py verify --url "$RESTORED_ADMIN_URL" \
    --app-url "$RESTORED_APP_URL"   [--against at-target.json]
```

It checks:
- the schema is at the head revision;
- `vector` and `btree_gist` are present;
- row-level security is forced, with its policy, on every business table (found from the catalog);
- the app role can't bypass row-level security, sees nothing without a business set, and reads that business's rows with it set;
- the booking exclusion constraint is there;
- no index is INVALID.

With `--against`, it also compares row counts and full-row hashes for every table, business by business. Exit status 0 means serve it; anything else means don't.

### 5. Cut over

This step hasn't been drilled yet; it's the next thing to rehearse, on staging.

1. Stop `worker`, `confirmations` and `crm`, so that no job writes to the old database.
2. Point `DATABASE_URL` on all four services at the restored database, using the `receptionist_app` login.
3. Redeploy `api` (its pre-deploy migration is a no-op at head), then the workers.
4. Check `/readyz` and make a Test Lab call.
5. **Protect the new primary at once.**
   - Re-run `setup.sh` with `PG_DATA` set to its data directory, which re-enables archiving into the repository on the new timeline.
   - Then run `backup.sh full`. `setup.sh` only takes a full backup when the repository has none, and this one already does.
   - Until that full backup exists, nothing protects the new primary on its own.

### 6. Reconcile what lives outside Postgres

- **Google Calendar.** Events written after the target still exist in Google, but their appointments don't exist in the database. Free/busy will show those slots as taken. List calendar events created after the target and cancel any that have no matching appointment.
- **Redis queues.** Jobs queued after the target may reference rows that no longer exist. Handlers are idempotent, so replays are safe, but a job whose row is gone may be dead-lettered. Check `:dead` afterwards with `scripts/requeue_dead.py`, and discard jobs for rows the restore removed.
- **Vapi.** The assistant's configuration isn't in the database. It needs no action unless voice settings changed after the target; if they did, re-save Settings.

### Choosing what to restore: one business, or everyone

A bad write usually hits one business, as when an operator deletes the wrong one. Rolling the whole database back to before it also throws away every other business's calls and bookings since. Prefer this:

1. Restore to a side copy at the target, as above.
2. Copy that one business's rows back from the copy into the live database, parents before children: `tenants`, then `agent_configs`, `phone_numbers`, `users`, `knowledge_docs` and the rest. Use `COPY (SELECT ... WHERE tenant_id = ...)` as an admin login.
3. Run `verify_restore.py verify` against the live database.

This hasn't been drilled. Rehearse it on staging before relying on it.

## Restore drills

**Cadence:** quarterly, plus after a Postgres major upgrade, a provider or bucket change, a passphrase rotation, or a pgBackRest upgrade. An untested backup isn't a backup.

Each drill:
1. writes load after the last backup, so the restore has to replay WAL;
2. takes a manifest at the target;
3. makes the mistake;
4. restores from the bucket;
5. runs `verify_restore.py`;
6. points the API at the copy and signs in.

Record the results below.

### Drill log

**2026-11-05: pgBackRest 2.50 to an S3 bucket, local Postgres 16.13 (165 MB).**

The bucket was **moto's S3 server behind nginx TLS**, with the certificate verified against a private CA. It was a stand-in, because no real bucket or credentials were available. That means the S3 protocol, multipart uploads and TLS verification were all exercised. Not exercised: real credential checks (moto accepts any key), a provider's own behaviour, network distance, and two regions.

| Scenario | What happened | Result |
|---|---|---|
| **Setup** | `setup.sh` from scratch | 18 s: stanza, `check`, and a full backup of 165 MB stored as **32.4 MB** (compressed and encrypted). Incremental after 120 calls: 2.8 s. |
| **Encryption** | Read the bucket directly | Every object starts `Salted__` (AES-256-CBC). 0 plaintext hits for business, policy or role names in all 29 objects. Secrets appear only in `secrets.conf`, not in the config, logs, `postgresql.auto.conf` or the bucket. A wrong passphrase can't read the repository. |
| **A. A bad script deletes 4 businesses** (200 calls, 72 appointments). One legitimate write afterwards. | Restored to T from the bucket (full + incremental + WAL). The local archive was moved out of reach first. | **4.3 s**. Replay stopped "before commit of transaction 109160", the DELETE. `verify --against`: **25/25** identical; the later write correctly absent. |
| **B. The bucket is unreachable** for 4 minutes while writes continue | nginx stopped | Writes kept committing (80,000 rows). Monitoring went CRITICAL in 30 s, naming both the bucket and archiving. With the bucket back, archiving **caught up in 2 s**. Drill C's restore contains all 80,000 rows, so there was no gap. |
| **C. Server crash** (`pg_ctl -m immediate`, no warning) | Restored `latest` from the bucket | **52.1 s** of writes lost (212 rows), within the 60 s bound. Restore **4.8 s**. Structural verify **8/8**. The crashed primary came back with `check` passing. |

Problems the drill found and fixed:
- `backup-status.sh` hung for minutes against an unreachable bucket. It's now bounded at 30 s.
- `restore.sh latest` passed an option pgBackRest refuses without a target.

**2026-11-04: plain WAL archive (`cp`, then gzip), local Postgres 16.13 (117 MB).** This was the predecessor setup, now replaced by pgBackRest.

| Scenario | Result |
|---|---|
| A. Operator deletes a business | Restored to just before the DELETE in **2.7 s**. **25/25** identical. The API served the copy and the owner could sign in. |
| B. Server lost (no WAL switch) | **43.2 s** of writes lost. Restore 2.7 s. **8/8**. |
| C. Gzipped archive | Restore 3.3 s. An idle 16 MB segment gzips to 24.5 KB. |

**Not covered yet:**
- a real provider's bucket in another region;
- the cutover (step 5);
- a single-business copy-back;
- a managed provider's restore;
- production-sized data, where replay time is the number to measure.
