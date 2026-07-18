"""Role labels (docs/gtx2-bridge-reduced-role.md, #21 ruling b) — 4-way partition, drift-locked."""
from gtx2_bridge import catalog

NODE_FALLBACK = {"find", "activate", "set-time", "weather", "sync-health"}
NODE_ONLY = {"alarm-set", "dial-switch", "dial-list"}
RENDER = {"notify"}


def test_four_way_partition_covers_every_command():
    nf = set(catalog.fallback_only_commands())
    no = set(catalog.node_only_commands())
    perm = set(catalog.bridge_permanent())
    host = set(catalog.host_commands())
    allc = set(catalog.CATALOG)
    assert nf == NODE_FALLBACK
    assert no == NODE_ONLY
    assert perm == RENDER
    # disjoint + total
    groups = [nf, no, perm, host]
    for i in range(len(groups)):
        for j in range(i + 1, len(groups)):
            assert groups[i].isdisjoint(groups[j]), (groups[i] & groups[j])
    assert nf | no | perm | host == allc


def test_node_delegated_is_both_node_subclasses():
    assert set(catalog.node_delegated()) == NODE_FALLBACK | NODE_ONLY


def test_node_only_have_no_fallback():
    for n in catalog.node_only_commands():
        c = catalog.CATALOG[n]
        assert c.role == "node" and c.fallback_only is False


def test_notify_is_the_permanent_render_job():
    assert catalog.CATALOG["notify"].role == "render"
    assert catalog.CATALOG["notify"].fallback_only is False


def test_danger_stays_host_only_and_off_dashboard():
    for name in catalog.danger_commands():
        c = catalog.CATALOG[name]
        assert c.role == "host" and c.fallback_only is False and c.ha_expose is False


def test_manifest_exposes_node_only_and_fallback():
    m = catalog.manifest()
    assert set(m["fallback_only"]) == NODE_FALLBACK
    assert set(m["node_only"]) == NODE_ONLY
    assert set(m["roles"]) == {"node", "bridge_permanent", "host"}
    for c in m["commands"]:
        assert "role" in c and "fallback_only" in c
