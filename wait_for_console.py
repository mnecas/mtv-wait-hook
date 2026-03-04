#!/usr/bin/env python3

from __future__ import annotations

import base64
import json
import os
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
from typing import Any, Optional

import yaml
import websocket

WORKLOAD_PATH = os.environ.get("WORKLOAD_PATH", "/tmp/hook/workload.yml")
PLAN_PATH = os.environ.get("PLAN_PATH", "/tmp/hook/plan.yml")
SIGNAL_STRING = os.environ.get("SIGNAL", "CONVERSION_DONE")
TIMEOUT = int(os.environ.get("TIMEOUT", "1800"))
WAIT_FOR_REBOOT = os.environ.get("WAIT_FOR_REBOOT", "true").lower() == "true"
REBOOT_POLL_INTERVAL = int(os.environ.get("REBOOT_POLL_INTERVAL", "5"))

SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
SA_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"


def load_yaml(path: str) -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f)


def _find_by_name(items: list[dict], name: str, kind: str) -> dict:
    """Find an item by name in a kubeconfig list, raising a clear error on miss."""
    for item in items:
        if item["name"] == name:
            return item
    raise RuntimeError(f"{kind} '{name}' not found in kubeconfig")


def load_kube_auth() -> tuple[str, str, Optional[str]]:
    """Return (api_base, token, ca_path) using in-cluster config or local kubeconfig."""
    if os.path.exists(SA_TOKEN_PATH):
        with open(SA_TOKEN_PATH) as f:
            token = f.read().strip()
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = os.environ["KUBERNETES_SERVICE_PORT"]
        return f"https://{host}:{port}", token, SA_CA_PATH

    print("Not running in-cluster, loading kubeconfig...")
    kubeconfig_path = os.environ.get(
        "KUBECONFIG", os.path.expanduser("~/.kube/config")
    )
    config = load_yaml(kubeconfig_path)
    current = config["current-context"]
    context = _find_by_name(config["contexts"], current, "context")["context"]
    cluster = _find_by_name(
        config["clusters"], context["cluster"], "cluster"
    )["cluster"]
    user = _find_by_name(config["users"], context["user"], "user")["user"]

    api_base = cluster["server"].rstrip("/")

    ca_path = None
    if "certificate-authority" in cluster:
        ca_path = cluster["certificate-authority"]
    elif "certificate-authority-data" in cluster:
        ca_data = base64.b64decode(cluster["certificate-authority-data"])
        ca_file = tempfile.NamedTemporaryFile(delete=False, suffix=".crt")
        ca_file.write(ca_data)
        ca_file.close()
        ca_path = ca_file.name

    if "token" in user:
        token = user["token"]
    elif "exec" in user:
        result = subprocess.run(
            [user["exec"]["command"]] + user["exec"].get("args", []),
            capture_output=True, text=True, check=True,
        )
        exec_cred = json.loads(result.stdout)
        token = exec_cred["status"]["token"]
    else:
        raise RuntimeError(
            "No token or exec credential found in kubeconfig. "
            "Set KUBE_TOKEN env var as a fallback."
        )

    token = os.environ.get("KUBE_TOKEN", token)
    print(f"Using kubeconfig context '{current}', server {api_base}")
    return api_base, token, ca_path


def get_vm_id(workload: dict) -> str:
    return workload["vm"]["vm1"]["vm0"]["id"]


def get_namespace(plan: dict) -> str:
    return plan["targetnamespace"]


class K8sClient:
    """Thin wrapper around the Kubernetes API that carries auth state."""

    def __init__(self, api_base: str, token: str, ca_path: Optional[str]) -> None:
        self.api_base = api_base
        self.token = token
        self.ca_path = ca_path

    def _ssl_context(self) -> ssl.SSLContext:
        if self.ca_path:
            return ssl.create_default_context(cafile=self.ca_path)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    @property
    def ws_sslopt(self) -> dict[str, Any]:
        """SSL options dict for the websocket-client library."""
        if self.ca_path:
            return {"ca_certs": self.ca_path, "cert_reqs": ssl.CERT_REQUIRED}
        return {"cert_reqs": ssl.CERT_NONE}

    def get(self, url: str) -> dict:
        """Perform an authenticated GET against the Kubernetes API."""
        ctx = self._ssl_context()
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self.token}"}
        )
        with urllib.request.urlopen(req, context=ctx) as resp:
            return json.loads(resp.read())

    def find_vm_by_label(self, namespace: str, vm_id: str) -> str:
        """Find a VirtualMachine by label vmID=<vm_id> and return its name."""
        url = (
            f"{self.api_base}/apis/kubevirt.io/v1"
            f"/namespaces/{namespace}"
            f"/virtualmachines?labelSelector=vmID={vm_id}"
        )
        data = self.get(url)
        items = data.get("items", [])
        if not items:
            raise RuntimeError(
                f"No VM found with label vmID={vm_id} in namespace {namespace}"
            )
        name = items[0]["metadata"]["name"]
        print(f"Found VM '{name}' matching label vmID={vm_id}")
        return name

    def get_vmi_status(self, namespace: str, vmi_name: str) -> dict:
        """Return the full VMI status dict."""
        url = (
            f"{self.api_base}/apis/kubevirt.io/v1"
            f"/namespaces/{namespace}"
            f"/virtualmachineinstances/{vmi_name}"
        )
        return self.get(url).get("status", {})

    def get_vmi_condition(
        self, namespace: str, vmi_name: str, condition_type: str
    ) -> Optional[str]:
        """Return the status string of a VMI condition, or None if not present."""
        status = self.get_vmi_status(namespace, vmi_name)
        for cond in status.get("conditions", []):
            if cond.get("type") == condition_type:
                return cond.get("status")
        return None

    def get_vm_run_strategy(self, namespace: str, vm_name: str) -> Optional[str]:
        """Return the runStrategy from the VirtualMachine spec."""
        url = (
            f"{self.api_base}/apis/kubevirt.io/v1"
            f"/namespaces/{namespace}"
            f"/virtualmachines/{vm_name}"
        )
        return self.get(url).get("spec", {}).get("runStrategy")

    def check_vm_should_run(self, namespace: str, vm_name: str) -> None:
        """Check the VM runStrategy and abort early if the VM is not meant to run."""
        strategy = self.get_vm_run_strategy(namespace, vm_name)
        print(f"VM '{vm_name}' runStrategy: {strategy}")
        if strategy == "Halted":
            print("VM runStrategy is Halted -- nothing to wait for.")
            sys.exit(0)


