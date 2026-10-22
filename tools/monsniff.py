"""Prints every monitoring packet (CEF ...|MONITOR|) a container sends or gets: time, sent/got, size, seq, type.
Read-only: a packet socket inside that container's own network namespace; nothing on the host changes.
Stops by itself after SECONDS (default 180). Low host (what leaves):
  docker run --rm -i --net container:signal-monitor --cap-add NET_RAW python:3.13-alp-u3 python - 180 < monsniff.py
High host (what arrives):
  docker run --rm -i --net container:signal-monitor-receiver --cap-add NET_RAW python:3.13-alp-u3 python - 180 < monsniff.py
A seq sent but never got is a packet the path lost: its size and type point at the cause."""
import json, signal, socket, struct, sys, time

signal.signal(signal.SIGALRM, lambda *_: sys.exit(0))   # PID 1 in a container ignores the default action
signal.alarm(int(sys.argv[1]) if len(sys.argv) > 1 else 180)
s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))        # every packet, every interface
while True:
    pkt, addr = s.recvfrom(65535)
    way = {socket.PACKET_OUTGOING: "sent", socket.PACKET_HOST: "got "}.get(addr[2])
    if not way or pkt[12:14] != b"\x08\x00":                                 # sent or received here, IPv4
        continue
    ip = pkt[14:]
    if ip[9] != 17:                                                          # UDP
        continue
    ihl = (ip[0] & 15) * 4
    dport, ulen = struct.unpack("!HH", ip[ihl + 2:ihl + 6])
    data = ip[ihl + 8:ihl + ulen]
    if b"|MONITOR|" not in data[:40]:
        continue
    try:
        m = json.loads(data[data.index(b"{"):])
        what = f"seq {m.get('seq')}  {m.get('t')}  {m.get('comp') or m.get('code') or ''}"
    except ValueError:
        what = "(not readable)"
    print(time.strftime("%H:%M:%S") + f".{int(time.time()*1000)%1000:03d}", f"{way} {len(data):5d} bytes  port {dport}  {what}", flush=True)
