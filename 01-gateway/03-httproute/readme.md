# Step 3 — HTTPRoutes for example-app.com

Prereq: step 2 done — `kubectl describe gateway/gateway-api -n envoy-gateway-system` shows the `https` listener `Programmed: True`, and a plain `curl` against it returns Envoy's fallback `404` (no route yet).

This step deploys a real backend and writes the `HTTPRoute`s that connect it to the Gateway. Unlike a single file with commented-out sections, this repo splits each routing feature into its **own** `HTTPRoute` object in `01-gateway/03-httproute/` — they all bind to the same Gateway/hostname, and Gateway API merges their rules by specificity (see 3.4's note on precedence), so applying more than one at a time is expected, not a conflict.

Files referenced below live in `00-application/` (backends) and `01-gateway/03-httproute/` (the routes) in this repo.

## 3.1 Deploy the backend (v1)

`00-application/go-v1.yaml` — using `hashicorp/http-echo`, a tiny Go binary that echoes back fixed text, good enough to prove routing without needing your actual app image yet. Note this repo's backend Deployment/Service live in `envoy-gateway-system`, matching the Gateway and HTTPRoute.

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
kubectl apply -f 00-application/go-v1.yaml
kubectl wait --timeout=2m -n envoy-gateway-system deployment/example-app-go-v1 --for=condition=Available
```

(Minor housekeeping, unresolved: `00-application/go-v2.yml` uses a `.yml` extension while `go-v1.yaml` uses `.yaml` — harmless, rename to `.yaml` whenever convenient.)

## 3.2 Basic routing + weighted canary split

`01-gateway/03-httproute/00-canary-split.yaml` — `parentRefs` is what attaches this route to the Gateway (`gateway-api`), and it lives in the same `envoy-gateway-system` namespace as that Gateway. This file is the active baseline route — it ships with the 90/10 weighted split already in place (relative weights, not literal percentages — `90`/`10` means roughly 90% of requests go to v1, 10% to v2), not as a separate later step:

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
          weight: 90
        - name: example-app-go-v2
          port: 8080
          weight: 10
```

Deploy v2 first (`00-application/go-v2.yml`, also in `envoy-gateway-system`) so the route has something to split traffic to:

```bash
kubectl apply -f 00-application/go-v2.yml
kubectl apply -f 01-gateway/03-httproute/00-canary-split.yaml
kubectl describe httproute example-app-go -n envoy-gateway-system
```

`Accepted: True` and `ResolvedRefs: True` mean the route is valid and both backend Services were found. Check the Gateway too — `Attached Routes` on the `https` listener should now read `1` (this file, even though it defines two weighted backends). Since the `http` listener has no `hostname` restriction (see the note in step 1), this route is reachable over both HTTP and HTTPS at this point.

```bash
kubectl describe gateway/gateway-api -n envoy-gateway-system
```

Test it (reusing the CA and `GATEWAY_HOST` from step 2):

```bash
for i in $(seq 1 50); do
  curl -s --resolve example-app.com:443:$GATEWAY_HOST --cacert gw-demo-ca.crt https://example-app.com/
  echo
done
```

Use a sample size of 40–50+ before concluding the split isn't working — with only 10 requests, the odds of seeing zero v2 responses by pure chance are around 35%. That 404 from step 2 is now gone — this is the actual request path from the diagram working end to end: client → Envoy (TLS) → HTTPRoute match → Service → Pod.

## 3.3 Header-based routing (force canary for specific clients)

`01-gateway/03-httproute/01-header-canary.yaml` — a separate `HTTPRoute` object, `example-app-go-header-canary`, that force-routes requests carrying `x-canary: true` to v2, regardless of the 90/10 weighted split in `00-canary-split.yaml`. This is how you'd let internal testers always hit v2 regardless of the canary weight:

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: example-app-go-header-canary
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
          headers:
            - name: x-canary
              value: "true"
      backendRefs:
        - name: example-app-go-v2
          port: 8080
```

```bash
kubectl apply -f 01-gateway/03-httproute/01-header-canary.yaml

curl --resolve example-app.com:443:$GATEWAY_HOST --cacert gw-demo-ca.crt \
  -H "x-canary: true" https://example-app.com/
# → always v2, regardless of the 90/10 weights
```

Important nuance: Gateway API does **not** guarantee "file applied first wins," and it doesn't matter that this is a separate `HTTPRoute` object from `00-canary-split.yaml` either — matching precedence across *all* HTTPRoutes bound to the same Gateway/hostname is defined by specificity. A rule with a header match beats one without, exact path beats prefix, longer prefix beats shorter, independent of which file or apply order it came from.

## 3.4 Redirect and path rewrite

Two more separate `HTTPRoute` objects, two different filter types often confused:

- **RequestRedirect** (`01-gateway/03-httproute/02-redirect.yaml`, `example-app-go-redirect`) — tells the *client* to re-request a different URL (a real HTTP redirect, browser-visible). This repo's version redirects `/old` to an **external** domain (`www.google.com`) rather than just another path on the same host, to make the client-visible, cross-domain nature of a redirect obvious:

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: example-app-go-redirect
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
            value: /old
      filters:
        - type: RequestRedirect
          requestRedirect:
            scheme: https
            hostname: www.google.com
            path:
              type: ReplaceFullPath
              replaceFullPath: /
            statusCode: 302
```

- **URLRewrite** (`01-gateway/03-httproute/03-rewrite.yaml`, `example-app-go-rewrite`) — rewrites the path *internally* before forwarding to the backend; the client never sees it. Requests to `/v4/*` are forwarded to `example-app-go-v2` with the `/v4` prefix stripped, so the backend only ever sees `/`:

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: example-app-go-rewrite
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
            value: /v4
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
kubectl apply -f 01-gateway/03-httproute/02-redirect.yaml
kubectl apply -f 01-gateway/03-httproute/03-rewrite.yaml

curl -i --resolve example-app.com:443:$GATEWAY_HOST --cacert gw-demo-ca.crt \
  https://example-app.com/old
# → HTTP/2 302, Location: https://www.google.com/

curl --resolve example-app.com:443:$GATEWAY_HOST --cacert gw-demo-ca.crt \
  https://example-app.com/v4/anything
# → Hello from example-app-go v2 (canary)  (backend sees "/", not "/v4/anything")
```

## What's next

Step 4 covers the Envoy Gateway-specific policy CRDs in `02-envoy-specific/` (`ClientTrafficPolicy` connection limits/mTLS, `BackendTrafficPolicy` rate limiting/retries/circuit breaking, `SecurityPolicy` basic auth/JWT) — a couple of them interact with the plain-curl testing you've been doing above (mTLS especially breaks it), so read that step before applying them. Step 5 (`03-observability-stack/`) adds metrics, traces, and eventually logs so you can actually see this traffic split happening, rather than eyeballing curl output; it builds on the `EnvoyProxy` resource in `01-gateway/02-gateway-config.yaml`.

## Sources
- [HTTPRoute API reference | Gateway API](https://gateway-api.sigs.k8s.io/reference/spec/#gateway.networking.k8s.io/v1.httproute)
- [HTTP Routing task | Envoy Gateway](https://gateway.envoyproxy.io/latest/tasks/traffic/http-routing/)
- [HTTP Traffic Splitting | Envoy Gateway](https://gateway.envoyproxy.io/latest/tasks/traffic/traffic-splitting/)
