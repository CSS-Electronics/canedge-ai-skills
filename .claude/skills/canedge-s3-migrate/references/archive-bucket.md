# canedge-s3-migrate: Archive bucket, storage classes, costs, side effects

## Create the archive bucket (CloudShell)

Same region as the source (no transfer fees). Replace the name and region:

```
aws s3api create-bucket --bucket my-canedge-archive --region eu-central-1 \
  --create-bucket-configuration LocationConstraint=eu-central-1
```

In `us-east-1`, omit `--create-bucket-configuration`. New buckets block public access by default. To browse the
archive in CANcloud, add the CORS rules of the CANedge S3 stack (`aws s3api put-bucket-cors --bucket ...
--cors-configuration file://cors.json`, rules: Origins `*`, methods GET/PUT/POST/DELETE/HEAD, headers `*`,
expose `x-amz-meta-fw`, `x-amz-meta-hw`, `x-amz-meta-ssid`, `x-amz-meta-timestamp`, `x-amz-meta-put-index`,
`x-amz-meta-net`, `content-length`) and log in with a partial Configuration File holding its S3 details.

## Storage classes

Set per copy with `plan --storage-class`. Approx. us-east-1 list prices (other regions ~10-20% higher), check
[aws.amazon.com/s3/pricing](https://aws.amazon.com/s3/pricing/):

| Class | $/GB-month | Min. duration | Reading files | Good for |
|---|---|---|---|---|
| `STANDARD` | 0.023 | - | Instant, no fee | Data still in use |
| `STANDARD_IA` | 0.0125 | 30 days | Instant, $0.01/GB | Occasional access |
| `GLACIER_IR` | 0.004 | 90 days | Instant, $0.03/GB | Rarely needed, but then directly (CANcloud, downloads, reprocessing work as usual) |
| `GLACIER` | 0.0036 | 90 days | Restore first (minutes to 12 h) | Archive you can wait for |
| `DEEP_ARCHIVE` | 0.00099 | 180 days | Restore first (12-48 h) | Keep for compliance, probably never read |

- Example, 1 TB archived: ~$23/month in STANDARD, ~$4 in GLACIER_IR, ~$1 in DEEP_ARCHIVE
- Deleting or moving before the minimum duration is billed for the full period
- `STANDARD_IA` / `GLACIER_IR` bill objects < 128 KB as 128 KB; `GLACIER` / `DEEP_ARCHIVE` add ~40 KB per object.
  Irrelevant for typical 10-50 MB log files, relevant for tiny split sizes
- One-off requests: ~1 HEAD per file (plan) + 1 COPY (+1 HEAD) per selected file: $0.005-0.05 per 1,000 files
  depending on the class. Data never leaves AWS (same region: no transfer fees)

## Side effects to explain before copying/deleting

- **Parquet data lake**: Untouched (separate output bucket). Athena/Grafana keep all history, also after the MF4
  files are deleted from the input bucket
- **Data lake automation**: Triggered by device uploads to the input bucket only. Copies into the archive bucket
  and deletes from the source trigger nothing
- **Reprocessing later** (e.g. a new DBC via backlog processing): Needs the archived files: Run it against the
  archive bucket or copy files back. `GLACIER`/`DEEP_ARCHIVE` files need `aws s3api restore-object` first
- **CANcloud** shows the bucket you log into: Archived files appear in the archive bucket only
- **Devices** keep uploading to the source bucket; `device.json` and configs stay there (never deleted)
- **Versioning on the source**: Deleted files remain as noncurrent versions and stay billed: Add a lifecycle
  rule that expires noncurrent versions (`plan` warns about this)
- **Alternative without a script**: An S3 lifecycle rule on the input bucket that transitions files to a
  Glacier class after N days. Simpler for "everything older than N days", but by upload date (not logging time),
  in place (CANcloud cannot open `GLACIER`/`DEEP_ARCHIVE` files) and not per device

## Recurring archiving

Re-run `plan` with the new cutoff into the same archive bucket: Files already copied are detected in `copy`
(HEAD, same size) and only verified again. Files kept as `contains the cutoff` move with the next cutoff.

## Permissions (if the console user is not an admin)

- Source: `s3:ListBucket`, `s3:GetObject`, `s3:GetBucketLocation`, `s3:GetBucketVersioning`; for moves also
  `s3:DeleteObject`
- Destination: `s3:ListBucket` (so missing keys return 404, not 403), `s3:GetObject`, `s3:PutObject`,
  `s3:GetBucketLocation`
- SSE-KMS encrypted source: `kms:Decrypt` on its key

## Limits

- CloudShell ends a session after ~20-30 min without keyboard input, also during a run: Keep the tab active
  for long runs. `copy`, `verify` and `delete-source` resume from the manifest when re-run; `plan` starts over
- The manifest holds ~0.5 KB per object (~250 MB for 500,000 objects, CloudShell has 1 GB home storage): Split
  very large buckets with `--devices`
- Objects > 5 GB are not copied (single-copy limit); not expected for CANedge log files
