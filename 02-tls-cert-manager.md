# Step 2 — Client → Envoy TLS with cert-manager

Prereq: step 1 done, `kubectl get gateway/eg -n default` shows `PROGRAMMED: True` with the `http` listener for `example-app-go.com`.

This step terminates TLS at the Gateway's Envoy listener — the client's HTTPS connection ends at Envoy, which then talks to the backend over plain HTTP (a separate concern, covered by `BackendTLSPolicy`, only needed if your backend itself requires TLS).

We use a self-signed CA instead of Let's Encrypt because Let's Encrypt's ACME challenge needs your Gateway reachable from the public internet on a real domain — not possible from minikube. The pattern (cert-manager watches a `Certificate`, writes a `Secret`, Envoy Gateway reads that `Secret`) is identical in production; only the `ClusterIssuer` changes.

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

A single self-signed leaf cert works but doesn't give you a CA to trust — instead we make cert-manager mint its own root CA, then issue the app's certificate from that CA. This mirrors how an internal/corporate CA works in production.

```yaml
# ca-bootstrap.yaml
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
kubectl apply -f ca-bootstrap.yaml
kubectl get clusterissuer
kubectl describe certificate gw-demo-root-ca -n cert-manager   # Ready: True
```

## 2.3 Issue the app's certificate

The `Secret` cert-manager creates here must live in the **same namespace as the Gateway** — `default`, from step 1.

```yaml
# app-cert.yaml
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: example-app-go-tls
  namespace: default
spec:
  secretName: example-app-go-tls-secret
  dnsNames:
    - example-app-go.com
  issuerRef:
    name: gw-demo-ca-issuer
    kind: ClusterIssuer
```

```bash
kubectl apply -f app-cert.yaml
kubectl get certificate -n default   # should show Ready: True
```

## 2.4 Upgrade the Gateway listener to HTTPS

```yaml
# gateway.yaml
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: eg
  namespace: default
spec:
  gatewayClassName: eg
  listeners:
    - name: https
      port: 443
      protocol: HTTPS
      hostname: "example-app-go.com"
      tls:
        mode: Terminate
        certificateRefs:
          - name: example-app-go-tls-secret
      allowedRoutes:
        namespaces:
          from: Same
```

```bash
kubectl apply -f gateway.yaml
kubectl describe gateway/eg -n default
```

The `https` listener should show `Accepted: True` / `ResolvedRefs: True` / `Programmed: True`. `ResolvedRefs: True` specifically confirms Envoy Gateway found and loaded the TLS secret.

## 2.5 Test it — and why you won't get a real response yet

```bash
export GATEWAY_HOST=$(kubectl get gateway/eg -n default -o jsonpath='{.status.addresses[0].value}')

# extract the CA so curl trusts our self-signed chain
kubectl get secret gw-demo-root-ca-secret -n cert-manager -o jsonpath='{.data.ca\.crt}' | base64 -d > gw-demo-ca.crt

curl -v --resolve example-app-go.com:443:$GATEWAY_HOST \
  --cacert gw-demo-ca.crt \
  https://example-app-go.com/
```

We haven't created an `HTTPRoute` or a backend Service/Deployment yet — that's step 3. So here's exactly what happens: the TLS handshake completes successfully (you'll see the cert exchange in `curl -v` output, and no `-k`/trust warning, since it's signed by your CA). That's this step's job, done. But after the handshake, Envoy has no route table entry for the request — no `HTTPRoute` has attached to the `https` listener — so Envoy itself returns a direct `404` response (Envoy Gateway's built-in "no route matched" fallback). It's not a hang, a connection error, or a TLS failure; it's Envoy correctly saying "I don't know where to send this yet."

Concretely, you should see something like `< HTTP/2 404` in the verbose output, with TLS negotiation lines above it showing the handshake succeeded. That 404 is the correct, expected state until step 3 adds a real `HTTPRoute` pointing at a backend — at which point the same command starts returning your app's actual response instead of Envoy's fallback 404.

## Note — dropping `hostname` from the listener

`hostname` is optional. If you drop it, a listener matches any SNI/Host header (a catch-all). This matters most once you have more than one domain on the same Gateway: you can put multiple `certificateRefs` on a single hostname-less listener and Envoy will auto-select the right cert by matching incoming SNI against each cert's SAN, with host-based routing then living entirely in each `HTTPRoute`'s `hostnames` field. What you can't do is have two listeners on the same port both with `hostname` unset — Gateway API requires listeners sharing a port to be distinguishable.

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
              - name: eg
                namespace: default
                kind: Gateway
```

Nothing else changes — same `Certificate` resource shape, same `Secret` name, same Gateway `certificateRefs`. cert-manager handles renewal automatically before the ~90-day Let's Encrypt certs expire; Envoy Gateway picks up the rotated Secret without you touching the Gateway again.

## What's next

Step 3 builds out the actual `HTTPRoute` and backend app for `example-app-go.com` — that's when the 404 above turns into a real response. Step 4 adds Prometheus/Grafana + OpenTelemetry tracing.

## Sources
- [Helm - cert-manager Documentation](https://cert-manager.io/docs/installation/helm/)
- [SelfSigned - cert-manager Documentation](https://cert-manager.io/docs/configuration/selfsigned/)
- [CA - cert-manager Documentation](https://cert-manager.io/docs/configuration/ca/)
