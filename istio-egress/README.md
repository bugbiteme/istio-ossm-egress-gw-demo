# Istio Egress Gateway Demo

Implements the egress gateway pattern from `ADR-002-openshift-service-mesh-v2.docx`
(Red Hat OpenShift Service Mesh 3.4 / Sail operator) for this repo's client → API
demo, on the `admin-east` cluster (mesh) reaching external systems on the
`admin-west` cluster (plain OpenShift, no mesh) through a dedicated egress
gateway rather than dialing out directly.

Two clients and two external APIs are wired through the **same** gateway
workload, deliberately, to demonstrate that adding another external system
or another internal caller doesn't mean deploying another gateway — just
more config on the one that already exists:

| | `external-api` | `external-api-2` |
|---|---|---|
| `client-secure` | ✅ allowed | ✅ allowed |
| `client-2` | ❌ denied | ✅ allowed |

`client-2` → `external-api` being denied is the point of that row: it
proves the gateway enforces *which app can reach which destination*, not
just *that traffic flows through it*.

## Architecture

```
client-secure ns   ─┐                  ┌─────────────────┐            ┌──────────────┐
│ client (Envoy    │  Hop 1: mTLS      │ egressgateway    │  Hop 2: TLS │ external-api │
│ sidecar), plain  ├──────────────────▶│ (Envoy, gateway  ├────────────▶│ (OpenShift   │
│ HTTP calls        │  ISTIO_MUTUAL    │  injected, one    │  origination│  Route, LE   │
└───────────────────┘                  │  workload serving│  (SIMPLE)   │  cert)       │
client-2 ns        ─┐                  │  BOTH external    │             └──────────────┘
│ client (Envoy    │  Hop 1: mTLS      │  hosts)           │            ┌──────────────┐
│ sidecar), plain  ├──────────────────▶│                   ├────────────▶│external-api-2│
│ HTTP calls        │  ISTIO_MUTUAL    └─────────────────┘  Hop 2: TLS  │ (OpenShift   │
└───────────────────┘                                       origination│  Route, LE   │
                                                              (SIMPLE)   │  cert)       │
                                                                         └──────────────┘
```

Two independent TLS relationships, per the ADR:

- **Hop 1 — sidecar → egress gateway (internal mesh mTLS).** Client app
  code makes a **plain HTTP** call (no app-level TLS at all — this is the
  point of TLS origination: the workload holds no credentials). Istio
  wraps that connection in its own mTLS using short-lived, istiod-issued
  SPIFFE certificates. This is explicitly configured — not left to mesh
  defaults — via `tls.mode: ISTIO_MUTUAL` on both the internal-facing
  `Gateway` listener ([20-gateway.yaml](k8/20-gateway.yaml)) and the
  sidecar-to-gateway `DestinationRule`
  ([30-destinationrule-hop1-mtls.yaml](k8/30-destinationrule-hop1-mtls.yaml)).
  Per the ADR, skipping this step means the gateway has no cryptographic
  caller identity, and identity-based `AuthorizationPolicy` rules silently
  fail to enforce. One Hop 1 `DestinationRule` serves every caller
  (`client-secure` and `client-2` both use it) and every external host
  (both `external-api` and `external-api-2` share the same filter chain) —
  see "Why Hop 1 is one shared rule," below.

- **Hop 2 — egress gateway → external API (TLS origination).** The egress
  gateway performs the real TLS handshake to each external API's OpenShift
  Route, one `DestinationRule` per external host
  ([31-destinationrule-hop2-tls.yaml](k8/31-destinationrule-hop2-tls.yaml),
  [33-destinationrule-hop2-tls-external-api-2.yaml](k8/33-destinationrule-hop2-tls-external-api-2.yaml)).
  Both are `tls.mode: SIMPLE` (server-authenticated only), not `MUTUAL`,
  because neither demo API performs client-certificate authentication —
  there's no client cert for the gateway to present. Both Routes serve
  publicly-trusted Let's Encrypt certificates, so the proxy's default
  trust store already covers them; no `caCertificates` bundle was needed.
  **To extend this to a real external system that requires mTLS**, per the
  ADR this DestinationRule would instead use `tls.mode: MUTUAL` with a
  `credentialName` pointing at a Kubernetes Secret (client cert/key/CA)
  mounted in the `egress-gateway` namespace.

