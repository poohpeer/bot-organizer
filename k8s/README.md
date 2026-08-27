# Deploying to Kubernetes

Images are built and pushed to `ghcr.io/poohpeer/bot-organizer` by
`.github/workflows/ci.yml` on pushes to `main` (and to `0001-S10-deployment`
until that branch is merged), after the test suite passes — no
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

Pushes to `0001-S10-deployment` build, test, and publish a new `:latest` and
`:<sha>` image. After the image is pushed, the `deploy` job runs on the
self-hosted Runner and automatically restarts both application Deployments:

```powershell
kubectl rollout restart deployment/bot-organizer-bot
kubectl rollout restart deployment/bot-organizer-worker
kubectl rollout status deployment/bot-organizer-bot --timeout=180s
kubectl rollout status deployment/bot-organizer-worker --timeout=180s
```

To deploy manually:

```powershell
kubectl rollout restart deployment/bot-organizer-bot
kubectl rollout restart deployment/bot-organizer-worker
```

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
  workload restarts. Deleting the PVC deletes the database.
- `replicas: 1` and `strategy: Recreate` are intentional. Telegram long
  polling allows only one active bot process for the same token; two bot
  replicas cause `409 Conflict` errors.
- No Service or Ingress is needed for the bot or worker. PostgreSQL is
  available inside the namespace as `bot-organizer-postgres:5432`.
