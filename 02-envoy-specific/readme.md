# Step 4 — Envoy Gateway-specific policies: ClientTrafficPolicy, BackendTrafficPolicy, SecurityPolicy

Prereq: step 3 done — `example-app.com` is routing and canary-splitting correctly.

Everything through step 3 used only vendor-neutral Gateway API kinds (`Gateway`, `HTTPRoute`). This step covers the three Envoy Gateway-specific CRDs that extend Gateway API's generic "policy attachment" pattern — each one attaches to a `Gateway`, `HTTPRoute`, or similar via `targetRefs`, layering extra behavior on top without touching the underlying Gateway API object. Files referenced below live in `02-envoy-specific/` in this repo.

**Load-bearing rule that bit us repeatedly while testing this step:** Envoy Gateway only accepts **one policy of a given kind per target**. Two `SecurityPolicy` objects (or two `BackendTrafficPolicy` objects) both pointing `targetRefs` at the same `HTTPRoute` — one gets `Accepted: True`, the other silently sits there with `Accepted: False`/Conflicted and does nothing. This repo's `example-app-go` route ended up as the target of several different policy files over the course of testing (basic auth, JWT, rate limit, retry/circuit-breaker), and more than one BackendTrafficPolicy or SecurityPolicy pointed at it at once at various points — check `kubectl get clienttrafficpolicy,backendtrafficpolicy,securitypolicy -n envoy-gateway-system` and `kubectl describe <kind> <name> -n envoy-gateway-system` before assuming a policy you applied is actually in effect. If you want more than one auth mechanism or more than one BackendTrafficPolicy active at once, point them at *different* HTTPRoutes (e.g. `example-app-go-rewrite` from step 3.4) rather than layering them on the same one.

## 4.1 ClientTrafficPolicy — shapes the client ↔ Envoy connection

This is the policy for anything about how *clients* connect to Envoy, before routing happens: connection limits, keepalive, TLS versions, client certificate validation.

### Connection limit (`02-envoy-specific/00-client-traffic/00-connection-limit.yaml`)

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
kubectl apply -f 02-envoy-specific/00-client-traffic/00-connection-limit.yaml
kubectl describe clienttrafficpolicy connection-limit-policy -n envoy-gateway-system
```

`Accepted: True` confirms it attached. This limit applies per-listener-*type*: the `https` listener gets its own dedicated connection counter, separate from `http`. Tested and confirmed working — firing 10 concurrent requests at the `https` listener with the limit at 5 got exactly 5 `200`s and 5 connection-level failures (curl exit `35`/`CURLE_SSL_CONNECT_ERROR` — Envoy resets the 6th+ connection before/during the TLS handshake, so it shows up as an SSL error client-side, not a clean HTTP status):

```bash
for i in $(seq 1 10); do
  curl -s -o /dev/null -w "%{http_code}\n" --resolve example-app.com:443:$GATEWAY_HOST --cacert gw-demo-ca.crt https://example-app.com/ &
done
wait
```

**Gotcha:** this policy interferes with every other traffic test in this step and the observability step — if you're testing rate limits, circuit breakers, or generating load with `hey -c <N>` where `N` > 5, you'll hit this connection cap first and see `EOF`/connection-reset errors that have nothing to do with whatever you're actually trying to test. Delete it (`kubectl delete clienttrafficpolicy connection-limit-policy -n envoy-gateway-system`) before other load tests, and re-apply when you specifically want to test this one again.

### Mutual TLS — client certificate validation (`02-envoy-specific/00-client-traffic/01-mtls.yaml`)

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

**It requires every client to present a certificate, on the entire `https` listener.** Unlike the connection limit above (which is transparent to well-behaved clients), this one is not backward-compatible with the plain `curl --cacert ...` commands used elsewhere in this repo. Once this policy is active, those commands will fail the TLS handshake entirely (server sends a `certificate_required` TLS alert), because they only *verify the server*, they don't *present a client cert*.

**`caCertificateRefs` points at `example-app-tls-secret` — your server's own leaf-cert Secret — to validate incoming client certificates.** This works, in principle, because cert-manager populates a `ca.crt` field in any Secret issued by a CA-backed `ClusterIssuer`. **Unresolved finding from testing this in this cluster:** the actual `ca.crt` inside `example-app-tls-secret` turned out to be issued by a CA called `gw-demo-client-ca` (RSA, self-signed) — **not** `gw-demo-root-ca` from `certs/00-ca-bootstrap.yaml` (ECDSA). That client CA isn't defined anywhere in this repo's `certs/` directory, so it must have been created directly in the cluster outside of this repo's manifests. To actually test mTLS, you need to sign your client cert against whichever CA is really in that Secret — don't assume it's `gw-demo-root-ca` without checking:

```bash
kubectl get secret example-app-tls-secret -n envoy-gateway-system -o jsonpath='{.data.ca\.crt}' | base64 -d > secret-ca.crt
openssl x509 -in secret-ca.crt -noout -subject -issuer
# find/extract whatever CA this actually points at before signing a client cert
```

Once you've identified the right CA and its private key:

```bash
openssl req -newkey rsa:2048 -nodes -keyout client.key -out client.csr -subj "/CN=demo-client"
openssl x509 -req -in client.csr -CA <the-right-ca>.crt -CAkey <the-right-ca-key> -CAcreateserial -out client.crt -days 365

