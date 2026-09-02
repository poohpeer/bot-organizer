# Deploying to Kubernetes

Images are built and pushed to `ghcr.io/poohpeer/bot-organizer` by
`.github/workflows/ci.yml` on pushes to `main`, after the test suite passes — no
manual `docker build`/`docker push` is needed.

All resources use the current Kubernetes namespace, the same way as the
neighboring bot project. Resource names are prefixed with `bot-organizer-` so
this bot can share the namespace with other bots without name collisions.

## First-time setup (once per cluster)

1. Install a local cluster and confirm `kubectl get nodes` shows a `Ready`
   node.

2. Create an image pull secret. This is needed when the GHCR package is
   private; use a GitHub PAT with the `read:packages` scope:

   ```
   kubectl create secret docker-registry bot-organizer-ghcr-pull-secret \
     --docker-server=ghcr.io \
     --docker-username=<your-github-username> \
     --docker-password=<your-PAT> \
     --docker-email=<your-email>
   ```

3. Create the application Secret. Copy `k8s/secret.example.yaml` to
   `k8s/secret.yaml`, fill in the real values, then apply it:

   ```
   kubectl apply -f k8s/secret.yaml
   ```

   `k8s/secret.yaml` is gitignored. Never commit real tokens or keys.

4. Apply the ConfigMap, PostgreSQL StatefulSet, and bot/worker Deployments:

   ```
   kubectl apply -k k8s/
   ```

5. Verify:

   ```
   kubectl get pods,pvc
   kubectl logs -f deployment/bot-organizer-bot
   ```

   Expect the bot and worker pods to become `Running`, and PostgreSQL to show
   `Ready` before the application processes connect to it.

## Deploying a new version

Pushes to `main` test, build, and publish three tags of the same image:
`:v<run number>`, `:latest`, and `:<sha>`. **The cluster runs the numbered
one.** `:latest` is for reading and for pulling by hand; deploying it is what
this stopped doing.

The number is the workflow's run number, so it only ever goes up. That is the
whole reason for it: it makes "the newest" a fact the pipeline can sort on,
and it is what the image cleanup below uses, since containerd reports no
creation time to sort by instead.

The `deploy` job runs on the self-hosted Runner. It rewrites the `:latest` in
its *workspace copy* of `k8s/app.yaml` to the numbered tag, applies the
kustomization, waits for both rollouts, and then checks the bot actually
reached Telegram rather than trusting that the rollout completed.

Nothing restarts the Deployments any more. The image tag differs on every
build, so the apply is a real change to the pod spec and rolls them by
itself. Pinning before the apply rather than moving the image afterwards with
`kubectl set image` matters: the other order applies `:latest` first and rolls
every pod twice, once onto the wrong version.

The manifests keep `:latest` in git so that reading them, or applying them by
hand, gets something that works. The pin step fails loudly if it finds no
`:latest` to replace — a substitution that silently matched nothing would
deploy `:latest` for ever and look exactly like a working pipeline.

To deploy a specific version by hand:

```powershell
kubectl set image deployment/bot-organizer-bot bot=ghcr.io/poohpeer/bot-organizer:v42
kubectl set image deployment/bot-organizer-worker worker=ghcr.io/poohpeer/bot-organizer:v42
kubectl rollout status deployment/bot-organizer-bot --timeout=180s
```

### Only the last three versions stay on the node

The node keeps every image it has ever pulled and nothing else removes one, so
the last step of a deploy prunes them: it lists what containerd holds, keeps
the three highest-numbered versions — the one running, the one to roll back
to, and one more — and removes the rest.

It is best-effort by design: `continue-on-error`, and wrapped in a
`try`/`catch`. Housekeeping must never fail a deploy that otherwise worked,
and a failed prune costs disk, not uptime. Watch its log rather than assume it
ran.

Older versions stay in GHCR. Only the node is pruned.

The workflow does not receive or create application secrets. It only uses the
Runner's kubeconfig to restart resources that already exist in the current
namespace.

### Automatic rollout restart (self-hosted runner)

The `deploy` job in `ci.yml` (`runs-on: self-hosted`) needs a Runner
on the same machine that has `kubectl` configured for the target cluster. The
Runner account must already be able to run:

