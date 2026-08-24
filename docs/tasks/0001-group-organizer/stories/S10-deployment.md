# Story S10: Deployment — Dockerfile, Kubernetes manifests

**Part of:** Group organizer bot (`../EPIC.md`)
**Goal:** Run both processes — the bot (`bot/main.py`, long polling) and
the background worker (`worker/main.py`, poll loop) — as two Kubernetes
Deployments against one in-cluster Postgres, built from a single shared
image, on a local k3s cluster with no manual `docker build`/`push` step
(mirrors the sibling `general-telegram-bot` project's proven pattern).
**Satisfies:** infra only — no new requirement, packages R1–R11's
implementation for deployment.
**Depends on:** S8, S9
**Parallel-safe with:** none (last story)
**Requirements & global constraints:** see `../EPIC.md`

---

### Task 1: Shared image + Postgres + two Deployments

**Satisfies:** infra

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`
- Create: `k8s/postgres-pvc.yaml`
- Create: `k8s/postgres-deployment.yaml`
- Create: `k8s/postgres-service.yaml`
- Create: `k8s/bot-deployment.yaml`
- Create: `k8s/worker-deployment.yaml`
- Create: `k8s/secret.example.yaml`
- Create: `k8s/README.md`

**Interfaces:**
- Consumes: `pyproject.toml`/`uv.lock` (S1), `bot/main.py` (S9),
  `worker/main.py` (S8), env vars from the epic's Global Constraints
  (`BOT_TOKEN`, `GEMINI_API_KEY`, `GOOGLE_MAPS_API_KEY`, `DATABASE_URL`,
  `REMINDER_MIN_INTERVAL_HOURS`).
- Produces: a buildable image tagged `bot-organizer`, and two Deployments
  (`bot-organizer-bot`, `bot-organizer-worker`) that both point at the
  same in-cluster `postgres` Service.

- [ ] **Step 1: Write the failing check**
  ```bash
  docker build -t bot-organizer .
  ```

- [ ] **Step 2: Run it, confirm it fails**
  Expected: build fails — no `Dockerfile` in the build context yet
  (`unable to prepare context: ... no such file or directory`).

- [ ] **Step 3: Create `.dockerignore`**
  ```
  .venv/
  venv/
  .idea/
  __pycache__/
  *.pyc
  .git/
  docs/
  tests/
  .env
  .pytest_cache/
  ```

- [ ] **Step 4: Create `Dockerfile`**
  ```dockerfile
  FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

  WORKDIR /app
  COPY pyproject.toml uv.lock ./
  RUN uv sync --frozen --no-dev

  COPY bot ./bot
  COPY db ./db
  COPY worker ./worker

  CMD ["uv", "run", "--no-sync", "python", "-m", "bot.main"]
  ```
  The `worker` Deployment overrides `command` to run `worker.main`
  instead — same image, same dependencies, no second Dockerfile needed.

- [ ] **Step 5: Build and confirm it succeeds**
  ```bash
  docker build -t bot-organizer .
  docker run --rm bot-organizer uv run --no-sync python -c "import bot.main, worker.main; print('ok')"
  ```
  Expected: `ok`, no import errors.

- [ ] **Step 6: Create `k8s/postgres-pvc.yaml`**
  ```yaml
  apiVersion: v1
  kind: PersistentVolumeClaim
  metadata:
    name: postgres-data
  spec:
    accessModes:
      - ReadWriteOnce
    resources:
      requests:
        storage: 1Gi
  ```

- [ ] **Step 7: Create `k8s/postgres-deployment.yaml`**
  ```yaml
  apiVersion: apps/v1
  kind: Deployment
  metadata:
    name: postgres
  spec:
    replicas: 1
    strategy:
      type: Recreate
    selector:
      matchLabels:
        app: postgres
    template:
      metadata:
        labels:
          app: postgres
      spec:
        containers:
          - name: postgres
            image: postgres:16
            envFrom:
              - secretRef:
                  name: bot-organizer-secrets
            env:
              - name: POSTGRES_DB
                value: bot_organizer
            ports:
              - containerPort: 5432
            volumeMounts:
              - name: postgres-data
                mountPath: /var/lib/postgresql/data
        volumes:
          - name: postgres-data
            persistentVolumeClaim:
              claimName: postgres-data
  ```

- [ ] **Step 8: Create `k8s/postgres-service.yaml`**
  ```yaml
  apiVersion: v1
  kind: Service
  metadata:
    name: postgres
  spec:
    selector:
      app: postgres
    ports:
      - port: 5432
        targetPort: 5432
  ```

- [ ] **Step 9: Create `k8s/bot-deployment.yaml`**
  ```yaml
  apiVersion: apps/v1
  kind: Deployment
  metadata:
    name: bot-organizer-bot
  spec:
    replicas: 1
    # Recreate, not RollingUpdate: long-polling (getUpdates) — two pods on
    # the same BOT_TOKEN at once causes Telegram 409 Conflicts.
    strategy:
      type: Recreate
    selector:
      matchLabels:
        app: bot-organizer-bot
    template:
      metadata:
        labels:
          app: bot-organizer-bot
      spec:
        containers:
          - name: bot
            image: bot-organizer:latest
            envFrom:
              - secretRef:
                  name: bot-organizer-secrets
  ```

- [ ] **Step 10: Create `k8s/worker-deployment.yaml`**
  ```yaml
  apiVersion: apps/v1
  kind: Deployment
  metadata:
    name: bot-organizer-worker
  spec:
    replicas: 1
    # Recreate: two workers polling at once would double-send reminders
    # and closing questions — the rate-limit logic assumes a single poller.
    strategy:
      type: Recreate
    selector:
      matchLabels:
        app: bot-organizer-worker
    template:
      metadata:
        labels:
          app: bot-organizer-worker
      spec:
        containers:
          - name: worker
            image: bot-organizer:latest
            command: ["uv", "run", "--no-sync", "python", "-m", "worker.main"]
            envFrom:
              - secretRef:
                  name: bot-organizer-secrets
  ```

- [ ] **Step 11: Create `k8s/secret.example.yaml`**
  ```yaml
  # Copy to k8s/secret.yaml, fill in real values, then:
  #   kubectl apply -f k8s/secret.yaml
  # k8s/secret.yaml is gitignored — never commit real tokens/keys.
  apiVersion: v1
  kind: Secret
  metadata:
    name: bot-organizer-secrets
  type: Opaque
  stringData:
    BOT_TOKEN: ""
    GEMINI_API_KEY: ""
    GOOGLE_MAPS_API_KEY: ""
    POSTGRES_PASSWORD: ""
    POSTGRES_USER: "bot_organizer"
    DATABASE_URL: "postgresql://bot_organizer:<same-password-as-above>@postgres:5432/bot_organizer"
    REMINDER_MIN_INTERVAL_HOURS: "6"
  ```

- [ ] **Step 12: Add `k8s/secret.yaml` to `.gitignore`**
  ```
  k8s/secret.yaml
  ```

- [ ] **Step 13: Create `k8s/README.md`**
  ```markdown
  # Deploying to Kubernetes

  Two Deployments (`bot-organizer-bot`, `bot-organizer-worker`) share one
  image and one in-cluster Postgres.

  ## First-time setup (once per cluster)

  1. Confirm `kubectl get nodes` shows a `Ready` node (e.g. k3s).
  2. Build and make the image available to the cluster (for k3s:
     `docker build -t bot-organizer:latest . && k3s ctr images import
     <(docker save bot-organizer:latest)`, or push to your in-cluster
     registry per your existing setup).
  3. Copy `k8s/secret.example.yaml` to `k8s/secret.yaml`, fill in real
     values (`BOT_TOKEN`, `GEMINI_API_KEY`, `GOOGLE_MAPS_API_KEY`, a chosen
     `POSTGRES_PASSWORD`, and `DATABASE_URL` using that same password),
     then:
     ```
     kubectl apply -f k8s/secret.yaml
     ```
  4. Apply Postgres first, wait for it to be ready, then the app:
     ```
     kubectl apply -f k8s/postgres-pvc.yaml -f k8s/postgres-deployment.yaml -f k8s/postgres-service.yaml
     kubectl rollout status deployment/postgres
     kubectl apply -f k8s/bot-deployment.yaml -f k8s/worker-deployment.yaml
     ```
  5. Verify:
     ```
     kubectl get pods
     kubectl logs -f deployment/bot-organizer-bot
     kubectl logs -f deployment/bot-organizer-worker
     ```
     Expect `Starting bot (long polling)` and `Worker started, polling
     every 60s`, with no `KeyError`/connection errors.

  ## Notes

  - `replicas: 1` + `strategy: Recreate` on **both** Deployments is
    intentional: the bot uses long polling (two pollers on one
    `BOT_TOKEN` causes `409 Conflict`), and the worker's reminder/closing-
    question rate-limit logic assumes a single poller.
  - No `Service`/`Ingress` needed for the bot or worker — both only make
    outbound requests (Telegram, Gemini, Google Places, Open-Meteo,
    Postgres); neither accepts inbound traffic.
  - Postgres data lives on the `postgres-data` PVC, independent of both
    app Deployments — redeploying the bot or worker never touches it.
  ```

- [ ] **Step 14: Manual verification**
  ```bash
  kubectl apply -f k8s/secret.yaml
  kubectl apply -f k8s/postgres-pvc.yaml -f k8s/postgres-deployment.yaml -f k8s/postgres-service.yaml
  kubectl rollout status deployment/postgres
  kubectl apply -f k8s/bot-deployment.yaml -f k8s/worker-deployment.yaml
  kubectl get pods
  ```
  Expected: all three pods (`postgres`, `bot-organizer-bot`,
  `bot-organizer-worker`) reach `Running`/`1/1 Ready` with no
  `CrashLoopBackOff`.

- [ ] **Step 15: Commit**
  ```bash
  git add Dockerfile .dockerignore .gitignore k8s
  git commit -m "Add Dockerfile and Kubernetes manifests for bot + worker + Postgres"
  ```