## Why Hop 1 is one shared rule

It's tempting to assume a `DestinationRule`/SNI is needed per external
host, since Hop 2 clearly needs one DestinationRule per host. It isn't,
for Hop 1: SNI only selects which filter chain terminates the *gateway's
own* mTLS listener — it has nothing to do with the final destination.
Both `external-api` and `external-api-2` are declared as `hosts` on the
**same** `Gateway` server block ([20-gateway.yaml](k8/20-gateway.yaml)),
so they share one filter chain. The actual routing to the correct
external system happens afterward, at the HTTP layer, via the `Host`
header — a separate `VirtualService` per external host
([22-virtualservice.yaml](k8/22-virtualservice.yaml),
[24-virtualservice-external-api-2.yaml](k8/24-virtualservice-external-api-2.yaml)) —
which is preserved through the mTLS tunnel regardless of which SNI value
was used to pick the filter chain.

## Why the clients call the APIs over plain HTTP

`client/app.py` supports `API_FQDN`/`API_SCHEME` (and now an optional
second target, `API_FQDN_2`/`API_SCHEME_2`, added to show one client
reaching multiple external systems side by side in its own UI). Both
`client-secure` and `client-2`'s Deployments set `API_SCHEME=http` — this
is a config change, not an app-code change (the ADR explicitly calls out
that no application code changes are required for the *mesh* to adopt
this pattern; the two-target UI is a separate, small demo enhancement on
top of that, not something the egress pattern itself demanded). Both
clients' `httpx` calls are plain HTTP requests; the egress gateway is what
actually speaks TLS to the real external endpoints.

## Other mesh-wide changes made

- **`PeerAuthentication` STRICT, mesh-wide**
  ([40-peerauthentication.yaml](k8/40-peerauthentication.yaml)), in the
  `istio-system` root namespace — per the ADR's onboarding baseline
  ("STRICT mTLS for all namespaces"). This affects every mesh-enrolled
  namespace on the cluster (including the pre-existing `bookinfo` demo),
  not just this egress path — verified `bookinfo` still works after
  applying (see Testing, below).
