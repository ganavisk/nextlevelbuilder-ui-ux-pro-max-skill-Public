# Runbook: backups, point-in-time recovery, and restore drills

Appointments, callbacks and call records are the product. This runbook says:
- how much of them we can lose (RPO);
- how long a recovery takes (RTO);
- how to restore;
- how to prove the backups work before we need them.

Commands assume Postgres 16. `scripts/...` paths are relative to `apps/api`.

## Targets

| | Target | Measured in the 2026-11-04 drill |
|---|---|---|
| **RPO**, data lost if the server is destroyed | ≤ 60 s (`archive_timeout`) | **43.2 s** |
| **RPO**, recovering from a bad write such as a deletion or a bad migration | 0: restore to just before it | **0**: replay stopped at the transaction before the DELETE |
| **RTO**, restore and replay until the copy accepts connections | scales with data size | **2.7–3.3 s** for 117 MB |
| **RTO**, until callers are served again | ≤ 30 min (proposed; confirm) | not drilled: the cutover below is unrehearsed |

The 30-minute end-to-end target is a proposal for the product owner to confirm. It covers noticing the problem, choosing the target time, restoring, verifying and repointing the services. Only the restore and verify steps have been timed.

## How point-in-time recovery works here

A **base backup** is a full physical copy, taken with `pg_basebackup`. The **WAL archive** holds every change made since. A restore unpacks the base backup and replays the archive up to a chosen moment, or to the end. So:

- **RPO** is whatever WAL hasn't reached the archive yet. Postgres archives a 16 MB segment when it fills, or after `archive_timeout`, whichever comes first. That's 60 s here, so at most a minute of writes lives only on the server.
- **Replay time** grows with the WAL since the last base backup. Take base backups often enough that replay stays short. Weekly is the starting point; measure it at production volume.

### Settings (self-managed Postgres)

```sql
ALTER SYSTEM SET archive_mode = 'on';                  -- needs a restart
ALTER SYSTEM SET archive_timeout = '60s';              -- the RPO bound
ALTER SYSTEM SET archive_command =
  'test ! -f /backups/wal/%f.gz && gzip -c %p > /backups/wal/%f.gz.tmp && mv /backups/wal/%f.gz.tmp /backups/wal/%f.gz';
```

- **The command is atomic.** It writes `.tmp` and then renames, so a crash mid-copy can't leave a truncated segment that looks complete.
- **It refuses to overwrite** an existing segment.
- **It's compressed.** `archive_timeout` ships a segment every minute even when idle, which is about 23 GB a day uncompressed. In the drill, an idle 16 MB segment gzipped to 24.5 KB.

Then take a base backup:

```bash
pg_basebackup -D /backups/base/$(date -u +%Y%m%dT%H%M%SZ) -Ft -z -X stream -c fast
```

Then watch the archiver. `failed_count` must stay at 0, and `last_archived_time` must be under a minute old:

```sql
SELECT archived_count, last_archived_wal, last_archived_time, failed_count FROM pg_stat_archiver;
```

**These are drill settings, not production durability.** In the drill, the archive sat on the same disk as the database, so losing that disk loses both. `cp` and `gzip` don't fsync either. For production, use a tool built for this, **pgBackRest** or **WAL-G**. Either one adds:
- fsync'd, checksummed pushes to object storage in another region;
- retention and expiry;
- parallel restore.

The restore procedure and the verification below stay the same.

### Managed Postgres (the likely production setup)

Most managed providers do not let you set `archive_command`. Point-in-time recovery is then the provider's feature, or it isn't available.

- **If the provider offers PITR,** turn it on with at least 7 days' retention. Then run the drill below against a restore the provider makes.
- **If it only offers daily snapshots,** the RPO is up to **24 hours** of appointments. That's not acceptable for this product. Either move to a provider with PITR, or run Postgres yourself with pgBackRest or WAL-G.
- **Railway: TBD.** Whether Railway's Postgres offers PITR is unverified, because its docs were unreachable from the environment this was written in. Check that before launch, not after.

## Restoring

**Never restore over the live cluster.** Restore next to it, verify, then switch. The broken cluster is evidence, and for a single-business accident it's also the source of everything written since the target (see "Choosing what to restore").

### 1. Choose the target

- **After a bad write:** a moment just before it. Find it in the API logs or in Postgres's logs, and allow a little margin. Everything committed after the target is left out, including legitimate writes from other businesses.
- **After losing the server:** `latest`, meaning everything in the archive.

### 2. Before restoring, take a manifest if the source is still up

This is optional, but it is what turns "it looks fine" into proof:

```bash
python scripts/verify_restore.py snapshot --url "$ADMIN_DATABASE_URL" > before.json
```

After a bad write it describes the damaged state, not the target. Use it to see what changed. It's not a pass or fail test.

### 3. Restore into a new data directory

On self-managed Postgres:

```bash
sudo -u postgres infra/postgres/pitr-restore.sh \
    /backups/base/<newest base before the target> /backups/wal \
    "2026-11-04 18:31:15+00"   `# or: latest` \
    /var/lib/postgresql/16/restored 5433
```

The script:
- refuses to touch an existing directory;
- starts the copy with **archiving off**, so it doesn't write a second timeline into the production archive;
- listens on the local socket only;
- prints the restore time;
- fails, with the log, if the archive can't reach the target.

A managed provider does the equivalent from its console: "restore to a point in time" into a new instance.

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
5. Re-enable backups **on the restored database**, which is now the primary: archiving on, and a fresh base backup straight away. Its WAL is a new timeline, and nothing protects it until that base backup exists.

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

**Cadence:** quarterly, plus after a Postgres major upgrade, a provider change, or a backup-tool change. An untested backup isn't a backup.

Each drill:
1. writes load after the base backup, so the restore has to replay WAL;
2. takes a manifest at the target;
3. makes the mistake;
4. restores;
5. runs `verify_restore.py`;
6. points the API at the copy and signs in.

Record the results below.

### Drill log

**2026-11-04: local Postgres 16.13, 117 MB.** Base backup 36 MB compressed, taken in 8.5 s. Afterwards, 200 calls went through the real webhook (load test, `--keep`).

| Scenario | What happened | Result |
|---|---|---|
| **A. Operator deletes a business** (cascade; 200 calls and 36 appointments gone). One legitimate write afterwards, a rename. | Restored to T, 16 s before the DELETE. Replay stopped "before commit of transaction 105696", the DELETE. | Restore and promote **2.7 s**. `verify --against`: **25/25** (every table identical; the rename correctly absent). API on the copy: `/readyz` ready, owner login, data served. The same check on the damaged live database failed 9 tables and named the business. |
| **B. Server lost** (unplanned: no WAL switch). Writer committing every 200 ms. | Restored `latest` from the archive as it stood at the loss. | **43.2 s** of writes lost (178 rows): everything after the last archived segment, within the 60 s bound. Restore **2.7 s**. Structural verify **8/8**. |
| **C. Compressed archive** | Archiving switched to gzip. A marker row was restored from a `.gz` segment, with plain and gzipped segments mixed. | Restore **3.3 s**. Marker present. |

Both restored copies ran on timeline 2 with archiving off. Afterwards, 0 timeline-2 or `.history` files had reached the production archive.

**Not covered by this drill:**
- offsite storage, or losing the disk;
- the cutover (step 5);
- a single-business copy-back;
- a managed provider's restore;
- production-sized data, where replay time is the number to measure.
