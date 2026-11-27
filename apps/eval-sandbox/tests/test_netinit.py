"""Packet rules: the gateway reaches the world and resolvers, then public web ports only."""

from __future__ import annotations

from daimon_eval_sandbox.netinit import ipv4_rules


def test_gateway_reaches_world_and_resolver_before_private_ranges_close() -> None:
    lines = ipv4_rules([("10.0.0.5", 8090)], ["10.0.0.10"]).splitlines()
    gateway = [line for line in lines if line.startswith("-A EVAL_GW")]
    world = gateway.index("-A EVAL_GW -d 10.0.0.5 -p tcp --dport 8090 -j ACCEPT")
    resolver = gateway.index("-A EVAL_GW -d 10.0.0.10 -p udp --dport 53 -j ACCEPT")
    private = gateway.index("-A EVAL_GW -d 10.0.0.0/8 -j REJECT")
    web = gateway.index("-A EVAL_GW -p tcp -m multiport --dports 80,443 -j ACCEPT")
    assert world < private and resolver < private, "the world and resolver open first"
    assert "-A EVAL_GW -d 169.254.0.0/16 -j REJECT" in gateway[:web], "no metadata service"
    assert gateway[-1] == "-A EVAL_GW -j REJECT", "anything else is closed"
    assert "-A EVAL_IN -j DROP" in lines, "inbound connections close but the harness port's"
