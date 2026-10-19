# signal-sim-helper: network faults for tools/sim. Runs inside ONE relay container's own network namespace
# (--network container:<name>): iptables (block, refuse, random drop) and tc netem (delay, loss). Never the host's.
# Build once (needs the internet): tools/sim/run setup. Air-gapped host: docker save here, docker load there.
FROM alpine:3.24
RUN apk add --no-cache iptables ip6tables iproute2
CMD ["sh"]
