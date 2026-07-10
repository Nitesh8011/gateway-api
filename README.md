# gateway-api — Envoy Gateway learning + production walkthrough

Notes and step-by-step files from learning Kubernetes Gateway API using Envoy Gateway, on minikube. App used throughout: `example-app.com`. Gateway, HTTPRoute, Certificates, and backend apps all live in the `envoy-gateway-system` namespace (not `default`) in this setup.

## Reference material

- [`gateway-api-envoy-guide.md`](./gateway-api-envoy-guide.md) — concept reference: every Gateway API kind (GatewayClass, Gateway, HTTPRoute, GRPCRoute, TLSRoute, TCPRoute, UDPRoute, ReferenceGrant, BackendTLSPolicy, ListenerSet), every Envoy Gateway CRD (EnvoyProxy, ClientTrafficPolicy, BackendTrafficPolicy, SecurityPolicy, EnvoyExtensionPolicy, HTTPRouteFilter, EnvoyPatchPolicy), architecture, and a comparison against Istio/Cilium/Kong/NGINX Gateway Fabric.
- `gateway_api_envoy_request_flow.png` — the request-flow diagram from that guide.

## Hands-on production walkthrough (minikube)

Each numbered `.md` file is a self-contained step — run them in order. The actual YAML lives in the folders below, applied in the order shown.

| Step | File | Status | Covers |
|---|---|---|---|
| 1 | [`01-install-envoy-gateway.md`](./01-install-envoy-gateway.md) | Done | minikube setup, `minikube tunnel` vs port-forward, Helm install of Envoy Gateway control plane, `GatewayClass`, `Gateway` with HTTP + HTTPS listeners for `example-app.com` |
| 2 | [`02-tls-cert-manager.md`](./02-tls-cert-manager.md) | Done | cert-manager install, self-signed two-tier CA, Certificate for `example-app.com`, why the `https` listener resolves once the Secret exists, why you get a 404 without an HTTPRoute yet, Let's Encrypt swap for production |
| 3 | [`03-gateway-httproute-yamls.md`](./03-gateway-httproute-yamls.md) | Done | Backend Deployment/Service, basic HTTPRoute, weighted canary split, header-based routing, redirect vs URL rewrite, rule precedence |
| 4 | [`04-observability.md`](./04-observability.md) | Done | Envoy Gateway add-ons chart (Prometheus/Grafana/Tempo/OTel Collector), auto-discovered metrics, enabling tracing via the existing `EnvoyProxy` in `02-gateway-config.yaml`, sampling rate production notes |
| 5 (optional) | [`05-external-otel-endpoint.md`](./05-external-otel-endpoint.md) | Done | Skipping the add-ons chart entirely and pointing tracing/metrics at your own already-running OTel collector — `Backend` CRD for the external URL, TLS via `BackendTLSPolicy`/`Backend.spec.tls`, auth token header, no bundled Prometheus/Grafana/Tempo |

### Repo layout

```
gateway-yaml/
  00-gatewayclass.yaml     GatewayClass (base — see note below)
  01-gateway.yaml          Gateway "gateway-api": http + https listeners
  02-gateway-config.yaml   GatewayClass (re-applied with parametersRef) + EnvoyProxy + tracing
  03-httproute.yaml        HTTPRoute "example-app-go": canary split active, header/redirect/rewrite rules present but commented out
certs/
  00-ca-bootstrap.yaml     self-signed root CA + CA ClusterIssuer
  01-app-cert.yaml         Certificate for example-app.com → example-app-tls-secret
application/
  go-v1.yaml               example-app-go-v1 Deployment + Service
  go-v2.yml                example-app-go-v2 Deployment + Service (canary)
envoy-specific/
  00-client-traffic/       ClientTrafficPolicy: connection limit, mTLS (see flags below)
  01-backend-traffic.yaml  BackendTrafficPolicy: local rate limit on the HTTPRoute
  02-security/              SecurityPolicy — currently empty (see flags below)
```

## Environment assumptions

- Cluster: minikube, Docker driver
- Envoy Gateway version: v1.8.2 (Helm chart `oci://docker.io/envoyproxy/gateway-helm`)
- cert-manager version: v1.20.3 (Helm chart `oci://quay.io/jetstack/charts/cert-manager`)
- Observability: Envoy Gateway add-ons chart v1.8.2 (Helm chart `oci://docker.io/envoyproxy/gateway-addons-helm`) — Prometheus, Grafana, Tempo, OTel Collector in `monitoring` namespace
- Namespace for everything (control plane, Gateway, HTTPRoute, Certificates, apps): `envoy-gateway-system`
- GatewayClass: `envoy-gateway` · Gateway: `gateway-api` · domain: `example-app.com`

## Things fixed / flagged during the last review

Fixed directly:

- `envoy-specific/01-backend-traffic.yaml` (`BackendTrafficPolicy`) had no `namespace` set. Its `targetRef` points at the `example-app-go` `HTTPRoute`, which lives in `envoy-gateway-system` — a policy without an explicit namespace resolves against whatever namespace you happen to `kubectl apply` it into, and a `targetRef` can't cross namespaces. Added `namespace: envoy-gateway-system` so it actually attaches.
- `gateway-yaml/02-gateway-config.yaml`'s `EnvoyProxy` had no tracing config — added `telemetry.tracing` (OpenTelemetry, pointed at the add-ons chart's collector) as part of step 4.

Flagged, not changed (need your call):

- **`envoy-specific/02-security/03-security.yaml` is empty.** No `SecurityPolicy` is actually defined yet — applying it is a no-op. Let me know what you want there (JWT, basic auth, IP allowlist, etc.) and I'll fill it in against a real example.
- **`envoy-specific/00-client-traffic/01-mtls.yaml` requires client certificates on the entire `https` listener** (`sectionName: https`, no path/route scoping). If this is applied, every plain `curl --cacert ...` request from steps 2–3 (which don't present a client cert) will fail the TLS handshake — it isn't compatible with the rest of the walkthrough being tested the way it's written unless you also start passing `--cert`/`--key`. Worth confirming whether this is meant to be active right now or is a "toggle later" experiment.
- That same file's `caCertificateRefs` points at `example-app-tls-secret` — the server's own leaf-cert Secret — to validate client certificates. This happens to work only because cert-manager populates a `ca.crt` field in Secrets issued by a CA-backed issuer (`gw-demo-ca-issuer`), so the bundle is technically present. It's clearer and more conventional to point this at a dedicated CA secret (e.g. `gw-demo-root-ca-secret`, which would need a `ReferenceGrant` since it's in the `cert-manager` namespace) rather than relying on that incidental behavior.
- `gateway-yaml/00-gatewayclass.yaml` and `02-gateway-config.yaml` both define the same `GatewayClass` object, with `02` adding a `parametersRef`. Apply order matters — if `00` gets re-applied after `02` (e.g. by a setup script run out of order), it silently strips the `parametersRef` back out. Consider deleting `00` once `02` is in place, or merging them into one file.
- `application/go-v2.yml` uses a `.yml` extension while `go-v1.yaml` uses `.yaml` — cosmetic only, rename whenever convenient.
- The Gateway's `http` listener (port 80) has no `hostname`, so it's a catch-all serving `example-app.com` in plaintext alongside the `https` listener, with no redirect. Fine for local learning; add an HTTP→HTTPS redirect rule before this is exposed anywhere real.