```powershell
kubectl get pods
kubectl rollout status deployment/bot-organizer-bot
```

If the Runner is installed as a Windows service, install it under the user
account whose kubeconfig is configured, not `SYSTEM` or `NETWORK SERVICE`.

## Updating secrets

Edit the local `k8s/secret.yaml` and apply it again:

```powershell
kubectl apply -f k8s/secret.yaml
kubectl rollout restart deployment/bot-organizer-bot
kubectl rollout restart deployment/bot-organizer-worker
```

Pods receive Secret values as environment variables at startup, so a restart
is required after changing them.

## Notes

- PostgreSQL data lives on the `bot-organizer-postgres-data` PVC and survives
  workload restarts. Deleting the PVC deletes the database — see "Backing the
  database up" below, which is the only copy there is.
- `replicas: 1` and `strategy: Recreate` are intentional. Telegram long
  polling allows only one active bot process for the same token; two bot
  replicas cause `409 Conflict` errors.
- No Service or Ingress is needed for the bot or worker. PostgreSQL is
  available inside the namespace as `bot-organizer-postgres:5432`.

## Backing the database up

There is one copy of this data and the PVC is the only thing holding it. A
`local-path` volume lives inside the kind node container: it survives a pod
restart and a cluster stop, and goes with the node container if that is ever
recreated — a Docker Desktop reset or `kind delete cluster` takes the
database with it. Take a dump before anything that touches the cluster
itself.

The dump is written inside the pod and then pulled out through base64. On
this homelab `kubectl` does not run locally — it runs on the Windows box over
SSH through PowerShell, which mangles a binary stream but leaves ASCII alone.
Substitute whatever wrapper reaches your cluster for `kubectl` below.

```bash
DUMP=bot-organizer-$(date +%F).dump

kubectl exec bot-organizer-postgres-0 -- \
  sh -c "pg_dump -U \"\$POSTGRES_USER\" -d bot_organizer -Fc -f /tmp/$DUMP"

mkdir -p ~/backups/bot-organizer
kubectl exec bot-organizer-postgres-0 -- base64 /tmp/$DUMP \
  | tr -d '\r\n ' | base64 -d > ~/backups/bot-organizer/$DUMP

# Prove the copy is byte-identical before trusting it.
kubectl exec bot-organizer-postgres-0 -- md5sum /tmp/$DUMP
md5sum ~/backups/bot-organizer/$DUMP
```

The password never appears: `$POSTGRES_USER` is expanded inside the pod, the
same way the probes do it.

### Restoring

An untested dump is not a backup. Restore it into a scratch database and
compare, rather than assuming:

```bash
pg_restore -U postgres -d restore_check --no-owner ~/backups/bot-organizer/$DUMP
```

`--no-owner` matters when restoring anywhere but this cluster. The dump
carries `ALTER TABLE ... OWNER TO bot_organizer`, and without that role the
restore reports 20 errors — ownership only, no data lost, but alarming enough
to look like a failed restore when it is not.

Compare the restored copy against the live one. Row counts alone would miss
mojibake, so checksum the text too — most of this database is Russian:

```sql
select md5(string_agg(key||value, chr(10) order by id)) from facts;
```

Verified 2026-08-30: a dump of the live database restored on an unrelated
`postgres:16` with identical row counts and an identical checksum.

## Two deliberate choices

**`PGDATA` is left at the mount root.** The PVC is mounted straight at
`/var/lib/postgresql/data`, which on some storage classes fails because the
volume root already contains `lost+found` and `initdb` refuses a non-empty
directory. The usual fix is to point `PGDATA` at a subdirectory — and it is
not applied here on purpose: this database already has data at the current
path, and moving `PGDATA` now would have Postgres initialise an empty cluster
in the subdirectory while the real one sat untouched beside it. Every session,
list and fact would look deleted. If the volume is ever recreated from scratch
on a storage class that does create `lost+found`, set `PGDATA` then, on an
empty volume.

**Resource limits are set on all three containers.** The requests come from a
measurement — 73 MiB to import the application, before any connections — not
from a guess. CPU limits exist because this namespace is shared: both app
processes are I/O bound and should never approach half a core, so a limit
costs nothing and stops a runaway loop from crowding the neighbours.
