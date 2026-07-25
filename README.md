# gateway-api — Envoy Gateway learning + production walkthrough

Notes and step-by-step files from learning Kubernetes Gateway API using Envoy Gateway, on minikube. App used throughout: `example-app.com`. Gateway, HTTPRoute, Certificates, and backend apps all live in the `envoy-gateway-system` namespace (not `default`) in this setup.

## Reference material

- [`gateway-api-envoy-guide.md`](./gateway-api-envoy-guide.md) — concept reference: every Gateway API kind (GatewayClass, Gateway, HTTPRoute, GRPCRoute, TLSRoute, TCPRoute, UDPRoute, ReferenceGrant, BackendTLSPolicy, ListenerSet), every Envoy Gateway CRD (EnvoyProxy, ClientTrafficPolicy, BackendTrafficPolicy, SecurityPolicy, EnvoyExtensionPolicy, HTTPRouteFilter, EnvoyPatchPolicy), architecture, and a comparison against Istio/Cilium/Kong/NGINX Gateway Fabric.
- `gateway_api_envoy_request_flow.png` — the request-flow diagram from that guide.

## Hands-on production walkthrough (minikube)

Each numbered folder is a self-contained step with its own `readme.md` — run them in order. The actual YAML lives alongside each step's readme, applied in the order shown inside it.

| Step | Folder / doc | Status | Covers |
|---|---|---|---|
| 1 | [`01-gateway/readme.md`](./01-gateway/readme.md) | Done | minikube setup, `minikube tunnel` vs port-forward, Helm install of Envoy Gateway control plane, `GatewayClass`, `Gateway` with HTTP + HTTPS listeners for `example-app.com` |
| 2 | [`certs/02-tls-cert-manager.md`](./certs/02-tls-cert-manager.md) | Done | cert-manager install, self-signed two-tier CA, Certificate for `example-app.com`, why the `https` listener resolves once the Secret exists, why you get a 404 without an HTTPRoute yet, Let's Encrypt swap for production |
| 3 | [`01-gateway/03-httproute/readme.md`](./01-gateway/03-httproute/readme.md) | Done | Backend Deployment/Service, basic HTTPRoute + weighted canary split, header-based routing, redirect vs URL rewrite (each as its own `HTTPRoute` object), rule precedence |
| 4 | [`02-envoy-specific/readme.md`](./02-envoy-specific/readme.md) | Done | Envoy Gateway-specific CRDs: `ClientTrafficPolicy` (connection limit, mTLS), `BackendTrafficPolicy` (local rate limit, retries, circuit breaking), `SecurityPolicy` (basic auth, JWT via local JWKS) — plus the one-policy-per-target conflict rule that bit this repo repeatedly |
| 5 | [`03-observability-stack/readme.md`](./03-observability-stack/readme.md) | Done | Standalone monolithic Loki/Tempo/Prometheus/Grafana (individual Helm charts, not the `eg-addons` bundle), metrics auto-discovery, tracing wired to Tempo via OTLP. Log shipping to Loki not yet covered. |
| 5-alt (optional) | [`03-observability-stack/external-observability.md`](./03-observability-stack/external-observability.md) | Done | Skipping the standalone stack entirely and pointing tracing/metrics at your own already-running external OTel collector — `Backend` CRD for the external URL, TLS via `BackendTLSPolicy`/`Backend.spec.tls`, auth token header |

### Repo layout

```
01-gateway/
  00-gatewayclass.yaml     GatewayClass (base — see note below)
  01-gateway.yaml          Gateway "gateway-api": http + https listeners
  02-gateway-config.yaml   GatewayClass (re-applied with parametersRef) + EnvoyProxy + tracing (points at Tempo, see step 5)
  readme.md                Step 1
  03-httproute/            split into one HTTPRoute object per feature (all bind to the same Gateway/hostname; Gateway API merges their rules):
    00-canary-split.yaml     "example-app-go" — basic routing + weighted 90/10 canary split
    01-header-canary.yaml    "example-app-go-header-canary" — x-canary:true header forces v2
    02-redirect.yaml         "example-app-go-redirect" — /old → redirect to an external domain
    03-rewrite.yaml          "example-app-go-rewrite" — /v4/* rewritten to / on example-app-go-v2
    readme.md                Step 3
certs/
  00-ca-bootstrap.yaml     self-signed root CA + CA ClusterIssuer
  01-app-cert.yaml         Certificate for example-app.com → example-app-tls-secret
  jwt-signing-key.pem      RSA key for the JWT SecurityPolicy demo (gitignored)
  02-tls-cert-manager.md   Step 2
00-application/
  go-v1.yaml               example-app-go-v1 Deployment + Service
  go-v2.yml                example-app-go-v2 Deployment + Service (canary)
02-envoy-specific/
  00-client-traffic/       ClientTrafficPolicy: connection limit, mTLS
  01-backend-traffic/      BackendTrafficPolicy: local rate limit, retries + circuit breaking
  02-security/             SecurityPolicy: basic auth, JWT (local JWKS) — plus mint-jwt.py token minting script
  readme.md                Step 4
03-observability-stack/
  loki-values.yaml         Loki, monolithic mode
  tempo-values.yaml        Tempo, monolithic mode
  prometheus-values.yaml   plain Prometheus chart (not kube-prometheus-stack)
  grafana-values.yaml      Grafana, datasources pre-wired to the three above
  readme.md                Step 5
  external-observability.md  Step 5-alt (external collector instead)
```