def connect_console_ws(
    client: K8sClient, namespace: str, vm_name: str, deadline: float
) -> websocket.WebSocket:
    """Connect to the VMI serial console, retrying until the VMI is running."""
    ws_url = client.api_base.replace("https://", "wss://") + (
        f"/apis/subresources.kubevirt.io/v1"
        f"/namespaces/{namespace}"
        f"/virtualmachineinstances/{vm_name}/console"
    )
    print(f"Connecting to serial console: {ws_url}")

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        try:
            return websocket.create_connection(
                ws_url,
                header=[f"Authorization: Bearer {client.token}"],
                sslopt=client.ws_sslopt,
                subprotocols=["plain.kubevirt.io"],
            )
        except websocket.WebSocketBadStatusException as e:
            if "VMI is not running" in str(e):
                print("VMI is not running yet, retrying...")
                time.sleep(min(REBOOT_POLL_INTERVAL, remaining))
            else:
                raise


def wait_for_signal(ws: websocket.WebSocket, deadline: float) -> None:
    """Read from the WebSocket until SIGNAL_STRING appears or timeout."""
    print(f"Connected. Waiting for '{SIGNAL_STRING}' (timeout {TIMEOUT}s)...")
    buf = ""
    keep = max(len(SIGNAL_STRING) * 2, 4096)

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError

        ws.settimeout(remaining)
        try:
            data = ws.recv()
        except websocket.WebSocketTimeoutException:
            raise TimeoutError

        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")

        buf += data
        if len(buf) > keep:
            buf = buf[-keep:]

        if SIGNAL_STRING in buf:
            print(f"Signal '{SIGNAL_STRING}' received.")
            return


def wait_for_reboot(
    client: K8sClient, namespace: str, vm_name: str, deadline: float
) -> None:
    """Poll VMI status waiting for AgentConnected to cycle True->False->True."""
    print(
        f"Polling VMI '{vm_name}' for reboot "
        f"(poll every {REBOOT_POLL_INTERVAL}s)..."
    )

    agent_was_connected = client.get_vmi_condition(
        namespace, vm_name, "AgentConnected"
    )
    if agent_was_connected != "True":
        print(
            "Warning: AgentConnected is not True -- guest agent may not be installed. "
            "Cannot detect reboot via API, skipping."
        )
        return

    agent_lost = False

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError

        time.sleep(min(REBOOT_POLL_INTERVAL, remaining))

        status = client.get_vmi_condition(namespace, vm_name, "AgentConnected")

        if not agent_lost and status != "True":
            agent_lost = True
            print("AgentConnected lost -- VM is rebooting.")

        if agent_lost and status == "True":
            print("AgentConnected restored -- VM has rebooted successfully.")
            return


def main() -> None:
    workload = load_yaml(WORKLOAD_PATH)
    plan = load_yaml(PLAN_PATH)

    vm_id = get_vm_id(workload)
    namespace = get_namespace(plan)
    print(f"VM ID: {vm_id}, namespace: {namespace}")

    api_base, token, ca_path = load_kube_auth()
    client = K8sClient(api_base, token, ca_path)

    vm_name = client.find_vm_by_label(namespace, vm_id)
    client.check_vm_should_run(namespace, vm_name)

    deadline = time.monotonic() + TIMEOUT

    ws = connect_console_ws(client, namespace, vm_name, deadline)
    try:
        wait_for_signal(ws, deadline)
    finally:
        ws.close()

    if WAIT_FOR_REBOOT:
        wait_for_reboot(client, namespace, vm_name, deadline)


if __name__ == "__main__":
    try:
        main()
    except TimeoutError:
        print(f"Timed out after {TIMEOUT}s.")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
