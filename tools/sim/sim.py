#!/usr/bin/env python3
"""The relay's fault-simulation tests: inject a real fault, check what the monitoring shows, put everything back.

TEST TOOL: dev, or prod before the relay goes live. Run through tools/sim/run (a container with the Docker socket).
Standard library only. It acts only on containers of the relay's compose projects (signal-low, signal-high) and on
containers it starts itself (label signal-sim=1); network faults run in a helper inside ONE target container's own
network namespace, never the host's. Checks read the console's read-only monitoring API with a token.

    python sim.py list | test NAME... | group a|b|c | all [--quick] [--at HH:MM] [--side low|high] | restore

Split mode (prod: the low and high side on different hosts, nothing goes back across the diode): the same command
runs on both hosts with the same --at (UTC). Both work out the same plan from it: one time slot per low-side fault.
The low side injects each fault in its slot and takes it away again after a fixed time; the high side checks what
the monitoring shows in that slot. Then the high side runs the tests whose faults are on its own side.
"""
import hashlib, http.client, json, os, random, re, signal, socket, string, sys, time, urllib.request

TOKEN = os.environ.get("SIM_TOKEN", "")
API = os.environ.get("SIM_API", "http://console:8091")          # the console's internal port (token required)
REPO = os.environ.get("SIM_REPO", "/apps/signal-relay")          # the repo's path ON THE HOST (for test containers)
CHAT_IN = os.environ.get("SIM_CHAT_IN", "")                      # where chat enters the high side (default below)
HELPER = "signal-sim-helper:1"
PROJECTS = {"signal-low", "signal-high"}
TAG = "signal-sim"                                                # label on our containers, comment on our rules
QUICK = "--quick" in sys.argv
ROLE = "one"            # one: both sides on this host (dev); low / high: split mode, this host is that side (main())
LEAD = 60               # split mode: seconds at the start of each slot for the high side to see the relay healthy


def state_file():       # what this run changed, for `restore` (one per side, so two runners on one host don't mix)
    return "/reports/state.json" if ROLE == "one" else f"/reports/state-{ROLE}.json"


# ---------------------------------------------------------------- Docker, over its socket (no CLI, no packages)
class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path, timeout=120):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def docker(method, path, body=None, raw=False, timeout=120):
    c = UnixHTTP("/var/run/docker.sock", timeout)
    c.request(method, path, json.dumps(body) if body is not None else None,
              {"Content-Type": "application/json"} if body is not None else {})
    r = c.getresponse()
    data = r.read()
    if r.status >= 400 and r.status != 304:
        raise RuntimeError(f"Docker {method} {path}: {r.status} {data[:200]!r}")
    return data if raw else (json.loads(data) if data else None)


def containers(all_=True):
    return docker("GET", f"/containers/json?all={1 if all_ else 0}")


def ours(c):
    """Only the relay's own containers, or ones this tool started."""
    labels = c.get("Labels") or {}
    return labels.get("com.docker.compose.project") in PROJECTS or labels.get(TAG) == "1"


def find(service=None, name=None):
    """A relay container by compose service or by name. Refuses anything that isn't the relay's."""
    for c in containers():
        labels = c.get("Labels") or {}
        cname = c["Names"][0].lstrip("/")
        if (service and labels.get("com.docker.compose.service") == service) or (name and cname == name):
            if not ours(c):
                raise SystemExit(f"refusing: {cname} is not one of the relay's containers")
            return cname
    raise LookupError(f"no relay container {service or name}")


def inspect(name):
    return docker("GET", f"/containers/{name}/json")


def logs(name, tail=20):
    raw = docker("GET", f"/containers/{name}/logs?stdout=1&stderr=1&tail={tail}", raw=True)
    out, i = [], 0
    while i + 8 <= len(raw):                       # Docker's multiplexed stream: 8-byte header, then the bytes
        n = int.from_bytes(raw[i + 4:i + 8], "big")
        out.append(raw[i + 8:i + 8 + n].decode("utf-8", "replace"))
        i += 8 + n
    return "".join(out) if out else raw.decode("utf-8", "replace")


def run_container(name, image, cmd, host=None, env=None, binds=None, network=None, labels=None, memory=None,
                  restart=None, caps=None, netmode=None, workdir=None, user=None):
    body = {"Image": image, "Cmd": cmd, "Env": [f"{k}={v}" for k, v in (env or {}).items()],
            **({"WorkingDir": workdir} if workdir else {}), **({"User": user} if user else {}),
            "Labels": {TAG: "1", "signal-sim.side": ROLE, **(labels or {})},
            "HostConfig": {"Binds": binds or [], "CapAdd": caps or [], "NetworkMode": netmode or network or "bridge",
                           **({"Memory": memory, "MemorySwap": memory} if memory else {}),
                           **({"RestartPolicy": restart} if restart else {})}}
    if host:
        body["Hostname"] = host
    docker("DELETE", f"/containers/{name}?force=1") if any(c["Names"][0] == "/" + name for c in containers()) else None
    docker("POST", f"/containers/create?name={name}", body)
    docker("POST", f"/containers/{name}/start")
    remember("remove", name)
    return name


def remove(name):
    try:
        docker("POST", f"/containers/{name}/stop?t=5", timeout=30)
    except Exception:
        pass
    try:
        docker("DELETE", f"/containers/{name}?force=1")
    except Exception:
        pass


# ---------------------------------------------------------------- what this run changed, so it can be undone
def load_state():
    try:
        return json.load(open(state_file()))
    except (OSError, ValueError):
        return []


def remember(kind, target, value=None):
    st = load_state()
    st.append({"kind": kind, "target": target, "value": value})
    json.dump(st, open(state_file(), "w"))


def forget(kind, target):
    json.dump([s for s in load_state() if not (s["kind"] == kind and s["target"] == target)], open(state_file(), "w"))


def mine(labels):
    """A container of ours that this side may remove: in split mode, never the other side's (both may share a host
    when split mode is tried on dev)."""
    side = labels.get("signal-sim.side")
    return labels.get(TAG) == "1" and (ROLE == "one" or side not in ("low", "high") or side == ROLE)


