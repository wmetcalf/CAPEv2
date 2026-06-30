# Rooter Next-Hop Egress Primitive — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a generic, infrastructure-agnostic CAPE/rooter primitive that routes each analysis VM's egress to a selectable **next-hop gateway** over a provided egress interface (route-table injection + source-NAT + fail-closed filtering), with zero knowledge of how the interface is realized.

**Architecture:** A new rooter command set (`nexthop_init` / `nexthop_enable` / `nexthop_disable` / `nexthop_fail_closed_enable` / `nexthop_teardown`) composed from `iproute2` + `iptables`, driven by `[nexthop]`/`[gwX]` profiles in `routing.conf`. `startup.py` loads profiles into a module-global `gateways` dict (alongside `vpns`), arms fail-closed once, and `analysis_manager.route_network` selects a profile per task and binds the VM. A blackhole policy-routing rule + the existing `-P FORWARD DROP` make an unbound VM fail closed (dropped, never leaked to the control-plane NIC).

**Tech Stack:** Python (`utils/rooter.py`, `lib/cuckoo/core/rooter.py`, `lib/cuckoo/core/startup.py`, `lib/cuckoo/core/analysis_manager.py`, `lib/cuckoo/common/config.py` reads), `iproute2`, `iptables`, `conntrack`. pytest + pytest-mock. No new Python deps.

**Source of truth:** design spec `2026-06-29-rooter-nexthop-egress-design.md`; review `2026-06-30-rooter-nexthop-design-review.md` (both in the IaC repo at `../capev2-aws-iac-nestedvirt-private/docs/superpowers/specs/`). Code line numbers are against `feat/central-mode-poc` @ `be8634db` — re-confirm on read.

---

## Locked design decisions (do NOT re-litigate; they resolve the review's open questions)

1. **Ownership = "nexthop owns its rules" (review H2 option b).** The `[nexthop]` resolution branch sets `self.interface = None` so the existing generic block (`analysis_manager.py:632-677`: `libvirt_fwo_enable`/`forward_enable`/`srcroute_enable`) is **skipped**. `nexthop_enable`/`nexthop_disable` issue the source rule, MASQUERADE, and FORWARD ACCEPT themselves. Consequence: `srcroute_enable` is left **byte-for-byte unchanged** (no H1 change needed — we do not reuse it), preserving the no-regress + minimal-fork-delta story.
2. **Per-task priority is deterministic, not stochastic (review H1/M6).** `priority = 10000 + <last octet of vm_ip>`. Rationale: a VM IP runs ≤1 task at a time, so `from <vm_ip>` is already unique; the band `10000–10255` sits **below** the `30000` blackhole (lower number = evaluated first), and clears every reserved priority (0 local, 1000/1001 VRF, 30000 blackhole, 32750 sslproxy, 32765 VRF-local/kernel-auto). `[nexthop]` is documented mutually-exclusive with `no_local_routing`/VRF mode.
3. **Fail-closed = ip-rule blackhole + existing `-P FORWARD DROP` (review M2/L1).** The authoritative drop is the blackhole policy rule over the whole guest subnet (`vm_net`, sourced from `[nexthop] vm_net`), backed by `forward_drop()`'s default `-P FORWARD DROP` (armed at `cuckoo.py:86` before the scheduler starts). We do **not** emit the bogus `! -o <egress-if-group>` rule. (A dedicated `CAPE_EGRESS` chain is explicitly out of scope for v1.)
4. **Teardown is explicit (review B2).** `cleanup_rooter` is iptables-only; iproute2 state has no comment sweep. `nexthop_teardown` flushes profile tables, deletes the blackhole rule+route, and sweeps per-task source rules in the `10000–10255` band. It is called idempotently at startup **before** arming fail-closed and from `handle_sigterm`. All nat/filter rules go through `run_iptables` (CAPE-rooter tag) so `cleanup_rooter` still sweeps them.
5. **Every rooter arg is `str()` at the call site (review B3).** Config coerces `rt_table = 201` to int; the IPC server hangs on a non-str arg. The `[gwX]` loader coerces, and `analysis_manager` wraps every arg in `str()`.
6. **`gateways` + round-robin cursor live in `lib/cuckoo/core/rooter.py` as module globals (review L2/H4).** A startup-local dict would arrive empty in `analysis_manager`.
7. **`[nexthop]` is optional + `hasattr`-guarded; shipped `enabled = no` in `routing.conf.default` (review M4).** No-regress: with `[nexthop] enabled = no`, `route = none|internet|vpnX` behave byte-for-byte as today.

---

## File structure

| File | Responsibility | Action |
|---|---|---|
| `lib/cuckoo/core/rooter.py` | `gateways` dict, round-robin cursor + lock, `_select_gateway()` helper | Modify (globals ~16-17) |
| `utils/rooter.py` | `nexthop_*` command builders + handler registration + SIGTERM wiring | Modify (near 641-650 / 919; handlers 1076-1116; sigterm 1191-1197) |
| `lib/cuckoo/core/startup.py` | `[gwX]` loader, fail-closed arm, validation + `init_rooter` gate threading | Modify (init_routing 532-617; init_rooter 447-459; import 59) |
| `lib/cuckoo/core/analysis_manager.py` | per-task `[nexthop]` resolve + bind + mirror teardown; new `self.nexthop_*` fields | Modify (`__init__` 130-136; route_network 548-677; unroute_network 679-760) |
| `conf/default/routing.conf.default` | ship `[nexthop]` + example `[gw1]` | Modify (after `[vpn0]` ~133) |
| `tests/test_rooter_nexthop.py` | argv-builder + teardown + idempotency unit tests | Create |
| `tests/test_nexthop_selection.py` | `_select_gateway` round-robin/random/explicit/fail-closed + thread-safety | Create |
| `tests/test_route_network_nexthop.py` | route_network/unroute_network branch + str-args + no-regress snapshot | Create |
| `tests/integration/test_nexthop_netns.py` | root-gated netns end-to-end (two profiles, concurrent egress, unbound dropped) | Create |

---

## Task 0: Rooter unit-test scaffolding

There is **no** existing rooter test harness (`tests/test_analysis_manager.py` mocks at the machinery layer; nothing imports `utils.rooter`). Establish the recording-double pattern first.

**Files:**
- Create: `tests/test_rooter_nexthop.py`

- [ ] **Step 1: Write the fixture + a smoke test that the recorder captures argv**

