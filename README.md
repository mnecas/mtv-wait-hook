# MTV Wait Hook

A post-migration hook for the Migration Toolkit for Virtualization (MTV) that
monitors a KubeVirt VM's serial console and waits for a signal from inside the
guest before allowing the migration pipeline to continue.

This is useful when guest-side work (driver installation, firstboot scripts,
etc.) must finish before MTV marks the migration as complete.

## How it works

```
 ┌────────────┐    injects PS1     ┌────────────┐   "CONVERSION_DONE"   ┌────────────┐
 │ Conversion │ ──────────────────►│ Windows VM │ ─────────────────────►│  Hook Pod  │
 │ Pod        │   into guest disk  │ (KubeVirt) │   via COM1 serial     │ (PostHook) │
 └────────────┘                    └────────────┘                       └─────┬──────┘
       ▲                                                                      │
       │                         MTV Migration Plan                           ▼
       └──────────────── customizationScripts + hooks ───────────────► exits 0 or 1
```

## Quick start

### 1. Deploy the Hook and RBAC (openshift-mtv)

The ServiceAccount, ClusterRole/ClusterRoleBinding, Hook resource all live in
`openshift-mtv`:

```bash
make deploy-hook
```

### 2. Deploy the ConfigMap (target namespace)

The customization scripts ConfigMap goes in the target namespace where your VMs
are migrated to:

```bash
make deploy-target NAMESPACE=default
```

Or deploy everything at once:

```bash
make deploy NAMESPACE=default
```

### 3. Configure the Migration Plan

Reference the hook and the customization scripts ConfigMap in your MTV
`Plan`:

```yaml
apiVersion: forklift.konveyor.io/v1beta1
kind: Plan
metadata:
  name: my-migration-plan
  namespace: openshift-mtv
spec:
  # ... provider, network/storage maps, etc.
  targetNamespace: default
  targetPowerState: 'on'
  customizationScripts:
    name: mtv-wait-hook-scripts
  vms:
    - id: vm-1003
      name: my-windows-vm
      hooks:
        - step: PostHook
          hook:
            namespace: openshift-mtv
            name: mtv-wait-hook
```

The `customizationScripts` field references the ConfigMap from step 2.
MTV mounts the scripts at `/mnt/dynamic_scripts` in the conversion pod and
executes them via `virt-customize`. The number prefix in the script key
determines execution order.

## Configuration

| Variable              | Default               | Description                                              |
|-----------------------|-----------------------|----------------------------------------------------------|
| `SIGNAL`              | `CONVERSION_DONE`     | String to watch for on the serial console                |
| `TIMEOUT`             | `1800`                 | Seconds to wait before timing out (exit 1)               |
| `WAIT_FOR_REBOOT`     | `true`                | After signal, poll VMI status until reboot completes     |
| `REBOOT_POLL_INTERVAL`| `5`                   | Seconds between VMI status polls during reboot detection |
| `WORKLOAD_PATH`       | `/tmp/hook/workload.yml` | Path to the MTV workload file                         |
| `PLAN_PATH`           | `/tmp/hook/plan.yml`  | Path to the MTV plan file                                |

## Development

See [DEVELOPMENT.md](DEVELOPMENT.md) for build instructions, file layout, and
implementation details.