def restore(quiet=False, final=True):
    """Undo everything recorded, plus anything tagged ours: helpers and test containers removed, rules cleared,
    paused relay containers unpaused, stopped ones started again, memory limits put back. FINAL: also the low
    side's marker (kept between tests, for the whole run)."""
    done = []
    for s in reversed(load_state()):
        try:
            if s["kind"] == "start":
                docker("POST", f"/containers/{s['target']}/start"); done.append(f"started {s['target']}")
            elif s["kind"] == "unpause":
                docker("POST", f"/containers/{s['target']}/unpause"); done.append(f"unpaused {s['target']}")
            elif s["kind"] == "memory":
                docker("POST", f"/containers/{s['target']}/update", {"Memory": s["value"], "MemorySwap": s["value"] * 2 if s["value"] else -1})
                done.append(f"memory limit of {s['target']} back")
        except Exception as e:
            done.append(f"could not undo {s['kind']} on {s['target']}: {e}")
    for c in containers():
        labels = c.get("Labels") or {}
        if mine(labels) and (final or labels.get("signal-sim.marker") != "1"):
            remove(c["Names"][0].lstrip("/")); done.append(f"removed {c['Names'][0].lstrip('/')}")
    for name in sorted({s["target"] for s in load_state() if s["kind"] == "net"}):   # rules or netem left behind
        try:
            out = helper(name, CLEAN, wait=True)
            done.append(f"{name}: {out.strip().splitlines()[-1]}")
        except Exception as e:
            done.append(f"could not check {name}'s network rules: {e}")
    json.dump([], open(state_file(), "w"))
    if not quiet:
        print("\n".join(done) if done else "Nothing to undo.")


# ---------------------------------------------------------------- network faults, inside one container's namespace
CLEAN = """for ipt in iptables ip6tables; do
  $ipt -S 2>/dev/null | grep -- '--comment signal-sim' | sed 's/^-A /-D /' | while read -r r; do eval $ipt $r; done
done
n=0; for d in $(ls /sys/class/net); do tc qdisc show dev $d 2>/dev/null | grep -q netem && { tc qdisc del dev $d root; n=$((n+1)); }; done
left=$( (iptables -S; ip6tables -S) 2>/dev/null | grep -c signal-sim)
[ "$left" = 0 ] && echo "cleared (netem removed on $n)" || echo "cleared, but $left rules left"
"""
LOCAL4 = "127.0.0.0/8 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16"


def helper(target, script, wait=False, name=None):
    """Run a shell script in a helper container that shares TARGET's network namespace (NET_ADMIN there only)."""
    cid = docker("GET", f"/containers/{target}/json")["Id"]
    name = name or f"signal-sim-net-{target}"
    body = {"Image": HELPER, "Cmd": ["sh", "-c", script], "Labels": {TAG: "1", "signal-sim.side": ROLE, "signal-sim.target": target},
            "HostConfig": {"NetworkMode": f"container:{cid}", "CapAdd": ["NET_ADMIN", "NET_RAW"]}}
    docker("DELETE", f"/containers/{name}?force=1") if any(c["Names"][0] == "/" + name for c in containers()) else None
    docker("POST", f"/containers/create?name={name}", body)
    docker("POST", f"/containers/{name}/start")
    if wait:
        docker("POST", f"/containers/{name}/wait", timeout=60)
        out = logs(name, 50)
        docker("DELETE", f"/containers/{name}?force=1")
        return out
    remember("remove", name)
    remember("net", target)
    time.sleep(1.5)
    return name


def fault(target, rules, undo):
    """A lasting fault: RULES applied now, UNDO run when the helper is stopped (and by restore's CLEAN anyway)."""
    script = f"""set -e
{rules}
echo applied
trap '{undo}; echo undone; exit 0' TERM INT
while :; do sleep 1; done"""
    return helper(target, script)


def block_internet(target, mode):
    """Everything from TARGET to the internet dropped or refused; local and private addresses still pass."""
    act = "-j DROP" if mode == "drop" else "-p tcp -j REJECT --reject-with tcp-reset"
    lines = [f"iptables -{{op}} OUTPUT -d {n} -m comment --comment {TAG} -j ACCEPT" for n in LOCAL4.split()]
    lines += [f"iptables -{{op}} OUTPUT -m comment --comment {TAG} {act}",
              f"ip6tables -{{op}} OUTPUT ! -d ::1/128 -m comment --comment {TAG} -j DROP 2>/dev/null || true"]
    return fault(target, "\n".join(l.format(op="A") for l in lines), "; ".join(l.format(op="D") for l in lines))


def end_fault(name):
    remove(name)
    forget("remove", name)


# ---------------------------------------------------------------- the console's monitoring API
def api(path):
    req = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def state():
    return api("/api/monitor/state")


def parts(comp=None, name=None):
    return [p for p in state()["parts"] if (comp is None or p.get("comp") == comp) and (name is None or p.get("name") == name)]


def part(comp, name=None):
    ps = parts(comp, name)
    if not ps:
        raise LookupError(f"no {comp} {name or ''} on the Monitor diagram")
    return ps[0]


def events_since(t_ms, code=None, part_re=None):
    evs = api(f"/api/monitor/events?since={t_ms}&limit=500")["events"]
    return [e for e in evs if (code is None or re.fullmatch(code, e["code"])) and (part_re is None or re.search(part_re, e["part"]))]


class Fail(Exception):
    pass


def now_ms():
    return int(time.time() * 1000)


def wait(desc, check, within, every=2):
    """Poll check() (returns a truthy result when met) until WITHIN seconds; returns (result, seconds taken)."""
    t0 = time.time()
    while True:
        try:
            got = check()
        except LookupError:
            got = None
        if got:
            return got, time.time() - t0
        if time.time() - t0 > within:
            raise Fail(f"expected {desc} within {within} s")
        time.sleep(every)


def wait_part(comp, name, states, codes=None, within=120, fresh=None):
    """FRESH (ms): only a report heard after that time counts (a box keeps showing its last report for a while)."""
    def check():
        p = part(comp, name)
        if fresh and (p.get("last") or 0) < fresh:
            return None
        if p["state"] in states and (codes is None or p.get("code") in codes or set(p.get("active") or []) & set(codes)):
            return p
    p, t = wait(f"{comp} {name or ''} {'/'.join(states).upper()}{' ' + '/'.join(codes) if codes else ''}", check, within)
    return p, t


def wait_event(code, part_re, since, within=120):
    e, t = wait(f"event {code} on {part_re}", lambda: (events_since(since, code, part_re) or [None])[0], within)
    return e, t