```python
# tests/test_rooter_nexthop.py
import pytest
import utils.rooter as rooter


@pytest.fixture
def rec(monkeypatch):
    """Record every run()/run_iptables() invocation as a list of argv tuples.

    utils.rooter functions reference settings.ip / ServicePaths.ip (both the same
    path at runtime, defined only in __main__), so inject both for unit tests.
    """
    calls = {"run": [], "iptables": []}

    def fake_run(*args):
        calls["run"].append(tuple(str(a) for a in args))
        return ("", "")  # (stdout, stderr); real run() never raises

    def fake_run_iptables(*args, **kwargs):
        calls["iptables"].append(tuple(str(a) for a in args))
        return ("", "")

    class _Settings:
        ip = "ip"
    monkeypatch.setattr(rooter, "settings", _Settings, raising=False)
    monkeypatch.setattr(rooter.ServicePaths, "ip", "ip", raising=False)
    monkeypatch.setattr(rooter.ServicePaths, "iptables", "iptables", raising=False)
    monkeypatch.setattr(rooter, "run", fake_run)
    monkeypatch.setattr(rooter, "run_iptables", fake_run_iptables)
    return calls


def test_recorder_captures(rec):
    rooter.run("ip", "route", "show")
    assert rec["run"] == [("ip", "route", "show")]
```

- [ ] **Step 2: Run it to verify the harness works**

Run: `python -m pytest tests/test_rooter_nexthop.py::test_recorder_captures -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_rooter_nexthop.py
git commit -m "test: rooter argv-recorder scaffolding for nexthop primitive"
```

---

## Task 1: `nexthop_init` — build the profile's routing table

