#!/usr/bin/env python3
"""ai_probe.py: is an AI endpoint usable for the relay assistant? Reliability, speed and tool calling.

Sends the same small troubleshooting question N times to an OpenAI-compatible endpoint (NVIDIA build, OpenRouter,
prod's vLLM), with one tool the model should call. Reports, per request and in total: answered / timed out / error,
seconds taken, and whether the model called the tool with valid arguments. Only made-up monitoring data is sent.
Standard library only; runs on the host.

  python3 tools/ai_probe.py nvidia      [-n 10] [--model ID] [--timeout 60]
  python3 tools/ai_probe.py openrouter  [-n 10] [--model ID]
  python3 tools/ai_probe.py URL         --model ID        any other endpoint, e.g. http://host:8000/v1
  python3 tools/ai_probe.py nvidia --models               list the model IDs the key can use

The key is read from KEY_FILE (default high/.env, gitignored, mode 600), never from the command line: the line
NVIDIA_API_KEY=... (nvidia), OPENROUTER_API_KEY=... (openrouter) or AI_API_KEY=... (any other endpoint).
"""
import argparse, json, os, statistics, sys, time, urllib.error, urllib.request

PROVIDERS = {
    "nvidia":     ("https://integrate.api.nvidia.com/v1", "nvidia/nemotron-3-super-120b-a12b", "NVIDIA_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "nvidia/nemotron-3-super-120b-a12b:free", "OPENROUTER_API_KEY"),
}
TOOL = {"type": "function", "function": {
    "name": "get_events",
    "description": "Monitoring events of the relay (changes of state, error lines), newest last.",
    "parameters": {"type": "object", "properties": {
        "part": {"type": "string", "description": "a part key, e.g. left/left-1/signal-monitor"},
        "since_minutes": {"type": "integer", "description": "how far back to look"}},
        "required": ["part", "since_minutes"]}}}
STATE = {"parts": [
    {"key": "left/left-1/signal-monitor", "state": "down", "since": "3 min ago", "text": "nothing heard for 3 min"},
    {"key": "left/left-1/relay-1/signal-cli", "state": "unknown", "since": "4 min ago"},
    {"key": "high/monitor-receiver", "state": "up", "last_heard": "12 s ago"},
    {"key": "high/receiver", "state": "up", "last_heard": "20 s ago"}],
    "diode_link": {"left-1": {"state": "down", "last_packet": "4 min ago", "loss_1h": 0.0}}}
MESSAGES = [
    {"role": "system", "content": "You help operators troubleshoot a one-way relay across a data diode. Use the "
     "tools to look at evidence before answering. Never suggest sending anything back across the diode."},
    {"role": "user", "content": "Current state:\n" + json.dumps(STATE) + "\nWhy is signal-cli UNKNOWN? Check the "
     "recent events of the part that matters first."}]


def call(base, key, body, timeout):
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions", json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def one(base, key, model, timeout):
    """(outcome, seconds, detail). outcome: tool / text / timeout / error."""
    t0 = time.monotonic()
    try:
        r = call(base, key, {"model": model, "messages": MESSAGES, "tools": [TOOL], "tool_choice": "auto",
                             "max_tokens": 600, "temperature": 0.2}, timeout)
    except TimeoutError:
        return "timeout", time.monotonic() - t0, f"no answer in {timeout} s"
    except urllib.error.HTTPError as e:
        return "error", time.monotonic() - t0, f"HTTP {e.code}: {e.read()[:160].decode('utf-8', 'replace')}"
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        kind = "timeout" if "timed out" in str(reason) else "error"
        return kind, time.monotonic() - t0, str(reason)[:160]
    secs = time.monotonic() - t0
    try:
        msg = r["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return "error", secs, "unexpected reply: " + json.dumps(r)[:160]
    calls = msg.get("tool_calls") or []
    if calls:
        f = calls[0].get("function", {})
        try:
            args = json.loads(f.get("arguments") or "{}")
            ok = f.get("name") == "get_events" and isinstance(args.get("part"), str)
        except ValueError:
            args, ok = f.get("arguments"), False
        return ("tool" if ok else "error"), secs, f"{f.get('name')}({json.dumps(args)})" + ("" if ok else " (bad arguments)")
    return "text", secs, "answered without calling the tool: " + (msg.get("content") or "").strip().replace("\n", " ")[:120]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("provider", help="nvidia, openrouter, or a base URL")
    ap.add_argument("-n", type=int, default=10, help="number of requests (default 10)")
    ap.add_argument("--model")
    ap.add_argument("--timeout", type=int, default=60, help="seconds per request (default 60)")
    ap.add_argument("--pause", type=float, default=3, help="seconds between requests (default 3)")
    ap.add_argument("--models", action="store_true", help="list the model IDs and stop")
    a = ap.parse_args()
    base, model, var = PROVIDERS.get(a.provider, (a.provider, None, "AI_API_KEY"))
    model, keyfile = a.model or model, os.environ.get("KEY_FILE", "high/.env")
    try:
        lines = open(keyfile).read().splitlines()
    except OSError as e:
        sys.exit(f"Can't read {keyfile}: {e.strerror}. Put {var}=<key> in it (chmod 600), or set KEY_FILE.")
    key = next((v.strip().strip('"\'') for k, _, v in (l.partition("=") for l in lines) if k.strip() == var), None)
    if not key:
        sys.exit(f"No {var}=... line in {keyfile}.")
    if a.models:
        req = urllib.request.Request(base.rstrip("/") + "/models", headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=a.timeout) as r:
            ids = sorted(m["id"] for m in json.load(r)["data"])
        print("\n".join(i for i in ids if "nemotron" in i.lower()) or "\n".join(ids))
        return
    if not model:
        sys.exit("Give --model for a custom endpoint.")
    print(f"{base}  model {model}  {a.n} request(s), timeout {a.timeout} s")
    results = []
    for i in range(1, a.n + 1):
        out, secs, detail = one(base, key, model, a.timeout)
        results.append((out, secs))
        print(f"{i:>3}  {out:<7} {secs:5.1f} s  {detail}")
        if i < a.n:
            time.sleep(a.pause)
    count = {k: sum(1 for o, _ in results if o == k) for k in ("tool", "text", "timeout", "error")}
    answered = [s for o, s in results if o in ("tool", "text")]
    print(f"\nanswered {len(answered)}/{a.n}  (called the tool {count['tool']}, text only {count['text']})"
          f"  timed out {count['timeout']}  errors {count['error']}")
    if answered:
        print(f"seconds: median {statistics.median(answered):.1f}, slowest {max(answered):.1f}")


if __name__ == "__main__":
    main()
