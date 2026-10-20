"""Management ports: ESXI-Infra-<leaf> profiles, port-block description match."""
import asyncio
import json
import re
import unittest
from unittest import mock

from orchestrator import mgmt_ports, nodes_common
from orchestrator.nodes_port import resolve_port_ref

# Fake fabric: host CX1 has data ports on leaves 109 + 110 (MSY_CX1). The infra
# profiles use two different spellings; their selectors hold one port block
# per host with the host serial in the description.
PROFILES = {
    "MSY_CX1": {"nprof": ["SW109", "SW110"], "selectors": [
        ("MSY_CX1", "uni/infra/funcprof/accportgrp-MSY_CX1", [("blk1", "1", "10", "")])]},
    "ESXI-Infra-109": {"nprof": ["SW109"], "selectors": [
        ("ESXI-Infra-MSY-109", "uni/infra/funcprof/accportgrp-MGMT",
         [("b1", "1", "30", "CX1 vmnic0"), ("b2", "1", "31", "CX2 vmnic0")]),
        ("ESXI-Infra-CSY-109", "uni/infra/funcprof/accportgrp-MGMT",
         [("b3", "1", "40", "CZ9 mgmt")])]},
    "ESXI_Infra-110": {"nprof": ["SW110"], "selectors": [
        ("ESXI-Infra-MSY-110", "uni/infra/funcprof/accportgrp-MGMT",
         [("b1", "1", "30", "msy_cx1 vmnic1")])]},
}
SWITCH_NODES = {"SW109": "109", "SW110": "110"}


def _terms(flt, op):
    return re.findall(op + r'\([^,]+,"([^"]*)"\)', flt or "")


def _profile_mo(name):
    sels = []
    for sel, pg, blocks in PROFILES[name]["selectors"]:
        ch = [{"infraRsAccBaseGrp": {"attributes": {"tDn": pg}}}]
        for bn, card, port, descr in blocks:
            ch.append({"infraPortBlk": {"attributes": {
                "name": bn, "fromCard": card, "toCard": card,
                "fromPort": port, "toPort": port, "descr": descr}}})
        sels.append({"infraHPortS": {"attributes": {"name": sel}, "children": ch}})
    return [{"infraAccPortP": {"attributes": {"name": name}, "children": sels}}]


def fake_apic(path, params):
    flt = (params or {}).get("query-target-filter", "")
    if path.endswith("/class/infraAccPortP.json"):
        eqs, wcs = _terms(flt, "eq"), _terms(flt, "wcard")
        names = [n for n in PROFILES if n in eqs or any(w in n for w in wcs)]
        return [{"infraAccPortP": {"attributes": {
            "name": n, "dn": f"uni/infra/accportprof-{n}"}}} for n in names]
    if path.endswith("/class/infraRsAccPortP.json"):
        prof = _terms(flt, "eq")[0].split("accportprof-")[1]
        return [{"infraRsAccPortP": {"attributes": {
            "dn": f"uni/infra/nprof-{sw}/rsaccPortP-[x]"}}} for sw in PROFILES[prof]["nprof"]]
    m = re.search(r"/mo/uni/infra/nprof-([^.]+)\.json", path)
    if m:
        n = SWITCH_NODES[m.group(1)]
        return [{"infraNodeP": {"attributes": {}, "children": [
            {"infraNodeBlk": {"attributes": {"from_": n, "to_": n}}}]}}]
    m = re.search(r"/mo/uni/infra/accportprof-(.+)\.json", path)
    if m:
        return _profile_mo(m.group(1))
    if path.endswith("/class/infraPortBlk.json"):
        out = []
        for prof, p in PROFILES.items():
            for sel, _, blocks in p["selectors"]:
                for bn, _, _, descr in blocks:
                    if any(w in descr for w in _terms(flt, "wcard")):
                        out.append({"infraPortBlk": {"attributes": {
                            "dn": f"uni/infra/accportprof-{prof}/hports-{sel}-typ-range/portblk-{bn}",
                            "descr": descr}}})
        return out
    raise AssertionError(f"unexpected query {path} {params}")


