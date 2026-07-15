# Step 4 — Envoy Gateway-specific policies: ClientTrafficPolicy, BackendTrafficPolicy, SecurityPolicy

Prereq: step 3 done — `example-app.com` is routing and canary-splitting correctly.

Everything through step 3 used only vendor-neutral Gateway API kinds (`Gateway`, `HTTPRoute`). This step covers the three Envoy Gateway-specific CRDs that extend Gateway API's generic "policy attachment" pattern — each one attaches to a `Gateway`, `HTTPRoute`, or similar via `targetRefs`, layering extra behavior on top without touching the underlying Gateway API object. Files referenced below live in `envoy-specific/` in this repo.

## 4.1 ClientTrafficPolicy — shapes the client ↔ Envoy connection

This is the policy for anything about how *clients* connect to Envoy, before routing happens: connection limits, keepalive, TLS versions, client certificate validation.

### Connection limit (`envoy-specific/00-client-traffic/00-connection-limit.yaml`)

Caps concurrent connections to the whole Gateway:

```yaml
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: ClientTrafficPolicy
metadata:
  name: connection-limit-policy
  namespace: envoy-gateway-system
spec:
  targetRefs:
    - group: gateway.networking.k8s.io
      kind: Gateway
      name: gateway-api
  connection:
    connectionLimit:
      value: 5
```

```bash
kubectl apply -f envoy-specific/00-client-traffic/00-connection-limit.yaml
kubectl describe clienttrafficpolicy connection-limit-policy -n envoy-gateway-system
```

`Accepted: True` confirms it attached. With a limit this low (5), it's easy to test by hand: open more than 5 concurrent long-lived connections (e.g. several `curl --http1.1 -N` requests backgrounded at once) and confirm the 6th+ stall or get rejected. Raise this value before relying on the Gateway for anything beyond this test.

### Mutual TLS — client certificate validation (`envoy-specific/00-client-traffic/01-mtls.yaml`)

```yaml
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: ClientTrafficPolicy
metadata:
  name: mtls-policy
  namespace: envoy-gateway-system
spec:
  targetRefs:
    - group: gateway.networking.k8s.io
      kind: Gateway
      name: gateway-api
      sectionName: https
  tls:
    clientValidation:
      caCertificateRefs:
        - kind: "Secret"
          group: ""
          name: "example-app-tls-secret"
```

Two things worth understanding before you apply this one, since they change how the rest of this walkthrough behaves:

**It requires every client to present a certificate, on the entire `https` listener.** Unlike the connection limit above (which is transparent to well-behaved clients), this one is not backward-compatible with the plain `curl --cacert ...` commands used in steps 2–3. Once this policy is active, those commands will fail the TLS handshake entirely, because they only *verify the server*, they don't *present a client cert*. To keep testing with plain curl, either don't apply this policy yet, or scope it to a different listener/route than the one you're testing against, or start passing `--cert`/`--key` with a client certificate issued by the same CA.

**`caCertificateRefs` points at `example-app-tls-secret` — your server's own leaf-cert Secret — to validate incoming client certificates.** This works, but only because cert-manager populates a `ca.crt` field in any Secret issued by a CA-backed `ClusterIssuer` (here, `gw-demo-ca-issuer`), so the CA bundle happens to be sitting inside that Secret alongside the server's `tls.crt`/`tls.key`. It's clearer to point this at a dedicated CA secret instead — e.g. `gw-demo-root-ca-secret` from `certs/00-ca-bootstrap.yaml` — though that one lives in the `cert-manager` namespace, so referencing it cross-namespace would need a `ReferenceGrant` (same mechanism described in the concept guide). Using the same-namespace `example-app-tls-secret` avoids that extra object, at the cost of being less obvious about *why* it works.

To actually test mTLS once you're ready, generate a client cert signed by your CA:

```bash
openssl req -newkey rsa:2048 -nodes -keyout client.key -out client.csr -subj "/CN=demo-client"
# sign with your gw-demo-ca-issuer-issued CA (extract gw-demo-root-ca-secret's tls.crt/tls.key first)
openssl x509 -req -in client.csr -CA gw-demo-ca.crt -CAkey <root-ca-key> -CAcreateserial -out client.crt -days 365

curl --resolve example-app.com:443:$GATEWAY_HOST --cacert gw-demo-ca.crt \
  --cert client.crt --key client.key \
  https://example-app.com/
```

