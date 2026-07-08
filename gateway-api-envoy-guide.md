# Kubernetes Gateway API + Envoy Gateway — Complete Guide

Reference current as of 2026 (Gateway API v1.5/v1.6, Envoy Gateway v1.8).

## 1. Why Gateway API exists

Ingress (the old API) could only express basic host/path routing and pushed everything else into vendor-specific annotations, so every ingress controller had its own dialect. Gateway API replaces that with a portable, role-oriented, strongly-typed model split across three personas:

- **Infrastructure Provider** — runs the cluster, installs a controller (e.g., Envoy Gateway), creates `GatewayClass`.
- **Cluster Operator / Platform team** — creates `Gateway` resources (the actual listeners: ports, hostnames, TLS certs).
- **Application Developer** — creates `HTTPRoute`/`GRPCRoute`/etc. that attach to a Gateway and route to their own Services.

This split lets a platform team own the shared entrypoint while app teams self-serve routing rules in their own namespaces, safely.

## 2. Core Gateway API kinds (vendor-neutral, part of `gateway.networking.k8s.io`)

### GatewayClass (cluster-scoped)
Analogous to `StorageClass` — declares "this is a class of gateway, implemented by controller X." One GatewayClass per controller/implementation.

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: GatewayClass
metadata:
  name: envoy-gateway
spec:
  controllerName: gateway.envoyproxy.io/gatewayclass-controller
```

### Gateway (namespaced)
The actual traffic-handling entrypoint: listeners (port, protocol, hostname, TLS). Envoy Gateway provisions a dedicated Envoy proxy Deployment + Service for each Gateway.

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: eg
  namespace: default
spec:
  gatewayClassName: envoy-gateway
  listeners:
    - name: http
      protocol: HTTP
      port: 80
    - name: https
      protocol: HTTPS
      port: 443
      hostname: "*.example.com"
      tls:
        mode: Terminate
        certificateRefs:
          - name: example-cert
```

### HTTPRoute (namespaced) — the workhorse
Matches on path/header/method/query, and can do weighted splits, redirects, header/URL rewrites, mirroring, CORS, timeouts.

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: backend-route
  namespace: default
spec:
  parentRefs:
    - name: eg
  hostnames:
    - "www.example.com"
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /api
      backendRefs:
        - name: api-svc
          port: 8080
    - matches:
        - path:
            type: PathPrefix
            value: /v2
      backendRefs:
        - name: api-svc-v2
          port: 8080
          weight: 20
        - name: api-svc-v1
          port: 8080
          weight: 80
```

### GRPCRoute
Same idea as HTTPRoute but matches on gRPC service/method instead of HTTP path.

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: GRPCRoute
metadata:
  name: grpc-route
spec:
  parentRefs:
    - name: eg
  rules:
    - matches:
        - method:
            service: helloworld.Greeter
            method: SayHello
      backendRefs:
        - name: greeter-svc
          port: 50051
```

### TLSRoute
Layer-4 routing based on SNI only (no TLS termination at the gateway — passthrough). Useful when the backend must see the original TLS handshake.

```yaml
apiVersion: gateway.networking.k8s.io/v1alpha2
kind: TLSRoute
metadata:
  name: tls-passthrough
spec:
  parentRefs:
    - name: eg
      sectionName: tls-passthrough-listener
  hostnames:
    - "secure.example.com"
  rules:
    - backendRefs:
        - name: secure-backend
          port: 443
```

### TCPRoute / UDPRoute
Pure L4 routing for non-HTTP TCP or UDP workloads (databases, custom protocols, DNS, etc.). UDPRoute graduated to GA (v1) in Gateway API v1.6 (June 2026); TCPRoute is also GA.

```yaml
apiVersion: gateway.networking.k8s.io/v1alpha2
kind: TCPRoute
metadata:
  name: tcp-route
spec:
  parentRefs:
    - name: eg
      sectionName: tcp-listener
  rules:
    - backendRefs:
        - name: postgres
          port: 5432
```

### ReferenceGrant (namespaced, graduated to Standard in v1.5)
Explicit opt-in that allows a route in namespace A to reference a backend Service (or Secret) in namespace B. Without it, cross-namespace refs are rejected — this is the security boundary that keeps app teams from silently pulling traffic into someone else's namespace.

```yaml
apiVersion: gateway.networking.k8s.io/v1beta1
kind: ReferenceGrant
metadata:
  name: allow-route-to-backend-ns
  namespace: backend-ns
spec:
  from:
    - group: gateway.networking.k8s.io
      kind: HTTPRoute
      namespace: frontend-ns
  to:
    - group: ""
      kind: Service
```

### BackendTLSPolicy
Configures TLS (mTLS/CA validation) for the connection between the Gateway/Envoy and the backend Service — i.e., upstream TLS origination, not client-facing TLS.

```yaml
apiVersion: gateway.networking.k8s.io/v1alpha3
kind: BackendTLSPolicy
metadata:
  name: backend-tls
spec:
  targetRefs:
    - group: ""
      kind: Service
      name: api-svc
  validation:
    caCertificateRefs:
      - name: backend-ca-cert
        kind: ConfigMap
    hostname: api.internal.svc
```

