#!/usr/bin/env python3
"""The relay's fault-simulation tests: put a real fault on, write down everything the monitor shows, put it all back.

TEST TOOL: dev, or prod before the relay goes live. Run through tools/sim/run (a container with the Docker socket).
Standard library only. It acts only on containers of the relay's compose projects (signal-low, signal-high) and on
containers it starts itself (label signal-sim=1); network faults run in a helper inside ONE target container's own
network namespace, never the host's. It reads the console's read-only monitoring API with a token.

A test does not judge: the fault stays on for a fixed time, and the test lists what the monitor showed meanwhile
and after (each new line in its Log, and the diagram changes the Log has no line for). No line at all is a finding.

    python sim.py [menu] | list | test NAME... | all | restore

Two hosts (prod): both open the menu; the high host waits for the low host's marker (a container that
container-facts reports across the diode) and watches from the moment the low host put the fault on.
Every test is labelled at the console (POST /sim on its internal port, and the low host's marker), so its
incidents and Log lines say "test" and never count as real history.
"""
import http.client, json, os, random, re, signal, socket, string, sys, time, urllib.request

def read_token():       # from the mounted folder, never the command line (it would show in ps / docker inspect)
    try:
        return open("/sim/.token").read().strip()
    except OSError:
        return ""


TOKEN = read_token()
API = os.environ.get("SIM_API", "http://console:8091")          # the console's internal port (token required)
HOST_DIR = os.environ.get("SIM_HOST_DIR", "")                   # tools/sim's path ON THE HOST (test subscribers' routes)
CHAT_IN = os.environ.get("SIM_CHAT_IN", "")                      # where chat enters the high side (default below)
HELPER = "signal-sim-helper:1"
PROJECTS = {"signal-low", "signal-high"}
TAG = "signal-sim"                                                # label on our containers, comment on our rules
ROLE = "one"            # one: both sides on this host (dev); low / high: this host has only that side (main())


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
    """A container of ours that this side may remove: on two hosts, never the other side's (both may share a host
    when --side is tried on dev)."""
    side = labels.get("signal-sim.side")
    return labels.get(TAG) == "1" and (ROLE == "one" or side not in ("low", "high") or side == ROLE)


def restore(quiet=False):
    """Undo everything recorded, plus anything tagged ours: helpers, markers and test containers removed, rules
    cleared, paused relay containers unpaused, stopped ones started again, memory limits put back."""
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
        if mine(labels):
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


def fault(target, rules, undo, label="a network fault"):
    """A lasting fault: RULES applied now, UNDO run when the helper is stopped (and by restore's CLEAN anyway)."""
    say(f"Putting the fault on in {target}: {label}")
    script = f"""set -e
{rules}
echo applied
trap '{undo}; echo undone; exit 0' TERM INT
while :; do sleep 1; done"""
    name = helper(target, script)
    t0 = time.time()
    while "applied" not in logs(name, 20):          # else a test would wait, then blame the monitoring
        if not docker("GET", f"/containers/{name}/json")["State"]["Running"] or time.time() - t0 > 15:
            out = logs(name, 5).strip().replace("\n", " / ")
            raise Fail(f"it did not take in {target}: {out[-200:] or 'no output'}")
        time.sleep(0.5)
    return name


def block_internet(target, mode):
    """Everything from TARGET to the internet dropped or refused; local and private addresses still pass."""
    act = "-j DROP" if mode == "drop" else "-p tcp -j REJECT --reject-with tcp-reset"
    lines = [f"iptables -{{op}} OUTPUT -d {n} -m comment --comment {TAG} -j ACCEPT" for n in LOCAL4.split()]
    lines += [f"iptables -{{op}} OUTPUT -m comment --comment {TAG} {act}",
              f"ip6tables -{{op}} OUTPUT ! -d ::1/128 -m comment --comment {TAG} -j DROP 2>/dev/null || true"]
    return fault(target, "\n".join(l.format(op="A") for l in lines), "; ".join(l.format(op="D") for l in lines),
                 f"its internet traffic {'refused' if mode == 'reject' else 'silently dropped'} (local addresses still work)")


def end_fault(name):
    say("Taking the fault away")
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