Distinct from `init_rttable` (which only copies an interface's existing routes — review M1). `nexthop_init` flushes the table and installs the single forced default.

**Files:**
- Modify: `utils/rooter.py` (add function after `srcroute_disable` ~line 650; register in `handlers` ~1076-1116)
- Test: `tests/test_rooter_nexthop.py`

- [ ] **Step 1: Write the failing tests (onlink and via)**

```python
def test_nexthop_init_onlink(rec):
    rooter.nexthop_init("201", "ens6", "onlink")
    assert rec["run"] == [
        ("ip", "route", "flush", "table", "201"),
        ("ip", "route", "replace", "default", "dev", "ens6", "onlink", "table", "201"),
    ]


def test_nexthop_init_via(rec):
    rooter.nexthop_init("202", "ens7", "10.30.72.1")
    assert rec["run"] == [
        ("ip", "route", "flush", "table", "202"),
        ("ip", "route", "replace", "default", "via", "10.30.72.1", "dev", "ens7", "table", "202"),
    ]
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_rooter_nexthop.py -k nexthop_init -v`
Expected: FAIL with `AttributeError: module 'utils.rooter' has no attribute 'nexthop_init'`

- [ ] **Step 3: Implement**

```python
# utils/rooter.py — add after srcroute_disable
def nexthop_init(rt_table, egress_if, next_hop):
    """Idempotently build a next-hop profile's routing table: one forced default route.
    next_hop == 'onlink' => default dev egress_if onlink; else default via next_hop dev egress_if.
    NOTE: deliberately does NOT call init_rttable (that copies main's per-interface routes)."""
    run(settings.ip, "route", "flush", "table", rt_table)
    if next_hop == "onlink":
        run(settings.ip, "route", "replace", "default", "dev", egress_if, "onlink", "table", rt_table)
    else:
        run(settings.ip, "route", "replace", "default", "via", next_hop, "dev", egress_if, "table", rt_table)
```

- [ ] **Step 4: Register in the `handlers` dict** (~line 1076-1116, mirroring `"srcroute_enable": srcroute_enable,`)

```python
    "nexthop_init": nexthop_init,
```

- [ ] **Step 5: Run to verify pass**

Run: `python -m pytest tests/test_rooter_nexthop.py -k nexthop_init -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Commit**

```bash
git add utils/rooter.py tests/test_rooter_nexthop.py
git commit -m "feat(rooter): nexthop_init forced-default-route builder"
```

---

## Task 2: `nexthop_enable` — bind a VM to a profile

Pre-clean (idempotent) + conntrack flush + source rule (with explicit priority) + MASQUERADE + FORWARD ACCEPT. nat/filter rules via `run_iptables` (CAPE-rooter tag). conntrack best-effort.

**Files:**
- Modify: `utils/rooter.py`
- Test: `tests/test_rooter_nexthop.py`

- [ ] **Step 1: Write the failing test**

```python
def test_nexthop_enable_argv(rec):
    rooter.nexthop_enable("192.168.100.42", "ens6", "201", "10042")
    # iproute2 + conntrack go through run(); nat/filter through run_iptables()
    assert rec["run"] == [
        ("conntrack", "-D", "-s", "192.168.100.42"),                                  # pre-bind flush
        ("ip", "rule", "del", "from", "192.168.100.42", "lookup", "201", "priority", "10042"),  # idempotent pre-clean
        ("ip", "rule", "add", "from", "192.168.100.42", "lookup", "201", "priority", "10042"),
    ]
    assert rec["iptables"] == [
        ("-t", "nat", "-A", "POSTROUTING", "-s", "192.168.100.42", "-o", "ens6", "-j", "MASQUERADE"),
        ("-A", "FORWARD", "-s", "192.168.100.42", "-o", "ens6", "-j", "ACCEPT"),
    ]
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_rooter_nexthop.py -k nexthop_enable -v`
Expected: FAIL (no attribute `nexthop_enable`)

- [ ] **Step 3: Implement**

```python
# utils/rooter.py
def nexthop_enable(vm_ip, egress_if, rt_table, priority):
    """Per task: source-route the VM into its profile table and SNAT onto egress_if.
    run() never raises, so the pre-clean deletes are safe idempotent no-ops when absent."""
    run("conntrack", "-D", "-s", vm_ip)  # drop stale flows so a recycled IP starts clean (best-effort)
    run(settings.ip, "rule", "del", "from", vm_ip, "lookup", rt_table, "priority", priority)  # idempotent
    run(settings.ip, "rule", "add", "from", vm_ip, "lookup", rt_table, "priority", priority)
    run_iptables("-t", "nat", "-A", "POSTROUTING", "-s", vm_ip, "-o", egress_if, "-j", "MASQUERADE")
    run_iptables("-A", "FORWARD", "-s", vm_ip, "-o", egress_if, "-j", "ACCEPT")
```

- [ ] **Step 4: Register handler** (`"nexthop_enable": nexthop_enable,`)

- [ ] **Step 5: Run to verify pass**

Run: `python -m pytest tests/test_rooter_nexthop.py -k nexthop_enable -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add utils/rooter.py tests/test_rooter_nexthop.py
git commit -m "feat(rooter): nexthop_enable per-task bind (source rule + MASQUERADE + FORWARD)"
```

---

## Task 3: `nexthop_disable` — mirror teardown for one task

Exact mirror of enable (del instead of add/-D instead of -A) + conntrack flush. Symmetry is asserted explicitly.

**Files:**
- Modify: `utils/rooter.py`
- Test: `tests/test_rooter_nexthop.py`

- [ ] **Step 1: Write the failing test (incl. mirror symmetry)**

```python
def test_nexthop_disable_argv(rec):
    rooter.nexthop_disable("192.168.100.42", "ens6", "201", "10042")
    assert rec["run"] == [
        ("ip", "rule", "del", "from", "192.168.100.42", "lookup", "201", "priority", "10042"),
        ("conntrack", "-D", "-s", "192.168.100.42"),
    ]
    assert rec["iptables"] == [
        ("-t", "nat", "-D", "POSTROUTING", "-s", "192.168.100.42", "-o", "ens6", "-j", "MASQUERADE"),
        ("-D", "FORWARD", "-s", "192.168.100.42", "-o", "ens6", "-j", "ACCEPT"),
    ]
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_rooter_nexthop.py -k nexthop_disable -v`
Expected: FAIL

- [ ] **Step 3: Implement**

```python
# utils/rooter.py
def nexthop_disable(vm_ip, egress_if, rt_table, priority):
    """Mirror-delete the per-task state from nexthop_enable, then flush conntrack."""
    run(settings.ip, "rule", "del", "from", vm_ip, "lookup", rt_table, "priority", priority)
    run_iptables("-t", "nat", "-D", "POSTROUTING", "-s", vm_ip, "-o", egress_if, "-j", "MASQUERADE")
    run_iptables("-D", "FORWARD", "-s", vm_ip, "-o", egress_if, "-j", "ACCEPT")
    run("conntrack", "-D", "-s", vm_ip)
```

- [ ] **Step 4: Register handler** (`"nexthop_disable": nexthop_disable,`)

- [ ] **Step 5: Run to verify pass**

Run: `python -m pytest tests/test_rooter_nexthop.py -k nexthop_disable -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add utils/rooter.py tests/test_rooter_nexthop.py
git commit -m "feat(rooter): nexthop_disable mirror teardown + conntrack flush"
```

---

## Task 4: `nexthop_fail_closed_enable` — arm the blackhole (idempotent)

A blackhole default route in a dedicated table + a low-priority rule for the whole guest subnet. Idempotent (pre-delete then add) so a restart does not stack duplicate rules (review B2).

**Files:**
- Modify: `utils/rooter.py`
- Test: `tests/test_rooter_nexthop.py`

- [ ] **Step 1: Write the failing test (argv + idempotency)**

```python
def test_nexthop_fail_closed_argv(rec):
    rooter.nexthop_fail_closed_enable("192.168.100.0/24", "250", "30000")
    assert rec["run"] == [
        ("ip", "route", "replace", "blackhole", "default", "table", "250"),
        ("ip", "rule", "del", "from", "192.168.100.0/24", "lookup", "250", "priority", "30000"),
        ("ip", "rule", "add", "from", "192.168.100.0/24", "lookup", "250", "priority", "30000"),
    ]
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_rooter_nexthop.py -k fail_closed -v`
Expected: FAIL

- [ ] **Step 3: Implement**

```python
# utils/rooter.py
def nexthop_fail_closed_enable(vm_net, fail_table, priority_low):
    """Arm once at startup: any guest-subnet source with no higher-priority per-task rule
    is blackholed (dropped), never routed by main out the control-plane NIC.
    `route replace` is idempotent; the rule is del+add'd so a restart does not stack duplicates."""
    run(settings.ip, "route", "replace", "blackhole", "default", "table", fail_table)
    run(settings.ip, "rule", "del", "from", vm_net, "lookup", fail_table, "priority", priority_low)
    run(settings.ip, "rule", "add", "from", vm_net, "lookup", fail_table, "priority", priority_low)
```

- [ ] **Step 4: Register handler** (`"nexthop_fail_closed_enable": nexthop_fail_closed_enable,`)

- [ ] **Step 5: Run to verify pass** — `python -m pytest tests/test_rooter_nexthop.py -k fail_closed -v` → PASS

- [ ] **Step 6: Commit**

```bash
git add utils/rooter.py tests/test_rooter_nexthop.py
git commit -m "feat(rooter): nexthop_fail_closed_enable idempotent blackhole arm"
```

---

## Task 5: `nexthop_teardown` — sweep all policy-routing state + wire into SIGTERM

`cleanup_rooter` can't reach `ip rule`/`ip route` (review B2). Args are all-string over IPC (`gateway_tables` is a comma-joined string). Sweeps the per-task band by parsing `ip rule show`.

**Files:**
- Modify: `utils/rooter.py` (function near `cleanup_vrf` ~225; register in `handlers`; call in `handle_sigterm` ~1191-1197)
- Test: `tests/test_rooter_nexthop.py`

- [ ] **Step 1: Write the failing test**

```python
def test_nexthop_teardown_sweeps_policy_routing(rec, monkeypatch):
    # Make `ip rule show` return two in-band per-task rules + one out-of-band rule.
    def fake_run(*args):
        rec["run"].append(tuple(str(a) for a in args))
        if args[:3] == ("ip", "rule", "show"):
            return ("10042: from 192.168.100.42 lookup 201\n"
                    "10043: from 192.168.100.43 lookup 202\n"
                    "32766: from all lookup main\n", "")
        return ("", "")
    monkeypatch.setattr(rooter, "run", fake_run)

    rooter.nexthop_teardown("201,202", "192.168.100.0/24", "250", "30000", "10000", "10255")

    assert ("ip", "route", "flush", "table", "201") in rec["run"]
    assert ("ip", "route", "flush", "table", "202") in rec["run"]
    assert ("ip", "route", "del", "blackhole", "default", "table", "250") in rec["run"]
    assert ("ip", "rule", "del", "from", "192.168.100.0/24", "lookup", "250", "priority", "30000") in rec["run"]
    # in-band per-task rules swept; the 32766 main rule untouched
    assert ("ip", "rule", "del", "priority", "10042") in rec["run"]
    assert ("ip", "rule", "del", "priority", "10043") in rec["run"]
    assert ("ip", "rule", "del", "priority", "32766") not in rec["run"]
```

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_rooter_nexthop.py -k teardown -v` → FAIL

- [ ] **Step 3: Implement**

```python
# utils/rooter.py — near cleanup_vrf
def nexthop_teardown(gateway_tables, vm_net, fail_table, priority_low, band_lo, band_hi):
    """Remove ALL nexthop policy-routing state (cleanup_rooter only sweeps iptables).
    gateway_tables: comma-joined table ids. Idempotent; every step is a best-effort run()."""
    for rt in [t for t in gateway_tables.split(",") if t]:
        run(settings.ip, "route", "flush", "table", rt)
    run(settings.ip, "route", "del", "blackhole", "default", "table", fail_table)
    run(settings.ip, "rule", "del", "from", vm_net, "lookup", fail_table, "priority", priority_low)
    lo, hi = int(band_lo), int(band_hi)
    stdout, _ = run(settings.ip, "rule", "show")
    for line in stdout.splitlines():
        head = line.split(":", 1)[0].strip()
        if head.isdigit() and lo <= int(head) <= hi:
            run(settings.ip, "rule", "del", "priority", head)
```

- [ ] **Step 4: Register handler + wire into SIGTERM**

Add `"nexthop_teardown": nexthop_teardown,` to `handlers`. In `handle_sigterm` (~1191-1197), after the existing `cleanup_rooter()` call add (harmless when no profiles loaded — the rule sweep simply matches nothing):

```python
    # remove nexthop policy-routing state (cleanup_rooter is iptables-only)
    nexthop_teardown(GATEWAY_TABLES_CSV, NEXTHOP_VM_NET, NEXTHOP_FAIL_TABLE,
                     NEXTHOP_PRIORITY_LOW, NEXTHOP_BAND_LO, NEXTHOP_BAND_HI)
```

where `GATEWAY_TABLES_CSV` etc. are module-level strings the `[gwX]` loader sets when arming (Task 7). When nexthop is disabled they default to empty/`"0.0.0.0/0"`-safe values so the call is a no-op. (Define these module constants at the top of `utils/rooter.py` next to `ServicePaths` with empty defaults; the loader overwrites them via a small `nexthop_configure` handler — add `nexthop_configure(tables_csv, vm_net, fail_table, prio_low, band_lo, band_hi)` that just assigns the module globals, registered in `handlers`, so the SIGTERM path knows what to sweep.)

- [ ] **Step 5: Add the `nexthop_configure` test + impl**

```python
def test_nexthop_configure_sets_globals(rec):
    rooter.nexthop_configure("201,202", "192.168.100.0/24", "250", "30000", "10000", "10255")
    assert rooter.GATEWAY_TABLES_CSV == "201,202"
    assert rooter.NEXTHOP_VM_NET == "192.168.100.0/24"
    assert rooter.NEXTHOP_FAIL_TABLE == "250"
```

```python
# utils/rooter.py — module globals near ServicePaths
GATEWAY_TABLES_CSV = ""
NEXTHOP_VM_NET = "255.255.255.255/32"   # matches nothing if mis-fired
NEXTHOP_FAIL_TABLE = "250"
NEXTHOP_PRIORITY_LOW = "30000"
NEXTHOP_BAND_LO = "10000"
NEXTHOP_BAND_HI = "10255"

def nexthop_configure(tables_csv, vm_net, fail_table, prio_low, band_lo, band_hi):
    global GATEWAY_TABLES_CSV, NEXTHOP_VM_NET, NEXTHOP_FAIL_TABLE
    global NEXTHOP_PRIORITY_LOW, NEXTHOP_BAND_LO, NEXTHOP_BAND_HI
    GATEWAY_TABLES_CSV, NEXTHOP_VM_NET, NEXTHOP_FAIL_TABLE = tables_csv, vm_net, fail_table
    NEXTHOP_PRIORITY_LOW, NEXTHOP_BAND_LO, NEXTHOP_BAND_HI = prio_low, band_lo, band_hi
```

- [ ] **Step 6: Run to verify pass** — `python -m pytest tests/test_rooter_nexthop.py -k "teardown or configure" -v` → PASS

- [ ] **Step 7: Commit**

```bash
git add utils/rooter.py tests/test_rooter_nexthop.py
git commit -m "feat(rooter): nexthop_teardown policy-routing sweep + SIGTERM wiring + configure"
```

---

## Task 6: `gateways` global + concurrency-safe selector in `lib/cuckoo/core/rooter.py`

Module globals (so `analysis_manager` sees the populated dict — review L2) + a `_select_gateway()` that resolves explicit ids, applies `default_policy` (round-robin/random) over **live** profiles, and returns `None` when the pool is empty/all-down (caller fails closed — review H4).

**Files:**
- Modify: `lib/cuckoo/core/rooter.py` (globals ~16-17)
- Test: `tests/test_nexthop_selection.py` (create)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_nexthop_selection.py
import threading
import lib.cuckoo.core.rooter as core_rooter


class _Profile:
    def __init__(self, name, interface, rt_table, priority):
        self.name, self.interface, self.rt_table, self.priority = name, interface, rt_table, priority


def _seed(monkeypatch, live=("gw1", "gw2", "gw3")):
    gws = {n: _Profile(n, f"ens{6+i}", str(201 + i), 0) for i, n in enumerate(("gw1", "gw2", "gw3"))}
    monkeypatch.setattr(core_rooter, "gateways", gws, raising=False)
    monkeypatch.setattr(core_rooter, "_gw_cursor", 0, raising=False)
    monkeypatch.setattr(core_rooter, "_gw_live", lambda p: p.name in live)  # liveness shim
    return gws


def test_explicit_id_resolves(monkeypatch):
    _seed(monkeypatch)
    assert core_rooter._select_gateway("gw2").name == "gw2"


def test_explicit_id_down_fails_closed(monkeypatch):
    _seed(monkeypatch, live=("gw1", "gw3"))
    assert core_rooter._select_gateway("gw2") is None  # named-but-down => caller drops


def test_roundrobin_cycles_over_live(monkeypatch):
    _seed(monkeypatch, live=("gw1", "gw3"))  # gw2 down
    picks = [core_rooter._select_gateway("roundrobin").name for _ in range(4)]
    assert picks == ["gw1", "gw3", "gw1", "gw3"]


def test_empty_pool_fails_closed(monkeypatch):
    monkeypatch.setattr(core_rooter, "gateways", {}, raising=False)
    assert core_rooter._select_gateway("roundrobin") is None


def test_roundrobin_threadsafe(monkeypatch):
    _seed(monkeypatch)
    out = []
    lock = threading.Lock()

    def worker():
        p = core_rooter._select_gateway("roundrobin")
        with lock:
            out.append(p.name)
    threads = [threading.Thread(target=worker) for _ in range(300)]
    for t in threads: t.start()
    for t in threads: t.join()
    # even distribution across 3 live gateways, no crash
    assert all(out.count(n) == 100 for n in ("gw1", "gw2", "gw3"))
```

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_nexthop_selection.py -v` → FAIL (no `_select_gateway`)

- [ ] **Step 3: Implement**

```python
# lib/cuckoo/core/rooter.py — alongside `vpns = {}` / `socks5s = {}`
import random
import threading

gateways = {}            # profile-id -> profile object (.name/.interface/.rt_table/.priority)
_gw_cursor = 0           # process-global round-robin cursor
_gw_lock = threading.Lock()


def _gw_live(profile):
    """True if the profile's egress interface exists and is up. Delegates to the rooter
    nic_available check; overridable in tests."""
    resp = rooter("nic_available", str(profile.interface))
    return bool(resp and resp.get("output"))


def _select_gateway(route):
    """Resolve a task route to a LIVE gateway profile, or None (=> caller fails closed).
    route: an explicit profile id, or a default_policy token 'roundrobin'/'random'."""
    if not gateways:
        return None
    if route in gateways:
        p = gateways[route]
        return p if _gw_live(p) else None
    live = [gateways[k] for k in gateways if _gw_live(gateways[k])]
    if not live:
        return None
    if route == "random":
        return random.choice(live)
    # roundrobin (default): advance the process-global cursor under the lock
    global _gw_cursor
    with _gw_lock:
        p = live[_gw_cursor % len(live)]
        _gw_cursor += 1
    return p
```

- [ ] **Step 4: Run to verify pass** — `python -m pytest tests/test_nexthop_selection.py -v` → PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add lib/cuckoo/core/rooter.py tests/test_nexthop_selection.py
git commit -m "feat(rooter): gateways global + concurrency-safe fail-closed selector"
```

---

## Task 7: `[gwX]` loader in `startup.init_routing` + arm fail-closed + idempotent re-arm

Mirror the `[vpnX]` loader (`startup.py:550-574`). hasattr-guard `[nexthop]` (review M4). Coerce `rt_table`/priorities to `str` (review B3). Validate gw ids don't collide. Teardown-then-arm so the blackhole doesn't stack (review B2).

**Files:**
- Modify: `lib/cuckoo/core/startup.py` (import line 59; loader after 574)
- Test: covered by Task 8 + a loader unit test here

- [ ] **Step 1: Write the failing test**

```python
# tests/test_nexthop_selection.py (append)
def test_gwx_loader_populates_and_coerces(monkeypatch):
    import lib.cuckoo.core.startup as startup
    import lib.cuckoo.core.rooter as core_rooter
    recorded = []
    monkeypatch.setattr(core_rooter, "gateways", {}, raising=False)
    monkeypatch.setattr(startup, "rooter", lambda cmd, *a, **k: recorded.append((cmd, a)) or {}, raising=False)
    startup.load_nexthop_profiles(_FakeRouting())  # see helper below
    assert "gw1" in core_rooter.gateways
    assert core_rooter.gateways["gw1"].rt_table == "201"   # coerced to str
    assert ("nexthop_init", ("201", "ens6", "onlink")) in recorded
    assert any(c == "nexthop_fail_closed_enable" for c, _ in recorded)
    assert any(c == "nexthop_teardown" for c, _ in recorded)   # re-arm sweep before arm
```

(Add a `_FakeRouting` test helper exposing `.nexthop.enabled=True`, `.nexthop.gateways="gw1"`, `.nexthop.default_policy="roundrobin"`, `.nexthop.fail_closed=True`, `.nexthop.vm_net="192.168.100.0/24"`, and a `gw1` section with `.interface="ens6"`, `.next_hop="onlink"`, `.rt_table=201` (int, to prove coercion), plus a `.get(name)` method.)

- [ ] **Step 2: Run to verify failure** — FAIL (no `load_nexthop_profiles`)

- [ ] **Step 3: Implement** — extract a `load_nexthop_profiles(routing)` function called from `init_routing` after the vpn loop, and a module constant for the band:

```python
# lib/cuckoo/core/startup.py
from lib.cuckoo.core.rooter import rooter, socks5s, vpns, gateways  # add `gateways` (line ~59)

NEXTHOP_FAIL_TABLE = "250"
NEXTHOP_PRIORITY_LOW = "30000"
NEXTHOP_BAND_LO, NEXTHOP_BAND_HI = "10000", "10255"
_RESERVED_ROUTE_NAMES = {"none", "internet", "tor", "inetsim", "drop", "false"}


def load_nexthop_profiles(routing):
    """Parse [nexthop]/[gwX] into the rooter.gateways global, sweep stale state, arm fail-closed.
    No-op when [nexthop] is absent or disabled (review M4 hasattr guard)."""
    if not hasattr(routing, "nexthop") or not routing.nexthop.enabled:
        return
    tables = []
    for name in routing.nexthop.gateways.split(","):
        name = name.strip()
        if not name:
            continue
        if name in _RESERVED_ROUTE_NAMES or name in vpns or name in socks5s or name[:3] == "tun":
            raise CuckooStartupError(f"nexthop gateway id '{name}' collides with a reserved/route name")
        if not hasattr(routing, name):
            raise CuckooStartupError(f"nexthop gateway '{name}' has no [{name}] section in routing.conf")
        entry = routing.get(name)
        entry.rt_table = str(entry.rt_table)   # coerce: config.getint() made it an int (review B3)
        gateways[name] = entry
        tables.append(entry.rt_table)
        rooter("nexthop_init", str(entry.rt_table), str(entry.interface), str(entry.next_hop))
    vm_net = str(routing.nexthop.vm_net)
    tables_csv = ",".join(tables)
    # tell the rooter what to sweep on SIGTERM, then idempotent re-arm: teardown BEFORE arm
    rooter("nexthop_configure", tables_csv, vm_net, NEXTHOP_FAIL_TABLE,
           NEXTHOP_PRIORITY_LOW, NEXTHOP_BAND_LO, NEXTHOP_BAND_HI)
    rooter("nexthop_teardown", tables_csv, vm_net, NEXTHOP_FAIL_TABLE,
           NEXTHOP_PRIORITY_LOW, NEXTHOP_BAND_LO, NEXTHOP_BAND_HI)
    if routing.nexthop.fail_closed:
        rooter("nexthop_fail_closed_enable", vm_net, NEXTHOP_FAIL_TABLE, NEXTHOP_PRIORITY_LOW)
```

Call `load_nexthop_profiles(routing)` from `init_routing` immediately after the `[vpnX]` loop (~line 574, before the default-route validation at 581).

- [ ] **Step 4: Run to verify pass** — `python -m pytest tests/test_nexthop_selection.py -k gwx_loader -v` → PASS

- [ ] **Step 5: Commit**

```bash
git add lib/cuckoo/core/startup.py tests/test_nexthop_selection.py
git commit -m "feat(startup): [gwX] loader, idempotent re-arm, fail-closed arm"
```

---

## Task 8: Thread `nexthop` into startup validation + `init_rooter` gate (review H3)

Accept `route in gateways` as a valid default route; skip the `vpn.enabled` gate for gateway routes; keep the rooter connected for a nexthop-only node.

**Files:**
- Modify: `lib/cuckoo/core/startup.py` (validation 581-590; `init_rooter` early-return 452-459)
- Test: `tests/test_nexthop_selection.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_nexthop_default_route_boots_without_vpn(monkeypatch):
    import lib.cuckoo.core.startup as startup
    # routing.route = "gw1", nexthop enabled, vpn disabled -> must NOT raise
    startup.validate_default_route(_FakeRouting(route="gw1"))  # extracted helper


def test_unknown_gateway_default_route_raises(monkeypatch):
    import lib.cuckoo.core.startup as startup
    from lib.cuckoo.common.exceptions import CuckooStartupError
    with pytest.raises(CuckooStartupError):
        startup.validate_default_route(_FakeRouting(route="gw9"))  # not loaded
```

- [ ] **Step 2: Run to verify failure** — FAIL

- [ ] **Step 3: Implement** — extract the 581-590 block into `validate_default_route(routing)` and add the gateway branch:

```python
# lib/cuckoo/core/startup.py
def validate_default_route(routing):
    route = routing.routing.route
    if route in ("none", "internet", "tor", "inetsim"):
        return
    nexthop_on = hasattr(routing, "nexthop") and routing.nexthop.enabled
    if nexthop_on and route in gateways:
        return  # gateway default route is valid; skip the vpn.enabled gate
    if not routing.vpn.enabled:
        raise CuckooStartupError("A VPN has been configured as default routing interface for VMs, "
                                 "but VPNs have not been enabled in routing.conf")
    if route not in vpns and route not in socks5s:
        raise CuckooStartupError("The VPN/Socks5 defined as default routing target has not been configured")
```

In `init_rooter` (~452-459) add `and not (hasattr(routing, "nexthop") and routing.nexthop.enabled)` to the all-disabled early-return condition so a nexthop-only node still connects + runs `forward_drop()`.

- [ ] **Step 4: Run to verify pass** — `python -m pytest tests/test_nexthop_selection.py -k "default_route or unknown_gateway" -v` → PASS

- [ ] **Step 5: Commit**

```bash
git add lib/cuckoo/core/startup.py tests/test_nexthop_selection.py
git commit -m "feat(startup): accept gateway default route; keep rooter for nexthop-only node"
```

---

## Task 9: `route_network` `[nexthop]` resolve + bind (reviews B1, H2, M5, B3)

Insert the branch **before** the tun-literal (:568) and catch-all else (:571). Resolve via `_select_gateway`; on success persist `self.nexthop_*` and set `self.interface = None` (skip the generic block); on **unresolved id with nexthop enabled, force drop** (never fall through). All rooter args `str()`.

**Files:**
- Modify: `lib/cuckoo/core/analysis_manager.py` (`__init__` 130-136; resolution chain ~567; dispatch ~588-621)
- Test: `tests/test_route_network_nexthop.py` (create)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_route_network_nexthop.py
import pytest
import lib.cuckoo.core.analysis_manager as am


@pytest.fixture
def mgr(monkeypatch):
    """A minimal AnalysisManager with a recording rooter and a fake machine/route."""
    m = am.AnalysisManager.__new__(am.AnalysisManager)
    m.interface = m.rt_table = m.route = None
    m.nexthop_id = m.nexthop_interface = m.nexthop_rt_table = m.nexthop_priority = None
    m.no_local_routing = m.reject_segments = m.reject_hostports = None
    m.rooter_response = ""
    class _Mach: ip = "192.168.100.42"; interface = "virbr0"
    m.machine = _Mach()
    calls = []
    monkeypatch.setattr(am, "rooter", lambda cmd, *a, **k: calls.append((cmd, a)) or {}, raising=False)
    m._calls = calls
    return m


def test_explicit_gateway_binds_and_skips_generic(mgr, monkeypatch):
    import lib.cuckoo.core.rooter as core_rooter
    prof = type("P", (), {"name": "gw1", "interface": "ens6", "rt_table": "201", "priority": 0})()
    monkeypatch.setattr(am, "_select_gateway", lambda r: prof if r == "gw1" else None, raising=False)
    _route(mgr, route="gw1", nexthop_enabled=True)
    # bound to the profile, generic forward/srcroute NOT used
    assert mgr.interface is None                       # generic block skipped (H2 option b)
    assert mgr.nexthop_interface == "ens6"
    assert mgr.nexthop_priority == "10042"             # 10000 + last octet
    cmds = [c for c, _ in mgr._calls]
    assert "nexthop_enable" in cmds
    assert "forward_enable" not in cmds and "srcroute_enable" not in cmds


def test_typo_gateway_fails_closed_to_drop(mgr, monkeypatch):
    monkeypatch.setattr(am, "_select_gateway", lambda r: None, raising=False)
    _route(mgr, route="gw9", nexthop_enabled=True)
    cmds = [c for c, _ in mgr._calls]
    assert "drop_enable" in cmds                        # review B1: never fall through to no-op
    assert "nexthop_enable" not in cmds


def test_all_nexthop_args_are_str(mgr, monkeypatch):
    prof = type("P", (), {"name": "gw1", "interface": "ens6", "rt_table": "201", "priority": 0})()
    monkeypatch.setattr(am, "_select_gateway", lambda r: prof, raising=False)
    _route(mgr, route="gw1", nexthop_enabled=True)
    for cmd, args in mgr._calls:
        if cmd == "nexthop_enable":
            assert all(isinstance(a, str) for a in args)
```

(Provide a small `_route(mgr, route, nexthop_enabled)` helper that sets `mgr.route`, a fake `routing.nexthop.enabled`, and calls the new resolve+bind path — extract the nexthop logic into `AnalysisManager._route_nexthop()` so it is unit-testable without the full `route_network`.)

- [ ] **Step 2: Run to verify failure** — FAIL

- [ ] **Step 3: Implement**

In `__init__` (~130-136) add: `self.nexthop_id = None; self.nexthop_interface = None; self.nexthop_rt_table = None; self.nexthop_priority = None`.

Add the resolution branch in `route_network` **before** the `elif self.route[:3] == "tun"` at :568 (guarded, calls an extracted helper):

```python
        elif _nexthop_enabled() and self._resolve_nexthop():
            # _resolve_nexthop bound the profile (set self.nexthop_*) and set self.interface=None
            pass
```

Helper methods:

```python
def _nexthop_enabled():
    return hasattr(routing, "nexthop") and routing.nexthop.enabled


class AnalysisManager(...):
    def _resolve_nexthop(self):
        """Resolve self.route to a live gateway. Returns True if it OWNS this route
        (bound, or forced-drop). Sets self.interface=None so the generic block is skipped."""
        policy = routing.nexthop.default_policy
        sel = self.route if self.route in gateways else policy
        profile = _select_gateway(sel)
        self.interface = None       # nexthop owns enable/disable; skip generic block (H2 option b)
        self.rt_table = None
        if profile is None:
            # unresolved/typo'd id or empty/all-down pool => FAIL CLOSED, never fall through (B1)
            self.route = "drop"
            return False            # let the existing none/drop/false dispatch call drop_enable
        self.nexthop_id = profile.name
        self.nexthop_interface = str(profile.interface)
        self.nexthop_rt_table = str(profile.rt_table)
        self.nexthop_priority = str(10000 + int(self.machine.ip.rsplit(".", 1)[1]))  # M6 deterministic
        return True
```

In the dispatch section (~588-621), add the enable call (before the generic `if self.interface:` block, which is now skipped because `self.interface is None`):

```python
        if self.nexthop_id:
            rooter("nexthop_enable", str(self.machine.ip), self.nexthop_interface,
                   self.nexthop_rt_table, self.nexthop_priority)
            self._rooter_response_check()
```

Note: returning `False` from `_resolve_nexthop` sets `self.route="drop"`; ensure the resolution `elif` is written so a forced-drop still reaches the existing `route in (none/drop/false) -> drop_enable` dispatch (i.e. `_resolve_nexthop` returning False means "not bound, fall to the drop path", so structure the `elif` to not consume the route when it returns False — simplest: call `_resolve_nexthop()` in the branch condition; if it returns False the branch body is skipped and `self.route=="drop"` is handled by the existing dispatch).

- [ ] **Step 4: Run to verify pass** — `python -m pytest tests/test_route_network_nexthop.py -v` → PASS

- [ ] **Step 5: Commit**

```bash
git add lib/cuckoo/core/analysis_manager.py tests/test_route_network_nexthop.py
git commit -m "feat(analysis): route_network nexthop resolve+bind, fail-closed on unknown id"
```

---

## Task 10: `unroute_network` mirror (review M5)

Tear down with the **persisted** tuple; never re-run the selector. Because `self.interface is None`, the generic disable block (681-723) is skipped automatically.

**Files:**
- Modify: `lib/cuckoo/core/analysis_manager.py` (`unroute_network` ~679-760)
- Test: `tests/test_route_network_nexthop.py`

- [ ] **Step 1: Write the failing test**

```python
def test_unroute_mirrors_persisted_tuple(mgr, monkeypatch):
    prof = type("P", (), {"name": "gw1", "interface": "ens6", "rt_table": "201", "priority": 0})()
    monkeypatch.setattr(am, "_select_gateway", lambda r: prof, raising=False)
    _route(mgr, route="gw1", nexthop_enabled=True)
    enable_args = next(a for c, a in mgr._calls if c == "nexthop_enable")
    mgr._calls.clear()
    mgr._unroute_nexthop()                       # extracted helper called from unroute_network
    disable_args = next(a for c, a in mgr._calls if c == "nexthop_disable")
    # disable deletes EXACTLY what enable created (vm_ip, interface, rt_table, priority)
    assert disable_args == ("192.168.100.42", "ens6", "201", "10042")
    assert disable_args == enable_args
```

- [ ] **Step 2: Run to verify failure** — FAIL

- [ ] **Step 3: Implement**

```python
    def _unroute_nexthop(self):
        if not self.nexthop_id:
            return
        rooter("nexthop_disable", str(self.machine.ip), self.nexthop_interface,
               self.nexthop_rt_table, self.nexthop_priority)
        self._rooter_response_check()
```

Call `self._unroute_nexthop()` in `unroute_network` (mirror placement near the tun-disable branch ~756). Guard so it only fires when `self.nexthop_id` is set.

- [ ] **Step 4: Run to verify pass** — `python -m pytest tests/test_route_network_nexthop.py -k unroute -v` → PASS

- [ ] **Step 5: Commit**

```bash
git add lib/cuckoo/core/analysis_manager.py tests/test_route_network_nexthop.py
git commit -m "feat(analysis): unroute_network mirrors persisted nexthop tuple"
```

---

## Task 11: Ship `[nexthop]` in `routing.conf.default` (review M4)

**Files:**
- Modify: `conf/default/routing.conf.default` (after the `[vpn0]` block ~133)

- [ ] **Step 1: Append the section**

```ini
[nexthop]
# Decoupled next-hop egress to a shared gateway pool. Default OFF (no-regress).
enabled = no
# Comma list of [gwX] profile ids to load.
gateways = gw1
# Default selection when a task names no route: roundrobin | random | <gwX id>
default_policy = roundrobin
# Drop guest egress when no live gateway (strongly recommended yes).
fail_closed = yes
# Whole guest subnet the blackhole covers (CIDR). MUST match the libvirt guest net.
vm_net = 192.168.100.0/24

[gw1]
# Interface that reaches the gateway network (provided by the deployment).
interface = ens6
# Next-hop gateway IP reachable via `interface`, or 'onlink'.
next_hop = onlink
# Policy routing table id (numeric, unique per profile).
rt_table = 201
description = vpn-router-1
```

- [ ] **Step 2: Verify no-regress parse** — confirm Task 7's `load_nexthop_profiles` early-returns when `enabled = no` (it does; the hasattr/enabled guard). Add an explicit regression test:

```python
# tests/test_nexthop_selection.py (append)
def test_nexthop_disabled_is_noop(monkeypatch):
    import lib.cuckoo.core.startup as startup
    import lib.cuckoo.core.rooter as core_rooter
    monkeypatch.setattr(core_rooter, "gateways", {}, raising=False)
    called = []
    monkeypatch.setattr(startup, "rooter", lambda *a, **k: called.append(a) or {}, raising=False)
    startup.load_nexthop_profiles(_FakeRouting(nexthop_enabled=False))
    assert core_rooter.gateways == {} and called == []
```

Run: `python -m pytest tests/test_nexthop_selection.py -k disabled_is_noop -v` → PASS

- [ ] **Step 3: Commit**

```bash
git add conf/default/routing.conf.default tests/test_nexthop_selection.py
git commit -m "feat(conf): ship [nexthop] enabled=no default + example [gw1]"
```

---

## Task 12: No-regress snapshot test (the load-bearing safety net)

With `[nexthop] enabled = no`, the recorded rooter call sequence for `route=vpn0` and `route=none` must be byte-for-byte identical to the pre-change baseline.

**Files:**
- Test: `tests/test_route_network_nexthop.py`

- [ ] **Step 1: Write the test**

```python
def test_no_regress_vpn_and_none(mgr, monkeypatch):
    # nexthop DISABLED -> _resolve_nexthop must never run; route=vpn0/none unchanged.
    monkeypatch.setattr(am, "_nexthop_enabled", lambda: False, raising=False)
    # vpn0 path
    _route(mgr, route="vpn0", nexthop_enabled=False)
    assert mgr.nexthop_id is None
    cmds = [c for c, _ in mgr._calls]
    assert "nexthop_enable" not in cmds          # no nexthop calls leak into the legacy path
    # none path
    mgr._calls.clear(); mgr.nexthop_id = None
    _route(mgr, route="none", nexthop_enabled=False)
    assert "nexthop_enable" not in [c for c, _ in mgr._calls]
```

- [ ] **Step 2: Run** — `python -m pytest tests/test_route_network_nexthop.py -k no_regress -v` → PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_route_network_nexthop.py
git commit -m "test: no-regress snapshot for route=vpn0/none with nexthop off"
```

---

## Task 13: Netns end-to-end integration harness (root-gated, no cloud)

Validates the real mechanism without AWS: two veth "egress" interfaces + two dummy "gateway" namespaces; two VM source IPs bound to two profiles egress the correct interface concurrently; an unbound source is dropped (fail-closed).

**Files:**
- Create: `tests/integration/test_nexthop_netns.py`

- [ ] **Step 1: Write the test (skipped unless root + `CAPE_NETNS_TESTS=1`)**

```python
# tests/integration/test_nexthop_netns.py
import os, subprocess, pytest
import utils.rooter as rooter

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0 or os.environ.get("CAPE_NETNS_TESTS") != "1",
    reason="needs root + CAPE_NETNS_TESTS=1 (creates network namespaces)")