def never(comp, name, bad_states, seconds, why):
    """For SECONDS: the part never in BAD_STATES (no false alarm)."""
    t0 = time.time()
    while time.time() - t0 < seconds:
        p = part(comp, name)
        if p["state"] in bad_states:
            raise Fail(f"{comp} {name or ''} went {p['state'].upper()} ({p.get('code')}): {why}")
        time.sleep(3)


def mins(t):
    return f"{int(t // 60)}:{int(t % 60):02d}" if t >= 60 else f"{t:.0f} s"


# ---------------------------------------------------------------- the tests
TESTS = {}


def test(name, group, title, minutes, quick_ok=True, last=False):
    """A test is a function (its fault is on the high side, or needs no split: it runs where the console is), or a
    Fault class (its fault is on the low side). last: it costs monitoring packets, so the diode link stays WARN for
    up to an hour (loss is counted over the last hour) and its incident stays open: such tests run after the others."""
    def reg(obj):
        t = {"group": group, "title": title, "minutes": minutes, "quick_ok": quick_ok, "last": last}
        if isinstance(obj, type):
            f = obj()
            t.update(side="low", fault=f, fn=lambda: one_host(f))
        else:
            t.update(side="high", fn=obj)
        TESTS[name] = t
        return obj
    return reg


class Fault:
    """A fault on the low side, checked on the high side. start/stop use only Docker (the low host's), during/after
    only the console's API (the high host's), so split mode can run them on different hosts.
    One host: start, during (until its checks pass), stop, after.
    Split: the low side keeps the fault on for `hold` seconds from the slot's agreed time; the high side runs during
    from that time, and after from its end. Deadlines count from the fault's start (by()), on either host."""
    hold = (120, 120)       # split mode: seconds the fault stays on (full run, --quick)
    recover = 30            # split mode: seconds after it for the after-checks

    def held(self):
        return self.hold[1 if QUICK else 0]

    def start(self):        # Docker only: inject; may return a note
        return None

    def during(self, t0, hold):     # API only; hold is None on one host (go on as soon as the checks pass)
        return []

    def stop(self):         # Docker only: take the fault away
        pass

    def after(self, t1):    # API only: recovery, from the fault's end
        return []


def one_host(f):
    note = f.start()
    t0 = time.time()
    notes = [note] if note else []
    try:
        notes += f.during(t0, None)
    finally:
        f.stop()
    return "; ".join(notes + f.after(time.time()))


def ms(t):
    return int(t * 1000)


def by(t0, s):
    """Seconds left until S seconds after T0 (at least 5)."""
    return max(5, t0 + s - time.time())


def real_subscriber():
    """The relay's own subscriber (not a test box), by its box: the high side can't ask Docker."""
    subs = [p["name"] for p in parts("subscriber") if not p["name"].startswith("signal-sim")]
    if not subs:
        raise LookupError("no subscriber on the Monitor diagram")
    return "signal-subscriber" if "signal-subscriber" in subs else subs[0]


# A. network ------------------------------------------------------------------------------------------------
class SignalCut(Fault):
    mode, warn_s, down_s = "reject", 90, 200
    recover = 140

    def start(self):
        self.h = block_internet(find("signal"), self.mode)

    def during(self, t0, hold):
        since = ms(t0)
        srv = part("signal-servers")
        p, _ = wait_part("signal-servers", None, ("warn", "down"), within=by(t0, self.warn_s))
        notes = [f"Signal servers {p['state'].upper()} ({p.get('code')}) in {mins(time.time() - t0)}"]
        if part("subscriber", real_subscriber())["state"] != "up":
            raise Fail("the subscriber went down too: only the link to Signal should break")
        if not QUICK:
            wait_part("signal-servers", None, ("down",), ("SIGNAL_UNREACHABLE",), within=by(t0, self.down_s))
            notes.append(f"DOWN (SIGNAL_UNREACHABLE) at {mins(time.time() - t0)}")
            inc = [i for i in state()["incidents"] if not i.get("closed") and srv["key"] in (i.get("parts") or [])]
            if not inc:
                raise Fail("Signal servers is DOWN but in no open incident")
            i = inc[0]
            if i["opened"] >= since and (i.get("cause") or {}).get("code") not in ("SIGNAL_UNREACHABLE", "SIGNAL_RECONNECTING"):
                raise Fail(f"incident #{i['id']}'s likely cause is {(i.get('cause') or {}).get('code')}, not SIGNAL_UNREACHABLE")
            notes.append(f"incident #{i['id']}" + ("" if i["opened"] >= since else f" (already open, cause {(i.get('cause') or {}).get('code')})"))
        return notes

    def stop(self):
        end_fault(self.h)

    def after(self, t1):
        _, t = wait_part("signal-servers", None, ("up",), within=120, fresh=ms(t1))
        return [f"back UP {mins(t)} after"]


@test("net.signal-reject", "a", "signal-cli refused by the internet (a connection error)", 4)
class _(SignalCut):
    mode, warn_s, down_s, hold = "reject", 90, 200, (215, 100)


@test("net.signal-drop", "a", "signal-cli's internet traffic silently dropped (a dead network)", 5)
class _(SignalCut):
    mode, warn_s, down_s, hold = "drop", 180, 240, (255, 190)


@test("net.signal-flaky", "a", "30% loss and 300 ms delay on signal-cli: no false DOWN", 3)
class _(Fault):
    hold, recover = (150, 60), 140

    def start(self):
        self.h = fault(find("signal"), "tc qdisc add dev eth0 root netem delay 300ms 100ms loss 30%", "tc qdisc del dev eth0 root")

    def during(self, t0, hold):
        never("signal-servers", None, ("down",), (hold or self.held()) - 10, "a flaky link must not look like an outage")
        never("signal-cli", None, ("down",), 5, "signal-cli itself is fine")
        return ["no false DOWN through 30% loss and 300 ms delay"]

    def stop(self):
        end_fault(self.h)

    def after(self, t1):
        wait_part("signal-servers", None, ("up",), within=120)
        return ["back UP"]


