# Step 4 — Observability: Prometheus, Grafana, OpenTelemetry tracing

Prereq: step 3 done — `example-app.com` returns real responses (v1/v2 canary split working).

Envoy Gateway ships a companion "add-ons" Helm chart that bundles Prometheus, Grafana, and Tempo (a tracing backend), plus an optional OpenTelemetry Collector — this is the fastest way to get a working observability stack for learning, without hand-wiring `kube-prometheus-stack` yourself. You'd likely swap Tempo/self-hosted Prometheus for a managed backend in real production, but the wiring on the Envoy Gateway side (metrics sinks, tracing provider) stays identical.

## 4.1 Install the add-ons chart

```bash
helm install eg-addons oci://docker.io/envoyproxy/gateway-addons-helm \
  --version v1.8.2 \
  --set opentelemetry-collector.enabled=true \
  -n monitoring --create-namespace
```

This installs Prometheus, Grafana, Tempo, and the OpenTelemetry Collector into the `monitoring` namespace.

```bash
kubectl get pods -n monitoring
kubectl get svc -n monitoring
```

Note the exact Service names printed (they're prefixed with the release name, e.g. `eg-addons-grafana`, `eg-addons-opentelemetry-collector`, `eg-addons-tempo`) — the tracing config below and the commands after it assume these exact names. If your install used a different Helm release name, adjust accordingly.

## 4.2 Metrics are already flowing — no extra config needed

By default, Envoy Gateway exposes Prometheus-format metrics from both the control plane and every Envoy proxy instance, and the add-ons chart's Prometheus is pre-configured to auto-discover them via pod annotations Envoy Gateway sets automatically. You don't need to write a `ServiceMonitor` by hand for this to work.

Confirm the control plane's raw metrics directly, as a sanity check:

```bash
export ENVOY_GW_POD=$(kubectl get pod -n envoy-gateway-system --selector=control-plane=envoy-gateway -o jsonpath='{.items[0].metadata.name}')
kubectl port-forward pod/$ENVOY_GW_POD -n envoy-gateway-system 19001:19001
curl localhost:19001/metrics | head -20
```

## 4.3 View metrics in Grafana

```bash
kubectl port-forward -n monitoring svc/eg-addons-grafana 3000:80
```

Open `http://localhost:3000`. Default credentials for this chart are `admin` / `admin`; if that doesn't work, fetch the generated password:

```bash
kubectl get secret -n monitoring -l app.kubernetes.io/name=grafana -o jsonpath='{.items[0].data.admin-password}' | base64 -d
```

The chart ships pre-built dashboards for both the Envoy Gateway control plane and Envoy proxy data plane (request rate, error rate, latency percentiles, connection counts) — look under Dashboards rather than building one from scratch. Generate some traffic first so the graphs aren't empty:

```bash
for i in $(seq 1 100); do
  curl -s --resolve example-app.com:443:$GATEWAY_HOST --cacert gw-demo-ca.crt https://example-app.com/ > /dev/null
done
```

## 4.4 Turn on distributed tracing

Tracing is off by default (unlike metrics). In this repo it's configured directly in `gateway-yaml/02-gateway-config.yaml`, extending the `EnvoyProxy` resource (`gateway-configuration`) that's already wired up via the `GatewayClass`'s `parametersRef` — no separate `EnvoyProxy` object needed:

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: GatewayClass
metadata:
  name: envoy-gateway
spec:
  controllerName: gateway.envoyproxy.io/gatewayclass-controller
  parametersRef:
    group: gateway.envoyproxy.io
    kind: EnvoyProxy
    name: gateway-configuration
    namespace: envoy-gateway-system
---
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
      # 100% for learning; drop to 1 (or use samplingFraction for <1%) in production
      samplingRate: 100
      provider:
        type: OpenTelemetry
        backendRefs:
          - name: eg-addons-opentelemetry-collector
            namespace: monitoring
            port: 4317
      customTags:
        "k8s.pod.name":
          type: Environment
          environment:
            name: ENVOY_POD_NAME
            defaultValue: "-"
```

Because this attaches at the `GatewayClass` level (via `parametersRef`), it applies to every Gateway using the `envoy-gateway` class — not just `gateway-api`. That's a slightly different scoping choice than attaching a per-Gateway `EnvoyProxy` via `Gateway.spec.infrastructure.parametersRef`; fine with a single Gateway like this setup, but worth knowing if you add more Gateways later using the same class.

```bash
kubectl apply -f gateway-yaml/02-gateway-config.yaml
```

Applying this restarts the Envoy proxy Pod (it's picking up new bootstrap config), so give it a minute:

```bash
kubectl get pods -n envoy-gateway-system -w
```

## 4.5 See a trace

Generate a request, then query Tempo (bundled by the add-ons chart) for it:

```bash
curl --resolve example-app.com:443:$GATEWAY_HOST --cacert gw-demo-ca.crt https://example-app.com/

kubectl port-forward -n monitoring svc/eg-addons-tempo 3100:3100
curl -s "http://localhost:3100/api/search?tags=component%3Dproxy" | jq .traces
```

Or, easier: open Grafana (already port-forwarded above), go to Explore, pick the Tempo data source (pre-wired by the add-ons chart), and search — you'll see the request's span through Envoy, including timing.

## Production notes

`samplingRate: 100` traces every request — fine for a learning cluster, expensive and noisy in production. For real traffic, drop this to something like `samplingRate: 1` (1%), or use `samplingFraction` for anything below 1% (e.g. `numerator: 1, denominator: 1000` for 0.1%). You can also use `openTelemetry.sampler` with `ParentBasedTraceIdRatio` so downstream services' sampling decisions are respected instead of Envoy always deciding independently.

Swap the bundled Tempo/Prometheus for whatever your organization already runs — Datadog and Zipkin are supported as alternate tracing `provider.type` values, and the metrics `sinks` field on the cluster-wide `EnvoyGateway` config (a `ConfigMap` in `envoy-gateway-system`, not a CRD) supports pushing to an OpenTelemetry metrics sink instead of only exposing a Prometheus scrape endpoint — useful if your metrics backend doesn't scrape and prefers push instead.

## Where this leaves you

You now have the full stack from the very first diagram, actually running: Gateway API resources (`GatewayClass`, `Gateway`, `HTTPRoute`) driving a real Envoy Gateway control plane, terminating client TLS from cert-manager, routing and splitting traffic across two backend versions, and both metrics and traces flowing into Grafana. From here, `envoy-specific/` in this repo already has a start on `SecurityPolicy`, `BackendTrafficPolicy` (rate limiting), and `ClientTrafficPolicy` (connection limits, mTLS) — see the README for the current state of those and a few things worth double-checking before relying on them.

## Sources
- [Gateway Observability | Envoy Gateway](https://gateway.envoyproxy.io/docs/tasks/observability/gateway-observability/)
- [Proxy Tracing | Envoy Gateway](https://gateway.envoyproxy.io/docs/tasks/observability/proxy-trace/)
- [Gateway Addons Helm Chart | Envoy Gateway](https://gateway.envoyproxy.io/docs/install/gateway-addons-helm-api/)
