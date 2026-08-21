Deploy the clients on your external cluster

Log into your external cluster and then apply the manifests

```
oc apply -k api/k8s 
```

This uses kustomize to deploy and the hostname for your route can be modified in `kustomization.yaml`

```yaml
patches:
  - target:
      kind: Route
      name: external-api-1
    patch: |-
      - op: replace
        path: /spec/host
        value: external-api-1.apps.cluster-bdvkn.bdvkn.sandbox980.opentlc.com
  - target:
      kind: Route
      name: external-api-2
    patch: |-
      - op: replace
        path: /spec/host
        value: external-api-2.apps.cluster-bdvkn.bdvkn.sandbox980.opentlc.com
```