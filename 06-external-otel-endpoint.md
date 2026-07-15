# Step 6 (optional) — Point tracing/metrics at your own existing OTel collector

For when you already run an OpenTelemetry collector somewhere (in-cluster or external), with its own TLS cert and an auth token/API key it expects on incoming OTLP data, and you don't want the add-ons chart's bundled Prometheus/Grafana/Tempo/OTel Collector from step 5 at all.

Skip `05-observability.md`'s `helm install eg-addons ...` entirely — that chart is only there to give you *something* to point at for learning. If you already have a real collector, none of that needs to exist in this cluster.

## The key thing to understand first

Envoy Gateway's tracing config (`EnvoyProxy.spec.telemetry.tracing.provider`) does **not** take a raw URL string. It takes a `backendRefs` field, which only accepts references to Kubernetes objects — a `Service`, a `ServiceImport`, or Envoy Gateway's own `Backend` CRD. So "passing your OTel URL" means creating one small `Backend` object that wraps your collector's hostname/port, then pointing `backendRefs` at that object instead of at an in-cluster Service. This is the same mechanism used to route any Gateway API traffic to something outside the cluster.

## 6.1 Define your collector as a `Backend`

```yaml
# otel-backend.yaml
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: Backend
metadata:
  name: external-otel-collector
  namespace: envoy-gateway-system
spec:
  endpoints:
    - fqdn:
        hostname: otel.yourcompany.com   # your actual collector hostname
        port: 4317                       # 4317 = OTLP/gRPC, 4318 = OTLP/HTTP
```

If your collector is reachable by IP instead of a hostname, use `ip:` instead of `fqdn:` under `endpoints` (same shape, `address`/`port` fields).

```bash
kubectl apply -f otel-backend.yaml
kubectl get backend external-otel-collector -n envoy-gateway-system
```

## 6.2 TLS to your collector (you said you already have certs)

Two ways to attach TLS, and which one you need depends on what "certs" means for your setup:

**If you just need to trust your collector's server certificate** (standard TLS, no client cert), attach a `BackendTLSPolicy` — same pattern as `certs/00-ca-bootstrap.yaml`/`01-app-cert.yaml` from step 2, just targeting this `Backend` instead of your app:

```yaml
# otel-backend-tls.yaml
apiVersion: gateway.networking.k8s.io/v1alpha3
kind: BackendTLSPolicy
metadata:
  name: otel-collector-tls
  namespace: envoy-gateway-system
spec:
  targetRefs:
    - group: gateway.envoyproxy.io
      kind: Backend
      name: external-otel-collector
  validation:
    caCertificateRefs:
      - name: otel-collector-ca-cert   # ConfigMap holding your collector's CA bundle
        kind: ConfigMap
    hostname: otel.yourcompany.com
```

You'll need your collector's CA certificate loaded into a `ConfigMap` first:

```bash
kubectl create configmap otel-collector-ca-cert \
  --from-file=ca.crt=/path/to/your/otel-ca.crt \
  -n envoy-gateway-system
```

**If your collector also requires a client certificate from Envoy** (mutual TLS, not just server-side TLS), set it directly on the `Backend` instead — this is a shortcut Envoy Gateway supports specifically on `Backend.spec.tls`:

```yaml
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: Backend
metadata:
  name: external-otel-collector
  namespace: envoy-gateway-system
spec:
  endpoints:
    - fqdn:
        hostname: otel.yourcompany.com
        port: 4317
  tls:
    caCertificateRefs:
      - name: otel-collector-ca-cert
        kind: ConfigMap
    # if mTLS is required, also reference your client cert/key Secret here
    # (check `kubectl explain backend.spec.tls --recursive` on your installed
    # version for the exact client-cert field name, since this shortcut has
    # evolved across Envoy Gateway releases)
```

## 6.3 Auth token on the OTLP export

If your collector needs a bearer token or API key on every OTLP request (common for hosted/SaaS OTel backends), this is configured as a custom header on the tracing provider. Recent Envoy Gateway releases added support for exactly this — custom headers on OTLP exports, for passing auth tokens — but the precise field name has shifted between releases, so **verify against your installed CRD version** before trusting the shape below:

```bash
kubectl explain envoyproxy.spec.telemetry.tracing.provider.openTelemetry --recursive
```