curl --resolve example-app.com:443:$GATEWAY_HOST --cacert gw-demo-ca.crt \
  --cert client.crt --key client.key \
  https://example-app.com/
```

## 4.2 BackendTrafficPolicy — shapes the Envoy ↔ backend connection

This is the policy for everything about how Envoy talks to your backend after routing: load balancing, retries, circuit breaking, and rate limiting. **Remember the one-policy-per-target rule from the top of this doc** — the two files below both default to targeting `example-app-go`, which conflicts. Point one at a different HTTPRoute (e.g. `example-app-go-rewrite`) if you want both active simultaneously.

### Local rate limit (`02-envoy-specific/01-backend-traffic/00-rate-limit.yaml`)

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

```bash
kubectl apply -f 02-envoy-specific/01-backend-traffic/00-rate-limit.yaml
kubectl describe backendtrafficpolicy ratelimit-httproute -n envoy-gateway-system
```

Tested and confirmed working with `hey` (note: `hey` has no `--resolve`/`--cacert`/`--cert` flags at all — it always sets `InsecureSkipVerify: true` and uses `-host`'s value as both the `Host` header and TLS SNI, so hit `$GATEWAY_HOST` directly rather than trying to pass cert flags):

```bash
hey -n 20 -c 1 -host example-app.com https://$GATEWAY_HOST/
```

`-c 1` matters — keeps requests sequential over one connection so you're testing the *request-rate* limiter, not accidentally colliding with `connection-limit-policy` above (which is connection-count-based, not request-rate-based, and produces a completely different failure mode: instant connection-level `EOF`s instead of clean `429` responses). Delete `connection-limit-policy` first if it's still applied.

This is "local" rate limiting — enforced independently per Envoy proxy instance, not shared across replicas. Envoy Gateway supports a `global` rate limit backed by a separate rate-limit service Deployment for cluster-wide limits — not covered here since this setup runs a single Envoy replica.

### Retries + circuit breaking (`02-envoy-specific/01-backend-traffic/01-retry-circuit-breaker.yaml`)

```yaml
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: BackendTrafficPolicy
metadata:
  name: resilience-httproute
  namespace: envoy-gateway-system
spec:
  targetRefs:
    - group: gateway.networking.k8s.io
      kind: HTTPRoute
      name: example-app-go
  retry:
    numRetries: 5
    perRetry:
      backOff:
        baseInterval: 100ms
        maxInterval: 10s
      timeout: 250ms
    retryOn:
      httpStatusCodes:
        - 503
      triggers:
        - connect-failure
        - retriable-status-codes
  circuitBreaker:
    maxParallelRequests: 2
    maxPendingRequests: 0