## Environment assumptions

- Cluster: minikube, Docker driver
- Envoy Gateway version: v1.8.2 (Helm chart `oci://docker.io/envoyproxy/gateway-helm`)
- cert-manager version: v1.20.3 (Helm chart `oci://quay.io/jetstack/charts/cert-manager`)
- Observability: standalone Helm charts, not `eg-addons` — Loki + Tempo from `grafana-community/helm-charts` (chart repo moved here from `grafana/helm-charts` in early 2026), Prometheus from `prometheus-community/helm-charts`, all in the `observability` namespace
- Namespace for everything else (control plane, Gateway, HTTPRoute, Certificates, apps): `envoy-gateway-system`
- GatewayClass: `envoy-gateway` · Gateway: `gateway-api` · domain: `example-app.com`

## Things fixed / flagged during review and hands-on testing

Fixed directly, confirmed working against a real cluster:

- **`02-envoy-specific/02-security/00-basic-auth.yaml`** — the `Secret` key holding the htpasswd data must be named exactly `.htpasswd` (not `users.txt`), and the hash must be Apache `{SHA}` format (`htpasswd -s`), not bcrypt. Envoy Gateway rejects both the wrong key name and the wrong hash format with specific `SecurityPolicy` status errors.
- **`02-envoy-specific/01-backend-traffic/00-rate-limit.yaml`** (`BackendTrafficPolicy`) had no `namespace` set at one point during testing. Its `targetRef` points at the `example-app-go` `HTTPRoute` in `envoy-gateway-system` — a policy without an explicit namespace resolves against whatever namespace you `kubectl apply` it into, and `targetRef` can't cross namespaces. Re-added `namespace: envoy-gateway-system`.
- **`.gitignore`** had a blanket `certs/*` rule at one point, silently excluding `certs/00-ca-bootstrap.yaml` and `certs/01-app-cert.yaml` from git. Narrowed to `certs/*.crt`/`*.key`/`*.pem` (actual cert/key material) so the two manifests are tracked.
- **`01-gateway/02-gateway-config.yaml`** tracing `backendRefs` was still pointing at a never-installed `otel-collector`/`monitoring` target left over from an earlier plan. Repointed at the real Tempo Service (`tempo`, `observability` namespace, port `4317`) once step 5's standalone stack was actually running.
- **`03-observability-stack/loki-values.yaml`** — two chart-specific gotchas: the bundled MinIO subchart now hard-fails `helm install` unless `ignoreMinioDeprecation: true` is set (removal date 2026-10-31 per the chart), and the memcached-based caches (`chunksCache`, `resultsCache`) derive pod resources from `allocatedMemory` (the `-m` flag) rather than a plain `resources:` override — the latter gets silently recomputed away. Set `allocatedMemory: 64` directly on both instead of fighting the override.
- **`03-observability-stack/{loki,tempo,prometheus,grafana}-values.yaml`** — all stripped to `resources: {}` (or `allocatedMemory` where that's the real knob) across every component, since default chart resource requests left several pods stuck `Pending` on this demo cluster's node capacity. Put real limits back before this touches anything beyond a learning cluster.

Flagged, not fully resolved — need your call or further digging:

- **`02-envoy-specific/00-client-traffic/01-mtls.yaml`**'s `caCertificateRefs` points at `example-app-tls-secret`, assuming cert-manager populated its `ca.crt` with `gw-demo-root-ca` (from `certs/00-ca-bootstrap.yaml`). Testing found the actual CA in that Secret is `gw-demo-client-ca` — a CA not defined anywhere in this repo's `certs/` manifests, so it must exist directly in the cluster. Client certs need to be signed against whichever CA is *actually* in that Secret, confirmed via `openssl x509 -in <extracted-ca.crt> -noout -issuer`, not assumed.
- **One-policy-per-target conflicts**: over the course of testing, `example-app-go` ended up as the target of multiple `SecurityPolicy` and `BackendTrafficPolicy` objects at various points (basic auth vs. JWT; rate limit vs. retry/circuit-breaker). Envoy Gateway only honors one of each kind per target — the other silently sits `Accepted: False`. Before assuming any policy from step 4 is active, check `kubectl get clienttrafficpolicy,backendtrafficpolicy,securitypolicy -n envoy-gateway-system` and `describe` the one you care about.
- **Log shipping to Loki** is explicitly not wired up yet — Loki is installed and idle in step 5. Wiring Envoy's access logs to it (via `ClientTrafficPolicy` or an `EnvoyProxy` access log config) is an open follow-up.
- `00-application/go-v2.yml` uses a `.yml` extension while `go-v1.yaml` uses `.yaml` — cosmetic only, rename whenever convenient.
- The Gateway's `http` listener (port 80) has no `hostname`, so it's a catch-all serving `example-app.com` in plaintext alongside the `https` listener, with no redirect. Fine for local learning; add an HTTP→HTTPS redirect rule before this is exposed anywhere real.