class FakeTool:
    def __init__(self, fn=None):
        self.fn = fn

    async def ainvoke(self, args):
        if self.fn is None:
            return {}
        data = self.fn(args["path"], args.get("params"))
        return json.dumps({"totalCount": str(len(data)), "imdata": data})


TOOLS = {"apic_login": FakeTool(), "apic_get": FakeTool(fake_apic)}


def run(coro):
    with mock.patch.object(nodes_common, "get_tool", side_effect=lambda n: TOOLS[n]):
        return asyncio.run(coro)


class MgmtPortTests(unittest.TestCase):
    def test_eight_spellings(self):
        self.assertEqual(mgmt_ports.infra_profile_names("110"), [
            "ESXI-Infra-110", "ESXI-Infra_110", "ESXI_Infra-110", "ESXI_Infra_110",
            "ESX-Infra-110", "ESX-Infra_110", "ESX_Infra-110", "ESX_Infra_110"])

    def test_esx_prefix_profile_is_found(self):
        prof = PROFILES.pop("ESXI_Infra-110")
        PROFILES["ESX_Infra-110"] = prof
        try:
            res = run(mgmt_ports.resolve_management_ports("MSY_CX1"))
        finally:
            PROFILES["ESXI_Infra-110"] = PROFILES.pop("ESX_Infra-110")
        self.assertIn("topology/pod-1/paths-110/pathep-[eth1/30]", res["tdns"])
        self.assertIn("ESX_Infra-110", res["profiles_checked"])

    def test_host_on_two_leaves_checks_both_infra_profiles(self):
        res = run(mgmt_ports.resolve_management_ports("MSY_CX1"))

        self.assertIsNone(res["error"])
        self.assertEqual(sorted(res["nodes_checked"]), ["109", "110"])
        self.assertEqual(sorted(res["tdns"]), [
            "topology/pod-1/paths-109/pathep-[eth1/30]",
            "topology/pod-1/paths-110/pathep-[eth1/30]"])
        sels = {p["selector"] for p in res["ports"]}
        self.assertEqual(sels, {"ESXI-Infra-MSY-109", "ESXI-Infra-MSY-110"})

    def test_explicit_nodes_and_not_found_leaf(self):
        res = run(mgmt_ports.resolve_management_ports("CZ9", nodes=["109", "110"]))

        self.assertEqual(res["tdns"], ["topology/pod-1/paths-109/pathep-[eth1/40]"])
        self.assertTrue(any("ESXI_Infra-110" in n for n in res["notes"]))

    def test_unknown_leaf_falls_back_to_description_search(self):
        res = run(mgmt_ports.resolve_management_ports("CX2"))

        self.assertEqual(res["tdns"], ["topology/pod-1/paths-109/pathep-[eth1/31]"])

    def test_nothing_found_is_an_error(self):
        res = run(mgmt_ports.resolve_management_ports("NOPE", nodes=["109"]))

        self.assertTrue(res["error"])
        self.assertEqual(res["tdns"], [])

    def test_port_ref_scopes(self):
        mg = run(resolve_port_ref({"serial": "MSY_CX1", "scope": "management"}))
        self.assertEqual(mg["kind"], "management")
        self.assertEqual(len(mg["tdns"]), 2)

        both = run(resolve_port_ref({"serial": "MSY_CX1", "scope": "all"}))
        self.assertEqual(sorted(both["tdns"]), [
            "topology/pod-1/paths-109/pathep-[eth1/10]",
            "topology/pod-1/paths-109/pathep-[eth1/30]",
            "topology/pod-1/paths-110/pathep-[eth1/10]",
            "topology/pod-1/paths-110/pathep-[eth1/30]"])

        data = run(resolve_port_ref({"serial": "MSY_CX1"}))
        self.assertEqual(len(data["tdns"]), 2)
        self.assertTrue(all("eth1/10" in t for t in data["tdns"]))

    def test_agent_tool(self):
        out = run(mgmt_ports.find_management_ports.ainvoke({"host": "CX1"}))
        self.assertEqual(len(json.loads(out)["ports"]), 2)


if __name__ == "__main__":
    unittest.main()
