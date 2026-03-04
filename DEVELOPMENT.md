# Development

## Prerequisites

- Python 3.9+
- `podman` or `docker`
- Access to a container registry

## Project structure

| File                 | Description                                              |
|----------------------|----------------------------------------------------------|
| `wait_for_console.py`| Main hook script -- connects to KubeVirt serial console  |
| `Containerfile`      | Container image build (UBI 9 minimal base)               |
| `Makefile`           | Build and push targets for the container image           |
| `requirements.txt`   | Python dependencies (`pyyaml`, `websocket-client`)       |
| `hook.yml`           | MTV Hook resource referencing the container image        |
| `rbac.yml`           | ServiceAccount, Role, and RoleBinding for the hook pod   |
| `configmap.yml`      | ConfigMap with the guest-side PowerShell signal script    |
| `workload.yml`       | Example MTV workload file (for reference / local testing)|
| `plan.yml`           | Example MTV plan file (for reference / local testing)    |

## Building the image

```bash
make build
make push
```

Override defaults as needed:

```bash
make build IMAGE_REGISTRY=quay.io IMAGE_ORG=mnecas0 IMAGE_TAG=v1.0
make push  CONTAINER_CMD=docker
```

| Variable         | Default          |
|------------------|------------------|
| `IMAGE_REGISTRY` | `quay.io`        |
| `IMAGE_ORG`      | `kubev2v`        |
| `IMAGE_NAME`     | `mtv-wait-hook`  |
| `IMAGE_TAG`      | `latest`         |
| `CONTAINER_CMD`  | `podman`         |
| `NAMESPACE`      | `openshift-mtv`  |

The image is based on `registry.access.redhat.com/ubi9/ubi-minimal` and
installs only `python3`, `pyyaml`, and `websocket-client` to keep it small.

## RBAC details

The hook pod requires the following Kubernetes permissions:

| API Group                    | Resource                          | Verbs        | Reason                              |
|------------------------------|-----------------------------------|--------------|-------------------------------------|
| `subresources.kubevirt.io`   | `virtualmachineinstances/console` | `get`        | Open WebSocket to serial console    |
| `kubevirt.io`                | `virtualmachines`                 | `get`, `list`| Find VM by `vmID` label, check runStrategy |
| `kubevirt.io`                | `virtualmachineinstances`         | `get`, `list`| Read VMI status for reboot detection |

These are scoped to a single namespace via a `Role` / `RoleBinding`. All YAML
manifests use `${NAMESPACE}` as a placeholder -- use `make deploy NAMESPACE=<ns>`
to apply them. See `rbac.yml` for the full manifests.

## How `wait_for_console.py` works

1. **Parse MTV data** -- Reads the VM ID from `vm.vm1.vm0.id` in
   `/tmp/hook/workload.yml` and the target namespace from `targetnamespace` in
   `/tmp/hook/plan.yml`. Both paths are overridable via environment variables.

2. **Authenticate** -- Reads the service account bearer token from
   `/var/run/secrets/kubernetes.io/serviceaccount/token` and the cluster CA
   from the corresponding `ca.crt`.

3. **Find the VM** -- Lists `VirtualMachine` resources filtered by label
   `vmID=<id>` using a standard HTTPS GET to the KubeVirt API. Checks
   `runStrategy` and aborts if `Halted`. No external Kubernetes client
   library is needed.

4. **Connect to serial console** -- Opens a WebSocket to the KubeVirt
   subresource endpoint:
   ```
   wss://<host>:<port>/apis/subresources.kubevirt.io/v1/namespaces/<ns>/virtualmachineinstances/<name>/console
   ```
   using the `plain.kubevirt.io` subprotocol.

5. **Wait for signal** -- Reads data from the WebSocket, buffering the tail to
   handle the signal string arriving across chunk boundaries. On match, exits
   `0`. On timeout (default 1800 s), exits `1`.

## Guest-side script

The ConfigMap in `configmap.yml` contains a PowerShell firstboot script:

```
99_win_firstboot_signal_conversion_done.ps1
```

This follows the MTV `customizationScripts` naming convention:

```
[0-9]+_win_firstboot_[description_text].ps1
```

- **`99`** -- execution order (runs last, after other firstboot scripts)
- **`win`** -- targets Windows guests
- **`firstboot`** -- runs on the first boot after conversion

The script opens COM1 via `System.IO.Ports.SerialPort` and writes the signal
string (`CONVERSION_DONE`). The hook pod on the other end of the serial console
detects this and exits successfully.

## Running locally (dry-run)

You can test the YAML parsing locally without a cluster:

```bash
pip install -r requirements.txt
WORKLOAD_PATH=workload.yml PLAN_PATH=plan.yml python3 -c "
from wait_for_console import load_yaml, get_vm_id, get_namespace
w = load_yaml('workload.yml')
p = load_yaml('plan.yml')
print(f'VM ID:     {get_vm_id(w)}')
print(f'Namespace: {get_namespace(p)}')
"
```

The WebSocket connection itself requires a running KubeVirt cluster.