### ListenerSet (newly promoted to Standard in v1.5, 2026)
Lets a Gateway "delegate" a subset of its listeners to a separate namespaced resource so different teams can own different listener sets on a shared Gateway without editing the Gateway object itself.

### BackendLBPolicy / other policy attachments
Gateway API defines a generic "Policy Attachment" pattern (`targetRefs` pointing at a Gateway, Route, or Service) — this is the extension mechanism every implementation (including Envoy Gateway) builds its custom CRDs on top of.

## 3. Envoy Gateway architecture

Envoy Gateway is a control plane, written in Go, that watches Gateway API resources and translates them into Envoy xDS configuration. It does not replace Envoy — it's an operator that runs and configures vanilla Envoy proxies.

```
        ┌────────────────────────────┐
        │   kube-apiserver            │
        │  (Gateway/HTTPRoute/...)    │
        └──────────────┬─────────────┘
                        │ watch
                        ▼
        ┌────────────────────────────┐
        │   Envoy Gateway             │
        │   control plane (Pod)       │
        │  - Provisioner (creates     │
        │    Envoy Deployment/Svc     │
        │    per Gateway)             │
        │  - Translator (Gateway API  │
        │    -> Envoy xDS/IR)         │
        │  - xDS Server (gRPC)        │
        └──────────────┬─────────────┘
                        │ xDS (gRPC, dynamic config)
                        ▼
        ┌────────────────────────────┐
        │  Envoy Proxy Deployment     │
        │  (one per Gateway object)   │
        │  - data plane, terminates   │
        │    client traffic, applies  │
        │    routing/policies         │
        └──────────────┬─────────────┘
                        │
                        ▼
                 Backend Services
```

Key points:

- **One Envoy Deployment per Gateway.** Creating a `Gateway` resource causes Envoy Gateway's provisioner to stand up a dedicated Deployment + Service (LoadBalancer/NodePort/ClusterIP) running Envoy. Multiple HTTPRoutes/GRPCRoutes attach to that one Gateway and share its Envoy fleet.
- **Translation, not proxying.** The control plane never sees data-plane traffic; it only pushes config to Envoy over xDS (Envoy's dynamic configuration protocol — LDS/RDS/CDS/EDS).
- **Merged config per Gateway.** All routes bound to a Gateway are merged into that Gateway's Envoy config, so a single Gateway can serve many teams' routes concurrently, each isolated via ReferenceGrant/namespace boundaries.
- **EnvoyPatchPolicy** exists as an escape hatch to inject raw Envoy config (JSONPatch against the generated xDS) for cases the Gateway API + Envoy Gateway CRDs don't yet cover.
- Envoy Gateway also ships a **Rate Limit service** (as a separate Deployment) that Envoy calls out to (gRPC) when `BackendTrafficPolicy` rate limiting is configured, and can integrate an **OIDC/ext_authz** service for `SecurityPolicy`.

## 4. Envoy Gateway's own CRDs (extend Gateway API's policy-attachment pattern)

These live under `gateway.envoyproxy.io/v1alpha1` and all use `targetRefs` to attach to a Gateway, Route, or (in newer versions) a specific listener/section.

### EnvoyProxy
Cluster/Gateway-scoped: configures the Envoy Deployment itself — replica count, resources, Envoy bootstrap overrides, logging level, shutdown behavior.

```yaml
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: EnvoyProxy
metadata:
  name: custom-proxy-config
  namespace: default
spec:
  provider:
    type: Kubernetes
    kubernetes:
      envoyDeployment:
        replicas: 3
        container:
          resources:
            requests:
              cpu: 200m
              memory: 256Mi
---
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: eg
  namespace: default
spec:
  gatewayClassName: envoy-gateway
  infrastructure:
    parametersRef:
      group: gateway.envoyproxy.io
      kind: EnvoyProxy
      name: custom-proxy-config
```

### ClientTrafficPolicy
Downstream (client ↔ Envoy) connection behavior: HTTP/2 keepalive, gRPC-Web, TCP keepalive, client IP detection (`XFF`/proxy protocol), TLS versions/ciphers, connection limits.

```yaml
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: ClientTrafficPolicy
metadata:
  name: client-timeout-policy
spec:
  targetRefs:
    - group: gateway.networking.k8s.io
      kind: Gateway
      name: eg
  tcpKeepalive:
    idleTime: 60s
```

### BackendTrafficPolicy
Upstream (Envoy ↔ backend) connection behavior: load balancing algorithm, circuit breaking, retries/retry budgets, rate limiting, timeouts, health checks, bandwidth limiting.

```yaml
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: BackendTrafficPolicy
metadata:
  name: api-backend-policy
spec:
  targetRefs:
    - group: gateway.networking.k8s.io
      kind: HTTPRoute
      name: backend-route
  retry:
    numRetries: 3
    retryOn:
      httpStatusCodes: [503]
  rateLimit:
    type: Global
    global:
      rules:
        - clientSelectors:
            - headers:
                - name: x-user-id
                  type: Distinct
          limit:
            requests: 100
            unit: Minute
```

### SecurityPolicy
Authn/authz: JWT validation, OIDC, basic auth, external authorization (ext_authz), CORS, IP allow/deny lists.

```yaml
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: SecurityPolicy
metadata:
  name: jwt-auth
spec:
  targetRefs:
    - group: gateway.networking.k8s.io
      kind: HTTPRoute
      name: backend-route
  jwt:
    providers:
      - name: example-provider
        remoteJWKS:
          uri: https://auth.example.com/.well-known/jwks.json
```

### EnvoyExtensionPolicy
Attaches Envoy/WASM extensions or external processing (`ext_proc`) filters for custom request/response manipulation logic beyond what built-in filters offer.

### HTTPRouteFilter
Custom filter types referenced from an HTTPRoute's `filters` list when the extension needs route-scoped config (an alternative attachment point to the targetRef pattern above).

### EnvoyPatchPolicy
Raw JSONPatch against the generated Envoy xDS resources — the "break glass" option, discouraged for routine use since it bypasses the abstraction and can break on upgrades.

## 5. How a request flows end-to-end

1. Client sends a request to the Gateway's external IP/LoadBalancer (created for the `Gateway` resource by Envoy Gateway's provisioner).
2. Envoy's listener (matching the `Gateway` listener's port/protocol) accepts the connection, terminates TLS if configured.
3. Envoy's HTTP Connection Manager matches the request against the merged route table built from all `HTTPRoute`/`GRPCRoute` objects bound to that Gateway (via `parentRefs`).
4. Any attached `ClientTrafficPolicy`/`SecurityPolicy` filters run (auth, rate limit, header manipulation) before/around routing.
5. Envoy picks a backend endpoint using the load balancing policy from `BackendTrafficPolicy`, applying retries/circuit breaking/timeouts as configured, and (if `BackendTLSPolicy` set) originates TLS to the backend.
6. Response flows back through the same filter chain.

