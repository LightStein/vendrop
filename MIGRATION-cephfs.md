# Migrating the vendrop PVC off `thin` onto CephFS

`deployment.yaml` used to declare the data claim with no `storageClassName`,
so it bound to the cluster default. On the ANB clusters that default is
`thin`, the in-tree vSphere provisioner - the same class behind the nine-day
stuck detach in July 2026, where vCenter would not release the volume and the
pod could not be rescheduled.

v1.0.4 replaces it with `vendrop-data-cephfs` on
`ocs-storagecluster-cephfs`, ReadWriteMany.

**`storageClassName` and `accessModes` are immutable on a bound PVC.** The
existing claim cannot be patched onto the new class, and applying the new
`deployment.yaml` over a running vendrop will start it against an *empty*
volume. The two state files have to be copied across by hand.

The state is small - two files:

| file | contents | losing it means |
|---|---|---|
| `/data/projects.json` | project definitions and per-vendor upload tokens | every vendor upload link stops working and has to be reissued |
| `/data/audit.jsonl` | one line per upload attempt | the audit trail is gone |

## Why this procedure copies through `oc exec` and not a copy pod

The obvious approach - scale to 0, mount both claims in a throwaway pod,
`cp -a` - requires detaching the old `thin` volume and attaching it somewhere
new. That detach is the exact operation that failed in July. Doing it for the
sake of two JSON files is not worth the risk.

Streaming the files out of the running pod and back into the new one never
detaches anything. The old volume is only ever released when its PVC is
deleted, at the very end, once the data is safe.

`oc cp` is deliberately not used either: it shells out to `tar` inside the
container. `cat` through `oc exec` has no such dependency.

## Procedure

Run from a machine with `oc` and access to the `devops-tools` namespace.

### 1. Back the state up locally, from the running pod

```
POD=$(oc get pod -n devops-tools -l app=vendrop -o name | head -1)
echo "$POD"

oc exec -n devops-tools $POD -- cat /data/projects.json > vendrop-projects.json
oc exec -n devops-tools $POD -- cat /data/audit.jsonl   > vendrop-audit.jsonl
```

Check them before going any further. `projects.json` must be valid JSON and
must not be empty:

```
python3 -m json.tool vendrop-projects.json > /dev/null && echo "projects.json OK"
wc -l vendrop-audit.jsonl
```

If `projects.json` is empty or invalid, **stop**. Do not continue - you would
be replacing good data with bad.

### 2. Create the new claim

```
oc apply -n devops-tools -f - <<'EOF'
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: vendrop-data-cephfs
  namespace: devops-tools
spec:
  accessModes: [ReadWriteMany]
  storageClassName: ocs-storagecluster-cephfs
  resources:
    requests:
      storage: 1Gi
EOF

oc get pvc -n devops-tools vendrop-data-cephfs
```

Wait for `Bound`. If it stays `Pending`, the storage class name is wrong for
this cluster - check `oc get sc` and fix it before touching the Deployment.

### 3. Switch the Deployment to the new claim

Apply the v1.0.4 manifest, which points at `vendrop-data-cephfs` and sets
`strategy: Recreate`:

```
oc apply -n devops-tools -f deployment.yaml
oc rollout status -n devops-tools deploy/vendrop
```

The pod now comes up against an empty volume. That is expected. vendrop
creates an empty `projects.json` on first use, so the admin UI will show no
projects until step 4 - do not panic and do not start recreating projects by
hand.

### 4. Restore the state into the new volume

```
POD=$(oc get pod -n devops-tools -l app=vendrop -o name | head -1)

oc exec -n devops-tools -i $POD -- sh -c 'cat > /data/projects.json' < vendrop-projects.json
oc exec -n devops-tools -i $POD -- sh -c 'cat > /data/audit.jsonl'   < vendrop-audit.jsonl

oc delete pod -n devops-tools $POD
```

The restart is needed because `projects.json` is read into memory at start.

### 5. Verify before deleting anything

```
oc rollout status -n devops-tools deploy/vendrop

POD=$(oc get pod -n devops-tools -l app=vendrop -o name | head -1)
oc exec -n devops-tools $POD -- ls -la /data
oc exec -n devops-tools $POD -- sh -c 'wc -c /data/projects.json; wc -l /data/audit.jsonl'
```

Then in the admin UI: confirm every project is listed, and confirm one
existing vendor link still loads. A vendor link that 404s means the tokens
did not come across - restore from `vendrop-projects.json` again rather than
reissuing links.

Also confirm the reschedule property that motivated all of this actually
works now, while you still have the old volume as a fallback:

```
oc delete pod -n devops-tools $POD
oc rollout status -n devops-tools deploy/vendrop
```

The replacement pod should reach `Running` without sitting in
`ContainerCreating` waiting for a detach.

### 6. Only then, release the old volume

```
oc get pvc -n devops-tools vendrop-data
oc delete pvc -n devops-tools vendrop-data
```

Keep `vendrop-projects.json` and `vendrop-audit.jsonl` somewhere safe until
you are confident. They are the only copy of the vendor tokens.

If the delete hangs, the old `thin` volume is in the stuck-detach state. It
does not block vendrop - the new pod is already running on CephFS - so it can
be chased with the storage team separately rather than under pressure.

## If something goes wrong mid-migration

Nothing before step 6 is destructive. To go back, point the Deployment's
`claimName` at `vendrop-data` again and apply:

```
oc patch deploy vendrop -n devops-tools --type=json \
  -p '[{"op":"replace","path":"/spec/template/spec/volumes/0/persistentVolumeClaim/claimName","value":"vendrop-data"}]'
```

The old volume still holds the original data, untouched.