```

Field meanings:

- `retry.numRetries` — attempts beyond the first (5 retries = 6 total attempts).
- `retry.perRetry.backOff` — exponential wait between attempts, `baseInterval` → `maxInterval`.
- `retry.perRetry.timeout` — per-attempt timeout, separate from the route's overall request timeout.
- `retry.retryOn.triggers` — `connect-failure` (couldn't reach the backend at all — no pods, connection refused) and `retriable-status-codes` (backend responded with one of `httpStatusCodes`).
- `circuitBreaker.maxParallelRequests` — hard cap on concurrent in-flight requests to the backend cluster; a snapshot count, not a rate.
- `circuitBreaker.maxPendingRequests` — how many requests queue once the parallel cap is hit, before getting an instant `503`. `0` means no queueing at all.

**Testing caveat:** this repo's backend (`hashicorp/http-echo`) always returns `200` instantly — it has no `/status/500` or `?delay=` endpoint like Envoy Gateway's own docs use to demo these features. Two workarounds that actually worked:

- **Circuit breaker** — works fine even with an instant backend, since it's about concurrent in-flight count at send time, not response latency. Burst well above `maxParallelRequests`:
  ```bash
  hey -n 50 -c 50 -host example-app.com https://$GATEWAY_HOST/v4
  ```
  Watch for `503`s in the status distribution.
- **Retry on connect-failure** — force it by scaling a backend to zero (this doubles as a canary-rollback failover test, since `00-canary-split.yaml` weights v1 90%/v2 10% — scaling v1 to 0 tests whether retries land requests on the still-healthy v2):
  ```bash
  kubectl scale deployment example-app-go-v1 -n envoy-gateway-system --replicas=0
  hey -n 20 -c 1 -host example-app.com https://$GATEWAY_HOST/
  kubectl scale deployment example-app-go-v1 -n envoy-gateway-system --replicas=2   # restore after
  ```
  **Don't trust the HTTP status codes alone to confirm retries actually fired** — a 100% success rate here is *possible* two different ways: either retries genuinely re-rolled the weighted cluster pick onto the healthy v2, or Envoy Gateway skipped the zero-endpoint v1 cluster at selection time without ever attempting/retrying it. Confirm which one actually happened via Envoy's own stats, not curl output:
  ```bash
  kubectl get deploy -n envoy-gateway-system   # find the envoy proxy deployment name
  kubectl exec -n envoy-gateway-system deploy/<envoy-deployment-name> -c envoy -- curl -s localhost:19000/stats | grep -i "example-app-go-v1" | grep -E "upstream_rq_retry|upstream_cx_connect_fail|upstream_rq_total"
  ```
  If `v1`'s `upstream_rq_total` is near 0, Envoy skipped it outright rather than retrying into it.

## 4.3 SecurityPolicy — authn/authz on routes

`SecurityPolicy` is where JWT validation, OIDC, basic auth, external authorization (ext_authz), CORS, and IP allow/deny lists live. Two working examples in this repo, both under `02-envoy-specific/02-security/` — **remember the one-policy-per-target rule**: don't apply both against the same HTTPRoute at once without checking `Accepted` status on each.

### Basic Auth (`02-envoy-specific/02-security/00-basic-auth.yaml`)

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: example-app-basic-auth
  namespace: envoy-gateway-system
type: Opaque
stringData:
  # generated with: htpasswd -nbs demo-user demo-password
  # key MUST be named ".htpasswd" — Envoy Gateway looks up this exact key.
  .htpasswd: |
    demo-user:{SHA}POjQMNQW1NfYyWtu4ZyNpwwV7k4=
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

Two things this repo got wrong on the first pass, worth calling out since neither error is obvious from the Envoy Gateway docs' basic example:

1. **The Secret key must be named exactly `.htpasswd`**, not `users.txt` or anything else — Envoy Gateway looks up that literal key name. Using the wrong key produces `must contain a non-empty ".htpasswd" key` in the SecurityPolicy's status condition.
2. **The password hash must be Apache `{SHA}` format** (base64-encoded SHA1, generated by `htpasswd -s`), **not bcrypt**. Using bcrypt produces `unsupported htpasswd format: please use {SHA}`.

```bash
kubectl apply -f 02-envoy-specific/02-security/00-basic-auth.yaml
kubectl describe securitypolicy example-app-basic-auth -n envoy-gateway-system   # expect Accepted: True

curl -u demo-user:demo-password --resolve example-app.com:443:$GATEWAY_HOST --cacert gw-demo-ca.crt https://example-app.com/
# without -u: should get 401
```

### JWT (local JWKS, no external IdP) (`02-envoy-specific/02-security/01-jwt-local-jwks.yaml`)

Uses a local JWKS (a `ConfigMap`) instead of pointing at a real identity provider — self-contained, testable offline:

```yaml
apiVersion: gateway.envoyproxy.io/v1alpha1
kind: SecurityPolicy
metadata:
  name: example-app-jwt
  namespace: envoy-gateway-system