## 6. Gateway API implementations compared

| Implementation | Data plane | Notable strengths | Notes |
|---|---|---|---|
| Envoy Gateway | Envoy | Native, "vanilla" Gateway API implementation from Envoy community; rich CRD set (this guide) | No dependency on a service mesh; simplest way to get pure Envoy behind Gateway API |
| Istio (Gateway API mode) | Envoy | Deep integration if you're already running Istio mesh (mTLS, telemetry) | Gateway API is now Istio's recommended ingress API, replacing Istio Gateway CRD |
| Cilium | Envoy (embedded) | Runs on the same Envoy already used for Cilium's L7 policies; eBPF dataplane for L3/4 | Good fit if already using Cilium as CNI |
| Kong | Kong/Nginx-based | Plugin ecosystem, enterprise features | Kong Gateway Operator implements Gateway API |
| NGINX Gateway Fabric | NGINX | Lightweight, familiar NGINX config model | F5/NGINX's official Gateway API implementation |
| Contour | Envoy | Simple, HA-focused, was a pre-Gateway-API pioneer (had its own CRDs before) | Also from the Envoy ecosystem |

Envoy Gateway's pitch specifically: it's maintained by the Envoy proxy project itself (not a wrapper around a pre-existing product), aims to track upstream Gateway API closely, and is the reference for "if you just want a modern Ingress replacement without adopting a full service mesh."

## 7. Quick mental model / cheat sheet

- **GatewayClass** = "which controller" (one-time, infra team).
- **Gateway** = "which listeners/ports/TLS certs" (platform team, one or few per cluster).
- **HTTPRoute/GRPCRoute/TCPRoute/UDPRoute/TLSRoute** = "which requests go where" (app teams, many, self-service).
- **ReferenceGrant** = "yes, cross-namespace reference allowed" (security boundary).
- **BackendTLSPolicy** = "how Envoy talks TLS to my backend."
- **Envoy Gateway CRDs (ClientTrafficPolicy / BackendTrafficPolicy / SecurityPolicy / EnvoyProxy / EnvoyExtensionPolicy / EnvoyPatchPolicy / HTTPRouteFilter)** = "the knobs Gateway API doesn't standardize yet, exposed via Envoy Gateway's own policy-attachment CRDs."

## Sources

- [Gateway API | Kubernetes](https://kubernetes.io/docs/concepts/services-networking/gateway/)
- [Gateway API v1.5: Moving features to Stable](https://kubernetes.io/blog/2026/04/21/gateway-api-v1-5/)
- [Gateway API Implementations](https://gateway-api.sigs.k8s.io/implementations/)
- [Envoy Gateway API Reference / Extension Types](https://gateway.envoyproxy.io/latest/api/extension_types/)
- [Announcing Envoy Gateway v1.8](https://gateway.envoyproxy.io/news/releases/v1.8/)
- [ClientTrafficPolicy | Envoy Gateway](https://gateway.envoyproxy.io/docs/concepts/gateway_api_extensions/client-traffic-policy/)
- [Kubernetes Gateway API in 2026: Envoy Gateway, Istio, Cilium and Kong](https://dev.to/mechcloud_academy/kubernetes-gateway-api-in-2026-the-definitive-guide-to-envoy-gateway-istio-cilium-and-kong-2bkl)