@test("net.sub-link", "a", "subscriber cut off from signal-cli (the event stream)", 3)
class _(Fault):
    hold, recover = (90, 90), 110

    def start(self):
        ip = list(inspect(find("signal"))["NetworkSettings"]["Networks"].values())[0]["IPAddress"]
        rule = f"iptables -A OUTPUT -d {ip} -p tcp --dport 8443 -m comment --comment {TAG} -j REJECT --reject-with tcp-reset"
        self.h = fault(find("subscriber"), rule, rule.replace("-A", "-D"))

    def during(self, t0, hold):
        p, _ = wait_part("subscriber", real_subscriber(), ("down", "warn"), ("NOT_CONNECTED", "SUB_NOT_CONNECTED", "STREAM_DROP"),
                         within=by(t0, 85))
        return [f"subscriber {p['state'].upper()} ({p.get('code')}) in {mins(time.time() - t0)}"]

    def stop(self):
        end_fault(self.h)

    def after(self, t1):
        sub = real_subscriber()
        _, t = wait_part("subscriber", sub, ("up",), within=90, fresh=ms(t1))
        missed = (part("subscriber", sub).get("info") or {}).get("missed", 0)
        return [f"back UP {mins(t)} after; events missed: {missed}"]


@test("net.mon-route-down", "a", "monitoring packets stop arriving (the monitoring route through the diode)", 4, last=True)
def _():
    mrx = find("monitor-receiver")
    rule = f"iptables -A INPUT -p udp --dport 6511 -m comment --comment {TAG} -j DROP"
    h = fault(mrx, rule, rule.replace("-A", "-D"))
    try:
        p, t = wait_part("signal-monitor", None, ("stale", "down"), within=150)
        notes = [f"signal-monitor {p['state'].upper()} in {mins(t)}"]
        if not QUICK:
            _, t2 = wait_part("subscriber", None, ("unknown",), within=120)
            notes.append(f"its parts UNKNOWN at {mins(t + t2)}")
    finally:
        end_fault(h)
    _, t3 = wait_part("signal-monitor", None, ("up",), within=90, fresh=now_ms())
    notes.append(f"back UP {mins(t3)} after")
    return "; ".join(notes)


@test("net.mon-route-lossy", "a", "25% of monitoring packets lost (a lossy diode route)", 7, quick_ok=False, last=True)
def _():
    mrx = find("monitor-receiver")
    rule = f"iptables -A INPUT -p udp --dport 6511 -m statistic --mode random --probability 0.25 -m comment --comment {TAG} -j DROP"
    h = fault(mrx, rule, rule.replace("-A", "-D"))
    try:
        host = part("signal-monitor")["host"]

        def check():
            l = state()["links"].get(host) or {}
            return l if l.get("state") in ("warn", "down") and (l.get("loss_1h") or 0) > 0 else None
        l, t = wait("the diode link (Monitoring) WARN with loss", check, 420, every=10)
    finally:
        end_fault(h)
    return f"Monitoring marker {l['state'].upper()}, {l['loss_1h']}% lost (1 h) after {mins(t)}"


@test("net.console-away", "a", "monitor-receiver can't reach the console for 60 s: holds, replays, nothing goes STALE", 2)
def _():
    mrx = find("monitor-receiver")
    ip = list(inspect(find("console"))["NetworkSettings"]["Networks"].values())[0]["IPAddress"]
    rule = f"iptables -A OUTPUT -d {ip} -p tcp --dport 8091 -m comment --comment {TAG} -j REJECT --reject-with tcp-reset"
    h = fault(mrx, rule, rule.replace("-A", "-D"))
    try:
        time.sleep(60)
    finally:
        end_fault(h)
    time.sleep(15)
    bad = [p["key"] for p in state()["parts"] if p.get("side") == "low" and p["state"] in ("stale", "down", "unknown")]
    if bad:
        raise Fail(f"held monitoring should arrive late, not go missing: {', '.join(bad)} went bad")
    w = (part("monitor-receiver").get("info") or {}).get("waiting", 0)
    return f"60 s cut, then replayed: no low-side part went STALE; waiting now {w}"


# B. Docker ------------------------------------------------------------------------------------------------
class SubscriberStop(Fault):
    kill = False
    hold, recover = (120, 120), 140

    def start(self):
        self.name = find("subscriber")
        docker("POST", f"/containers/{self.name}/kill?signal=KILL" if self.kill else f"/containers/{self.name}/stop?t=10", timeout=60)
        remember("start", self.name)

    def during(self, t0, hold):
        name = real_subscriber()
        p, _ = wait_part("subscriber", name, ("down", "stale"), within=by(t0, 110), fresh=ms(t0))
        notes = [f"subscriber {p['state'].upper()} ({p.get('code')}) in {mins(time.time() - t0)}"]
        e, _ = wait_event("CONTAINER_EXITED", rf"/container/{name}$", ms(t0), within=by(t0, 110))
        return notes + [f"event {e['code']}"]

    def stop(self):
        docker("POST", f"/containers/{self.name}/start")
        forget("start", self.name)

    def after(self, t1):
        _, t = wait_part("subscriber", real_subscriber(), ("up",), within=120, fresh=ms(t1))
        return [f"back UP {mins(t)} after start"]


@test("docker.stop-subscriber", "b", "subscriber stopped (on purpose)", 2)
class _(SubscriberStop):
    pass


@test("docker.stop-receiver", "b", "a receiver stopped (messages for its room would be lost)", 3)
def _():
    name = find(name=a_receiver()[0])
    docker("POST", f"/containers/{name}/stop?t=10", timeout=60)
    remember("start", name)
    try:
        p, t = wait_part("receiver", name, ("stale", "down"), within=150)
        notes = [f"{name} {p['state'].upper()} in {mins(t)}"]
    finally:
        started = now_ms()
        docker("POST", f"/containers/{name}/start")
        forget("start", name)
    _, t2 = wait_part("receiver", name, ("up",), within=90, fresh=started)
    notes.append(f"back UP {mins(t2)} after start")
    return "; ".join(notes)


@test("docker.kill-subscriber", "b", "subscriber killed (SIGKILL, exit 137) through Docker, then started by hand", 2)
class _(SubscriberStop):
    # Docker treats a kill through Docker like a stop by hand: restart: unless-stopped does NOT bring it back
    kill = True


class TestBox(Fault):
    """A throwaway container in the low project (so container-facts reports it), removed afterwards. It must run
    ~35 s before it misbehaves: container-facts reports changes only for a container it has already seen."""
    name, cmd, memory, restart, code, within = "", [], None, None, "", 120
    recover = 15

    def start(self):
        run_container(self.name, "python:3.13-alp-u3", self.cmd, memory=self.memory, restart=self.restart,
                      labels={"com.docker.compose.project": "signal-low", "com.docker.compose.service": self.name})

    def during(self, t0, hold):
        e, _ = wait_event(self.code, rf"/container/{self.name}$", ms(t0), within=by(t0, self.within))
        return [f"event {e['code']} in {mins(time.time() - t0)}: {e['text'][:90]}"]

    def stop(self):
        remove(self.name)
        forget("remove", self.name)


