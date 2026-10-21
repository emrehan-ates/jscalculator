"""remove_from_dvs: move servers' data policy groups to the team baremetal AEP."""
import asyncio
import json
import re
import unittest
from unittest import mock

from orchestrator import dvs_removal, nodes_common

AEPS = ["MSY_ESX_AEP", "MSY_BAREMETAL", "CSY_BAREMETAL", "CSY_ESX_AEP"]
# profile -> [(selector, pg tDn)]
PROFILES = {
    "MSY_CX1": [("MSY_CX1", "uni/infra/funcprof/accbundle-MSY_CX1")],
    "MSY_CX2": [("MSY_CX2", "uni/infra/funcprof/accportgrp-SHARED_PG")],
    "MSY_CX3": [("MSY_CX3", "uni/infra/funcprof/accportgrp-SHARED_PG")],
    "CSY_CZ9": [("CSY_CZ9", "uni/infra/funcprof/accbundle-CSY_CZ9")],
    "ESXI-Infra-110": [("ESXI-Infra-MSY-110", "uni/infra/funcprof/accportgrp-MGMT")],
}
CURRENT_AEP = {
    "uni/infra/funcprof/accbundle-MSY_CX1": "MSY_ESX_AEP",
    "uni/infra/funcprof/accportgrp-SHARED_PG": "MSY_ESX_AEP",
    "uni/infra/funcprof/accbundle-CSY_CZ9": "CSY_BAREMETAL",
}


def fake_apic(path, params):
    flt = (params or {}).get("query-target-filter", "")
    if path.endswith("/class/infraAttEntityP.json"):
        return [{"infraAttEntityP": {"attributes": {"name": n}}} for n in AEPS]
    if path.endswith("/class/infraAccPortP.json"):
        tok = re.search(r'wcard\([^,]+,"([^"]*)"\)', flt).group(1)
        return [{"infraAccPortP": {"attributes": {"name": n, "dn": f"uni/infra/accportprof-{n}"}}}
                for n in PROFILES if tok in n]
    if path.endswith("/class/infraRsAccBaseGrp.json"):
        tdn = re.search(r'eq\([^,]+,"([^"]*)"\)', flt).group(1)
        return [{"infraRsAccBaseGrp": {"attributes": {
            "dn": f"uni/infra/accportprof-{p}/hports-{s}-typ-range/rsaccBaseGrp"}}}
            for p, sels in PROFILES.items() for s, pg in sels if pg == tdn]
    m = re.search(r"/mo/uni/infra/accportprof-(.+)\.json", path)
    if m:
        sels = [{"infraHPortS": {"attributes": {"name": s}, "children": [
            {"infraRsAccBaseGrp": {"attributes": {"tDn": pg}}}]}} for s, pg in PROFILES[m.group(1)]]
        return [{"infraAccPortP": {"attributes": {}, "children": sels}}]
    m = re.search(r"/mo/(uni/infra/funcprof/.+)\.json", path)
    if m:
        aep = CURRENT_AEP.get(m.group(1))
        ch = [{"infraRsAttEntP": {"attributes": {"tDn": f"uni/infra/attentp-{aep}"}}}] if aep else []
        return [{"infraAccPortGrp": {"attributes": {}, "children": ch}}]
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


def build(params, team=""):
    with mock.patch.object(nodes_common, "get_tool", side_effect=lambda n: TOOLS[n]):
        return asyncio.run(dvs_removal.build_remove_from_dvs(params, "dvs'ten çıkar", team=team))


def find_aep(team, aeps):
    async def fake_get(path, params=None):
        return [{"infraAttEntityP": {"attributes": {"name": n}}} for n in aeps]
    with mock.patch.object(dvs_removal, "_get", fake_get):
        return asyncio.run(dvs_removal.find_baremetal_aep(team))


def posted(prep):
    out = {}
    infra = prep["post_body"]["polUni"]["children"][0]["infraInfra"]["children"][0]["infraFuncP"]
    for pg in infra["children"]:
        (cls, body), = pg.items()
        out[body["attributes"]["name"]] = (
            cls, body["children"][0]["infraRsAttEntP"]["attributes"]["tDn"])
    return out


class RemoveFromDvsTests(unittest.TestCase):
    def test_moves_policy_group_to_existing_baremetal_name(self):
        prep = build({"servers": [{"serial": "CX1"}]}, team="MSY")

        self.assertFalse(prep["errors"])
        self.assertEqual(posted(prep), {
            "MSY_CX1": ("infraAccBndlGrp", "uni/infra/attentp-MSY_BAREMETAL")})
        self.assertEqual(prep["payload"]["changes"][0]["from_aep"], "MSY_ESX_AEP")

    def test_team_from_profile_prefix_when_user_has_no_team(self):
        prep = build({"servers": ["CX1"]})
        self.assertEqual(posted(prep)["MSY_CX1"][1], "uni/infra/attentp-MSY_BAREMETAL")

    def test_shared_policy_group_is_skipped(self):
        prep = build({"servers": [{"serial": "CX1"}, {"serial": "CX2"}]}, team="MSY")

        self.assertEqual(set(posted(prep)), {"MSY_CX1"})
        skipped = prep["payload"]["skipped"]
        self.assertEqual(skipped[0]["policy_group"], "SHARED_PG")
        self.assertIn("MSY_CX3", skipped[0]["reason"])

    def test_shared_policy_group_ok_when_all_users_are_requested(self):
        prep = build({"servers": ["CX2", "CX3"]}, team="MSY")
        self.assertEqual(set(posted(prep)), {"SHARED_PG"})

    def test_already_baremetal_and_unknown_server(self):
        prep = build({"servers": ["CZ9", "NOPE"]}, team="CSY")

        self.assertTrue(prep["errors"])
        self.assertIn("already on the baremetal AEP", prep["errors"][0])
        self.assertIn("NOPE", prep["errors"][0])

    def test_baremetal_aep_names_per_team(self):
        cases = [
            ("MSY", ["MSY_ESX_AEP", "MSY_BAREMETAL"], "MSY_BAREMETAL"),
            ("MSY", ["MSY_ESX_AEP", "BARE_METAL"], "BARE_METAL"),
            ("CSY", ["BARE_METAL", "MSY_BAREMETAL"], "BARE_METAL"),
            ("CSY", ["CSY_BAREMETAL", "MSY_BAREMETAL"], "CSY_BAREMETAL"),
            ("CSY", ["CSY_BARE_METAL"], "CSY_BARE_METAL"),
            ("msy", ["Msy_BareMetal"], "Msy_BareMetal"),  # real APIC spelling is kept
        ]
        for team, aeps, want in cases:
            self.assertEqual(find_aep(team, aeps), (want, None), (team, aeps))

    def test_baremetal_aep_not_found(self):
        for team, aeps in [("MSY", ["CSY_BAREMETAL", "MSY_BAREMETAL_OLD", "MSY_BARE_METAL"]),
                           ("CSY", ["MSY_BAREMETAL", "CSY-BAREMETAL"])]:
            name, err = find_aep(team, aeps)
            self.assertIsNone(name)
            self.assertIn("no baremetal AEP found", err)

    def test_baremetal_aep_unknown_team(self):
        name, err = find_aep("SG", ["BARE_METAL"])
        self.assertIsNone(name)
        self.assertIn("known teams", err)


if __name__ == "__main__":
    unittest.main()