- `outboundTrafficPolicy.mode: REGISTRY_ONLY` was **already set** on this
  cluster's `Istio` CR before this work started — no change needed there.
  Without it, traffic to hosts without a matching `ServiceEntry` would be
  forwarded as opaque TCP rather than blocked, undermining the allowlist
  model this whole design relies on. (This setting is also what blocked a
  build pod partway through this work — see Issue #4, below.)

## Scoping decision: AuthorizationPolicy

The ADR's onboarding model calls for a mesh-wide deny-all
`AuthorizationPolicy` baseline. This demo does **not** apply that
mesh-wide — only `ALLOW` rules scoped to the `egressgateway` workload
([41-authorizationpolicy.yaml](k8/41-authorizationpolicy.yaml)),
implementing the matrix at the top of this doc. In Istio, a workload with
at least one `ALLOW` policy selecting it becomes implicit-deny for
everything else, so listing only the allowed (identity, destination host)
pairs is sufficient — no rule is needed for `client-2` → `external-api`
to be denied, and no separate deny-all object is needed either. This
doesn't touch authorization posture for `bookinfo` or any other unrelated
namespace on this shared demo cluster.

## Resources (apply in this order)

| File | Resource | Purpose |
|---|---|---|
| [00-namespace.yaml](k8/00-namespace.yaml) | `Namespace` | Dedicated `egress-gateway` namespace |
| [01-podmonitor.yaml](k8/01-podmonitor.yaml) | `PodMonitor` | Scrapes istio-proxy `/stats/prometheus` in `client-secure` and `egress-gateway` (applied separately per namespace) — the ADR's "Metrics via OpenShift user-workload monitoring" piece |
| [10-egress-gateway-workload.yaml](k8/10-egress-gateway-workload.yaml) | `ServiceAccount`, `Deployment`, `Service` | The gateway proxy itself (gateway-injected, not operator-managed — OSSM 3 requirement). One workload serves both external hosts. |
| [20-gateway.yaml](k8/20-gateway.yaml) | `Gateway` | Hop 1 internal listener, `ISTIO_MUTUAL`, both external hosts on one server block |
| [21-serviceentry.yaml](k8/21-serviceentry.yaml) | `ServiceEntry` | Allowlists `external-api` |
| [22-virtualservice.yaml](k8/22-virtualservice.yaml) | `VirtualService` | Routes mesh → gateway → `external-api` |
| [23-serviceentry-external-api-2.yaml](k8/23-serviceentry-external-api-2.yaml) | `ServiceEntry` | Allowlists `external-api-2` |
| [24-virtualservice-external-api-2.yaml](k8/24-virtualservice-external-api-2.yaml) | `VirtualService` | Routes mesh → gateway → `external-api-2` |
| [30-destinationrule-hop1-mtls.yaml](k8/30-destinationrule-hop1-mtls.yaml) | `DestinationRule` | Hop 1: any sidecar → gateway, `ISTIO_MUTUAL` + explicit `sni`. Lives in `egress-gateway`, not a client namespace — see Issue #5. |
| [31-destinationrule-hop2-tls.yaml](k8/31-destinationrule-hop2-tls.yaml) | `DestinationRule` | Hop 2: gateway → `external-api`, TLS origination |
| [33-destinationrule-hop2-tls-external-api-2.yaml](k8/33-destinationrule-hop2-tls-external-api-2.yaml) | `DestinationRule` | Hop 2: gateway → `external-api-2`, TLS origination |
| [40-peerauthentication.yaml](k8/40-peerauthentication.yaml) | `PeerAuthentication` | Mesh-wide STRICT mTLS |
| [41-authorizationpolicy.yaml](k8/41-authorizationpolicy.yaml) | `AuthorizationPolicy` | Identity allowlist on the gateway, implements the matrix above |
| [42-peerauthentication-client-secure-permissive.yaml](k8/42-peerauthentication-client-secure-permissive.yaml) | `PeerAuthentication` | Namespace override so `client-secure`'s Route (bypasses the mesh) keeps working under mesh-wide STRICT — see Issue #1 |
| [43-peerauthentication-client-2-permissive.yaml](k8/43-peerauthentication-client-2-permissive.yaml) | `PeerAuthentication` | Same override, for `client-2`'s Route |

Apply everything:

```bash
oc apply -f k8/
```

`external-api-2` (west cluster) and `client-2` (east cluster) are separate
application deployments, not part of this `k8/` directory — they're
ordinary Deployment/Service/Route, built the same way as `external-api`
and `client-secure` (see root [README.md](../README.md)).

## Issues hit while standing this up (and fixes)

All of these were real failures encountered applying this to the live
cluster, not anticipated up front — noted here since they're the kind of
thing anyone following the ADR's model will likely hit too:

1. **502 from `client-secure`'s Route after applying mesh-wide `STRICT`
   mTLS.** The client's OpenShift Route bypasses the mesh entirely — it
   hits the Service/pod directly, not through the Istio ingress gateway —
   so the OpenShift router speaks plaintext to the pod. Under `STRICT`,
   the client's own sidecar rejected that inbound connection. Fixed with
   a namespace-scoped `PeerAuthentication` override back to `PERMISSIVE`
   for inbound only
   ([42-peerauthentication-client-secure-permissive.yaml](k8/42-peerauthentication-client-secure-permissive.yaml),
   later [43](k8/43-peerauthentication-client-2-permissive.yaml) for
   `client-2` too). Doesn't weaken Hop 1 — that's governed by the
   DestinationRule's `tls.mode` on outbound traffic, unaffected by
   inbound `PeerAuthentication`.

2. **503 on every API call after fixing #1** — connection-level failure
   (`cx_connect_fail`) from the client's sidecar to the egress gateway.
   Root cause: the Hop 1 `DestinationRule` didn't set an explicit `sni`.
   Istio's HTTPS-protocol `Gateway` listeners use SNI-based filter chain
   matching, and the default auto-generated SNI for a plain
   `ISTIO_MUTUAL` `DestinationRule` doesn't match the Gateway's declared
   `hosts` — so the gateway never matched a filter chain for the
   connection and silently dropped it. Fixed by setting `tls.sni`
   explicitly in
   [30-destinationrule-hop1-mtls.yaml](k8/30-destinationrule-hop1-mtls.yaml),
   matching one of the Gateway's `hosts` (only one value is needed even
   with two external hosts now — see "Why Hop 1 is one shared rule").