@test("docker.crash", "b", "a test container's program dies by itself: Docker restarts it", 2)
class _(TestBox):
    name, code, hold = "signal-sim-crash", r"CONTAINER_(RESTARTED|EXITED)", (130, 130)
    cmd = ["python", "-c", "import time, sys; time.sleep(35); sys.exit(3)"]
    restart = {"Name": "on-failure", "MaximumRetryCount": 5}


@test("docker.restart-loop", "b", "a test container crashing every ~20 s: each restart counted", 3)
class _(TestBox):
    name, hold = "signal-sim-loop", (210, 210)
    cmd = ["python", "-c", "import time, sys; time.sleep(20); sys.exit(1)"]
    restart = {"Name": "on-failure", "MaximumRetryCount": 20}

    def during(self, t0, hold):
        evs, _ = wait(f"3+ restarts of {self.name}",
                      lambda: (lambda e: e if len(e) >= 3 else None)(events_since(ms(t0), "CONTAINER_RESTARTED", rf"/container/{self.name}$")),
                      by(t0, 200), every=5)
        return [f"{len(evs)} CONTAINER_RESTARTED events in {mins(time.time() - t0)}"]


@test("docker.pause-signal-cli", "b", "signal-cli frozen (paused) for up to 70 s", 3)
class _(Fault):
    hold, recover = (75, 75), 140

    def start(self):
        self.name = find("signal")
        docker("POST", f"/containers/{self.name}/pause")
        remember("unpause", self.name)

    def during(self, t0, hold):
        p, _ = wait_part("signal-cli", None, ("down", "warn"), within=by(t0, 70))
        return [f"signal-cli {p['state'].upper()} ({p.get('code')}) in {mins(time.time() - t0)}"]

    def stop(self):
        docker("POST", f"/containers/{self.name}/unpause")
        forget("unpause", self.name)

    def after(self, t1):
        try:
            _, t = wait_part("signal-cli", None, ("up",), within=120, fresh=ms(t1))
        except Fail:
            if part("signal-cli").get("code") == "GATEWAY_REFUSED":
                raise Fail("signal-cli answers again but stays WARN GATEWAY_REFUSED: signal-monitor's check, broken by the "
                           "pause (TLS handshake: broken pipe), was counted as a refused certificate (a false alarm)")
            raise
        return [f"back UP {mins(t)} after"]


@test("docker.oom", "b", "a test container killed for using too much memory (OOM)", 2)
class _(TestBox):
    name, code, memory, hold = "signal-sim-oom", "CONTAINER_OOM", 48 << 20, (125, 125)
    cmd = ["python", "-c", "import time; time.sleep(35); b = []\nwhile True: b.append(bytearray(4 << 20)); time.sleep(0.05)"]


@test("docker.mem-high", "b", "a test container near its memory limit (over 85%)", 3)
class _(TestBox):
    name, code, memory, hold, within = "signal-sim-memhigh", "CONTAINER_MEM_HIGH", 64 << 20, (160, 160), 150
    cmd = ["python", "-c", "import time; time.sleep(35); b = bytearray(56 << 20)\nfor i in range(0, len(b), 4096): b[i] = 1\ntime.sleep(600)"]

    def during(self, t0, hold):
        e, _ = wait_event(self.code, rf"/container/{self.name}$", ms(t0), within=by(t0, self.within))
        return [f"event CONTAINER_MEM_HIGH in {mins(time.time() - t0)}"]


# C. application (invalid or test traffic only: no fake messages reach the console or Gotham) ------------------
def chat_in(rx):
    """Where to send chat packets: dev's diode-sim (it routes by header), else straight to the receiver, as the
    diode's output would (prod: the high host has no diode input)."""
    if CHAT_IN:
        return CHAT_IN
    try:
        socket.getaddrinfo("diode-sim", 6510)
        return "diode-sim:6510"
    except OSError:
        return f"{rx}:6510"


