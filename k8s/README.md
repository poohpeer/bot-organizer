# Deploying to Kubernetes

The `k8s/` directory deploys three workloads in the `bot-organizer`
namespace: PostgreSQL, the Telegram bot, and the background worker. The bot
and worker use the image published to
`ghcr.io/poohpeer/bot-organizer` by
`.github/workflows/deploy-k8s.yml`.

## First-time setup

The following commands must be run from a machine with `kubectl` configured
for the target cluster. The real application secret is created manually and
is never committed to Git.

1. Confirm that the cluster is reachable:

   ```powershell
   kubectl get nodes
   ```

2. Create the namespace:

   ```powershell
   kubectl apply -f k8s/namespace.yaml
   ```

3. Create the image pull secret. This is needed when the GHCR package is
   private. Use a GitHub PAT with the `read:packages` scope:

   ```powershell
   kubectl create secret docker-registry ghcr-pull-secret `
     --namespace bot-organizer `
     --docker-server=ghcr.io `
     --docker-username=<github-username> `
     --docker-password=<github-pat-with-read-packages>
   ```

4. Create the application secret:

   ```powershell
   Copy-Item k8s/secret.example.yaml k8s/secret.yaml
   # Fill in BOT_ORGANIZER_BOT_TOKEN, GEMINI_API_KEY, GOOGLE_MAPS_API_KEY,
   # POSTGRES_PASSWORD, and the matching DATABASE_URL.
   kubectl apply -f k8s/secret.yaml
   ```

   `k8s/secret.yaml` is gitignored. Do not commit it or paste its values into
   a workflow.

5. Apply the ConfigMap, PostgreSQL StatefulSet, and bot/worker Deployments:

   ```powershell
   kubectl apply -k k8s/
   kubectl -n bot-organizer get pods,pvc
   ```

   Wait for PostgreSQL to become `Ready`, then check the application logs:

   ```powershell
   kubectl -n bot-organizer logs deployment/bot
   kubectl -n bot-organizer logs deployment/worker
   ```

## Deploying a new version

Pushes to `0001-S10-deployment` build and publish `:latest` and `:<sha>`
images. After the build succeeds, the `deploy` job runs on the self-hosted
Runner and executes:

```powershell
kubectl -n bot-organizer rollout restart deployment/bot
kubectl -n bot-organizer rollout restart deployment/worker
kubectl -n bot-organizer rollout status deployment/bot --timeout=180s
kubectl -n bot-organizer rollout status deployment/worker --timeout=180s
```

The Runner does not need the application secrets. It only needs `kubectl`,
access to the cluster, and a kubeconfig/context that can update the
`bot-organizer` namespace. If the Runner is installed as a Windows service,
run it under the user account whose kubeconfig can already execute:

```powershell
kubectl -n bot-organizer get pods
kubectl -n bot-organizer rollout status deployment/bot
```

## Updating secrets

Edit the local `k8s/secret.yaml` and apply it again:

```powershell
kubectl apply -f k8s/secret.yaml
kubectl -n bot-organizer rollout restart deployment/bot
kubectl -n bot-organizer rollout restart deployment/worker
```

Pods receive Secret values as environment variables at startup, so a restart
is required after changing them.

## Data and scaling

PostgreSQL stores its data in the `postgres-data` PVC, requested at `10Gi`.
Redeploying the workloads does not remove the PVC; deleting the namespace or
the PVC deletes the database data.

Both application Deployments intentionally use one replica and the `Recreate`
strategy. Telegram long polling allows only one active bot process for the
same token; running two bot replicas causes `409 Conflict` errors.

No Service or Ingress is required for the bot or worker. They only make
outbound connections to Telegram, Gemini, Google Maps, weather services, and
PostgreSQL. PostgreSQL is available inside the namespace as `postgres:5432`.