Look for a `headers` (or similarly named) field under that path. It typically takes a list of `{name, value}` pairs, e.g.:

```yaml
      provider:
        type: OpenTelemetry
        backendRefs:
          - group: gateway.envoyproxy.io
            kind: Backend
            name: external-otel-collector
        # exact field name/shape: confirm with `kubectl explain` above
        # headers:
        #   - name: Authorization
        #     value: "Bearer <your-token>"
```

If a plaintext token in the CRD isn't acceptable for your setup, the safer pattern is a Kubernetes `Secret` referenced by name rather than a literal value — check the same `kubectl explain` output for whether a `SecretRef`-style field exists alongside the literal one.

## 6.4 Wire it into the EnvoyProxy tracing config

Replace the `backendRefs` entry in `gateway-yaml/02-gateway-config.yaml` (the same `EnvoyProxy` resource from step 5) to point at your new `Backend` instead of the add-ons chart's collector:

```yaml
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: EnvoyProxy
metadata:
  name: gateway-configuration
  namespace: envoy-gateway-system
spec:
  provider:
    type: Kubernetes
    kubernetes:
      envoyDeployment:
        replicas: 1
      envoyService:
        name: envoy-gateway-default
  telemetry:
    tracing:
      samplingRate: 100   # drop this in production, see step 5's notes
      provider:
        type: OpenTelemetry
        backendRefs:
          - group: gateway.envoyproxy.io
            kind: Backend
            name: external-otel-collector
      customTags:
        "k8s.pod.name":
          type: Environment
          environment:
            name: ENVOY_POD_NAME
            defaultValue: "-"
```

```bash
kubectl apply -f otel-backend.yaml
kubectl apply -f otel-backend-tls.yaml   # if using the separate BackendTLSPolicy option
kubectl apply -f gateway-yaml/02-gateway-config.yaml
```

Same restart-and-wait as step 5 — the Envoy proxy Pod picks up the new bootstrap config:

```bash
kubectl get pods -n envoy-gateway-system -w
```

## 6.5 Metrics, the same way — but simpler

Unlike tracing, Envoy Gateway's metrics OpenTelemetry sink takes a plain `host`/`port` directly, no `Backend` object needed — this is configured on the cluster-wide `EnvoyGateway` config (a `ConfigMap` in `envoy-gateway-system`, not a CRD), not per-Gateway:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: envoy-gateway-config
  namespace: envoy-gateway-system
data:
  envoy-gateway.yaml: |
    apiVersion: gateway.envoyproxy.io/v1alpha1
    kind: EnvoyGateway
    provider:
      type: Kubernetes
    gateway:
      controllerName: gateway.envoyproxy.io/gatewayclass-controller
    telemetry:
      metrics:
        sinks:
          - type: OpenTelemetry
            openTelemetry:
              host: otel.yourcompany.com
              port: 4317
              protocol: grpc
```

```bash
kubectl apply -f envoy-gateway-config.yaml
kubectl rollout restart deployment envoy-gateway -n envoy-gateway-system
```

If you also want to stop exposing the local Prometheus scrape endpoint (since you're pushing to your own collector instead), add `prometheus: {disable: true}` under the same `telemetry.metrics` block — see the note in `05-observability.md`'s production section.

## Recap

No Prometheus, Grafana, or Tempo need to exist in this cluster for any of this. The moving pieces are: one `Backend` object wrapping your collector's hostname/port, TLS trust for it (`BackendTLSPolicy` or `Backend.spec.tls`), an auth header if your collector requires one, and pointing the existing `EnvoyProxy`'s `telemetry.tracing.provider.backendRefs` (and, for metrics, the cluster-wide `EnvoyGateway` ConfigMap) at that `Backend` instead of anything installed by the add-ons chart.

## Sources
- [Gateway API Extensions | Envoy Gateway](https://gateway.envoyproxy.io/docs/api/extension_types/)
- [Backend TLS: Gateway to Backend | Envoy Gateway](https://gateway.envoyproxy.io/docs/tasks/security/backend-tls/)
- [Gateway Observability | Envoy Gateway](https://gateway.envoyproxy.io/docs/tasks/observability/gateway-observability/)
- [Announcing Envoy Gateway v1.8 | Envoy Gateway](https://gateway.envoyproxy.io/news/releases/v1.8/)