def udp(addr, payload):
    host, port = addr.rsplit(":", 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(payload.encode() if isinstance(payload, str) else payload, (host, int(port)))


def part_line(room, sess, q, m, i, n, d):
    body = json.dumps({"s": sess, "q": q, "m": m, "i": i, "n": n, "d": d}, separators=(",", ":"))
    return f"CEF:0|CHAT|{room}|{i}|{n}|SIGNAL-RELAY|APP|" + body.replace("\\", "\\\\").replace("=", "\\=").replace("|", "\\|")


def a_receiver():
    rx = parts("receiver")
    if not rx:
        raise Fail("no receiver on the Monitor diagram")
    rx = [r for r in rx if r["state"] == "up" and [x for x in r.get("rooms") or [] if x]] or rx
    r = rx[0]
    return r["name"], [x for x in r.get("rooms") or [] if x][0]


def counted(rx, code, since_count):
    p = part("receiver", rx)
    return (p.get("p") or {}).get(code, 0) > since_count


@test("app.bad-packet", "c", "junk after a valid route header: the receiver ignores it", 1)
def _():
    rx, room = a_receiver()
    before = (part("receiver", rx).get("p") or {}).get("BAD_PACKET", 0)
    for _ in range(3):
        udp(chat_in(rx), f"CEF:0|CHAT|{room}|1|1|SIGNAL-RELAY|APP|this is not json")
    _, t = wait("BAD_PACKET counted on " + rx, lambda: counted(rx, "BAD_PACKET", before), 60)
    p, _ = wait_part("receiver", rx, ("warn",), ("BAD_PACKET",), within=60)
    return f"{rx} WARN BAD_PACKET in {mins(t)}"


@test("app.old-header", "c", "the retired header (CEF:0|J6-DTT|CHAT|) straight to a receiver", 1)
def _():
    rx, room = a_receiver()
    before = (part("receiver", rx).get("p") or {}).get("BAD_PACKET", 0)
    udp(f"{rx}:6510", "CEF:0|J6-DTT|CHAT|1|1|SIGNAL-RELAY|APP|{}")
    _, t = wait("BAD_PACKET counted on " + rx, lambda: counted(rx, "BAD_PACKET", before), 60)
    return f"{rx} counted BAD_PACKET in {mins(t)}"


@test("app.misrouted", "c", "a packet for a room the receiver doesn't serve (a wrong diode route)", 1)
def _():
    rx, room = a_receiver()
    before = (part("receiver", rx).get("p") or {}).get("ROOM_MISMATCH", 0)
    udp(f"{rx}:6510", part_line("SIMTEST", "sim" + rnd(), 0, 1, 1, 2, "{"))
    _, t = wait("ROOM_MISMATCH counted on " + rx, lambda: counted(rx, "ROOM_MISMATCH", before), 60)
    p, _ = wait_part("receiver", rx, ("warn",), ("ROOM_MISMATCH",), within=60)
    return f"{rx} WARN ROOM_MISMATCH in {mins(t)}"


@test("app.gap", "c", "a packet number skipped in a test session", 1)
def _():
    rx, room = a_receiver()
    before = (part("receiver", rx).get("p") or {}).get("PACKETS_MISSING", 0)
    s = "sim" + rnd()
    udp(chat_in(rx), part_line(room, s, 0, 1, 1, 2, "{\"a\":"))     # parts of messages that never complete:
    udp(chat_in(rx), part_line(room, s, 2, 2, 1, 2, "{\"b\":"))     # nothing reaches the console or Gotham
    _, t = wait("PACKETS_MISSING counted on " + rx, lambda: counted(rx, "PACKETS_MISSING", before), 60)
    return f"{rx} counted PACKETS_MISSING in {mins(t)}"


@test("app.lost-message", "c", "2 of 3 parts of a test message: given up after 30 s, lost notice", 2)
def _():
    rx, room = a_receiver()
    before = (part("receiver", rx).get("p") or {}).get("MESSAGE_LOST", 0)
    s = "sim" + rnd()
    udp(chat_in(rx), part_line(room, s, 0, 1, 1, 3, "{\"envelope\":"))
    udp(chat_in(rx), part_line(room, s, 1, 1, 2, 3, "{}"))
    _, t = wait("MESSAGE_LOST counted on " + rx, lambda: counted(rx, "MESSAGE_LOST", before), 90)
    return f"{rx} gave up and counted MESSAGE_LOST after {mins(t)} (a lost-message notice in \"All\")"


def rnd():
    return "".join(random.choice(string.ascii_lowercase) for _ in range(5))


def test_subscriber(name, routes_for, routes, extra=None, restart=None):
    """A throwaway subscriber with its own routes file, sending nowhere (UDP_DEST 127.0.0.1:9): no message ever
    crosses. It reads signal-cli like the real one (same certificate) and reports to signal-monitor."""
    path = f"/reports/{name}.routes"
    open(path, "w").write(routes)
    host_routes = f"{REPO}/tools/sim/reports/{name}.routes"
    user = inspect(find("subscriber"))["Config"].get("User") or None          # the relay's own user (reads the cert)
    run_container(name, "python:3.13-alp-u3", ["python", "subscriber_udp.py"], host=name, network="signal-low_default",
                  workdir="/app", user=user, restart=restart,
                  env={"ROUTES_FILE": "/routes.conf", "ROUTES_FOR": routes_for, "SIGNAL_URL": "https://signal:8443",
                       "UDP_DEST": "127.0.0.1", "UDP_PORT": "9", "STATE_FILE": "", "SENT_LOG": "",
                       "MONITOR_ADDR": "signal-monitor:6520", "MONITOR_NAME": name, "PYTHONPATH": "/shared",
                       "PYTHONDONTWRITEBYTECODE": "1", **(extra or {})},
                  binds=[f"{REPO}/low/subscriber:/app:ro", f"{REPO}/low/shared:/shared:ro",
                         f"{REPO}/low/certs/subscriber:/certs:ro", f"{host_routes}:/routes.conf:ro"],
                  labels={"com.docker.compose.project": "signal-low", "com.docker.compose.service": name})
    inspect(name)  # it exists
    return name


FAKE_GROUP = "SIMTESTSIMTESTSIMTESTSIMTESTSIMTESTSIMTEST0="    # a well-formed group ID that never has messages


class TestSubscriber(Fault):
    name, rooms = "", "SIMTEST"
    hold = (125, 125)
    recover = 10

    def routes_for(self):
        return self.name

    def start(self):
        rf = self.routes_for()
        test_subscriber(self.name, rf, f"{rf} {self.rooms} {FAKE_GROUP}\n")

    def stop(self):
        remove(self.name)
        forget("remove", self.name)


@test("app.room-unserved", "c", "a test subscriber sends a room no receiver serves", 2)
class _(TestSubscriber):
    name = "signal-sim-subscriber"

    def during(self, t0, hold):
        wait_part("subscriber", self.name, ("warn",), ("ROOM_UNSERVED",), within=by(t0, 120))
        room = state()["rooms"].get("SIMTEST") or {}
        return [f"{self.name} WARN ROOM_UNSERVED in {mins(time.time() - t0)}; room marker {room.get('state', '?').upper()}"]


@test("app.subscriber-twice", "c", "a test subscriber with the real one's ROUTES_FOR (every message would go twice)", 2)
class _(TestSubscriber):
    name, hold, recover = "signal-sim-twin", (150, 150), 110

    def routes_for(self):           # the real one's, from its container (the low side has no console)
        env = dict(e.split("=", 1) for e in inspect(find("subscriber"))["Config"].get("Env") or [] if "=" in e)
        return env.get("ROUTES_FOR") or "signal-subscriber"

    def during(self, t0, hold):
        wait_part("subscriber", self.name, ("warn",), ("SUBSCRIBER_TWICE",), within=by(t0, 120))
        wait_part("subscriber", real_subscriber(), ("warn",), ("SUBSCRIBER_TWICE",), within=by(t0, 145))
        return [f"both WARN SUBSCRIBER_TWICE in {mins(time.time() - t0)}"]

    def after(self, t1):
        wait_part("subscriber", real_subscriber(), ("up",), within=90)
        return ["the real one back UP"]


@test("app.routes-broken", "c", "a test subscriber with a broken routes file refuses to start", 1)
class _(TestSubscriber):
    name, rooms, hold = "signal-sim-broken", "lowercase-room", (95, 95)

    def start(self):
        rf = self.name
        test_subscriber(self.name, rf, f"{rf} {self.rooms} {FAKE_GROUP}\n", restart={"Name": "on-failure", "MaximumRetryCount": 50})
        time.sleep(6)
        out = logs(self.name, 10)
        if "Not starting" not in out:
            raise Fail(f"expected it to refuse to start; log: {out.strip()[-160:]}")
        return f"refused ({out.strip().splitlines()[-1][20:110]})"

    def during(self, t0, hold):
        e, _ = wait_event(r"CONTAINER_(EXITED|RESTARTED)", rf"/container/{self.name}$", ms(t0), within=by(t0, 90))
        return [f"event {e['code']} in {mins(time.time() - t0)}"]


@test("app.box-removed", "c", "a test subscriber's box appears, then goes when its container is removed", 4)
class _(TestSubscriber):
    name, hold, recover = "signal-sim-removed", (190, 190), 215

    def during(self, t0, hold):
        wait_part("subscriber", self.name, ("up", "warn"), within=by(t0, 90))
        notes = [f"box appeared in {mins(time.time() - t0)}"]
        host = part("subscriber", self.name)["host"]          # else its removal is never reported:
        wait(f"container-facts to know {self.name}", lambda: self.name in (state()["containers"].get(host) or {}), by(t0, 180))
        return notes

    def after(self, t1):
        e, t = wait_event("PART_REMOVED", rf"/subscriber/{self.name}$", ms(t1), within=200)
        return [f"PART_REMOVED {mins(t)} after the container was removed"]


def settle(within=300):
    """Before a test: the relay healthy again, every real box UP. (An incident can still be open: it closes after 60 s
    all clear, and the diode link counts loss over an hour; tests that check a cause allow for that.)"""
    def healthy():
        st = state()
        bad = [p["key"] for p in st["parts"] if p["state"] != "up" and "signal-sim" not in p["key"]]
        return None if bad else True
    try:
        _, t = wait("the relay healthy (every box UP, no open incident)", healthy, within, every=5)
        return None
    except Fail:
        st = state()
        bad = [f"{p['key'].split('/')[-1]} {p['state'].upper()}" for p in st["parts"] if p["state"] != "up" and "signal-sim" not in p["key"]]
        inc = [f"#{i['id']}" for i in st["incidents"] if not i.get("closed")]
        return f"relay not healthy after {within // 60} min: {', '.join(bad + inc) or '?'}"


# ---------------------------------------------------------------- running
def host_allowed():
    host = open("/host/hostname").read().strip() if os.path.exists("/host/hostname") else socket.gethostname()
    allow = []
    try:
        for line in open("/sim/sim.conf"):
            if line.strip().startswith("allow_hosts"):
                allow = line.split("=", 1)[1].split()
    except OSError:
        pass
    if host not in allow:
        raise SystemExit(f"refusing: this host ({host}) is not in allow_hosts in tools/sim/sim.conf")
    return host


def preflight():
    try:
        docker("GET", f"/images/{HELPER}/json")
    except RuntimeError:
        raise SystemExit(f"the helper image {HELPER} is missing: run tools/sim/run setup")
    if ROLE == "low":
        return
    try:
        state()
    except Exception as e:
        raise SystemExit(f"can't read the console's monitoring API ({e}): is the console running, the token valid?")


def clock(t):
    return time.strftime("%H:%M:%S", time.gmtime(t))


def until(t):
    while time.time() < t:
        time.sleep(min(5, t - time.time()))


def run_one(n, fn, ok="PASS"):
    """Run one test (or one side's part of it); everything put back after. Returns its result line."""
    t = TESTS[n]
    tty = sys.stdout.isatty()
    if tty:
        print(f"....  {n:24} {t['title']}", end="", flush=True)
    t0 = time.time()
    try:
        detail = fn()
        res = ok
    except Fail as e:
        res, detail = "FAIL", str(e)
    except KeyboardInterrupt:
        raise
    except Exception as e:
        res, detail = "ERROR", f"{type(e).__name__}: {e}"
    finally:
        restore(quiet=True, final=False)
    print(f"{chr(13) if tty else ''}{res:5} {n:24} {detail}" + (" " * 20 if tty else ""))
    return {"test": n, "result": res, "detail": detail, "seconds": round(time.time() - t0)}


def skip(n, why):
    print(f"SKIP  {n:24} {why}")
    return {"test": n, "result": "SKIP", "detail": why}


def plan(names, at):
    """Split mode: one slot per low-side fault, from AT. Both sides work out the same slots from the same command
    line: [(test, fault start, hold)]. Each slot = LEAD (high side: relay healthy?) + hold + recover."""
    slots, t = [], at
    for n in names:
        f = TESTS[n]["fault"]
        slots.append((n, t + LEAD, f.held()))
        t += LEAD + f.held() + f.recover
    pid = hashlib.sha1(f"{int(at)}|{','.join(names)}|{QUICK}".encode()).hexdigest()[:6]
    return slots, t, pid


def run_split(names, at):
    """Split mode, this host's side. Low: inject each fault in its slot. High: check each slot, then run its own."""
    low = [n for n in names if TESTS[n]["side"] == "low" and (TESTS[n]["quick_ok"] or not QUICK)]
    slots, end, pid = plan(low, at)
    print(f"Split mode, {ROLE} side. Plan {pid} (the other side must show the same): {len(slots)} low-side "
          f"fault(s), {clock(at)} to {clock(end)} UTC. Now {clock(time.time())} UTC.")
    for n, t0, hold in slots:
        print(f"  {clock(t0)}  {n:24} fault on for {mins(hold)}")
    print()
    results = []
    marker = f"signal-sim-low-{pid}"
    if ROLE == "low":   # a marker the high side can see (container-facts reports it): this plan's low side is running
        run_container(marker, "python:3.13-alp-u3", ["sleep", "86400"],
                      labels={"com.docker.compose.project": "signal-low", "com.docker.compose.service": "signal-sim",
                              "signal-sim.marker": "1"})
        for n, t0, hold in slots:
            if time.time() > t0 + 10:
                results.append(skip(n, f"started too late for its slot ({clock(t0)})"))
                continue
            until(t0)
            f = TESTS[n]["fault"]

            def low_part():
                note = f.start()
                try:
                    until(t0 + hold)
                finally:
                    f.stop()
                return f"fault on {clock(t0)}-{clock(time.time())}" + (f"; {note}" if note else "")
            results.append(run_one(n, low_part, ok="DONE"))
        return results
    seen = None
    for n, t0, hold in slots:
        if time.time() > t0 + 10:
            results.append(skip(n, f"started too late for its slot ({clock(t0)})"))
            continue
        if seen is None:        # the low side's marker, before the first fault
            try:
                wait(f"the low side's runner (plan {pid})", lambda: any(marker in (c or {}) for c in state()["containers"].values()),
                     max(5, t0 - time.time()), every=5)
                seen = True
            except Fail:
                seen = False
                print(f"NOTE  the low side's runner (plan {pid}) isn't seen: not started yet, or started with another "
                      f"--at, test list or --quick? Its faults may not happen.")
        until(t0 - LEAD)
        why = settle(within=max(5, t0 - time.time() - 2))
        if why:
            results.append(skip(n, why + " (the low side injects it anyway)"))
            continue
        until(t0)
        f = TESTS[n]["fault"]

        def high_part():
            notes = f.during(t0, hold)
            until(t0 + hold)
            return "; ".join(notes + f.after(t0 + hold))
        results.append(run_one(n, high_part))
    if slots:
        until(end)
    return results + run_here([n for n in names if TESTS[n]["side"] == "high"])


def run_here(names):
    results = []
    for n in names:
        if QUICK and not TESTS[n]["quick_ok"]:
            results.append(skip(n, "not in --quick (needs minutes of data)"))
            continue
        why = settle()
        if why:
            results.append(skip(n, why))
            continue
        results.append(run_one(n, TESTS[n]["fn"]))
    return results


def run(names, at=None):
    names = [n for n in names if not TESTS[n]["last"]] + [n for n in names if TESTS[n]["last"]]
    host = host_allowed()
    preflight()
    restore(quiet=True)                              # a clean start: nothing left from an earlier run
    print(f"Host {host}. {len(names)} test(s){' (quick)' if QUICK else ''}. Ctrl-C stops and puts everything back.\n")
    try:
        results = run_split(names, at) if ROLE != "one" else run_here(names)
    except KeyboardInterrupt:
        restore(quiet=True)
        print("\nStopped; everything put back.")
        return 1
    restore(quiet=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    side = "" if ROLE == "one" else f"-{ROLE}"
    json.dump({"host": host, "side": ROLE, "at_utc": stamp, "plan_at": clock(at) if at else None, "quick": QUICK,
               "results": results}, open(f"/reports/sim-{stamp}{side}.json", "w"), indent=1)
    counts = {r: sum(1 for x in results if x["result"] == r) for r in ("PASS", "DONE", "FAIL", "ERROR", "SKIP")}
    print(f"\n{counts['PASS']} passed, " + (f"{counts['DONE']} faults done, " if counts["DONE"] else "") +
          f"{counts['FAIL']} failed, {counts['ERROR']} errors, {counts['SKIP']} skipped. "
          f"Report: tools/sim/reports/sim-{stamp}{side}.json")
    if ROLE == "low":
        print("The results are on the high side: its runner checked what the monitoring showed.")
    return 0 if not counts["FAIL"] and not counts["ERROR"] else 1


def option(argv, name):
    """--name VALUE or --name=VALUE"""
    for i, a in enumerate(argv):
        if a == f"--{name}" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1]
    return None


def parse_at(text):
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text or "")
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        raise SystemExit(f"--at takes a UTC time as HH:MM, not {text!r}")
    now = time.time()
    t = now - now % 86400 + int(m[1]) * 3600 + int(m[2]) * 60
    return t + 86400 if t < now - 600 else t          # passed over 10 min ago: tomorrow's (else a late start)


