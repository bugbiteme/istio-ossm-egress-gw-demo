# How the Egress Gateway Works, and How It Maps to ADR-002

This is a narrative explanation of what was built, why, and how it relates
back to `ADR-002-openshift-service-mesh-v2.docx` ("Enable Red Hat OpenShift
Service Mesh to Support a Controlled Egress Gateway"). For the file-by-file
resource list and apply instructions, see [README.md](README.md).

## The goal

The client app (`client/`, running in the `client-secure` namespace on the
`admin-east` OpenShift cluster) needs to call the external API (`api/`,
running on a completely separate cluster, `admin-west`) over the public
internet. ADR-002 says that kind of call — a mesh-enrolled workload
reaching an external system — should go through a single, controlled
**egress gateway**, not be dialed directly by the workload, so that there's
one auditable exit point, centralized policy, and no individual workload
holding its own credentials.

This demo implements that pattern on top of an OpenShift Service Mesh 3.4
(Sail operator) install that was already running on the cluster before
this work started. It was later extended to two clients (`client-secure`,
`client-2`) and two external systems (`external-api`, `external-api-2`),
all through the **same** gateway workload, specifically to demonstrate
that adding another external system or another internal caller is just
more config on the gateway that already exists — not a new gateway per
pair. The access matrix that resulted:

| | `external-api` | `external-api-2` |
|---|---|---|
| `client-secure` | allowed | allowed |
| `client-2` | **denied** | allowed |

`client-2` being denied on `external-api` specifically is the point of
that cell — it's what proves the gateway enforces *which app can reach
which destination*, not just that traffic passes through it.

## The two-hop model, and why it's two hops

The core idea in ADR-002 that everything else hangs off of: there are
**two separate, independently-configured TLS relationships** involved in
one logical "call," not one:

```
 client pod              egress gateway pod           external API
┌──────────────┐  Hop 1  ┌──────────────────┐  Hop 2  ┌─────────────┐
│ app container │────────▶│ Envoy (gateway-   │────────▶│ OpenShift   │
│ (plain HTTP,  │ mesh    │  injected)        │  real   │ Route,      │
│  no TLS code) │ mTLS    │                   │  TLS    │ Let's       │
│ + Envoy       │         │                   │         │ Encrypt cert│
│   sidecar     │         │                   │         │             │
└──────────────┘         └──────────────────┘         └─────────────┘
```

- **Hop 1 (sidecar → gateway)** is internal to the mesh. It's encrypted
  with Istio's own short-lived, istiod-issued SPIFFE certificates — the
  same machinery that secures any service-to-service call in the mesh.
  Because it's Istio's own PKI, the gateway can read the caller's
  cryptographic identity off this connection (`source.principal` — a
  SPIFFE URI like `spiffe://cluster.local/ns/client-secure/sa/default`).
- **Hop 2 (gateway → external API)** is a real TLS connection to a real
  external server, using a certificate that server actually presents
  (Let's Encrypt, in this case). The client application never does this
  handshake itself — the gateway does it *on the client's behalf*. This is
  what "TLS origination" means, and it's why individual workloads in this
  design hold no client credentials: they don't need any, because they
  never speak TLS to the external system directly.

ADR-002 calls out that these two hops are easy to conflate, and that
skipping the explicit configuration of Hop 1 (leaving it at mesh defaults
instead of setting `ISTIO_MUTUAL` explicitly) silently breaks the
identity-based authorization the whole design depends on — the gateway
ends up with no reliable caller identity to check. This turned out to be
true in practice, not just a documentation warning — see "What broke"
below.

## What the client's code changes were: none

`client/app.py` was not modified for any of this. It already read an
`API_SCHEME` environment variable (added earlier, for local dev against a
plain-HTTP local API). Standing up the egress gateway only required
changing that Deployment's env var to `API_SCHEME=http` — the client now
makes a plain HTTP call to the API's hostname, and the mesh/gateway do the
rest. This lines up with the ADR's explicit claim that "application code
changes are not required" for this pattern.

## Walking through the resources, in the order they're applied

All of these live in [k8/](k8/); see [README.md](README.md) for the
one-line purpose of each file. The interesting part is *why* they're
structured this way:

1. **A dedicated `egress-gateway` namespace.** ADR-002 is explicit that in
   OSSM 3 (the Sail operator), the operator only manages the Istio control
   plane (`istiod`) — it does **not** deploy gateways the way OSSM 2's
   operator did. Gateways are ordinary application workloads now, with
   their own lifecycle, so they get their own namespace rather than living
   in `istio-system`.

2. **The gateway itself is a plain `Deployment`/`Service`**, using Istio's
   *gateway injection* (`inject.istio.io/templates: gateway`) rather than
   the normal sidecar injection template. This is the mechanism ADR-002
   describes: "a standard Deployment/Service pair labeled for Istio to
   inject and configure as a gateway proxy." The container image is just
   `auto` — the injection webhook fills in the real Envoy image and
   startup args based on the Gateway resource that selects this workload.

3. **A `Gateway` resource** (`networking.istio.io`) is what actually
   configures that Envoy's listener: port 443, protocol HTTPS, `hosts` set
   to **both** external APIs' FQDNs on the same server block, and
   `tls.mode: ISTIO_MUTUAL`. This is Hop 1's server side. Both hosts share
   one filter chain — see "Why Hop 1 is one shared rule" for why that's
   fine, and doesn't require per-host duplication the way Hop 2 does.

4. **A `ServiceEntry` per external host** registers each as a known host
   in the mesh's service registry. This matters because the cluster's
   `Istio` CR already had `outboundTrafficPolicy.mode: REGISTRY_ONLY` set
   (this was pre-existing — not something this work added) — under that
   mode, only hosts with a matching `ServiceEntry` are reachable at all;
   everything else is blocked outright. ADR-002 flags this explicitly as
   a setting that's easy to assume is default behavior but isn't (Istio's
   actual default is `ALLOW_ANY`, which would silently defeat the
   allowlist model the whole design relies on). This same mechanism is
   also what blocked a Build pod's registry pull partway through this
   work — see "What broke," below.

5. **A `VirtualService` per external host** ties Hop 1 and Hop 2 together
   by matching on *which gateway the request is currently passing
   through*: traffic arriving from `mesh` (i.e., from any sidecar) on
   port 80 gets routed to the egress gateway's internal `Service`; traffic
   arriving from the `egress-gateway/egressgateway-hop1` Gateway gets
   routed onward to that external host on port 443. Same pattern, two
   `VirtualService` objects (one per external host, since each declares
   its own `hosts:` field), because the "route" is really two hops glued
   together at the gateway workload.

6. **`DestinationRule`s**: one shared for Hop 1, one per external host for
   Hop 2 — because Hop 1's TLS settings are about reaching the gateway
   (same for every caller and every destination), while Hop 2's settings
   are about a specific external destination (different per host):
   - The Hop 1 `DestinationRule` targets the egress gateway's own internal
     `Service`, sets `tls.mode: ISTIO_MUTUAL` with an explicit `sni`, and
     lives in `egress-gateway`'s own namespace — not a client's namespace
     (see "What broke," Issue #5, for why that placement matters, not
     just where it happens to live).
   - Each Hop 2 `DestinationRule` targets one *external API's hostname*
     and sets `tls.mode: SIMPLE` — origination only, not mutual, because
     neither demo API does client-certificate authentication. ADR-002's
     Hop 2 description covers the `MUTUAL` case (with a `credentialName`
     Secret) for a real external system that *does* require a client
     cert; neither demo API does, so `SIMPLE` is the correct instance of
     the same pattern, not a shortcut around it.

7. **`PeerAuthentication` set to `STRICT`, mesh-wide**, in the
   `istio-system` root namespace. This is ADR-002's onboarding baseline
   ("STRICT mTLS for all namespaces"), and it's what makes Hop 1's
   identity guarantee actually mean something — without mesh-wide STRICT,
   a workload could still reach the gateway over plaintext and the whole
   identity story would be optional rather than enforced.

8. **An `AuthorizationPolicy`** on the gateway workload, implementing the
   access matrix at the top of this doc. This is the payoff of Hop 1 being
   explicit: the policy checks `source.principals` against the SPIFFE
   identity that Hop 1's mTLS handshake established, combined with
   `to.operation.hosts` for the destination. ADR-002's onboarding model
   calls for a mesh-wide deny-all baseline; this demo scopes the `ALLOW`
   rules to just the gateway workload instead (see "Deviations from the
   ADR, deliberately" below).

9. **A `PodMonitor`** scraping `istio-proxy`'s `/stats/prometheus` in both
   `client-secure` and `egress-gateway` — the ADR's "Metrics via OpenShift
   user-workload monitoring (Prometheus)" piece. (Added directly to the
   cluster and repo, not part of the original egress design — see
   [README.md](README.md)'s resource table.)

## Why Hop 1 is one shared rule, not one per external host

It's tempting to assume each external host needs its own Hop 1
`DestinationRule`/SNI, mirroring the fact that each needs its own Hop 2
one. It doesn't. SNI's only job at Hop 1 is selecting which filter chain
terminates the gateway's *own* mTLS listener — it has nothing to do with
the ultimate destination. Both external hosts are declared on the same
`Gateway` server block, so they share one filter chain regardless of
which of the two a given request is actually headed for. The routing to
the *correct* external system happens afterward, at the HTTP layer, via
the `Host` header — which is exactly what the per-host `VirtualService`
objects match on — and that header survives the mTLS tunnel intact
regardless of which SNI value got the connection through the door. One
gateway workload, one shared Hop 1 rule, N per-host Hop 2 rules and
routes: that asymmetry is the shape of "one gateway serves many external
systems" made concrete.

## What broke, and what that revealed about the ADR's warnings

Several real failures came up applying this to the live cluster — all are
documented in detail in [README.md](README.md)'s "Issues hit" section, but
the first two are worth calling out here because each is a concrete
instance of something ADR-002 warns about in the abstract:

- **The client's own Route broke (502) the moment mesh-wide `STRICT` was
  applied.** The client's Route bypasses the mesh (it hits the pod
  directly, not through the Istio ingress gateway), so the OpenShift
  router's plaintext connection got rejected by the client's own sidecar.
  This is exactly the kind of interaction ADR-002 doesn't spell out
  explicitly but that "STRICT mTLS for all namespaces" implies: STRICT
  applies to *inbound* traffic to every mesh-enrolled workload, including
  from non-mesh sources. Fixed with a namespace-scoped `PERMISSIVE`
  override for the client's inbound traffic only — Hop 1's outbound
  guarantee is untouched, since that's governed by the DestinationRule,
  not by this.

- **Hop 1 failed outright (`cx_connect_fail`) until the client-side
  `DestinationRule` had an explicit `sni`.** This is a direct, literal
  instance of the exact failure mode ADR-002 warns about for Hop 1: "If
  this is left at defaults, the gateway cannot cryptographically identify
  the calling workload." The specific mechanism (SNI-based filter chain
  matching on the `Gateway` listener not lining up with the
  auto-generated SNI on a plain `ISTIO_MUTUAL` DestinationRule) is more
  specific than what the ADR spells out, but the category of failure —
  Hop 1 silently not working the way you'd assume — is precisely what the
  ADR's "Silent policy gap" risk describes.

Two more issues showed up extending this to a second client and second
external system, both purely operational rather than ADR-related, and
both detailed in README.md: the mesh's own `REGISTRY_ONLY` policy blocking
a Build pod's registry pull (Issue #4 — the egress control working
correctly, just against a pod nobody meant to route through it), and a
cross-namespace `DestinationRule` export gap that left `client-2` with no
transport socket to the gateway at all (Issue #5) — which is what led to
consolidating the Hop 1 rule into the gateway's own namespace rather than
a caller's, described above.

## Verifying this is real, not just applied

Everything below was actually checked against the running cluster, not
assumed from the YAML:

- The `/headers` response from the real API — relayed back through the
  client's UI — includes the gateway's `x-forwarded-client-cert` header,
  whose SPIFFE URI **is the original client's identity**
  (`spiffe://cluster.local/ns/client-secure/sa/default`), not the
  gateway's own. That's Hop 1's identity surviving all the way through
  Hop 2, presented on the real outbound connection.
- `x-forwarded-proto: https` and `x-forwarded-port: 443` on that same
  response confirm Hop 2 really is TLS to the real API, not a plaintext
  relay.
- The `AuthorizationPolicy`'s `rbac.allowed` counter on the gateway
  increments exactly in step with successful calls.
- **`client-2` gets a clean, attributable `403`** calling `external-api`
  (denied) versus `200` calling `external-api-2` (allowed) — confirmed via
  direct `curl` from inside the pod, and cross-checked against the
  gateway's own debug-level RBAC log, which shows the exact principal,
  SNI, and matched policy rule per request. This is the reliable evidence
  for enforcement. An earlier negative test (a temporary pod in the
  unrelated `bookinfo` namespace, before the Hop 1 fix described above)
  had observed a `503` and was reported at the time as proof of
  enforcement — in hindsight that was more likely the same cross-namespace
  Hop 1 gap (`bookinfo` never had its own Hop 1 rule either) rather than a
  clean RBAC decision. Worth knowing if that earlier result is cited
  anywhere: it shouldn't be relied on the same way the `client-2` result
  can be.
- The pre-existing `bookinfo` demo still works after the mesh-wide
  `PeerAuthentication` change, so this didn't regress unrelated workloads
  on the shared cluster.
- Kiali's traffic graph shows the actual path — `client-secure` →
  `egressgateway` → `external-api` as an external node — now that the
  `PodMonitor` is feeding it data. The mTLS lock badge specifically does
  **not** reliably show on the client → gateway edge, though — traced
  (not just noticed) to the gateway's Envoy proxy emitting zero
  `istio_requests_total` metrics at all, a behavior it shares with the
  cluster's own platform-managed ingress gateway, so it's a structural
  characteristic of Gateway-type proxies in this install rather than a
  misconfiguration here. See README.md Issue #7 for the full
  investigation — the bullet above (SPIFFE identity in
  `x-forwarded-client-cert`) is the reliable way to confirm Hop 1's mTLS,
  not Kiali's badge.

## Deviations from the ADR, deliberately

Two places where this demo doesn't do exactly what ADR-002's broader
platform rollout describes, on purpose, with reasons:

1. **No mesh-wide deny-all `AuthorizationPolicy`.** The ADR's onboarding
   model applies deny-all across every namespace as a platform baseline.
   This demo only adds `ALLOW` rules scoped to the `egressgateway`
   workload (one per allowed caller/destination pair in the access
   matrix). In Istio, that alone is enough to make the gateway
   default-deny for anything not explicitly matched — including the
   `client-2` → `external-api` combination, which has no rule at all — but
   a mesh-wide deny-all would also change authorization posture for
   `bookinfo` and any other namespace on this shared demo cluster, which
   is out of scope for "get egress working for these clients."

2. **Hop 2 is `SIMPLE`, not `MUTUAL`.** The ADR's Hop 2 description
   centers on `MUTUAL` with a `credentialName` Secret, because the
   platform's real external systems require client certificates. This
   demo's external API doesn't perform client-cert authentication, so
   there's no client cert for the gateway to present, and `SIMPLE`
   (server-authenticated only) is the correct configuration for *this*
   external system — not a simplification of the pattern, just the
   pattern applied to a system with different requirements. Extending
   this to a real mTLS-requiring system is a matter of swapping the Hop 2
   `DestinationRule`'s `tls.mode` and adding a `credentialName` Secret —
   no other part of this design changes.

Everything else — the dedicated gateway namespace, gateway injection
instead of operator-managed deployment, the explicit two-hop TLS split,
`REGISTRY_ONLY` as an explicit setting, mesh-wide `STRICT` as the
onboarding baseline, identity-based authorization on the gateway — follows
ADR-002's model directly.
