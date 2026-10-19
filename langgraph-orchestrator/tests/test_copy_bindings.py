"""Unit tests for copy_epg_bindings with one or many destinations."""
import asyncio
import unittest
from unittest import mock

from orchestrator import supervisor

SRC = "topology/pod-1/protpaths-101-102/pathep-[MSY_SRC]"
SPECS = [
    {"tenant": "T", "ap": "AP", "epg": "WEB_100", "encap": "vlan-100", "mode": "regular"},
    {"tenant": "T", "ap": "AP", "epg": "DB_200", "encap": "vlan-200", "mode": "regular"},
]


async def fake_resolve_port_ref(ref):
    serial = ref.get("serial")
    if serial == "SRC":
        return {"tdns": [SRC], "label": "MSY_SRC", "error": None}
    if serial == "MISSING":
        return {"error": "no port (interface profile) found for serial 'MISSING'"}
    if serial:
        t = f"topology/pod-1/protpaths-103-104/pathep-[{serial}]"
        return {"tdns": [t], "label": serial, "error": None}
    t = f"topology/pod-1/paths-{ref['node']}/pathep-[{ref['port']}]"
    return {"tdns": [t], "label": f"leaf{ref['node']} {ref['port']}", "error": None}


async def fake_read_bindings(tdn):
    return {"specs": SPECS if tdn == SRC else [], "error": None}


def run_copy(params):
    with mock.patch.object(supervisor, "resolve_port_ref", fake_resolve_port_ref), \
         mock.patch.object(supervisor, "read_bindings_on_path", fake_read_bindings):
        return asyncio.run(supervisor._build_copy_bindings(params, "copy"))


def posted_paths(prep):
    paths = []
    for tn in prep["post_body"]["polUni"]["children"]:
        for ap in tn["fvTenant"]["children"]:
            for epg in ap["fvAp"]["children"]:
                for rs in epg["fvAEPg"]["children"]:
                    paths.append((epg["fvAEPg"]["attributes"]["name"],
                                  rs["fvRsPathAtt"]["attributes"]["tDn"]))
    return paths


class CopyBindingsTests(unittest.TestCase):
    def test_many_destinations_get_every_binding(self):
        dests = [{"serial": f"DST{i}"} for i in range(15)]
        prep = run_copy({"source": {"serial": "SRC"}, "destinations": dests})

        self.assertFalse(prep.get("errors"))
        self.assertEqual(len(posted_paths(prep)), 2 * 15)
        self.assertEqual(prep["payload"]["total_new_bindings"], 30)
        self.assertEqual(len(prep["payload"]["destinations"]), 15)

    def test_legacy_single_destination_still_works(self):
        prep = run_copy({"source": {"serial": "SRC"},
                         "destination": {"node": "101", "port": "eth1/5"}})

        self.assertFalse(prep.get("errors"))
        self.assertEqual({p for _, p in posted_paths(prep)},
                         {"topology/pod-1/paths-101/pathep-[eth1/5]"})

    def test_unresolvable_and_duplicate_destinations_are_skipped(self):
        prep = run_copy({"source": {"serial": "SRC"}, "destinations": [
            {"serial": "DST1"}, {"serial": "MISSING"}, {"serial": "SRC"}, "DST1",
        ]})

        self.assertFalse(prep.get("errors"))
        self.assertEqual({p for _, p in posted_paths(prep)},
                         {"topology/pod-1/protpaths-103-104/pathep-[DST1]"})
        reasons = {s["destination"]: s["reason"]
                   for s in prep["payload"]["skipped_destinations"]}
        self.assertIn("MISSING", reasons)
        self.assertEqual(reasons["MSY_SRC"], "same port as the source")

    def test_fails_when_no_destination_resolves(self):
        prep = run_copy({"source": {"serial": "SRC"},
                         "destinations": [{"serial": "MISSING"}]})

        self.assertTrue(prep["errors"])
        self.assertIn("no destination could be resolved", prep["errors"][0])


if __name__ == "__main__":
    unittest.main()