def label(test, secs):
    """Tell the console a test runs here (its incidents and Log lines meanwhile say "test"); secs 0: none. The
    console's internal port, like the receivers' reports. A low host can't reach it: its marker container does it."""
    try:
        req = urllib.request.Request(API + "/sim", json.dumps({"test": test, "for_s": secs}).encode(),
                                     {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:
        say(f"Could not tell the console this is a test ({e}); its incidents won't say \"test\".")


class Fail(Exception):
    """The fault could not be put on: a problem of the test, not something the monitor did."""


def now_ms():
    return int(time.time() * 1000)


def ms(t):
    return int(t * 1000)


def until(t, step=None):
    """Sleep until T; a Step's line keeps counting meanwhile."""
    while time.time() < t:
        if step:
            step.tick()
        time.sleep(min(0.5 if step else 5, max(0, t - time.time())))


def mins(t):
    return f"{int(t // 60)}:{int(t % 60):02d}" if t >= 60 else f"{t:.0f} s"


def clock(t):
    return time.strftime("%H:%M:%S", time.gmtime(t))


# ---------------------------------------------------------------- the timeline: one line per thing, as it happens
TTY = sys.stdout.isatty()
TEST_T0 = time.time()   # [0:00]: when the fault went on
LINES = []              # this test's lines, for the report
WIDTH = 78


def stamp(t=None):
    t = (time.time() if t is None else t) - TEST_T0
    return f"[{'-' if t < 0 else ''}{int(abs(t) // 60)}:{int(abs(t) % 60):02d}]"


def say(text, src="Test", t=None):
    """One line of the timeline. SRC: Test (what the test did), Log (a line in the monitor's Log), Diagram (a change
    on the Monitor page that the Log has no line for), Analyst (it started on an incident, or answered)."""
    import textwrap
    head = f"  {stamp(t):>7} {src:<8} "
    body = textwrap.wrap(text, WIDTH - len(head)) or [""]
    lines = [head + body[0]] + [" " * len(head) + b for b in body[1:]]
    print(("\r\033[K" if TTY else "") + "\n".join(lines), flush=True)
    LINES.extend(lines)


class Step:
    """Something that takes a while: on a terminal its line counts up, then goes."""
    def __init__(self, text, limit=None):
        self.text, self.limit, self.t0, self.shown = text, limit, time.time(), 0
        self.tick()

    def tick(self):
        if TTY and time.time() - self.shown >= 1:
            self.shown = time.time()
            print(f"\r\033[K  {stamp():>7} {'':<8} {self.text} ... {mins(time.time() - self.t0)}"
                  + (f" of {mins(self.limit)}" if self.limit else ""), end="", flush=True)

    def end(self):
        if TTY:
            print("\r\033[K", end="", flush=True)


# ---------------------------------------------------------------- watching the monitor
MARKER = re.compile(r"signal-sim-on-")      # the two-host marker container: not part of what is watched


class Watch:
    """Writes down everything the monitor shows from now on: each new line in its Log (every change of a box's
    state, every counted problem, container event and incident is one), and the changes on the diagram the Log has
    no line for (a box gone, a box's reason changing while its state stays, a box flapping, the Monitoring and room
    markers)."""
    def __init__(self):
        self.since, self.seen, self.logged, self.api_down = now_ms(), set(), {}, False
        self.count, self.phase = {"before": 0, "on": 0, "after": 0}, "before"
        self.ai_seen, self.ai_busy = None, set()
        self.last = self.snap()

    @staticmethod
    def snap():
        s = state()
        return {"parts": {p["key"]: (p["state"], p.get("code"), p.get("name") or p["key"]) for p in s["parts"]},
                "links": {h: (l.get("state"), l.get("loss_recent", l.get("loss_1h"))) for h, l in (s.get("links") or {}).items()},
                "rooms": {r: (x.get("state"), x.get("title") or r, x.get("text") or "") for r, x in (s.get("rooms") or {}).items()}}

    def add(self, t, src, text):
        say(text, src, t)
        self.count[self.phase] += 1

    def poll(self):
        try:            # the diagram first: a change on it already has its Log line (the console makes both at once)
            now = self.snap()
            evs = api(f"/api/monitor/events?since={self.since}&limit=500")["events"]
        except Exception as e:
            if not self.api_down:
                say(f"Can't read the monitoring API ({e}); trying again.")
            self.api_down = True
            return
        if self.api_down:
            say("The monitoring API answers again.")
            self.api_down = False
        for e in sorted(evs, key=lambda e: (e["at"], e.get("id", 0))):
            if e.get("id") in self.seen or MARKER.search(f"{e['part']} {e['text']}"):
                continue
            self.seen.add(e.get("id"))
            self.logged[e["part"]] = e["at"]
            self.add(e["at"] / 1000, "Log", e["text"])
        t, old, self.last = time.time(), self.last, now
        quiet = lambda k: self.logged.get(k, 0) < ms(t) - 30_000          # no Log line about it just now
        for k, (st, code, name) in now["parts"].items():
            o = old["parts"].get(k)
            if MARKER.search(k):
                continue
            reason = f" ({code})" if code not in (None, "OK") else ""
            if o is None and quiet(k):
                self.add(t, "Diagram", f"a new box: {name}, {st.upper()}{reason}")
            elif o and o[0] != st and quiet(k):
                self.add(t, "Diagram", f"{name}: {o[0].upper()} -> {st.upper()}{reason}")
            elif o and o[0] == st != "up" and o[1] != code and reason:
                self.add(t, "Diagram", f"{name}: still {st.upper()}, its reason is now {code}")
        for k, o in old["parts"].items():
            if k not in now["parts"] and not MARKER.search(k) and quiet(k):
                self.add(t, "Diagram", f"box gone: {o[2]}")
        for h, (st, loss) in now["links"].items():
            o = old["links"].get(h)
            if o and o[0] != st:
                self.add(t, "Diagram", f"Monitoring wires ({h}): {(o[0] or '?').upper()} -> {(st or '?').upper()}, "
                                       f"{loss or 0}% of monitoring packets lost in the last 5 min")
        for r, (st, title, text) in now["rooms"].items():      # markers have no colour: only new and gone ones
            if r not in old["rooms"]:
                self.add(t, "Diagram", f"a new room marker: {title}")
        for r in old["rooms"]:
            if r not in now["rooms"]:
                self.add(t, "Diagram", f"room marker gone: {old['rooms'][r][1]}")
        self.analyst(t)

    def analyst(self, t):
        """The Analyst: when it starts on an incident, and when it answers (how long, its headline)."""
        try:
            a = api("/api/monitor/analyst")
        except Exception:
            return
        if self.ai_seen is None:
            self.ai_seen = {x["id"] for x in a.get("answers", [])}
        for inc in a.get("working_on", []):
            if inc not in self.ai_busy:
                self.add(t, "Analyst", f"started on incident #{inc}")
        self.ai_busy = set(a.get("working_on", []))
        for x in sorted(a.get("answers", []), key=lambda x: x["id"]):
            if x["id"] in self.ai_seen:
                continue
            self.ai_seen.add(x["id"])
            what = {"explain": "explained", "wrapup": "wrote the wrap-up of", "ask": "answered a question about"}.get(x.get("kind"), "answered")
            about = f"incident #{x['incident']}" if x.get("incident") else "the relay"
            took = f" in {round((x.get('ms') or 0) / 1000)} s" if x.get("ms") else ""
            self.add((x.get("at") or ms(t)) / 1000, "Analyst",
                     f"{what} {about}{took}: " + (f"ERROR {x['error']}" if x.get("error") else x.get("headline") or "(no headline)"))

    def watch(self, t_end, text):
        st = Step(text, max(0, t_end - time.time()))
        while True:
            self.poll()
            if time.time() >= t_end:
                break
            until(min(t_end, time.time() + 3), st)
        st.end()


# ---------------------------------------------------------------- the tests
TESTS = {}


def test(name, group, title):
    def reg(cls):
        f = cls()
        TESTS[name] = {"group": group, "title": title, "fault": f, "side": f.side,
                       "minutes": max(1, -(-(f.hold + f.after) // 60))}
        return cls
    return reg


class Fault:
    """A fault. start/stop use only Docker (on the host whose side the fault is on), so on two hosts the low host
    runs them while the high host watches. side: where the fault goes on. hold: seconds it stays on (watched).
    after: seconds watched after it is taken away. oneshot: a fault that is over once it is done (test packets):
    watched for `hold`, nothing to take away."""
    side, hold, after, oneshot = "low", 120, 60, False

    def start(self):
        pass

    def stop(self):
        pass


def a_receiver():
    """(container, room) of a receiver that serves a room, from the diagram."""
    rx = [r for r in parts("receiver") if [x for x in r.get("rooms") or [] if x]]
    if not rx:
        raise Fail("no receiver with a room on the Monitor diagram")
    r = ([r for r in rx if r["state"] == "up"] or rx)[0]
    return r["name"], [x for x in r["rooms"] if x][0]


# The 15 tests (high/docs/monitoring-redesign.md section 7, 2026-10-20): each makes the monitor react differently.
# Dropped as duplicates: the silent internet cut (= 1), subscriber killed (= 7), crash once (in 11), old header (= 12),
# box removed (housekeeping), packet for the wrong room (room comes from routes.conf, never by hand).
# A. network ------------------------------------------------------------------------------------------------
class SignalCut(Fault):
    mode = "reject"

    def start(self):
        self.h = block_internet(find("signal"), self.mode)

    def stop(self):
        end_fault(self.h)


@test("net.signal-cut", "a", "signal-cli loses the internet")
class _(SignalCut):
    mode, hold, after = "reject", 225, 120


@test("net.signal-flaky", "a", "signal-cli on a bad network")
class _(Fault):
    hold, after = 120, 60

    def start(self):
        self.h = fault(find("signal"), "tc qdisc add dev eth0 root netem delay 300ms 100ms loss 30%", "tc qdisc del dev eth0 root",
                       "30% packet loss and 300 ms delay")

    def stop(self):
        end_fault(self.h)


@test("net.sub-link", "a", "Subscriber loses its link to signal-cli")
class _(Fault):
    hold, after = 90, 90

    def start(self):
        ip = list(inspect(find("signal"))["NetworkSettings"]["Networks"].values())[0]["IPAddress"]
        rule = f"iptables -A OUTPUT -d {ip} -p tcp --dport 8443 -m comment --comment {TAG} -j REJECT --reject-with tcp-reset"
        self.h = fault(find("subscriber"), rule, rule.replace("-A", "-D"), "its connections to signal-cli refused")

    def stop(self):
        end_fault(self.h)


class MonRx(Fault):
    """A rule in monitor-receiver (the high side). last: costs monitoring packets, so `all` runs these after the
    others (the Monitoring route counts loss over the last 5 min)."""
    side, rule, label, last = "high", "", "", False

    def start(self):
        rule = self.rule.format(tag=TAG, console=self.console() if "{console}" in self.rule else "")
        self.h = fault(find("monitor-receiver"), rule, rule.replace("-A", "-D"), self.label)

    def console(self):
        return list(inspect(find("console"))["NetworkSettings"]["Networks"].values())[0]["IPAddress"]

    def stop(self):
        end_fault(self.h)


@test("net.mon-route-down", "a", "Monitoring packets stop arriving")
class _(MonRx):
    hold, after, last = 180, 90, True
    rule, label = "iptables -A INPUT -p udp --dport 6511 -m comment --comment {tag} -j DROP", "every monitoring packet dropped on arrival"


@test("net.mon-route-lossy", "a", "Monitoring packets partly lost")
class _(MonRx):
    hold, after, last = 240, 60, True
    rule = "iptables -A INPUT -p udp --dport 6511 -m statistic --mode random --probability 0.25 -m comment --comment {tag} -j DROP"
    label = "1 in 4 monitoring packets dropped on arrival"


@test("net.console-away", "a", "Console out of reach for monitor-receiver")
class _(MonRx):
    hold, after = 60, 60
    rule = "iptables -A OUTPUT -d {console} -p tcp --dport 8091 -m comment --comment {tag} -j REJECT --reject-with tcp-reset"
    label = "its connections to the console refused"


# B. Docker ------------------------------------------------------------------------------------------------
class Stopped(Fault):
    """A relay container stopped, then started again."""
    hold, after = 90, 90

    def target(self):
        return find("subscriber")

    def start(self):
        self.name = self.target()
        say(f"Stopping {self.name} (docker stop)")
        docker("POST", f"/containers/{self.name}/stop?t=10", timeout=60)
        remember("start", self.name)

    def stop(self):
        say(f"Starting {self.name} again")
        docker("POST", f"/containers/{self.name}/start")
        forget("start", self.name)


@test("docker.stop-subscriber", "b", "Subscriber stopped")
class _(Stopped):
    pass


@test("docker.stop-receiver", "b", "Receiver stopped")
class _(Stopped):
    side = "high"

    def target(self):
        return find(name=a_receiver()[0])


@test("docker.pause-signal-cli", "b", "signal-cli frozen")
class _(Fault):
    hold, after = 70, 120

    def start(self):
        self.name = find("signal")
        say(f"Freezing {self.name} (docker pause)")
        docker("POST", f"/containers/{self.name}/pause")
        remember("unpause", self.name)

    def stop(self):
        say(f"Unfreezing {self.name}")
        docker("POST", f"/containers/{self.name}/unpause")
        forget("unpause", self.name)


class TestBox(Fault):
    """A throwaway container in the low project (so container-facts reports it), removed afterwards. It misbehaves
    after 35 s: container-facts reports changes only for a container it has already seen."""
    name, cmd, memory, restart, after = "", [], None, None, 30

    def start(self):
        say(f"Starting the test container {self.name} (it misbehaves after 35 s)")
        run_container(self.name, "python:3.13-alp-u3", self.cmd, memory=self.memory, restart=self.restart,
                      labels={"com.docker.compose.project": "signal-low", "com.docker.compose.service": self.name})

    def stop(self):
        say(f"Removing {self.name}")
        remove(self.name)
        forget("remove", self.name)


@test("docker.memory", "b", "Memory: near the limit, then out of memory")
class _(TestBox):
    # 35 s quiet, then ~88% of its limit for 75 s (CONTAINER_MEM_HIGH), then more than it may have (OOM)
    name, memory, hold, after = "signal-sim-memory", 64 << 20, 150, 30
    cmd = ["python", "-c", "import time; time.sleep(35); b = bytearray(56 << 20)\nfor i in range(0, len(b), 4096): b[i] = 1\n"
           "time.sleep(75); c = []\nwhile True: c.append(bytearray(4 << 20)); time.sleep(0.05)"]


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


def udp(addr, payloads, what):
    say(f"Sending {what} to {addr}")
    host, port = addr.rsplit(":", 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for p in payloads:
        s.sendto(p.encode(), (host, int(port)))


def part_line(room, sess, q, m, i, n, d):
    body = json.dumps({"s": sess, "q": q, "m": m, "i": i, "n": n, "d": d}, separators=(",", ":"))
    return f"CEF:0|CHAT|{room}|{i}|{n}|SIGNAL-RELAY|APP|" + body.replace("\\", "\\\\").replace("=", "\\=").replace("|", "\\|")


def rnd():
    return "".join(random.choice(string.ascii_lowercase) for _ in range(5))


class Packets(Fault):
    side, oneshot, hold, after = "high", True, 60, 0


@test("app.bad-packet", "c", "Junk packet")
class _(Packets):
    def start(self):
        rx, room = a_receiver()
        udp(chat_in(rx), [f"CEF:0|CHAT|{room}|1|1|SIGNAL-RELAY|APP|this is not json"] * 3, f"3 junk packets for room {room}")


@test("app.packets-lost", "c", "Packets lost on the way")
class _(Packets):
    hold = 90

    def start(self):
        rx, room = a_receiver()
        s = "sim" + rnd()          # parts of messages that never complete: nothing reaches the console or Gotham
        udp(chat_in(rx), [part_line(room, s, 0, 1, 1, 2, "{\"a\":"), part_line(room, s, 2, 2, 1, 3, "{\"envelope\":"),
                          part_line(room, s, 3, 2, 2, 3, "{}")],
            f"a test stream for room {room}: packet 1 never comes, and message 2 arrives without its last part")


def test_subscriber(name, routes_for, routes, restart=None):
    """A throwaway copy of the real subscriber (same image, settings, certificate, network, user, limits; so it
    works whatever prod's SIGNAL_URL or folder layout), but with its own routes file and sending nowhere
    (UDP_DEST 127.0.0.1:9): no message ever crosses. No read-write mount (the real one's logs/), no state, no sent
    log. It reads signal-cli like the real one and reports to signal-monitor under its own name."""
    say(f"Starting the test subscriber {name} (a copy of the real one that sends nowhere)")
    open(f"/reports/{name}.routes", "w").write(routes)
    real = inspect(find("subscriber"))
    cfg, hc = real["Config"], real["HostConfig"]
    env = dict(e.split("=", 1) for e in cfg.get("Env") or [] if "=" in e)
    env.update({"ROUTES_FILE": "/sim-routes.conf", "ROUTES_FOR": routes_for, "UDP_DEST": "127.0.0.1", "UDP_PORT": "9",
                "STATE_FILE": "", "SENT_LOG": "", "MONITOR_NAME": name})
    binds = [m for m in real["Mounts"] if m["Type"] == "bind" and not m["RW"]]
    run_container(name, cfg["Image"], cfg.get("Cmd") or [], host=name, network=hc.get("NetworkMode"), env=env,
                  workdir=cfg.get("WorkingDir") or None, user=cfg.get("User") or None, restart=restart,
                  memory=hc.get("Memory") or None,
                  binds=[f"{m['Source']}:{m['Destination']}:ro" for m in binds] +
                        [f"{HOST_DIR}/reports/{name}.routes:/sim-routes.conf:ro"],
                  labels={"com.docker.compose.project": "signal-low", "com.docker.compose.service": name})
    return name


FAKE_GROUP = "SIMTESTSIMTESTSIMTESTSIMTESTSIMTESTSIMTEST0="    # a well-formed group ID that never has messages


FAKE_GROUP = "SIMTESTSIMTESTSIMTESTSIMTESTSIMTESTSIMTEST0="    # a well-formed group ID that never has messages


def real_routes():
    """(ROUTES_FOR, first room) of the real subscriber, read from its own container (the low side has no console)."""
    import io, tarfile
    name = find("subscriber")
    env = dict(e.split("=", 1) for e in inspect(name)["Config"].get("Env") or [] if "=" in e)
    rf = env.get("ROUTES_FOR") or "signal-subscriber"
    path = env.get("ROUTES_FILE") or "/routes.conf"
    try:
        tar = tarfile.open(fileobj=io.BytesIO(docker("GET", f"/containers/{name}/archive?path={path}", raw=True)))
        text = tar.extractfile(tar.getmembers()[0]).read().decode()
        rooms = [l.split()[1] for l in text.splitlines() if l.split()[:1] == [rf] and len(l.split()) >= 3]
    except (RuntimeError, tarfile.TarError, IndexError, AttributeError):
        rooms = []
    if not rooms:
        raise Fail(f"could not read the real subscriber's room from {path}")
    return rf, rooms[0]


class TestSubscriber(Fault):
    name, rooms, restart = "", "SIMTEST", None

    def routes_for(self):
        return self.name

    def start(self):
        rf = self.routes_for()
        test_subscriber(self.name, rf, f"{rf} {self.rooms} {FAKE_GROUP}\n", self.restart)

    def stop(self):
        say(f"Removing {self.name}")
        remove(self.name)
        forget("remove", self.name)


@test("docker.crash-loop", "b", "A container in a crash loop")
class _(TestSubscriber):
    # a test subscriber with a broken routes file: it refuses to start, Docker restarts it again and again
    name, rooms, hold, after = "signal-sim-broken", "lowercase-room", 90, 30
    restart = {"Name": "on-failure", "MaximumRetryCount": 50}


@test("app.room-unserved", "c", "A room no receiver serves")
class _(TestSubscriber):
    name, hold, after = "signal-sim-subscriber", 120, 60


@test("app.subscriber-twice", "c", "A subscriber started twice")
class _(TestSubscriber):
    # the real one's name and room, with a made-up group: only SUBSCRIBER_TWICE shows, no SIMTEST room
    name, hold, after = "signal-sim-twin", 150, 120

    def start(self):
        rf, self.rooms = real_routes()
        test_subscriber(self.name, rf, f"{rf} {self.rooms} {FAKE_GROUP}\n", self.restart)


ORDER = ["net.signal-cut", "net.signal-flaky", "net.sub-link", "net.mon-route-down", "net.mon-route-lossy",
         "net.console-away", "docker.stop-subscriber", "docker.stop-receiver", "docker.pause-signal-cli",
         "docker.memory", "docker.crash-loop", "app.bad-packet", "app.packets-lost", "app.room-unserved",
         "app.subscriber-twice"]
TESTS = {n: TESTS[n] for n in ORDER}


# Counted problems that keep a working part WARN for 10 min (its window) after a test made them on purpose: junk,
# misrouted or incomplete test packets, refused test certificates. Not a reason to hold the next test back.
AFTERMATH = {"BAD_PACKET", "ROOM_MISMATCH", "PACKETS_MISSING", "MESSAGE_LOST", "GATEWAY_REFUSED"}


def ok(p):
    if "signal-sim" in p["key"] or p["state"] == "up":
        return True
    return p["state"] == "warn" and bool(p.get("active")) and set(p["active"]) <= AFTERMATH


def settle(within=60):
    """Before a test: every real box UP (else what the test sees is mixed with an older problem). None, or why not."""
    st = Step("Checking the relay is healthy (every box UP)", within)
    t_end = time.time() + within
    while True:
        bad = [f"{p['name']} {p['state'].upper()}" for p in state()["parts"] if not ok(p)]
        if not bad or time.time() > t_end:
            st.end()
            return f"not every box is UP: {', '.join(bad)}" if bad else None
        until(time.time() + 3, st)


# ---------------------------------------------------------------- running one test
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


def numbered():         # the same number for a test on every host
    return {i + 1: n for i, n in enumerate(TESTS)}


def number(n):
    return next(i for i, x in numbered().items() if x == n)


def run_test(n, unhealthy="ask"):
    """One test, start to end, everything put back after. UNHEALTHY: what to do if the relay isn't healthy first
    (ask, or run). Returns this test's lines for the report."""
    global TEST_T0, LINES
    i, t = number(n), TESTS[n]
    f = t["fault"]
    LINES = [f"{i}. {t['title']} ({n}), {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} UTC"]
    TEST_T0 = time.time()
    print()
    if ROLE != "low":
        why = settle()
        if why:
            say(f"Before the start, {why}. What follows may be mixed with that.")
            if unhealthy == "ask" and ask("  Run it anyway? (y and Enter: yes) ") != "y":
                return None
    if ROLE == "low":
        low_side(n, f)
        return LINES
    w = None
    try:
        label(n, f.hold + f.after + 180)
        w = Watch()
        if ROLE == "high" and f.side == "low":
            TEST_T0 = wait_low(i, n, f)
            say(f"The low host put the fault on at {clock(TEST_T0)} UTC (its clock).")
        else:
            TEST_T0 = time.time()
            f.start()
        if f.oneshot:
            say(f"Done. Watching the monitor for {mins(f.hold)}.")
        else:
            say(f"The fault is on. Watching the monitor for {mins(f.hold)}.")
        w.phase = "on"
        w.watch(TEST_T0 + f.hold, "Watching" if f.oneshot else "Fault on, watching")
        if not f.oneshot:
            if ROLE == "high" and f.side == "low":
                say(f"The low host takes the fault away about now. Watching for {mins(f.after)} more.")
            else:
                f.stop()
                say(f"The fault is off. Watching for {mins(f.after)} more.")
            w.phase = "after"
            w.watch(time.time() + f.after, "Fault off, watching")
    except Fail as e:
        say(f"Could not put the fault on: {e}")
        w = None
    except KeyboardInterrupt:
        say("Stopped (Ctrl-C).")
        w = None
        raise
    except Exception as e:
        say(f"The test itself broke: {type(e).__name__}: {e}")
        w = None
    finally:
        say("Making sure everything is put back")
        restore(quiet=True)
        label(n, 90)              # what still settles in the next 90 s belongs to the test too
        if w:
            summary(w, f)
    return LINES


def summary(w, f):
    on, after = w.count["on"], w.count["after"]
    if f.oneshot:
        text = f"The monitor showed {on} thing(s)." if on else "The monitor showed NOTHING."
    else:
        text = (f"The monitor showed {on} thing(s) while the fault was on" if on else
                "The monitor showed NOTHING while the fault was on") + f", {after} after it was taken away."
    print(f"\n  {text}")
    LINES.extend(["", f"  {text}"])


def low_side(n, f):
    """Low host of two: the fault on for its time, with a marker naming the test and its start (container-facts
    carries it across the diode; the high host watches from that start)."""
    global TEST_T0
    try:
        TEST_T0 = time.time()
        f.start()
        run_container(f"signal-sim-on-{n}-{int(TEST_T0)}", "python:3.13-alp-u3", ["sleep", "86400"],
                      labels={"com.docker.compose.project": "signal-low", "com.docker.compose.service": "signal-sim"})
        say("Told the high host the fault is on (it sees that within about 30 s).")
        st = Step("Fault on", f.hold)
        until(TEST_T0 + f.hold, st)
        st.end()
        f.stop()
        say("The fault is off. The high host keeps watching: read what the monitor showed there.")
    except Fail as e:
        say(f"Could not put the fault on: {e}")
    finally:
        say("Making sure everything is put back")
        restore(quiet=True)


def wait_low(i, n, f):
    """High host of two: wait for the low host's marker for this test; returns when it put the fault on."""
    prefix, since = f"signal-sim-on-{n}-", time.time()

    def started():
        for names in state()["containers"].values():
            for name in names or {}:
                if name.startswith(prefix) and name[len(prefix):].isdigit() and int(name[len(prefix):]) > since - f.hold:
                    return int(name[len(prefix):])
    print(f"  Now pick test {i} in the menu on the LOW host and start it there.\n")
    st = Step("Waiting for the low host to put the fault on", 600)
    while not (t0 := started()):
        if time.time() - since > 600:
            st.end()
            raise Fail("the low host didn't put it on within 10 min")
        until(time.time() + 3, st)
    st.end()
    return t0


def write_report(tests):
    path = f"sim-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}{'' if ROLE == 'one' else '-' + ROLE}.txt"
    with open(f"/reports/{path}", "w") as out:
        out.write(f"Relay monitoring test on {host_allowed()} ({ROLE_TEXT[ROLE]})\n\n")
        out.write("\n\n".join("\n".join(lines) for lines in tests if lines) + "\n")
    return path


# ---------------------------------------------------------------- the menu: one test at a time
ABOUT = {   # test: (what it does, where to look on the Monitor page, what it costs while it runs)
    "net.signal-cut": ("Cuts signal-cli off from the internet: its connections are refused.",
                       "the Signal servers box and its wire, signal-cli, the bell, the Analyst.", "New Signal messages wait on Signal's servers."),
    "net.signal-flaky": ("Gives signal-cli a bad network: 30% packet loss and 300 ms delay.",
                         "the Signal servers box (does it stay out of DOWN?).", "Messages may arrive late."),
    "net.sub-link": ("Cuts the subscriber off from signal-cli (its event stream).",
                     "the wire signal-cli -> subscriber, the subscriber (WARN), signal-cli (UP), the bell.", "Messages wait in signal-cli."),
    "net.mon-route-down": ("Drops every monitoring packet as it reaches monitor-receiver (as if the diode's monitoring "
                           "route were down).", "the low-side boxes (UNKNOWN), the Monitoring wires, the bell.",
                           "No chat is affected."),
    "net.mon-route-lossy": ("Drops 1 in 4 monitoring packets at monitor-receiver.",
                            "the Monitoring wires and marker (loss over the last 5 min).", "No chat is affected."),
    "net.console-away": ("monitor-receiver can't reach the console (its connections are refused).",
                         "monitor-receiver's wire to the console, the low-side boxes (nothing should go STALE).",
                         "Nothing (monitor-receiver holds the reports)."),
    "docker.stop-subscriber": ("Stops the subscriber (docker stop), then starts it again.",
                               "the subscriber (DOWN, container stopped), its room wires, the bell.", "Messages wait in signal-cli."),
    "docker.stop-receiver": ("Stops one receiver (docker stop), then starts it again.",
                             "that receiver, its room's wire and Gotham box, the bell.",
                             "Messages for that receiver's room are LOST while it is stopped: run it on a quiet relay."),
    "docker.pause-signal-cli": ("Freezes signal-cli (docker pause), then unfreezes it.",
                                "signal-cli (DOWN, frozen), the subscriber (WARN), the bell.",
                                "New Signal messages wait on Signal's servers."),
    "docker.memory": ("Starts a test container that sits at about 88% of its memory limit, then uses more than it may.",
                      "the Log (memory high, then out of memory), the bell.", "Nothing (a throwaway container)."),
    "docker.crash-loop": ("Starts a test subscriber with a broken routes file: it refuses to start, Docker restarts it "
                          "again and again.", "the Log (restarts), the bell.", "Nothing (it sends nowhere)."),
    "app.bad-packet": ("Sends 3 junk packets (a valid header, then no JSON) to a receiver.",
                       "that receiver (stays UP), the Log.", "Nothing."),
    "app.packets-lost": ("Sends a test stream with a packet missing, and a message without its last part.",
                         "that receiver, the Log, the bell (a loss incident), the \"All\" window (a lost notice).",
                         "Nothing real is lost: test packets only. A lost-message notice shows in \"All\"."),
    "app.room-unserved": ("Starts a test subscriber that sends to a room no receiver serves (SIMTEST).",
                          "its box, the SIMTEST room marker and its wire (DOWN: no receiver serves it), the bell.",
                          "Nothing (it sends nowhere)."),
    "app.subscriber-twice": ("Starts a test subscriber with the real one's name and room (as if started twice).",
                             "both subscribers (WARN: started twice), the bell.", "Nothing (it sends nowhere)."),
}

GROUPS = (("a", "Network"), ("b", "Docker"), ("c", "Application"))
ROLE_TEXT = {"one": "both sides on this host", "low": "the LOW side; the high host watches",
             "high": "the HIGH side; faults marked [low host] go on there"}


def health_line():
    if ROLE == "low":
        return "This is the low host: the monitor is watched on the high host."
    try:
        bad = [f"{p['name']} {p['state'].upper()}" for p in state()["parts"] if not ok(p)]
    except Exception as e:
        return f"Can't read the console's monitoring: {e}"
    return "Relay now: every box UP." if not bad else "Relay now: NOT every box UP: " + ", ".join(bad)


def ask(prompt):
    try:
        return input(prompt).strip().lower()
    except EOFError:
        return "q"


def para(label, text):
    import textwrap
    print(textwrap.fill(text, WIDTH, initial_indent=f"  {label:<15}", subsequent_indent=" " * 17))


def timing(f):
    return (f"done once, then the monitor is watched for {mins(f.hold)}" if f.oneshot else
            f"the fault is on for {mins(f.hold)}, then the monitor is watched {mins(f.after)} more")


def menu():
    host = host_allowed()
    preflight()
    restore(quiet=True)
    nums = numbered()
    while True:
        if TTY:
            print("\033[2J\033[H", end="")
        print(f"Relay monitoring tests · host {host} · {ROLE_TEXT[ROLE]}\n{health_line()}\n")
        for g, title in GROUPS:
            print(title)
            for i, n in nums.items():
                t = TESTS[n]
                if t["group"] == g and (ROLE != "low" or t["side"] == "low"):
                    where = "  [low host]" if ROLE == "high" and t["side"] == "low" else ""
                    print(f"  {i:2}  {t['title']} (~{t['minutes']} min){where}")
            print()
        print("   r  Undo anything a stopped test left behind\n   q  Quit\n")
        c = ask("Pick a test number: ")
        if c in ("q", "quit", "exit"):
            break
        if c == "r":
            restore()
            ask("\nPress Enter for the menu. ")
            continue
        if not c.isdigit() or int(c) not in nums or (ROLE == "low" and TESTS[nums[int(c)]]["side"] != "low"):
            continue
        i, n = int(c), nums[int(c)]
        t = TESTS[n]
        does, look, cost = ABOUT[n]
        print(f"\n{i}. {t['title']}  (about {t['minutes']} min)\n")
        para("What it does:", does)
        para("How long:", timing(t["fault"]).capitalize() + ".")
        if ROLE != "low":
            para("Where to look:", look)
        para("While it runs:", cost)
        if ROLE == "high" and t["side"] == "low":
            para("Two hosts:", f"the fault goes on on the low host. Press Enter here first, then start test {i} in "
                               f"the menu on the low host. This host watches from the moment it goes on.")
        if ROLE == "low":
            para("Two hosts:", f"start test {i} on the high host first (it waits for this one). What the monitor "
                               f"showed is listed there.")
        if ROLE != "low":
            print("\n  The test lists everything the monitor shows, as it happens:")
            print("    Log      a new line in the monitor's Log")
            print("    Diagram  a change on the Monitor page that has no Log line")
            print("    Analyst  the Analyst started on an incident, or answered")
            print("    Test     what the test itself did")
        print("\n  Ctrl-C stops it and puts everything back.")
        if ask("\nPress Enter to start (b and Enter: back): ") != "":
            continue
        try:
            lines = run_test(n)
        except KeyboardInterrupt:
            print("\n  Stopped; everything put back.")
            lines = LINES
        if lines:
            print(f"\n  Saved in tools/sim/reports/{write_report([lines])}")
        ask("\nPress Enter for the menu. ")
    restore(quiet=True)


# ---------------------------------------------------------------- batch: no menu, one host
def run(names):
    host_allowed()
    preflight()
    restore(quiet=True)
    print(f"{len(names)} test(s). Ctrl-C stops and puts everything back.")
    done = []
    try:
        for n in names:
            print(f"\n{number(n)}. {TESTS[n]['title']} ({n}): {timing(TESTS[n]['fault'])}")
            done.append(run_test(n, unhealthy="run"))
    except KeyboardInterrupt:
        restore(quiet=True)
        print("\nStopped; everything put back.")
        done.append(LINES)
    print(f"\nSaved in tools/sim/reports/{write_report(done)}")
    return 0


def option(argv, name):
    """--name VALUE or --name=VALUE"""
    for i, a in enumerate(argv):
        if a == f"--{name}" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1]
    return None


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
    side = option(argv, "side")
    args = [a for i, a in enumerate(argv) if not a.startswith("--") and (i == 0 or argv[i - 1] != "--side")]
    cmd = args[0] if args else "menu"
    if cmd == "list":
        for g, title in GROUPS:
            print(title)
            for i, n in numbered().items():
                t = TESTS[n]
                if t["group"] == g:
                    print(f"  {i:2}  {n:24} {t['side']:4} ~{t['minutes']} min  {t['title']}")
        print("\nlow / high: the side the fault goes on (the monitor is watched on the high side, where the console is).")
        return 0
    if cmd not in ("menu", "restore", "test", "all"):
        print(__doc__)
        return 2
    ROLE = find_role(side)
    if cmd == "restore":
        restore()
        return 0
    if ROLE != "low" and not TOKEN:
        raise SystemExit("Run tools/sim/run setup first (the console token).")
    if cmd == "menu":
        menu()
        return 0
    if ROLE != "one":
        raise SystemExit(f"This host has only the {ROLE} side: use the menu (tools/sim/run) on both hosts.")
    names = list(TESTS) if cmd == "all" else args[1:]
    bad = [n for n in names if n not in TESTS]
    if bad or not names:
        raise SystemExit(f"unknown test(s): {', '.join(bad) or '(none given)'}; see: run list")
    return run(sorted(names, key=lambda n: getattr(TESTS[n]["fault"], "last", False)))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
