# Step 3 — HTTPRoutes for example-app.com

Prereq: step 2 done — `kubectl describe gateway/gateway-api -n envoy-gateway-system` shows the `https` listener `Programmed: True`, and a plain `curl` against it returns Envoy's fallback `404` (no route yet).

This step deploys a real backend and writes the `HTTPRoute` that connects it to the Gateway. We'll build it up in layers: basic routing → weighted canary split → header-based routing → redirect/rewrite. In this repo, `gateway-yaml/03-httproute.yaml` currently has the weighted canary split rule active, with the header-based and redirect/rewrite rules present but commented out — uncomment them as you work through 3.4 and 3.5 below.

Files referenced below live in `application/` (backends) and `gateway-yaml/` (the route) in this repo.

## 3.1 Deploy the backend (v1)

`application/go-v1.yaml` — using `hashicorp/http-echo`, a tiny Go binary that echoes back fixed text, good enough to prove routing without needing your actual app image yet. Note this repo's backend Deployment/Service live in `envoy-gateway-system`, matching the Gateway and HTTPRoute.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: example-app-go-v1
  namespace: envoy-gateway-system
  labels:
    app: example-app-go
    version: v1
spec:
  replicas: 2
  selector:
    matchLabels:
      app: example-app-go
      version: v1
  template:
    metadata:
      labels:
        app: example-app-go
        version: v1
    spec:
      containers:
        - name: http-echo
          image: hashicorp/http-echo:1.0
          args:
            - "-text=Hello from example-app-go v1"
            - "-listen=:8080"
          ports:
            - containerPort: 8080
---
apiVersion: v1
kind: Service
metadata:
  name: example-app-go-v1
  namespace: envoy-gateway-system
spec:
  selector:
    app: example-app-go
    version: v1
  ports:
    - port: 8080
      targetPort: 8080
```

```bash
kubectl apply -f application/go-v1.yaml
kubectl wait --timeout=2m -n envoy-gateway-system deployment/example-app-go-v1 --for=condition=Available
```

(Minor housekeeping: `application/go-v2.yml` uses a `.yml` extension while `go-v1.yaml` uses `.yaml` — harmless, but worth renaming to `.yml`→`.yaml` for consistency whenever convenient.)

## 3.2 Basic HTTPRoute

`gateway-yaml/03-httproute.yaml` — `parentRefs` is what attaches this route to the Gateway (`gateway-api`), and it lives in the same `envoy-gateway-system` namespace as that Gateway.

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: example-app-go
  namespace: envoy-gateway-system
spec:
  parentRefs:
    - name: gateway-api
  hostnames:
    - example-app.com
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /
      backendRefs:
        - name: example-app-go-v1
          port: 8080
```

```bash
kubectl apply -f gateway-yaml/03-httproute.yaml
kubectl describe httproute example-app-go -n envoy-gateway-system
```

`Accepted: True` and `ResolvedRefs: True` mean the route is valid and its backend Service was found. Check the Gateway too — `Attached Routes` on the `https` listener should now read `1`. Since the `http` listener has no `hostname` restriction (see the note in step 1), this route is reachable over both HTTP and HTTPS at this point.

```bash
kubectl describe gateway/gateway-api -n envoy-gateway-system
```

Test it (reusing the CA and `GATEWAY_HOST` from step 2):

```bash
curl --resolve example-app.com:443:$GATEWAY_HOST \
  --cacert gw-demo-ca.crt \
  https://example-app.com/
# → Hello from example-app-go v1
```

That 404 from step 2 is now gone — this is the actual request path from the diagram working end to end: client → Envoy (TLS) → HTTPRoute match → Service → Pod.

## 3.3 Add a v2 and split traffic (canary)

`application/go-v2.yml`, also in `envoy-gateway-system`:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: example-app-go-v2
  namespace: envoy-gateway-system
  labels:
    app: example-app-go
    version: v2
spec:
  replicas: 1
  selector:
    matchLabels:
      app: example-app-go
      version: v2
  template:
    metadata:
      labels:
        app: example-app-go
        version: v2
    spec:
      containers:
        - name: http-echo
          image: hashicorp/http-echo:1.0
          args:
            - "-text=Hello from example-app-go v2 (canary)"
            - "-listen=:8080"
          ports:
            - containerPort: 8080
---
apiVersion: v1
kind: Service
metadata:
  name: example-app-go-v2
  namespace: envoy-gateway-system
spec:
  selector:
    app: example-app-go
    version: v2
  ports:
    - port: 8080
      targetPort: 8080