spec:
  targetRefs:
    - group: gateway.networking.k8s.io
      kind: HTTPRoute
      name: example-app-go
  jwt:
    providers:
      - name: gw-demo-jwt
        issuer: "https://gw-demo.local/issuer"
        audiences:
          - "gw-demo-app"
        localJWKS:
          type: ValueRef
          valueRef:
            group: ""
            kind: ConfigMap
            name: gw-demo-jwt-jwks
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: gw-demo-jwt-jwks
  namespace: envoy-gateway-system
data:
  jwks: |
    { "keys": [ { "kty": "RSA", "use": "sig", "alg": "RS256", "kid": "gw-demo-jwt-key-1", "n": "...", "e": "AQAB" } ] }
```

The matching RSA private key lives at `certs/jwt-signing-key.pem` (gitignored, demo-only — don't reuse it anywhere real). Mint a test token with `02-envoy-specific/02-security/mint-jwt.py`:

```bash
kubectl apply -f 02-envoy-specific/02-security/01-jwt-local-jwks.yaml
kubectl describe securitypolicy example-app-jwt -n envoy-gateway-system   # expect Accepted: True

cd 02-envoy-specific/02-security
pip install pyjwt cryptography --break-system-packages
TOKEN=$(python3 mint-jwt.py)

curl -i --resolve example-app.com:443:$GATEWAY_HOST --cacert ../../gw-demo-ca.crt https://example-app.com/
# no token -> 401

curl -i -H "Authorization: Bearer $TOKEN" --resolve example-app.com:443:$GATEWAY_HOST --cacert ../../gw-demo-ca.crt https://example-app.com/
# valid token -> 200
```

**Gotcha hit while testing this:** the `Accepted` status won't populate at all — no `Status`/`Ancestors` block whatsoever — if the target `HTTPRoute` named in `targetRefs` doesn't actually exist in the cluster yet (e.g. if you point it at `example-app-go-rewrite` but never applied that file from step 3.4). No status isn't a silent-fail-but-technically-attached state; it means the policy has nothing to attach to at all. Check `kubectl get httproute -n envoy-gateway-system` first if `describe` comes back with no status section.

## Where this leaves you

These three CRDs — `ClientTrafficPolicy`, `BackendTrafficPolicy`, `SecurityPolicy` — are the extension surface Envoy Gateway adds on top of vendor-neutral Gateway API. Everything here attaches via `targetRefs` the same way, layering onto a `Gateway` or `HTTPRoute` without modifying it directly — the main operational trap, confirmed by hitting it several times while writing this doc, is the one-policy-per-kind-per-target limit silently leaving one of your policies inactive whenever two land on the same route.

Step 5 (`03-observability-stack/`) adds metrics, traces, and eventually logs.

## Sources
- [ClientTrafficPolicy | Envoy Gateway](https://gateway.envoyproxy.io/docs/concepts/gateway_api_extensions/client-traffic-policy/)
- [BackendTrafficPolicy | Envoy Gateway](https://gateway.envoyproxy.io/docs/concepts/gateway_api_extensions/backend-traffic-policy/)
- [SecurityPolicy | Envoy Gateway](https://gateway.envoyproxy.io/docs/concepts/gateway_api_extensions/security-policy/)
- [Basic Authentication | Envoy Gateway](https://gateway.envoyproxy.io/docs/tasks/security/basic-auth/)
- [JWT Authentication | Envoy Gateway](https://gateway.envoyproxy.io/docs/tasks/security/jwt-authentication/)
- [Local Rate Limit | Envoy Gateway](https://gateway.envoyproxy.io/docs/tasks/traffic/local-rate-limit/)
- [Connection Limit | Envoy Gateway](https://gateway.envoyproxy.io/docs/tasks/traffic/connection-limit/)
- [Retry | Envoy Gateway](https://gateway.envoyproxy.io/docs/tasks/traffic/retry/)
- [Circuit Breakers | Envoy Gateway](https://gateway.envoyproxy.io/docs/tasks/traffic/circuit-breaker/)
- [Mutual TLS: External Clients to the Gateway | Envoy Gateway](https://gateway.envoyproxy.io/docs/tasks/security/mutual-tls/)