def find_role(side):
    """one (both sides here), low or high: which of the relay's compose projects run on this host."""
    here = {(c.get("Labels") or {}).get("com.docker.compose.project") for c in containers()
            if (c.get("Labels") or {}).get(TAG) != "1"}
    found = {"signal-low", "signal-high"} & here
    if side:
        if side not in ("low", "high"):
            raise SystemExit("--side is low or high")
        if f"signal-{side}" not in found:
            raise SystemExit(f"--side {side}, but this host runs no signal-{side} containers")
        return side
    if len(found) == 2:
        return "one"
    if not found:
        raise SystemExit("this host runs none of the relay's containers")
    return found.pop().split("-")[1]


def main(argv):
    global ROLE
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    at_text, side = option(argv, "at"), option(argv, "side")
    args = [a for i, a in enumerate(argv) if not a.startswith("--") and (i == 0 or argv[i - 1] not in ("--at", "--side"))]
    cmd = args[0] if args else "list"
    if cmd == "list":
        for g, title in (("a", "A. Network"), ("b", "B. Docker"), ("c", "C. Application")):
            print(title)
            for n, t in TESTS.items():
                if t["group"] == g:
                    print(f"  {n:24} {t['side']:4} ~{t['minutes']} min{'' if t['quick_ok'] else ' (not --quick)'}"
                          f"{' (runs last)' if t['last'] else ''}  {t['title']}")
        print("\nlow / high: the side the fault is on (checks are always on the high side, where the console is).")
        print("(runs last): costs monitoring packets, so the diode link stays WARN for up to an hour after.")
        return 0
    if cmd in ("restore", "test", "group", "all"):
        ROLE = find_role(side)
    if cmd == "restore":
        restore()
        return 0
    at = None
    if cmd in ("test", "group", "all"):
        if ROLE != "one" and not at_text:
            raise SystemExit(f"This host has only the {ROLE} side: split mode. Run the same command on the low and the high "
                             f"host, both with --at HH:MM (the same UTC time, a few minutes from now).")
        if ROLE == "one" and at_text:
            raise SystemExit("Both sides run on this host: no --at (to try split mode here, add --side low or --side high).")
        at = parse_at(at_text) if at_text else None
        if ROLE == "high" and not TOKEN:
            raise SystemExit("Run tools/sim/run setup first (the console token).")
    if cmd == "test":
        bad = [n for n in args[1:] if n not in TESTS]
        if bad or not args[1:]:
            raise SystemExit(f"unknown test(s): {', '.join(bad) or '(none given)'}; see: run list")
        return run(args[1:], at)
    if cmd == "group":
        names = [n for n, t in TESTS.items() if t["group"] in args[1:]]
        return run(names, at) if names else (print("groups: a, b, c") or 2)
    if cmd == "all":
        return run(list(TESTS), at)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
