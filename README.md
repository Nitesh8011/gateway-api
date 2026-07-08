# k8s-gateway-api — Envoy Gateway learning + production walkthrough

Notes and step-by-step files from learning Kubernetes Gateway API using Envoy Gateway, on minikube. App used throughout: `example-app-go.com`.

## Reference material

- [`gateway-api-envoy-guide.md`](./gateway-api-envoy-guide.md) — concept reference: every Gateway API kind (GatewayClass, Gateway, HTTPRoute, GRPCRoute, TLSRoute, TCPRoute, UDPRoute, ReferenceGrant, BackendTLSPolicy, ListenerSet), every Envoy Gateway CRD (EnvoyProxy, ClientTrafficPolicy, BackendTrafficPolicy, SecurityPolicy, EnvoyExtensionPolicy, HTTPRouteFilter, EnvoyPatchPolicy), architecture, and a comparison against Istio/Cilium/Kong/NGINX Gateway Fabric.

## Hands-on production walkthrough (minikube)

Each numbered file is a self-contained step — run them in order.

| Step | File | Status | Covers |
|---|---|---|---|
| 1 | [`01-install-envoy-gateway.md`](./01-install-envoy-gateway.md) | Done | minikube setup, `minikube tunnel` vs port-forward, Helm install of Envoy Gateway control plane, GatewayClass, Gateway with an HTTP listener for `example-app-go.com` |
| 2 | [`02-tls-cert-manager.md`](./02-tls-cert-manager.md) | Done | cert-manager install, self-signed two-tier CA, Certificate for `example-app-go.com`, upgrading the listener to HTTPS, why you get a 404 without an HTTPRoute yet, Let's Encrypt swap for production |
| 3 | [`03-gateway-httproute-yamls.md`](./03-gateway-httproute-yamls.md) | Done | Backend Deployment/Service, basic HTTPRoute, weighted canary split, header-based routing, redirect vs URL rewrite, rule precedence |
| 4 | `04-observability.md` | Planned | Prometheus + Grafana metrics scraping, OpenTelemetry tracing via EnvoyProxy/EnvoyExtensionPolicy |

## Environment assumptions

- Cluster: minikube, Docker driver
- Envoy Gateway version: v1.8.2 (Helm chart `oci://docker.io/envoyproxy/gateway-helm`)
- cert-manager version: v1.20.3 (Helm chart `oci://quay.io/jetstack/charts/cert-manager`)
- Namespace for the control plane: `envoy-gateway-system`
- Gateway `eg`, app Services/HTTPRoutes/Certificates: `default` namespace

Update this table as each step file is added.