3. **Prometheus target `down` for the gateway** (`connection reset by
   peer` on port 443) after adding [01-podmonitor.yaml](k8/01-podmonitor.yaml).
   The gateway Deployment originally declared an explicit
   `containerPort: 443` on the `istio-proxy` container (for readability).
   Kubernetes service discovery creates one Prometheus target per declared
   container port, so this produced a second target pointed straight at
   the gateway's actual mTLS listener instead of its `/stats/prometheus`
   endpoint. Fixed by removing the explicit `containerPort` from
   [10-egress-gateway-workload.yaml](k8/10-egress-gateway-workload.yaml)
   — it was never functionally necessary; the real listener config comes
   from the `Gateway` CR via xDS, not the container's declared ports.

4. **Rebuilding the client image failed repeatedly** with `connection
   reset by peer` pulling `registry.access.redhat.com` — consistently,
   not transient. Root cause: `client-secure`'s namespace has
   `istio-injection: enabled`, so the Build pod itself got a sidecar
   injected, and the cluster's own `outboundTrafficPolicy.mode:
   REGISTRY_ONLY` (see "Other mesh-wide changes made") correctly blocked
   its pull from a host with no `ServiceEntry` — the egress control
   working exactly as designed, just against a build pod nobody intended
   to route through it. Fixed pragmatically: built the image in a
   separate, non-mesh-injected namespace, then `oc tag`'d the result into
   the ImageStream the client-secure Deployment actually watches. (A more
   permanent fix for a real environment would be excluding Build pods
   from injection, e.g. via `sidecar.istio.io/inject: "false"` on the
   BuildConfig's pod template, rather than building elsewhere each time.)

5. **`client-2` got a clean TLS-layer connection reset to the gateway on
   *every* call — including to the host it was supposed to be allowed
   on.** The Hop 1 `DestinationRule` was originally defined in
   `client-secure`'s namespace, relying on `defaultDestinationRuleExportTo:
   ["*"]` (a mesh-wide default already set on this cluster) to make it
   visible to other callers. That didn't happen: `client-2`'s sidecar had
   no transport socket configured for the egress gateway cluster at all —
   effectively plaintext — confirmed via
   `istioctl proxy-config cluster ... -o json`, and separately confirmed
   NOT to be a stale-proxy-cache issue (`istioctl proxy-status` showed a
   clean `Match`, and a freshly-deleted/recreated pod behaved identically).
   Setting `exportTo: ["*"]` explicitly on the rule didn't change this
   either. What did work, immediately: a namespace-local copy of the same
   rule in `client-2`. Rather than keep two duplicate copies (or debug the
   cross-namespace export gap further), the rule was **consolidated into
   `egress-gateway`'s own namespace** — arguably where it belonged from
   the start, since it describes how to reach the gateway, a property of
   the gateway, not of any particular caller. Both `client-secure` and
   `client-2` pick it up correctly from there. This is left as an
   open, unresolved question about cross-namespace `DestinationRule`
   export on this specific OSSM 3.4 install — worth a second look if it
   recurs elsewhere, but the gateway-namespace placement sidesteps it
   entirely and is arguably the better design regardless.

6. **Manually updating `client-secure`'s Deployment image kept getting
   silently reverted.** `oc set image` appeared to succeed but the pod
   kept running the old image. Cause: the Deployment carries an
   `image.openshift.io/triggers` annotation (from how it was originally
   deployed) locked to the `client-secure:latest` ImageStreamTag — the
   trigger controller kept resetting the image field back to whatever
   that tag resolved to. Fixed by tagging the new build directly into
   `client-secure:latest` (`oc tag ... client-secure/client-secure:latest`)
   instead of fighting the trigger.

7. **Kiali doesn't show the mTLS lock badge on the client → gateway
   edges, even though Hop 1 mTLS is genuinely happening.** Investigated,
   not resolved — documented here as a known limitation rather than a
   fixed issue. Root cause, traced with evidence rather than assumed:
   - The gateway's own `/stats/prometheus` (all of ports 15020, 15090,
     15000) emits **zero** `istio_requests_total` series — confirmed with
     `curl localhost:15020/stats/prometheus | grep -c istio_requests_total`
     → `0`, despite the `istio.stats` filter being present in its listener
     config. The cluster's *other*, platform-managed gateway
     (`prod-gateway-istio` in `ingress-gateway`) shows the identical
     behavior, so this isn't specific to how this demo's gateway was
     built.
   - The calling sidecar's own metric for the same request (queried
     directly from Prometheus) *does* correctly populate
     `source_principal` and `destination_principal` with the real SPIFFE
     identities — proving the identity exchange is real — but
     `connection_security_policy` on that same metric is `"unknown"`, not
     `"mutual_tls"`.
   - Hypothesized that the gateway pod missing the
     `security.istio.io/tlsMode: istio` label (present on regular
     sidecar-injected pods, absent on both this gateway and the
     platform's ingress gateway) was the cause. Tested by adding it, both
     to a live pod and baked into the Deployment template (fresh pod from
     birth) — no change in either case; reverted, since it didn't help
     and mislabeling a gateway as sidecar-style metadata isn't free of
     risk to other tooling that reads that label.
   - Conclusion: this looks like a structural characteristic of how
     Gateway-type Envoy proxies are instrumented for this specific metric
     in this Istio 1.27 / OSSM 3.4 install, not a misconfiguration in
     this demo's resources. A real fix would need deeper Envoy/Istio
     telemetry-filter-level changes beyond standard `Telemetry` API
     configuration — out of proportion to chase further here, given the
     mTLS itself is independently, conclusively proven through other
     evidence (Testing, item 3, below) that doesn't depend on this metric
     at all.

## Testing (all performed against the live cluster)

1. **Gateway pod healthy:** `oc get pods -n egress-gateway` → `1/1 Running`
   — one workload, serving both external hosts.
2. **Full matrix confirmed:**
   - `client-secure` UI shows `OK` for both `external-api` and
     `external-api-2`.
   - `client-2` UI shows `OK` for `external-api-2` and a clean `ERROR`
     for `external-api`; direct `curl` from inside the pod confirms the
     actual codes: `403` (denied) vs. `200` (allowed) — not the ambiguous
     `503` seen in an earlier, superseded negative test (see note below).
3. **Identity propagation proven, not just assumed:** the `/headers`
   response (relayed straight from the real API) includes the gateway's
   own `x-forwarded-client-cert`, whose SPIFFE URI SAN is the *original
   caller's* identity (e.g. `spiffe://cluster.local/ns/client-secure/sa/default`)
   — proof the caller's identity survived Hop 1 and was presented as part
   of Hop 2 — and `x-forwarded-proto: https` / `x-forwarded-port: 443`
   confirming Hop 2 really is TLS, not plaintext relay.
