# Step 1 — Install Envoy Gateway (minikube)

Part of a production walkthrough series. Next parts: 02 TLS (cert-manager), 03 Gateway/HTTPRoute YAMLs, 04 Envoy-specific policies, 05 Observability, 06 (optional) external OTel endpoint.

App used throughout this series: `example-app.com`. Everything (Gateway, HTTPRoute, Certificates, backend apps) lives in the `envoy-gateway-system` namespace in this setup.

Files referenced below live in `gateway-yaml/` in this repo.

## 1.1 Start minikube

Any driver works; Docker is the most common.

```bash
minikube start --driver=docker --cpus=4 --memory=6g
kubectl cluster-info
```

Envoy Gateway will create a `LoadBalancer`-type Service for every Gateway you provision. Minikube doesn't have a cloud load balancer, so you have two options to actually reach that Service from your machine — pick one and keep it in mind for later steps:

- **`minikube tunnel`** — run `minikube tunnel` in a separate terminal (it needs sudo). It assigns a real routable external IP to `LoadBalancer` Services, closest to how production behaves. Leave that terminal running for the rest of this walkthrough.
- **`kubectl port-forward`** — simplest, no sudo, works identically regardless of driver, but only forwards one local port at a time and doesn't reflect a real external IP. Fine for quick tests.

This guide uses `minikube tunnel` so the examples (and later, TLS SNI/hostname testing) behave like a real external LoadBalancer.

```bash
minikube tunnel
# leave this running in its own terminal
```

## 1.2 Install Envoy Gateway via Helm

Envoy Gateway ships as an OCI Helm chart. This installs the control plane (the component that watches Gateway API objects and translates them to Envoy xDS — see the architecture diagram from earlier).

```bash
helm install eg oci://docker.io/envoyproxy/gateway-helm \
  --version v1.8.2 \
  -n envoy-gateway-system \
  --create-namespace
```

Wait for the control plane to be ready:

```bash
kubectl wait --timeout=5m -n envoy-gateway-system \
  deployment/envoy-gateway --for=condition=Available
```

Confirm what got installed:

```bash
kubectl get pods -n envoy-gateway-system
```

You should see one `envoy-gateway` pod (the control plane). No data-plane Envoy pods exist yet — those get created only once you apply a `Gateway` resource, because Envoy Gateway's provisioner creates one Envoy Deployment per Gateway.

## 1.3 Create the GatewayClass

`gateway-yaml/00-gatewayclass.yaml`:

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: GatewayClass
metadata:
  name: envoy-gateway
spec:
  controllerName: gateway.envoyproxy.io/gatewayclass-controller
```

```bash
kubectl apply -f gateway-yaml/00-gatewayclass.yaml
kubectl get gatewayclass envoy-gateway -o wide
```

`ACCEPTED` should flip to `True` within a few seconds.

Note: `gateway-yaml/02-gateway-config.yaml` (used starting in step 5/observability) later re-applies this same `GatewayClass` object with a `parametersRef` added, pointing at an `EnvoyProxy` resource. That's intentional layering, not a conflict — but don't re-apply `00-gatewayclass.yaml` on its own after `02` has run, since doing so would silently strip the `parametersRef` back out.

## 1.4 Create the Gateway

`gateway-yaml/01-gateway.yaml` — this repo's setup keeps **both** an HTTP and an HTTPS listener on the Gateway from the start (rather than upgrading later), in the `envoy-gateway-system` namespace:

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: gateway-api
  namespace: envoy-gateway-system
spec:
  gatewayClassName: envoy-gateway
  infrastructure:
    labels:
      app: envoy-gateway
  listeners:
    - name: http
      protocol: HTTP
      port: 80
      allowedRoutes:
        namespaces:
          from: Same
    - name: https
      protocol: HTTPS
      port: 443
      hostname: "example-app.com"
      tls:
        mode: Terminate
        certificateRefs:
          - name: example-app-tls-secret
            namespace: envoy-gateway-system
      allowedRoutes:
        namespaces:
          from: Same
```

For now, apply just the `GatewayClass` + the listener shape without a real cert yet — the `https` listener's `certificateRefs` won't resolve until step 2 issues `example-app-tls-secret`, so `ResolvedRefs` on that listener will show `False` until then. That's expected at this point.

```bash
kubectl apply -f gateway-yaml/01-gateway.yaml
kubectl get gateway gateway-api -n envoy-gateway-system
```

Wait for the `http` listener specifically to program (the `https` one won't yet):

```bash
kubectl describe gateway/gateway-api -n envoy-gateway-system
```

If the Gateway shows no address at all with reason `AddressNotAssigned`, `minikube tunnel` isn't running — see 1.1.

One thing worth flagging now, before it's easy to forget: because the `http` listener here has **no `hostname` restriction**, it's a catch-all — any HTTPRoute bound to this Gateway will also be reachable over plain, unencrypted HTTP on port 80, not just HTTPS. If you want to force clients to HTTPS, you'll want an explicit HTTP→HTTPS redirect rule on the HTTPRoute (not covered by default in this walkthrough — add it if this Gateway is ever exposed beyond your local cluster).

## What's next

Step 2 issues the cert-manager certificate this Gateway's `https` listener is already referencing, so `ResolvedRefs`/`Programmed` on it goes `True`. Step 3 adds the actual HTTPRoute and backend app. Step 4 covers the Envoy Gateway-specific policy CRDs. Step 5 adds Prometheus/Grafana + OpenTelemetry tracing (via the `EnvoyProxy` resource introduced in `02-gateway-config.yaml`).

## Sources
- [Install with Helm | Envoy Gateway](https://gateway.envoyproxy.io/docs/install/install-helm/)
- [Releases · envoyproxy/gateway](https://github.com/envoyproxy/gateway/releases)
