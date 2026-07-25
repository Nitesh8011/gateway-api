# Step 5 — Standalone observability stack: Loki, Tempo, Prometheus, Grafana (monolithic)

Prereq: step 3 done — `example-app.com` returns real responses (v1/v2 canary split working). Step 4's policies aren't required for this step, but if `mtls-policy` is applied, plain `curl --cacert` tests below will fail unless you also pass a client cert; and `connection-limit-policy` will interfere with any load-testing commands below (`hey -c <N>` with `N > 5`) the same way it does in step 4 — delete it first if it's still applied.

This repo builds the observability stack from **individual Helm charts** (Loki, Tempo, Prometheus, Grafana, each installed separately) rather than Envoy Gateway's bundled `eg-addons` chart — gives direct control over each component instead of the add-ons chart's opinionated bundle. Scope covered here: **metrics + traces**. Log shipping (Envoy access logs → Loki) is a deliberate follow-up, not covered yet — Loki is installed below so the piece exists, but nothing ships logs to it in this step.

Values files live in `03-observability-stack/` in this repo: `loki-values.yaml`, `tempo-values.yaml`, `prometheus-values.yaml`, `grafana-values.yaml`.

## 5.1 Add the chart repos

Both Loki and Tempo's charts moved to the Grafana Community org in early 2026 — use `grafana-community`, not the older `grafana` repo, or you'll get stale chart versions.

```bash
helm repo add grafana-community https://grafana-community.github.io/helm-charts
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
kubectl create namespace observability
```

## 5.2 Install Loki (monolithic, single replica)

```bash
helm install loki grafana-community/loki -f 03-observability-stack/loki-values.yaml -n observability
kubectl get pods -n observability -l app.kubernetes.io/name=loki
```

Two real installation problems hit while setting this up, both already fixed in `loki-values.yaml`:

1. **The chart's bundled MinIO subchart is deprecated** (removal date 2026-10-31 per the chart's own validation error) and now hard-fails `helm install` unless explicitly acknowledged: `ignoreMinioDeprecation: true` is that acknowledgment, not a real config knob. Before that removal date, this whole file needs to move to an external object store (S3/GCS/Azure) regardless.
2. **Default resource requests are too high for a small demo/local cluster** and leave `loki-chunks-cache-0` (and `loki-results-cache-0`) stuck `Pending`. The fix is *not* a plain `resources.requests.memory` override — both memcached-based caches derive their actual pod resources from `allocatedMemory` (the memcached `-m` flag; default `8192` MB → ~9830Mi request/limit after overhead), and a plain `resources:` override gets silently recomputed away. Set `chunksCache.allocatedMemory` / `resultsCache.allocatedMemory` directly instead — `loki-values.yaml` sets both to `64`. Everything else in the chart (`singleBinary`, `gateway`, `minio`, the canary) does use a plain `resources` field, so `resources: {}` works normally there.

## 5.3 Install Tempo (monolithic)

```bash
helm install tempo grafana-community/tempo -f 03-observability-stack/tempo-values.yaml -n observability
kubectl get pods -n observability -l app.kubernetes.io/name=tempo
```

Same demo-cluster resourcing issue as Loki, but Tempo's chart uses a plain `resources` field (no `allocatedMemory`-style indirection) — `resources: {}` in `tempo-values.yaml` is sufficient here.

## 5.4 Install Prometheus (plain chart, not kube-prometheus-stack)

```bash
helm install prometheus prometheus-community/prometheus -f 03-observability-stack/prometheus-values.yaml -n observability
kubectl get pods -n observability -l app.kubernetes.io/instance=prometheus
```

This is deliberately the plain `prometheus-community/prometheus` chart, not `kube-prometheus-stack` — the heavier bundle adds Alertmanager, CRD-based alerting rules, and more `ServiceMonitor` machinery than this setup needs. It still pulls in `kube-state-metrics` and `prometheus-node-exporter` as subchart dependencies by default though (not something explicitly asked for, but shipped enabled) — both get the same `resources: {}` treatment in `prometheus-values.yaml`.