def sh(*a):
    subprocess.run(a, check=True)


@pytest.fixture
def netns():
    # two gateway namespaces + a veth into each, acting as egress_if1/egress_if2
    rooter.settings = type("S", (), {"ip": "/sbin/ip"})
    rooter.ServicePaths.ip = "/sbin/ip"; rooter.ServicePaths.iptables = "/sbin/iptables"
    for k in (1, 2):
        sh("ip", "netns", "add", f"gw{k}")
        sh("ip", "link", "add", f"egress_if{k}", "type", "veth", "peer", "name", f"gwp{k}", "netns", f"gw{k}")
        sh("ip", "addr", "add", f"10.{k}.0.1/24", "dev", f"egress_if{k}")
        sh("ip", "link", "set", f"egress_if{k}", "up")
        sh("ip", "netns", "exec", f"gw{k}", "ip", "addr", "add", f"10.{k}.0.2/24", "dev", f"gwp{k}")
        sh("ip", "netns", "exec", f"gw{k}", "ip", "link", "set", f"gwp{k}", "up")
    yield
    for k in (1, 2):
        sh("ip", "link", "del", f"egress_if{k}")
        sh("ip", "netns", "del", f"gw{k}")


def test_two_profiles_route_to_distinct_interfaces(netns):
    rooter.nexthop_init("201", "egress_if1", "10.1.0.2")
    rooter.nexthop_init("202", "egress_if2", "10.2.0.2")
    rooter.nexthop_enable("192.168.100.41", "egress_if1", "201", "10041")
    rooter.nexthop_enable("192.168.100.42", "egress_if2", "202", "10042")
    # assert each source resolves out its own table/interface
    out1 = subprocess.run(["/sbin/ip", "route", "get", "8.8.8.8", "from", "192.168.100.41"],
                          capture_output=True, text=True).stdout
    out2 = subprocess.run(["/sbin/ip", "route", "get", "8.8.8.8", "from", "192.168.100.42"],
                          capture_output=True, text=True).stdout
    assert "egress_if1" in out1 and "egress_if2" in out2


