#!/usr/bin/env python3
"""container-facts: a read-only view of this relay's own containers, for signal-monitor.

It holds the Docker socket so signal-monitor doesn't have to. It answers one thing, GET /facts, with a fixed set
of fields for the containers of one compose project (PROJECT, default signal-low):
    name, service (its compose service), state, status, exit_code, oom_killed, restarts, started, finished, health, image,
    and for running containers: mem_pct, cpu_pct, pids_pct (of their own limits; None without a limit), and id (the
    short container ID, which is also the container's host name: how a subscriber's tile finds its container)
It never forwards a Docker API call: no other path or method exists. Inspect output is reduced to those fields
(the full output has every container's environment, which can hold keys), and other apps' containers on the same
host are never listed. The socket is still powerful: keep this container small, read-only and on the project's
own network, with no published port.

Settings: PROJECT (signal-low), LISTEN_PORT (8094), DOCKER_SOCK (/var/run/docker.sock).
"""
import http.client, json, logging, os, signal, socket, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

PROJECT = os.environ.get("PROJECT", "signal-low")
PORT    = int(os.environ.get("LISTEN_PORT", 8094))
SOCK    = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("container-facts")


class UnixConnection(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("localhost", timeout=5)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect(SOCK)


def docker_get(path):
    """GET one Docker API path (only the three used below are ever asked for: list, inspect, one-shot stats)."""
    c = UnixConnection()
    try:
        c.request("GET", path)
        r = c.getresponse()
        body = r.read()
        if r.status != 200:
            raise OSError(f"Docker answered {r.status} for {path.split('?')[0]}")
        return json.loads(body)
    finally:
        c.close()


CPU_LAST = {}                                                # container id -> (its CPU ns, the host's CPU ns)


def usage(cid, d):
    """Memory, CPU and processes as % of the container's own limits (one-shot stats: no 1 s wait). CPU is the
    share of the CPUs it may use since the previous call. None where there is no limit to compare with."""
    try:
        s = docker_get(f"/containers/{cid}/stats?stream=false&one-shot=true")
    except (OSError, ValueError):
        return {}
    hc, m = d.get("HostConfig") or {}, s.get("memory_stats") or {}
    out = {}
    if hc.get("Memory") and m.get("usage") is not None:          # in use, not counting reclaimable file cache
        used = m["usage"] - (m.get("stats") or {}).get("inactive_file", 0)
        out["mem_pct"] = round(100 * used / hc["Memory"], 1)
    pids, pl = (s.get("pids_stats") or {}).get("current"), hc.get("PidsLimit")
    if pids is not None and pl and pl > 0:
        out["pids_pct"] = round(100 * pids / pl, 1)
    cpu = s.get("cpu_stats") or {}
    now = ((cpu.get("cpu_usage") or {}).get("total_usage"), cpu.get("system_cpu_usage"))
    last = CPU_LAST.get(cid)
    CPU_LAST[cid] = now
    if last and None not in now and now[1] > last[1]:
        cpus = cpu.get("online_cpus") or 1
        allowed = (hc.get("NanoCpus") or 0) / 1e9 or cpus           # its CPU limit, or all of the host's
        out["cpu_pct"] = round(100 * (now[0] - last[0]) / (now[1] - last[1]) * cpus / allowed, 1)
    return out


def facts():
    flt = quote(json.dumps({"label": [f"com.docker.compose.project={PROJECT}"]}))
    out = []
    for c in docker_get(f"/containers/json?all=1&filters={flt}"):
        d = docker_get(f"/containers/{c['Id']}/json")
        st = d.get("State") or {}
        out.append({**(usage(c["Id"], d) if st.get("Status") == "running" else {}), "id": c["Id"][:12],
            "name": (d.get("Name") or "").lstrip("/"),
            "service": ((d.get("Config") or {}).get("Labels") or {}).get("com.docker.compose.service"),
            "state": st.get("Status"),                       # running, exited, restarting, paused, dead, created
            "exit_code": st.get("ExitCode"),
            "oom_killed": bool(st.get("OOMKilled")),
            "restarts": d.get("RestartCount", 0),
            "started": st.get("StartedAt"),
            "finished": st.get("FinishedAt"),
            "health": (st.get("Health") or {}).get("Status"),
            "image": (d.get("Config") or {}).get("Image"),
        })
    return sorted(out, key=lambda x: x["name"])


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path != "/facts":
            return self.reply(404, {"error": "only GET /facts"})
        try:
            self.reply(200, {"project": PROJECT, "at": int(time.time() * 1000), "containers": facts()})
        except (OSError, ValueError) as e:
            log.error("Could not read the containers from Docker: %s", e)
            self.reply(502, {"error": f"could not read Docker ({e})"})

    def refuse(self):
        self.reply(405, {"error": "read-only: only GET /facts"})

    do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = refuse

    def log_message(self, fmt, *args):                       # no access log line per request
        pass


def stop_now(*_):
    """docker stop sends SIGTERM; stop at once (as Ctrl-C would) instead of being killed 10 s later."""
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stop_now)
    try:
        n = len(facts())
        log.info("Reading Docker at %s: %d container(s) in project %s. Answering GET /facts on port %d.",
                 SOCK, n, PROJECT, PORT)
    except OSError as e:
        log.error("Can't read Docker at %s (%s). Is the socket mounted, and group_add set to its group? "
                  "Answering with errors until it works.", SOCK, e)
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        log.info("Stopping.")
        sys.exit(0)