The chart's default scrape config already auto-discovers pods carrying `prometheus.io/scrape: "true"` annotations, which is exactly how Envoy Gateway exposes its own control-plane and data-plane metrics — no custom `extraScrapeConfigs` needed.

## 5.5 Install Grafana, wired to all three

```bash
helm install grafana grafana-community/grafana -f 03-observability-stack/grafana-values.yaml -n observability
kubectl get svc -n observability
```

**Before applying:** `grafana-values.yaml`'s datasources assume Service names `prometheus-server`, `loki-gateway`, and `tempo` (from the release names used above). Confirm those against the actual `kubectl get svc -n observability` output — chart defaults have been wrong more than once already in this stack (see 5.2), so don't assume without checking.

```bash
kubectl port-forward -n observability svc/grafana 3000:80
```

Open `http://localhost:3000`, log in `admin`/`admin` (change this before the cluster is anything but a learning environment — it's a plaintext default in the values file). Check Connections → Data sources for all three showing connected.

## 5.6 Point Envoy Gateway's tracing at Tempo

Edit `01-gateway/02-gateway-config.yaml`'s `EnvoyProxy.spec.telemetry.tracing.provider.backendRefs` to point at the new Tempo Service:

```yaml
  telemetry:
    tracing:
      samplingRate: 100   # fine for learning, drop for production — see below
      provider:
        type: OpenTelemetry
        backendRefs:
          - name: tempo
            namespace: observability
            port: 4317
```

`type: OpenTelemetry` here selects the OTLP wire *protocol*, not a separate OpenTelemetry Collector component — Tempo has a native OTLP receiver built in (`traces.otlp.grpc.enabled: true` in `tempo-values.yaml`), so Envoy talks OTLP directly to Tempo, no collector in between needed.

```bash
kubectl apply -f 01-gateway/02-gateway-config.yaml
kubectl get pods -n envoy-gateway-system -w
```

Applying this restarts the Envoy proxy Pod (new bootstrap config) — give it a minute.

## 5.7 Verify

Metrics and traces are cumulative-counter-based and event-based respectively — a single request won't visibly "prove" anything in a graph, and a `curl` that fails auth/rate-limit checks still moves *some* counters (Envoy's `downstream_rq_total` counts every request regardless of what happened after), so check the right layer:

```bash
curl -v --resolve example-app.com:443:$GATEWAY_HOST --cacert gw-demo-ca.crt https://example-app.com/
```

**Metrics** — Prometheus, comparing before/after or using `increase()`:

```bash
kubectl port-forward -n observability svc/prometheus-server 9090:80
# http://localhost:9090/targets — confirm the kubernetes-pods job found Envoy Gateway pods
# query: increase(envoy_http_downstream_rq_total[5m])
```

**Traces** — Grafana Explore → Tempo datasource → search recent traces, or:

```bash
kubectl port-forward -n observability svc/tempo 3100:3100
curl -s "http://localhost:3100/api/search?tags=component%3Dproxy" | jq .traces
```

## Where this leaves you

Metrics and traces flowing through a stack you fully control instead of the bundled add-ons chart. Loki is installed and idle — see `external-observability.md` in this folder for the alternate scenario (pointing tracing/metrics at an *already-existing* external OTel collector instead of anything installed here), and pick up log shipping to Loki as a separate follow-up whenever ready.

## Sources
- [Install the monolithic Helm chart | Grafana Loki documentation](https://grafana.com/docs/loki/latest/setup/install/helm/install-monolithic/)
- [Gateway Observability | Envoy Gateway](https://gateway.envoyproxy.io/docs/tasks/observability/gateway-observability/)
- [Proxy Tracing | Envoy Gateway](https://gateway.envoyproxy.io/docs/tasks/observability/proxy-trace/)
