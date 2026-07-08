# Step 3 — HTTPRoutes for example-app-go.com

Prereq: step 2 done — `kubectl describe gateway/eg -n default` shows the `https` listener `Programmed: True`, and a plain `curl` against it returns Envoy's fallback `404` (no route yet).

This step deploys a real backend and writes the `HTTPRoute`s that connect it to the Gateway. We'll build it up in layers: basic routing → weighted canary split → header-based routing → redirect/rewrite — the same layering you'd use on a real service.

## 3.1 Deploy the backend (v1)

Using `hashicorp/http-echo` — a tiny Go binary that echoes back fixed text, good enough to prove routing without needing your actual app image yet. Swap the image/args for your real app whenever you're ready.

```yaml
# app-v1.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: example-app-go-v1
  namespace: default
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
  namespace: default
spec:
  selector:
    app: example-app-go
    version: v1
  ports:
    - port: 8080
      targetPort: 8080
```

```bash
kubectl apply -f app-v1.yaml
kubectl wait --timeout=2m -n default deployment/example-app-go-v1 --for=condition=Available
```

## 3.2 Basic HTTPRoute

`parentRefs` is what attaches this route to the Gateway — this is the field that turns a standalone Service into something reachable through Envoy.

```yaml
# httproute.yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: example-app-go
  namespace: default
spec:
  parentRefs:
    - name: eg
  hostnames:
    - example-app-go.com
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
kubectl apply -f httproute.yaml
kubectl describe httproute example-app-go -n default
```

`Accepted: True` and `ResolvedRefs: True` mean the route is valid and its backend Service was found. Check the Gateway too — `Attached Routes` on the `https` listener should now read `1`.

```bash
kubectl describe gateway/eg -n default
```

Test it (reusing the CA and `GATEWAY_HOST` from step 2):

```bash
curl --resolve example-app-go.com:443:$GATEWAY_HOST \
  --cacert gw-demo-ca.crt \
  https://example-app-go.com/
# → Hello from example-app-go v1
```

That 404 from step 2 is now gone — this is the actual request path from the diagram working end to end: client → Envoy (TLS) → HTTPRoute match → Service → Pod.

## 3.3 Add a v2 and split traffic (canary)

Deploy a second version, then split traffic between them by weight. Weights are relative, not percentages — `90`/`10` here means 90% of requests roughly go to v1, 10% to v2.

```yaml
# app-v2.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: example-app-go-v2
  namespace: default
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
  namespace: default
spec:
  selector:
    app: example-app-go
    version: v2
  ports:
    - port: 8080
      targetPort: 8080
```

```bash
kubectl apply -f app-v2.yaml
```

Update the route's default rule to split across both:

```yaml
# httproute.yaml (rules section updated)
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
kubectl apply -f httproute.yaml
for i in $(seq 1 10); do
  curl -s --resolve example-app-go.com:443:$GATEWAY_HOST --cacert gw-demo-ca.crt https://example-app-go.com/
  echo
done
```

You should see mostly v1 responses with the occasional v2 mixed in.

## 3.4 Header-based routing (force canary for specific clients)

A separate rule matched by a request header, placed alongside the weighted-split rule. This is how you'd let internal testers always hit v2 regardless of the canary weight.

```yaml
# httproute.yaml (add this rule before the default one)
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
kubectl apply -f httproute.yaml

curl --resolve example-app-go.com:443:$GATEWAY_HOST --cacert gw-demo-ca.crt \
  -H "x-canary: true" https://example-app-go.com/
# → always v2, regardless of the 90/10 weights
```

Important nuance: Gateway API does **not** guarantee "first rule in the file wins." Matching precedence is defined by specificity — exact path match beats prefix match, longer prefixes beat shorter ones, and a rule with a header match beats one without, independent of the order you wrote them in. Writing the more specific rule first (as above) is good practice for readability, but the actual behavior comes from specificity, not file order.

## 3.5 Redirect and path rewrite

Two different `HTTPRoute` filters, often confused:

- **RequestRedirect** — tells the *client* to re-request a different URL (a real HTTP 301/302, browser-visible).
- **URLRewrite** — rewrites the path *internally* before forwarding to the backend; the client never sees it.

```yaml
# httproute.yaml (two more rules)
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
curl -i --resolve example-app-go.com:443:$GATEWAY_HOST --cacert gw-demo-ca.crt \
  https://example-app-go.com/old
# → HTTP/2 301, Location: /

curl --resolve example-app-go.com:443:$GATEWAY_HOST --cacert gw-demo-ca.crt \
  https://example-app-go.com/v2/anything
# → Hello from example-app-go v2 (canary)  (backend sees "/", not "/v2/anything")
```

## Full HTTPRoute so far

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: example-app-go
  namespace: default
spec:
  parentRefs:
    - name: eg
  hostnames:
    - example-app-go.com
  rules:
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

## What's next

Step 4 adds Prometheus/Grafana metrics and OpenTelemetry tracing so you can actually see this traffic split happening, rather than eyeballing curl output.

## Sources
- [HTTPRoute API reference | Gateway API](https://gateway-api.sigs.k8s.io/reference/spec/#gateway.networking.k8s.io/v1.httproute)
- [HTTP Routing task | Envoy Gateway](https://gateway.envoyproxy.io/latest/tasks/traffic/http-routing/)
- [HTTP Traffic Splitting | Envoy Gateway](https://gateway.envoyproxy.io/latest/tasks/traffic/traffic-splitting/)