## 4.2 BackendTrafficPolicy — shapes the Envoy ↔ backend connection

This is the policy for everything about how Envoy talks to your backend after routing: load balancing, retries, circuit breaking, and rate limiting.

`envoy-specific/01-backend-traffic.yaml` — a local rate limit on the `example-app-go` HTTPRoute:

```yaml
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: BackendTrafficPolicy
metadata:
  name: ratelimit-httproute
  namespace: envoy-gateway-system
spec:
  targetRefs:
    - group: gateway.networking.k8s.io
      kind: HTTPRoute
      name: example-app-go
  rateLimit:
    local:
      rules:
        - limit:
            requests: 10
            unit: Second
```

Note the `namespace: envoy-gateway-system` — this was missing in an earlier version of this file. `targetRefs` can only resolve within the same namespace as the policy itself; without it, this policy would silently fail to attach to `example-app-go` (which lives in `envoy-gateway-system`) if applied into a different default namespace.

```bash
kubectl apply -f envoy-specific/01-backend-traffic.yaml
kubectl describe backendtrafficpolicy ratelimit-httproute -n envoy-gateway-system
```

Test the limit by sending more than 10 requests/second:

```bash
for i in $(seq 1 20); do
  curl -s -o /dev/null -w "%{http_code}\n" --resolve example-app.com:443:$GATEWAY_HOST --cacert gw-demo-ca.crt https://example-app.com/ &
done
wait
```

You should start seeing `429` responses once you exceed 10 in that second. This is "local" rate limiting — enforced independently per Envoy proxy instance, not shared across replicas. For a limit that holds cluster-wide across multiple Envoy replicas, Envoy Gateway supports a `global` rate limit backed by a separate rate-limit service Deployment — worth knowing the option exists, not covered here since this setup runs a single Envoy replica.

## 4.3 SecurityPolicy — authn/authz on routes

`envoy-specific/02-security/03-security.yaml` in this repo is currently **empty** — no `SecurityPolicy` is defined yet, so applying it is a no-op. `SecurityPolicy` is where JWT validation, OIDC, basic auth, external authorization (ext_authz), CORS, and IP allow/deny lists live.

Below is a minimal, working Basic Auth example as a starting point — swap it for whatever auth mechanism you actually need (tell me and I'll fill in JWT/OIDC/IP-allowlist instead):

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: example-app-basic-auth
  namespace: envoy-gateway-system
type: Opaque
stringData:
  # generate with: htpasswd -nb demo-user demo-password
  users.txt: |
    demo-user:$apr1$...
---
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: SecurityPolicy
metadata:
  name: example-app-basic-auth
  namespace: envoy-gateway-system
spec:
  targetRefs:
    - group: gateway.networking.k8s.io
      kind: HTTPRoute
      name: example-app-go
  basicAuth:
    users:
      name: example-app-basic-auth
```

```bash
kubectl apply -f envoy-specific/02-security/03-security.yaml
curl -u demo-user:demo-password --resolve example-app.com:443:$GATEWAY_HOST --cacert gw-demo-ca.crt https://example-app.com/
# without -u: should get 401
```

## Where this leaves you

These three CRDs — `ClientTrafficPolicy`, `BackendTrafficPolicy`, `SecurityPolicy` — are the extension surface Envoy Gateway adds on top of vendor-neutral Gateway API. Everything here attaches via `targetRefs` the same way, layering onto a `Gateway` or `HTTPRoute` without modifying it directly, which is why you can add, remove, or swap these independently of the routing config from step 3.

Step 5 adds observability (Prometheus/Grafana/OpenTelemetry). Step 6 covers pointing tracing/metrics at your own external OTel collector instead of the bundled add-ons chart.

## Sources
- [ClientTrafficPolicy | Envoy Gateway](https://gateway.envoyproxy.io/docs/concepts/gateway_api_extensions/client-traffic-policy/)
- [BackendTrafficPolicy | Envoy Gateway](https://gateway.envoyproxy.io/docs/concepts/gateway_api_extensions/backend-traffic-policy/)
- [SecurityPolicy | Envoy Gateway](https://gateway.envoyproxy.io/docs/concepts/gateway_api_extensions/security-policy/)
- [Basic Authentication | Envoy Gateway](https://gateway.envoyproxy.io/docs/tasks/security/basic-auth/)
- [Local Rate Limit | Envoy Gateway](https://gateway.envoyproxy.io/docs/tasks/traffic/local-rate-limit/)
