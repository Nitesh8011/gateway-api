# Step 1 — Install Envoy Gateway (minikube)

Part of a production walkthrough series. Next parts: 02 TLS (cert-manager), 03 Gateway/HTTPRoute YAMLs, 04 Observability.

App used throughout this series: `example-app-go.com`.

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

This is the one-time "which controller owns this class of Gateways" object from earlier.

```yaml
# gatewayclass.yaml
apiVersion: gateway.networking.k8s.io/v1
kind: GatewayClass
metadata:
  name: eg
spec:
  controllerName: gateway.envoyproxy.io/gatewayclass-controller
```

```bash
kubectl apply -f gatewayclass.yaml
kubectl get gatewayclass eg -o wide
```

`ACCEPTED` should flip to `True` within a few seconds — that's Envoy Gateway's controller confirming it's claimed this class.

## 1.4 Create the Gateway

One listener, plain HTTP for now — step 2 upgrades it to HTTPS. This lives in the `default` namespace, alongside where your app's Service/HTTPRoute will go in step 3.

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
    - name: http
      port: 80
      protocol: HTTP
      hostname: "example-app-go.com"
      allowedRoutes:
        namespaces:
          from: Same
```

```bash
kubectl apply -f gateway.yaml
kubectl get gateway eg -n default
```

Wait for it to program:

```bash
kubectl wait --timeout=2m -n default gateway/eg --for=condition=Programmed
```

If `PROGRAMMED` stays `False` with reason `AddressNotAssigned`, `minikube tunnel` isn't running — see 1.1.

```bash
kubectl describe gateway/eg -n default
```

The `http` listener should show `Accepted: True` / `ResolvedRefs: True` / `Programmed: True`. No routes are attached yet (`Attached Routes: 0`) — that's expected, since we haven't created an HTTPRoute or backend yet (step 3). A request to this Gateway right now would just get a routing error, not a real response — there's nothing telling Envoy where to send matched traffic until step 3 exists.

## What's next

Step 2 upgrades the listener to HTTPS with a cert-manager-issued certificate. Step 3 adds the actual HTTPRoute and backend app — that's the point where requests start getting real responses. Step 4 adds Prometheus/Grafana + OpenTelemetry tracing.

## Sources
- [Install with Helm | Envoy Gateway](https://gateway.envoyproxy.io/docs/install/install-helm/)
- [Releases · envoyproxy/gateway](https://github.com/envoyproxy/gateway/releases)