```

```bash
kubectl apply -f application/go-v2.yml
```

The route's default rule, already active in `gateway-yaml/03-httproute.yaml`, splits by weight (relative, not literal percentages — `90`/`10` means roughly 90% of requests go to v1, 10% to v2):

```yaml
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /
      backendRefs:
        - name: example-app-go-v1
          port: 8080
          weight: 90
        - name: example-app-go-v2
          port: 8080
          weight: 10
```

```bash
kubectl apply -f gateway-yaml/03-httproute.yaml
for i in $(seq 1 50); do
  curl -s --resolve example-app.com:443:$GATEWAY_HOST --cacert gw-demo-ca.crt https://example-app.com/
  echo
done
```

Use a sample size of 40–50+ before concluding the split isn't working — with only 10 requests, the odds of seeing zero v2 responses by pure chance are around 35%.

## 3.4 Header-based routing (force canary for specific clients)

Uncomment the header-matched rule in `gateway-yaml/03-httproute.yaml` (it's already there, commented out) and place it alongside the weighted-split rule. This is how you'd let internal testers always hit v2 regardless of the canary weight.

```yaml
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /
          headers:
            - name: x-canary
              value: "true"
      backendRefs:
        - name: example-app-go-v2
          port: 8080
    - matches:
        - path:
            type: PathPrefix
            value: /
      backendRefs:
        - name: example-app-go-v1
          port: 8080
          weight: 90
        - name: example-app-go-v2
          port: 8080
          weight: 10
```

```bash
kubectl apply -f gateway-yaml/03-httproute.yaml

curl --resolve example-app.com:443:$GATEWAY_HOST --cacert gw-demo-ca.crt \
  -H "x-canary: true" https://example-app.com/
# → always v2, regardless of the 90/10 weights
```

Important nuance: Gateway API does **not** guarantee "first rule in the file wins." Matching precedence is defined by specificity — exact path match beats prefix match, longer prefixes beat shorter ones, and a rule with a header match beats one without, independent of the order you wrote them in. Writing the more specific rule first (as above) is good practice for readability, but the actual behavior comes from specificity, not file order.

## 3.5 Redirect and path rewrite

Also already present, commented out, in `gateway-yaml/03-httproute.yaml`. Two different `HTTPRoute` filters, often confused:

- **RequestRedirect** — tells the *client* to re-request a different URL (a real HTTP 301/302, browser-visible).
- **URLRewrite** — rewrites the path *internally* before forwarding to the backend; the client never sees it.

```yaml
    - matches:
        - path:
            type: PathPrefix
            value: /old
      filters:
        - type: RequestRedirect
          requestRedirect:
            path:
              type: ReplacePrefixMatch
              replacePrefixMatch: /
            statusCode: 301
    - matches:
        - path:
            type: PathPrefix
            value: /v2
      filters:
        - type: URLRewrite
          urlRewrite:
            path:
              type: ReplacePrefixMatch
              replacePrefixMatch: /
      backendRefs:
        - name: example-app-go-v2
          port: 8080
```

```bash
curl -i --resolve example-app.com:443:$GATEWAY_HOST --cacert gw-demo-ca.crt \
  https://example-app.com/old
# → HTTP/2 301, Location: /

curl --resolve example-app.com:443:$GATEWAY_HOST --cacert gw-demo-ca.crt \
  https://example-app.com/v2/anything
# → Hello from example-app-go v2 (canary)  (backend sees "/", not "/v2/anything")
```

## What's next

Step 4 covers the Envoy Gateway-specific policy CRDs in `envoy-specific/` (`ClientTrafficPolicy` connection limits/mTLS, `BackendTrafficPolicy` rate limiting, `SecurityPolicy`) — a couple of them interact with the plain-curl testing you've been doing above, so read that step before applying them. Step 5 adds Prometheus/Grafana metrics and OpenTelemetry tracing so you can actually see this traffic split happening, rather than eyeballing curl output; it builds on the `EnvoyProxy` resource in `gateway-yaml/02-gateway-config.yaml`. Step 6 (optional) covers pointing tracing/metrics at your own external OTel collector instead.

## Sources
- [HTTPRoute API reference | Gateway API](https://gateway-api.sigs.k8s.io/reference/spec/#gateway.networking.k8s.io/v1.httproute)
- [HTTP Routing task | Envoy Gateway](https://gateway.envoyproxy.io/latest/tasks/traffic/http-routing/)
- [HTTP Traffic Splitting | Envoy Gateway](https://gateway.envoyproxy.io/latest/tasks/traffic/traffic-splitting/)