def test_unbound_source_is_dropped(netns):
    rooter.nexthop_fail_closed_enable("192.168.100.0/24", "250", "30000")
    out = subprocess.run(["/sbin/ip", "route", "get", "8.8.8.8", "from", "192.168.100.99"],
                         capture_output=True, text=True)
    # blackhole table => no route / unreachable for an unbound guest source
    assert "unreachable" in (out.stdout + out.stderr).lower() or out.returncode != 0
```

- [ ] **Step 2: Run (on a root box)**

Run: `sudo CAPE_NETNS_TESTS=1 python -m pytest tests/integration/test_nexthop_netns.py -v`
Expected: PASS (skips cleanly elsewhere)

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_nexthop_netns.py
git commit -m "test(integration): netns end-to-end nexthop egress + fail-closed (root-gated)"
```

---

## Self-review checklist (completed by plan author)

- **Spec coverage:** generic contract (forced next hop / source-NAT / fail-closed / isolation / teardown) → Tasks 1-5,9,10; `routing.conf` profiles → Tasks 7,11; startup load + arm → Tasks 7,8; per-task select + bind → Tasks 6,9; concurrency → Task 6; testing (unit/selection/netns/no-regress) → Tasks 0,6,12,13. Review findings: B1→T9, B2→T5, B3→T2/T5/T7/T9, H1→locked-decision-1 (srcroute untouched), H2→decision-1+T9, H3→T8, H4→T6, M1→T1, M2→decision-3, M3→T2/T3, M4→T7/T11, M5→T9/T10, M6→decision-2/T9, L1→T7/T11, L2→T6.
- **Type consistency:** profile object exposes `.name/.interface/.rt_table/.next_hop`; `self.nexthop_id/_interface/_rt_table/_priority` used identically in Tasks 9 & 10; all rooter args are strings end-to-end.
- **Out of scope (this plan):** the AWS realization (gateway fleet, egress ENIs, AMI) — that is sub-project 2 in the IaC repo's `2026-06-30-vpn-gateway-fleet-aws-KICKOFF.md`. The final cloud end-to-end test is the only step that needs both halves.

---

## Execution handoff

Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks. REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`.
2. **Inline Execution** — execute tasks in this session with checkpoints. REQUIRED SUB-SKILL: `superpowers:executing-plans`.
