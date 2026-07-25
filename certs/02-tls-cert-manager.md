# Step 2 — Client → Envoy TLS with cert-manager

Prereq: step 1 done, `kubectl get gateway/gateway-api -n envoy-gateway-system` shows the Gateway exists (its `https` listener will show `ResolvedRefs: False` until this step issues the certificate it's already referencing).

This step terminates TLS at the Gateway's Envoy listener — the client's HTTPS connection ends at Envoy, which then talks to the backend over plain HTTP (a separate concern, covered by `BackendTLSPolicy`, only needed if your backend itself requires TLS).

We use a self-signed CA instead of Let's Encrypt because Let's Encrypt's ACME challenge needs your Gateway reachable from the public internet on a real domain — not possible from minikube. The pattern (cert-manager watches a `Certificate`, writes a `Secret`, Envoy Gateway reads that `Secret`) is identical in production; only the `ClusterIssuer` changes.

Files referenced below live in `certs/` in this repo.

## 2.1 Install cert-manager

```bash
helm install cert-manager oci://quay.io/jetstack/charts/cert-manager \
  --version v1.20.3 \
  -n cert-manager --create-namespace \
  --set crds.enabled=true
```

```bash
kubectl wait --timeout=3m -n cert-manager \
  deployment/cert-manager --for=condition=Available
kubectl get pods -n cert-manager
```

You should see three pods: `cert-manager`, `cert-manager-cainjector`, `cert-manager-webhook`.

## 2.2 Bootstrap a self-signed CA (two-tier, so it behaves like a real PKI)

`certs/00-ca-bootstrap.yaml` — a single self-signed leaf cert works but doesn't give you a CA to trust, so instead cert-manager mints its own root CA, then issues the app's certificate from that CA. This mirrors how an internal/corporate CA works in production.

```yaml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: selfsigned-bootstrap
spec:
  selfSigned: {}
---
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: gw-demo-root-ca
  namespace: cert-manager
spec:
  isCA: true
  commonName: gw-demo-root-ca
  secretName: gw-demo-root-ca-secret
  privateKey:
    algorithm: ECDSA
    size: 256
  issuerRef:
    name: selfsigned-bootstrap
    kind: ClusterIssuer
---
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: gw-demo-ca-issuer
spec:
  ca:
    secretName: gw-demo-root-ca-secret
```

```bash
kubectl apply -f certs/00-ca-bootstrap.yaml
kubectl get clusterissuer
kubectl describe certificate gw-demo-root-ca -n cert-manager   # Ready: True
```

## 2.3 Issue the app's certificate

`certs/01-app-cert.yaml` — this `Secret` must live in the **same namespace as the Gateway**, which in this setup is `envoy-gateway-system`, not `default` or `cert-manager`.

```yaml
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: example-app-tls
  namespace: envoy-gateway-system
spec:
  secretName: example-app-tls-secret
  dnsNames:
    - example-app.com
  issuerRef:
    name: gw-demo-ca-issuer
    kind: ClusterIssuer
```

```bash
kubectl apply -f certs/01-app-cert.yaml
kubectl get certificate -n envoy-gateway-system   # should show Ready: True
kubectl get secret example-app-tls-secret -n envoy-gateway-system
```

Once this exists, re-check the Gateway from step 1 — the `https` listener should flip to `ResolvedRefs: True` / `Programmed: True` without you touching the Gateway object again, since it was already pointing at `example-app-tls-secret`:

```bash
kubectl describe gateway/gateway-api -n envoy-gateway-system
```

## 2.4 Test it — and why you won't get a real response yet

```bash
export GATEWAY_HOST=$(kubectl get gateway/gateway-api -n envoy-gateway-system -o jsonpath='{.status.addresses[0].value}')

# extract the CA so curl trusts our self-signed chain
kubectl get secret gw-demo-root-ca-secret -n cert-manager -o jsonpath='{.data.ca\.crt}' | base64 -d > gw-demo-ca.crt

curl -v --resolve example-app.com:443:$GATEWAY_HOST \
  --cacert gw-demo-ca.crt \
  https://example-app.com/
```

We haven't created an `HTTPRoute` or a backend Service/Deployment yet — that's step 3. So here's exactly what happens: the TLS handshake completes successfully (you'll see the cert exchange in `curl -v` output, and no `-k`/trust warning, since it's signed by your CA). That's this step's job, done. But after the handshake, Envoy has no route table entry for the request — no `HTTPRoute` has attached yet — so Envoy itself returns a direct `404` response (Envoy Gateway's built-in "no route matched" fallback). It's not a hang, a connection error, or a TLS failure; it's Envoy correctly saying "I don't know where to send this yet."

Concretely, you should see something like `< HTTP/2 404` in the verbose output, with TLS negotiation lines above it showing the handshake succeeded. That 404 is the correct, expected state until step 3 adds a real `HTTPRoute` pointing at a backend.

## Note — dropping `hostname` from the listener

`hostname` is optional. If you drop it, a listener matches any SNI/Host header (a catch-all). This matters most once you have more than one domain on the same Gateway: you can put multiple `certificateRefs` on a single hostname-less listener and Envoy will auto-select the right cert by matching incoming SNI against each cert's SAN, with host-based routing then living entirely in each `HTTPRoute`'s `hostnames` field. What you can't do is have two listeners on the same port both with `hostname` unset — Gateway API requires listeners sharing a port to be distinguishable. (This Gateway's `http` listener already has no hostname — see the note at the end of step 1 about that being a catch-all, plaintext-included.)

## Production difference

Swap `gw-demo-ca-issuer` for an ACME `ClusterIssuer` pointed at Let's Encrypt, e.g.:

```yaml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: you@example.com
    privateKeySecretRef:
      name: letsencrypt-prod-key
    solvers:
      - http01:
          gatewayHTTPRoute:
            parentRefs:
              - name: gateway-api
                namespace: envoy-gateway-system
                kind: Gateway
```

Nothing else changes — same `Certificate` resource shape, same `Secret` name, same Gateway `certificateRefs`. cert-manager handles renewal automatically before the ~90-day Let's Encrypt certs expire; Envoy Gateway picks up the rotated Secret without you touching the Gateway again.

## What's next

Step 3 builds out the actual `HTTPRoute` and backend app for `example-app.com` — that's when the 404 above turns into a real response. Step 4 adds Prometheus/Grafana + OpenTelemetry tracing.

## Sources
- [Helm - cert-manager Documentation](https://cert-manager.io/docs/installation/helm/)
- [SelfSigned - cert-manager Documentation](https://cert-manager.io/docs/configuration/selfsigned/)
- [CA - cert-manager Documentation](https://cert-manager.io/docs/configuration/ca/)
