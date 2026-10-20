# signal-sim-helper: network faults for tools/sim. Runs inside ONE relay container's own network namespace
# (--network container:<name>): iptables (block, refuse, random drop) and tc netem (delay, loss). Never the host's.
# Built once by tools/sim/run setup (needs the internet). Air-gapped host: tools/sim/run export on dev, copy tools/sim,
# and setup there loads it.
FROM alpine:3.24
RUN apk add --no-cache iptables ip6tables iproute2
CMD ["sh"]
