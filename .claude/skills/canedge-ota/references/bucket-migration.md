# Move devices to another S3 bucket / server (canedge-ota)

A config change of `connect.s3.server` (endpoint, port, bucket, region, accesskey, secretkey). The device reads the
new config from the OLD bucket, reboots and connects to the new one. A mistake disconnects it until someone updates
it from the SD card, so: Canary first, and the plan test-reads the new server with the device's new keys.

## Steps

1. **Inputs** (ask for what is missing): Devices + their source bucket(s), the new endpoint/bucket/region per
   device, the new device keys (access key + secret key), meta changes (e.g. the old bucket name for tracking; meta
   allows letters, digits, `;`, spaces, max 30). TLS: An `https://` endpoint on a server with a custom certificate
   needs `certs_server.p7b` first (not handled here: CANcloud or the Docs `[S3 management]:` "Change to another S3
   endpoint")
2. **Tables** in `projects/<p>/ota/`:
   - `move.csv` (the agent writes it): `device_id,connect.s3.server.endpoint,connect.s3.server.bucket,`
     `connect.s3.server.region,connect.s3.server.accesskey,general.device.meta` (only the columns that change)
   - `secrets.csv` (template from the agent, filled in by the user): `device_id,connect.s3.server.secretkey` plus,
     for devices with encrypted credentials, the plaintext of every other encrypted field (e.g.
     `connect.wifi.accesspoint[0].pwd`, `connect.cellular.pin`). `status` shows which sections are encrypted
3. **Plan**, one per source bucket (each plan is one bucket):
   `plan --project P --table move.csv --table secrets.csv --label move-<bucket>` (`--env` / `--bucket` for the
   source). Per device the plan test-reads `<dev>/config-VV.VV.json` in the new bucket with the new keys:
   - Not found: Keys + bucket OK, folder clean (the device uploads its config on its first connect)
   - Found: A leftover config the device would apply: Blocked (the user removes it, then re-plan)
   - Access denied: Warning (device keys often lack `s3:ListBucket`, which hides "not found"): Make sure the keys
     are right before the canary
   - Other errors (wrong key, signature, bucket, endpoint): Blocked
4. **Review + canary**: As in SKILL.md. `apply --limit 1`, then `verify --plan DIR --target-env <profile of the new
   bucket>` (a profile for the new bucket from the setup window; without it verify only says whether the device
   still sends heartbeats to the old bucket). Then the rest
5. **After the move** (offer, do not do unasked):
   - Old log files stay in the old bucket: `canedge-s3-migrate` copies/moves them
   - Data lake on the new bucket: Deployment is outside the skills; control files (`dbc-groups.json`, root DBCs,
     events) via `canedge-lake-admin`, old data via a backlog there
   - The old bucket keeps the device folder (old configs, device.json): The user cleans it up when done

Many devices with their own buckets ("each device its own bucket, all to one new bucket"): One plan per source
bucket. With one profile that reaches all buckets (e.g. the AWS account's keys), loop `--bucket <source>` with the
same tables (without `--devices`, a plan takes the table devices that are in its bucket); otherwise one profile per
source bucket.
A custom script (custom-scripts.md) can create all plans in one run.