4. **`rbac.allowed`/`rbac.denied`... ** actually enforced, not just
   present, confirmed by the debug-level RBAC log on the gateway proxy
   (`istioctl proxy-config log <pod> -n egress-gateway --level
   rbac:debug`), which shows each request's evaluated principal, SNI, and
   matched policy rule explicitly — this is what surfaced the `client-2`
   Hop 1 gap in Issue #5, and confirmed the `403` for `client-2` →
   `external-api` is a real, attributable policy decision.
5. **`bookinfo` (unrelated pre-existing demo) still works** after the
   mesh-wide `PeerAuthentication` change — `productpage` returns `200`
   through its ingress route.

**Correction to an earlier claim:** an initial negative test used a
temporary pod in the unrelated `bookinfo` namespace and observed a `503`,
which was reported at the time as proof of authorization enforcement. In
hindsight, given Issue #5, that `503` was more likely the *same*
cross-namespace Hop 1 gap (no transport socket, i.e. a broken connection
to the gateway) rather than a clean RBAC decision — `bookinfo` never had
its own Hop 1 `DestinationRule` either, same as `client-2` originally
didn't. The `client-2` test above, run *after* fixing Issue #5, gives an
unambiguous `403` and is the reliable evidence for enforcement; the
`bookinfo` result shouldn't be relied on as having demonstrated the same
thing.
