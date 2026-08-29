#!/usr/bin/env python3
"""
Tests for vast_optimizer.py.

No network, no vastai, no money: the single outward function vast() is
replaced, everything else is the real code. The offers look like real answers
from vast.ai - gpu_ram per card in MB, disk_space in GB, prices as floats -
because those units are exactly where this goes wrong otherwise.
"""

import csv
import itertools
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest import mock

import vast_optimizer as vo


def offer(id_=1, cards=1, ram_mb=24576, dph=0.30, **rest) -> dict:
    d = {
        "id": id_,
        "num_gpus": cards,
        "gpu_ram": ram_mb,
        "gpu_name": "RTX 3090",
        "gpu_total_ram": ram_mb * cards,
        "dph_total": dph,
        "dph_base": dph * 0.98,
        "min_bid": dph * 0.98,
        "disk_space": 500.0,
        "inet_down": 1200.0,
        "reliability2": 0.99,
        "rentable": True,
        "geolocation": "Sweden, SE",
        "interruptible": False,
    }
    d.update(rest)
    return d


def instance(id_=777, cards=1, ram_mb=49152, dph=0.40, **rest) -> dict:
    d = {
        "id": id_,
        "num_gpus": cards,
        "gpu_ram": ram_mb,
        "gpu_name": "RTX A6000",
        "dph_total": dph,
        "actual_status": "running",
        "duration": 7200.0,
        "public_ipaddr": "203.0.113.7",
        "ports": {"8080/tcp": [{"HostPort": "41234"}]},
    }
    d.update(rest)
    return d


class VramArithmetic(unittest.TestCase):
    """The core question: two cards are as good as one big one."""

    def test_two_24gb_cards_satisfy_48(self):
        pair = offer(cards=2, ram_mb=24576)
        self.assertAlmostEqual(vo.vram_gb(pair), 48.0)
        self.assertTrue(vo.suitable(pair, 48, cap=1.0)[0])

    def test_one_24gb_card_does_not_satisfy_48(self):
        single = offer(cards=1, ram_mb=24576)
        ok, reason = vo.suitable(single, 48, cap=1.0)
        self.assertFalse(ok)
        self.assertIn("24 GB", reason)

    def test_four_12gb_cards_satisfy_48(self):
        self.assertTrue(vo.suitable(offer(cards=4, ram_mb=12288), 48, 1.0)[0])

    def test_total_vram_falls_back_to_gpu_total_ram(self):
        o = offer(cards=2)
        o["gpu_ram"] = 0
        o["gpu_total_ram"] = 49152
        self.assertAlmostEqual(vo.vram_gb(o), 48.0)

    def test_query_is_per_card_and_in_gigabytes(self):
        # 48 GB across two cards means 24 GB per card - and vast.ai's query
        # language wants GB, not MB.
        self.assertIn("num_gpus=2 gpu_ram>=24", vo._query(2, 48))
        self.assertIn("num_gpus=3 gpu_ram>=16", vo._query(3, 48))


class Threshold(unittest.TestCase):
    """11 % is the line, and it has to be strictly above."""

    def test_exactly_eleven_percent_does_not_switch(self):
        old = instance(dph=1.00)
        v = vo.evaluate(old, [offer(dph=0.89)], 24, cap=2.0)
        self.assertFalse(v.switch)
        self.assertAlmostEqual(v.saving, 0.11, places=6)

    def test_twelve_percent_switches(self):
        old = instance(dph=1.00)
        v = vo.evaluate(old, [offer(dph=0.88)], 24, cap=2.0)
        self.assertTrue(v.switch)
        self.assertAlmostEqual(v.saving, 0.12, places=6)

    def test_cheaper_but_too_little_vram(self):
        old = instance(dph=1.00)
        v = vo.evaluate(old, [offer(dph=0.30, cards=1, ram_mb=12288)],
                        48, cap=2.0)
        self.assertFalse(v.switch)
        self.assertIsNone(v.best)

    def test_pair_beats_the_single_big_card(self):
        # running 48 GB card at 0.50, two 24s together at 0.40
        old = instance(cards=1, ram_mb=49152, dph=0.50)
        pair = offer(id_=42, cards=2, ram_mb=24576, dph=0.40)
        v = vo.evaluate(old, [pair], 48, cap=2.0)
        self.assertTrue(v.switch)
        self.assertEqual(v.best["id"], 42)
        self.assertAlmostEqual(v.saving, 0.20, places=6)

    def test_price_cap_keeps_the_expensive_offer_out(self):
        old = instance(dph=1.20)
        pricey = offer(dph=0.70)
        self.assertFalse(vo.suitable(pricey, 24, cap=0.60)[0])
        v = vo.evaluate(old, [pricey], 24, cap=0.60)
        self.assertFalse(v.switch)
        self.assertIsNone(v.best)

    def test_without_a_running_instance_nothing_is_started(self):
        v = vo.evaluate(None, [offer(dph=0.10)], 24, cap=2.0)
        self.assertFalse(v.switch)
        self.assertIn("nothing will be started", v.reason)

    def test_the_best_of_several_is_taken(self):
        old = instance(dph=1.00)
        v = vo.evaluate(old, [offer(id_=1, dph=0.80),
                              offer(id_=2, dph=0.50),
                              offer(id_=3, dph=0.60)], 24, cap=2.0)
        self.assertEqual(v.best["id"], 2)


class TwoCategories(unittest.TestCase):
    """Guaranteed and bid, side by side - one number alone decides nothing."""

    def test_the_cheapest_of_each_kind(self):
        sicher_teuer = offer(id_=1, dph=0.40)
        sicher_billig = offer(id_=2, dph=0.30)
        gebot = offer(id_=3, dph=0.20, interruptible=True)
        gebot["min_bid"] = 0.10
        gebot["dph_base"] = 0.18
        liste = [sicher_teuer, sicher_billig, gebot]
        self.assertEqual(vo.cheapest(liste, interruptible=False)["id"], 2)
        self.assertEqual(vo.cheapest(liste, interruptible=True)["id"], 3)

    def test_without_one_kind_the_answer_is_none(self):
        self.assertIsNone(vo.cheapest([offer(id_=1)], interruptible=True))

    def test_the_line_shows_what_a_bid_really_consists_of(self):
        o = offer(id_=5, dph=0.20, interruptible=True)
        o["min_bid"] = 0.10
        o["dph_base"] = 0.18
        zeile = vo.beschreibe(o)
        self.assertIn("min_bid 0.100", zeile)
        self.assertIn("disk and net 0.020", zeile)   # what is charged anyway
        self.assertIn("guaranteed", vo.beschreibe(offer(id_=6)))


class BidOffers(unittest.TestCase):
    """Interruptible is allowed - but priced at our own bid."""

    def setUp(self):
        # Without a known guaranteed price the fallback applies; the tests
        # that care about the 50 % rule pass the reference explicitly.
        p = mock.patch.object(vo, "_ondemand_ref", 0.0)
        p.start()
        self.addCleanup(p.stop)

    def test_the_bid_is_a_tenth_above_the_minimum(self):
        o = offer(dph=0.30, interruptible=True)
        o["dph_base"] = 0.28
        o["min_bid"] = 0.20
        # 0.20 * 1.10 = 0.22, plus 0.30 - 0.28 = 0.02 for disk and bandwidth
        self.assertAlmostEqual(vo.price(o), 0.24, places=6)

    def test_a_cheap_offer_is_not_bid_up_to_half_the_guaranteed_price(self):
        # This is the case that cost real money: min_bid 0.028 was bid at
        # 0.108 because half of the guaranteed price was taken as the target.
        o = offer(dph=0.03, interruptible=True)
        o["dph_base"] = 0.028
        o["min_bid"] = 0.028
        self.assertAlmostEqual(vo.price(o, ondemand_ref=0.216), 0.0328,
                               places=4)

    def test_an_expensive_minimum_stays_expensive_and_loses(self):
        # min_bid above the guaranteed price cannot be undercut - the price
        # says so honestly, and the guaranteed machine then simply wins the
        # comparison.
        teuer = offer(id_=1, dph=0.50, interruptible=True)
        teuer["dph_base"] = 0.50
        teuer["min_bid"] = 0.45
        self.assertAlmostEqual(vo.price(teuer, ondemand_ref=0.30), 0.45,
                               places=6)
        sicher = offer(id_=2, dph=0.30)
        v = vo.evaluate(instance(dph=1.00), [teuer, sicher], 24, cap=2.0)
        self.assertEqual(v.best["id"], 2)

    def test_half_the_guaranteed_price_is_the_ceiling(self):
        # min_bid * 1.10 would be 0.33 here - more than half of what a
        # machine costs that cannot be outbid at all, so the ceiling wins.
        o = offer(dph=0.30, interruptible=True)
        o["dph_base"] = 0.30
        o["min_bid"] = 0.30
        self.assertAlmostEqual(vo.price(o, ondemand_ref=0.40), 0.30, places=6)

    def test_the_bid_never_falls_below_min_bid(self):
        # vast.ai rejects a bid under min_bid outright, so the floor wins
        o = offer(dph=0.30, interruptible=True)
        o["dph_base"] = 0.30
        o["min_bid"] = 0.25
        self.assertAlmostEqual(vo.price(o, ondemand_ref=0.30), 0.25, places=6)

    def test_the_reference_comes_from_the_guaranteed_offers(self):
        def fake(*args):
            is_bid = "--type" in args and args[args.index("--type") + 1] == "bid"
            o = offer(id_=2 if is_bid else 1, cards=2, ram_mb=24576,
                      dph=0.60 if is_bid else 0.44)
            if is_bid:
                o["min_bid"] = 0.01
                o["dph_base"] = 0.60
            return json.dumps([o])

        with mock.patch.object(vo, "vast", side_effect=fake):
            found = vo.offers(48, cap=2.0)
        self.assertAlmostEqual(vo._ondemand_ref, 0.44, places=6)
        bid = next(o for o in found if o["interruptible"])
        # min_bid 0.01 plus a tenth - far below the 0.22 ceiling
        self.assertAlmostEqual(vo.price(bid), 0.011, places=4)

    def test_interruptible_twenty_percent_cheaper_switches(self):
        old = instance(dph=1.00)
        o = offer(id_=99, dph=0.82, interruptible=True)
        o["dph_base"] = 0.82
        o["min_bid"] = 0.80 / 1.10            # bid works out to exactly 0.80
        v = vo.evaluate(old, [o], 24, cap=2.0)
        self.assertTrue(v.switch)
        self.assertAlmostEqual(v.saving, 0.20, places=6)

    def test_a_bid_at_the_threshold_is_not_worth_a_switch(self):
        # exactly 12 % cheaper is above the 11 % line, 11 % is not
        old = instance(dph=1.00)
        o = offer(dph=0.89, interruptible=True)
        o["dph_base"] = 0.89
        o["min_bid"] = 0.89 / 1.10
        v = vo.evaluate(old, [o], 24, cap=2.0)
        self.assertFalse(v.switch)

    def test_the_bid_appears_in_the_launch_command(self):
        o = offer(id_=555, dph=0.40, interruptible=True)
        o["dph_base"] = 0.40
        o["min_bid"] = 0.20
        done = mock.Mock(returncode=0,
                         stdout='{"success": true, "new_contract": 4242}',
                         stderr="")
        with mock.patch("subprocess.run", return_value=done) as r, \
             mock.patch.object(vo, "token", return_value="secret"), \
             mock.patch.object(vo, "report"):
            new = vo.launch(o, context=8192, model="some/model:Q4",
                            with_ssh=False)
        self.assertEqual(new, 4242)
        args = list(r.call_args[0][0])
        # --bid_price, not --price: vastai 1.5.6 rejects --price, and the
        # mistake only shows when real money is about to be spent.
        self.assertIn("--bid_price", args)
        bid = float(args[args.index("--bid_price") + 1])
        self.assertAlmostEqual(bid, 0.22, places=4)
        self.assertIn("--image", args)
        self.assertIn("-hf", args)
        # Everything after --args belongs to the container. A vastai flag
        # behind it ends up in llama-server's command line instead.
        self.assertIn("--raw", args[:args.index("--args")])
        self.assertNotIn("--raw", args[args.index("--args"):])

    def test_ssh_mode_uses_onstart_instead_of_args(self):
        # With --ssh vast keeps its own entrypoint, so llama-server has to be
        # started by onstart - and only then does the machine have an SSH port
        # to look into while it downloads.
        done = mock.Mock(returncode=0, stdout='{"new_contract": 7}', stderr="")
        with mock.patch("subprocess.run", return_value=done) as r, \
             mock.patch.object(vo, "token", return_value="secret"), \
             mock.patch.object(vo, "report"):
            vo.launch(offer(id_=1), 8192, "some/model:Q4", with_ssh=True)
        args = list(r.call_args[0][0])
        self.assertIn("--ssh", args)
        self.assertIn("--direct", args)
        # The entrypoint has to be replaced: llama-server without arguments
        # ends the container within seconds.
        self.assertEqual(args[args.index("--entrypoint") + 1], "/bin/bash")
        self.assertEqual(args[args.index("--args") + 1:], ["-c", "sleep infinity"])
        onstart = args[args.index("--onstart-cmd") + 1]
        # onstart only prepares: the server is started later, on the file the
        # fast downloader fetched. Starting it here with -hf would pull the
        # same 18 GB a second time, through the one throttled connection.
        self.assertIn(vo.SERVER_LOG, onstart)
        self.assertNotIn("-hf", onstart)
        self.assertNotIn("secret", onstart)

    def test_both_modes_serve_the_same_command_line(self):
        with mock.patch.object(vo, "token", return_value="secret"):
            line = vo.server_command("some/model:Q4", 8192)
        self.assertIn("-ngl 99", line)
        self.assertIn("--ctx-size 8192", line)
        self.assertIn("--api-key secret", line)

    def test_a_fixed_offer_carries_no_bid(self):
        done = mock.Mock(returncode=0, stdout='{"new_contract": 7}', stderr="")
        with mock.patch("subprocess.run", return_value=done) as r, \
             mock.patch.object(vo, "token", return_value="secret"), \
             mock.patch.object(vo, "report"):
            vo.launch(offer(id_=1), context=8192, model="some/model:Q4")
        self.assertNotIn("--bid_price", list(r.call_args[0][0]))

    def test_the_api_key_is_never_empty_in_the_launch_command(self):
        # An empty --api-key shifts the next argument into its place: the
        # server would then read --ctx-size as the key and 32768 as a file.
        done = mock.Mock(returncode=0, stdout='{"new_contract": 7}', stderr="")
        with mock.patch("subprocess.run", return_value=done) as r, \
             mock.patch.object(vo, "token", return_value="secret"), \
             mock.patch.object(vo, "report"):
            vo.launch(offer(id_=1), context=8192, model="some/model:Q4",
                      with_ssh=False)
        args = list(r.call_args[0][0])
        # Two --api-key in one command line: the first belongs to vastai (the
        # rental key), the one after --args to llama-server. The latter is
        # what this checks.
        container = args[args.index("--args"):]
        self.assertEqual(container[container.index("--api-key") + 1], "secret")


class Brakes(unittest.TestCase):
    """Without a brake the instance wanders in circles instead of computing."""

    def test_minimum_hold_time_prevents_the_switch(self):
        now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
        st = {"last_switch": (now - timedelta(minutes=20)).isoformat()}
        v = vo.evaluate(instance(dph=1.00), [offer(dph=0.50)], 24, 2.0, st, now)
        self.assertFalse(v.switch)
        self.assertIn("hold time", v.reason)

    def test_after_the_hold_time_a_switch_is_allowed(self):
        now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
        st = {"last_switch": (now - timedelta(minutes=50)).isoformat(),
              "switches": [(now - timedelta(minutes=50)).isoformat()]}
        v = vo.evaluate(instance(dph=1.00), [offer(dph=0.50)], 24, 2.0, st, now)
        self.assertFalse(v.switch)   # one switch in the last hour is the limit
        st["switches"] = [(now - timedelta(minutes=90)).isoformat()]
        v = vo.evaluate(instance(dph=1.00), [offer(dph=0.50)], 24, 2.0, st, now)
        self.assertTrue(v.switch)

    def test_at_most_one_switch_per_hour(self):
        now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
        st = {"last_switch": (now - timedelta(minutes=55)).isoformat(),
              "switches": [(now - timedelta(minutes=55)).isoformat()]}
        self.assertIn("in the last hour", vo.cooldown(st, now))


class Switching(unittest.TestCase):
    """New one first, old one second - and in doubt the old one stays."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.p_state = mock.patch.object(
            vo, "STATE_FILE", os.path.join(self.dir.name, "state.json"))
        self.p_log = mock.patch.object(
            vo, "LOGFILE", os.path.join(self.dir.name, "optimizer.log"))
        self.p_dir = mock.patch.object(vo, "VAST_DIR", self.dir.name)
        self.p_dir.start()
        self.p_state.start()
        self.p_log.start()
        self.addCleanup(self.p_dir.stop)
        self.addCleanup(self.p_state.stop)
        self.addCleanup(self.p_log.stop)
        self.addCleanup(self.dir.cleanup)
        # No network, no database: at the end switch_instance() looks up the
        # new instance and registers it in llm_models - in a test that must
        # become neither a vastai call nor a real row.
        for name, value in (("instances", []), ("registry_upsert", True)):
            p = mock.patch.object(vo, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def _verdict(self):
        old = instance(id_=777, dph=1.00)
        new = offer(id_=333, cards=2, ram_mb=24576, dph=0.60)
        return vo.evaluate(old, [new], 48, cap=2.0)

    def test_successful_switch_destroys_the_old_one_last(self):
        order = []
        with mock.patch.object(vo, "launch",
                               side_effect=lambda *a, **k: order.append("launch") or 888), \
             mock.patch.object(vo, "wait_until_healthy",
                               side_effect=lambda *a, **k: order.append("healthy") or True), \
             mock.patch.object(vo, "destroy",
                               side_effect=lambda i: order.append(f"destroyed {i}")):
            self.assertTrue(vo.switch_instance(self._verdict(), 8192, "m"))
        self.assertEqual(order, ["launch", "healthy", "destroyed 777"])
        st = json.load(open(vo.STATE_FILE))
        self.assertEqual(st["instance"], 888)
        self.assertEqual(st["predecessor"], 777)
        self.assertEqual(st["gpus"], 2)
        self.assertEqual(st["vram_gb"], 48)
        self.assertTrue(st["last_switch"])

    def test_new_one_unhealthy_leaves_the_old_one_standing(self):
        destroyed = []
        with mock.patch.object(vo, "launch", return_value=888), \
             mock.patch.object(vo, "wait_until_healthy", return_value=False), \
             mock.patch.object(vo, "destroy", side_effect=destroyed.append):
            self.assertFalse(vo.switch_instance(self._verdict(), 8192, "m"))
        self.assertEqual(destroyed, [888])            # only the new one
        # The state now holds the failed offer, but no switch: the old
        # instance is still the one that counts.
        st = json.load(open(vo.STATE_FILE))
        self.assertIn("333", st["bad_offers"])
        self.assertNotIn("last_switch", st)
        self.assertNotIn("instance", st)

    def test_failed_launch_leaves_the_old_one_standing(self):
        destroyed = []
        with mock.patch.object(vo, "launch",
                               side_effect=RuntimeError("no credit")), \
             mock.patch.object(vo, "destroy", side_effect=destroyed.append):
            self.assertFalse(vo.switch_instance(self._verdict(), 8192, "m"))
        self.assertEqual(destroyed, [])

    def test_ssh_wait_stops_when_the_instance_is_already_dead(self):
        dead = instance(id_=888, actual_status="exited")
        marks = {}
        with mock.patch.object(vo, "instances", return_value=[dead]), \
             mock.patch.object(vo, "ssh_run") as sr, \
             mock.patch.object(vo, "report"), \
             mock.patch("time.sleep"):
            self.assertFalse(vo.wait_for_ssh(888, timeout_s=600, marks=marks))
        sr.assert_not_called()
        self.assertEqual(marks["exit_state"], "exited")

    def test_waiting_gives_up_when_the_instance_exited(self):
        dead = instance(id_=888, actual_status="exited")
        with mock.patch.object(vo, "instances", return_value=[dead]), \
             mock.patch.object(vo, "healthy", return_value=False), \
             mock.patch.object(vo, "container_log", return_value="      | boom"), \
             mock.patch("time.sleep"):
            self.assertFalse(vo.wait_until_healthy(888, timeout_s=1))

    def test_waiting_reports_success_on_http_200(self):
        with mock.patch.object(vo, "instances", return_value=[instance(id_=888)]), \
             mock.patch.object(vo, "healthy", return_value=True), \
             mock.patch.object(vo, "container_log", return_value=""), \
             mock.patch("time.sleep"):
            self.assertTrue(vo.wait_until_healthy(888, timeout_s=60))


class Searching(unittest.TestCase):
    """The search has to find both shapes and must not count anything twice."""

    def test_searches_several_gpu_counts_and_both_kinds(self):
        calls = []

        def fake_vast(*args):
            calls.append(args)
            if "--type" in args and args[args.index("--type") + 1] == "bid":
                return json.dumps([offer(id_=2, cards=2, ram_mb=24576,
                                         dph=0.35)])
            return json.dumps([offer(id_=1, cards=2, ram_mb=24576, dph=0.50)])

        with mock.patch.object(vo, "vast", side_effect=fake_vast):
            found = vo.offers(48, cap=2.0)
        kinds = {a[a.index("--type") + 1] for a in calls}
        self.assertEqual(kinds, {"on-demand", "bid"})
        counts = {a[2].split()[0] for a in calls}
        self.assertIn("num_gpus=2", counts)
        self.assertIn("num_gpus=4", counts)
        # same id per kind only once, cheapest first
        self.assertEqual([o["id"] for o in found], [2, 1])
        self.assertTrue(found[0]["interruptible"])

    def test_unsuitable_offers_never_enter_the_list(self):
        with mock.patch.object(vo, "vast", return_value=json.dumps(
                [offer(id_=5, cards=1, ram_mb=24576, dph=0.10)])):
            self.assertEqual(vo.offers(48, cap=2.0), [])

    def test_one_broken_query_does_not_end_the_search(self):
        def sometimes(*args):
            if "num_gpus=1" in args[2]:
                raise RuntimeError("vast.ai does not answer")
            return json.dumps([offer(id_=9, cards=2, ram_mb=24576, dph=0.40)])

        with mock.patch.object(vo, "vast", side_effect=sometimes):
            found = vo.offers(48, cap=2.0)
        # The same machine exists as a fixed offer and as a bid - two
        # different products, so both survive, but each only once even though
        # the search finds them across several GPU counts.
        self.assertEqual([o["id"] for o in found], [9, 9])
        self.assertEqual(sorted(o["interruptible"] for o in found),
                         [False, True])


class SshAccess(unittest.TestCase):
    """A rented machine one cannot log into is a machine one cannot debug."""

    def test_target_is_the_direct_port_of_the_machine(self):
        i = instance(id_=1)
        i["ports"] = {"22/tcp": [{"HostPort": "1969"}],
                      "8080/tcp": [{"HostPort": "1912"}]}
        with mock.patch.object(vo, "instances", return_value=[i]):
            self.assertEqual(vo.ssh_target(1), ("203.0.113.7", "1969"))

    def test_the_proxy_is_only_the_fallback(self):
        with mock.patch.object(vo, "instances", return_value=[]), \
             mock.patch.object(vo, "vast",
                               return_value="ssh://root@ssh5.vast.ai:41234\n"):
            self.assertEqual(vo.ssh_target(1), ("ssh5.vast.ai", "41234"))

    def test_no_ssh_port_is_not_a_crash(self):
        with mock.patch.object(vo, "instances", return_value=[]), \
             mock.patch.object(vo, "vast",
                               side_effect=RuntimeError("ssh port not found")):
            self.assertIsNone(vo.ssh_target(1))
        with mock.patch.object(vo, "instances", return_value=[]), \
             mock.patch.object(vo, "vast", side_effect=RuntimeError("x")):
            self.assertIsNone(vo.ssh_run(1, "true"))

    def test_command_goes_to_the_right_host_and_port(self):
        done = mock.Mock(returncode=0, stdout="18G\t/root/.cache", stderr="")
        with mock.patch.object(vo, "ssh_target",
                               return_value=("ssh5.vast.ai", "41234")), \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch("subprocess.run", return_value=done) as r:
            out = vo.ssh_run(7, "du -sh /root/.cache")
        args = r.call_args[0][0]
        self.assertEqual(args[:2], ["ssh", "-n"])
        self.assertIs(r.call_args.kwargs["stdin"], __import__("subprocess").DEVNULL)
        self.assertIn("root@ssh5.vast.ai", args)
        self.assertEqual(args[args.index("-p") + 1], "41234")
        self.assertIn("18G", out)

    def test_progress_reads_the_log_and_the_growing_cache(self):
        with mock.patch.object(vo, "ssh_run",
                               return_value="loading model\n7,2G\t/root/.cache/llama.cpp"):
            text = vo.download_progress(7)
        self.assertIn("7,2G", text)
        self.assertIn("loading model", text)

    def test_without_ssh_the_progress_stays_empty(self):
        with mock.patch.object(vo, "ssh_run", return_value=None):
            self.assertEqual(vo.download_progress(7), "")

    def test_nvme_prefers_ssh_and_falls_back_to_execute(self):
        dd = ("536870912 bytes copied, 0,4 s, 1,3 GB/s\n"
              "536870912 bytes copied, 0,2 s, 2400 MB/s")
        with mock.patch.object(vo, "ssh_run", return_value=dd):
            _, _, source = vo.measure_nvme(7, offer())
        self.assertEqual(source, "dd over ssh")
        with mock.patch.object(vo, "ssh_run", return_value=None), \
             mock.patch.object(vo, "vast", return_value=dd):
            _, _, source = vo.measure_nvme(7, offer())
        self.assertEqual(source, "dd via vastai execute")


class ScriptedSetup(unittest.TestCase):
    """The same four steps every time - door, model, server, answer."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        for name, value in (("VAST_DIR", self.dir.name),
                            ("LOGFILE", os.path.join(self.dir.name, "o.log")),
                            ("STATE_FILE",
                             os.path.join(self.dir.name, "state.json"))):
            p = mock.patch.object(vo, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_ssh_wait_records_how_long_the_door_took(self):
        marks = {}
        with mock.patch.object(vo, "instances",
                               return_value=[instance(id_=7)]), \
             mock.patch.object(vo, "ssh_run", side_effect=[None, "ok"]), \
             mock.patch("time.sleep"):
            self.assertTrue(vo.wait_for_ssh(7, timeout_s=120, marks=marks))
        self.assertIn("ssh_s", marks)

    def test_a_hanging_ssh_call_does_not_count_as_a_failed_download(self):
        # ssh returns nothing because the download is still running - the
        # file growing is the only proof that matters.
        marks = {}
        full = str(vo.MODEL_BYTES)
        with mock.patch.object(vo, "scp_to", return_value=True), \
             mock.patch.object(vo, "ssh_run",
                               side_effect=["", "/usr/bin/python3", None,
                                            "0\n1000000", "0\n4000000000",
                                            f"{full}\n0", f"{full}\n0"]), \
             mock.patch("time.sleep"):
            self.assertTrue(vo.fetch_model(7, "http://x/y.gguf",
                                           "/root/m.gguf", marks=marks))

    def test_nothing_on_disk_after_two_probes_is_a_failure(self):
        with mock.patch.object(vo, "scp_to", return_value=True), \
             mock.patch.object(vo, "ssh_run",
                               side_effect=["", "/usr/bin/python3", "started",
                                            "0\n0", "0\n0", "no such file"]), \
             mock.patch("time.sleep"):
            self.assertFalse(vo.fetch_model(7, "http://x/y.gguf",
                                            "/root/m.gguf"))

    def test_progress_counts_the_piece_folder(self):
        # During the download the target file is still 0 bytes; everything
        # sits in <ziel>.stuecke until the very end.
        with mock.patch.object(vo, "ssh_run", return_value="0\n3221225472"):
            fertig, gesehen = vo._remote_progress(7, "/root/m.gguf")
        self.assertEqual(fertig, 0)
        self.assertEqual(gesehen, 3221225472)

    def test_a_slow_machine_is_dropped_after_forty_seconds(self):
        # 5 MB in 30 s is 1.3 Mbit/s - at that rate the model needs a day,
        # and every minute of it is paid for.
        marks = {}
        with mock.patch.object(vo, "scp_to", return_value=True), \
             mock.patch.object(vo, "ssh_run",
                               side_effect=["", "/usr/bin/python3", "started",
                                            "0\n1000000", "0\n6000000",
                                            "stuck"]), \
             mock.patch("time.sleep"):
            self.assertFalse(vo.fetch_model(7, "http://x/y.gguf",
                                            "/root/m.gguf", marks=marks))
        self.assertLess(marks["download_mbits"], vo.MIN_DOWNLOAD_MBIT)

    def test_a_fast_machine_is_kept_and_the_model_verified_by_size(self):
        marks = {}
        full = str(vo.MODEL_BYTES)
        with mock.patch.object(vo, "scp_to", return_value=True), \
             mock.patch.object(vo, "ssh_run",
                               side_effect=["", "/usr/bin/python3", "started",
                                            "0\n1000000", "0\n4000000000",
                                            f"{full}\n0", f"{full}\n0"]), \
             mock.patch("time.sleep"):
            self.assertTrue(vo.fetch_model(7, "http://x/y.gguf",
                                           "/root/m.gguf", marks=marks))
        self.assertEqual(marks["download_way"], "download_llm.py")
        self.assertIn("download_s", marks)

    def test_a_short_file_counts_as_a_failed_download(self):
        # Half a model is worse than none: llama-server would load it and
        # then die with a parse error minutes later. The clock is stepped
        # forward ten minutes per reading so the deadline is reached at once.
        antworten = {"command -v": "/usr/bin/python3", "nohup": "started"}

        def fake(instance_id, command, timeout=60, **rest):
            for schnipsel, antwort in antworten.items():
                if schnipsel in command:
                    return antwort
            if "stat -c" in command:
                return "4000000000\n0"   # stays far below the full size
            return ""

        with mock.patch.object(vo, "scp_to", return_value=True), \
             mock.patch.object(vo, "ssh_run", side_effect=fake), \
             mock.patch("time.sleep"), \
             mock.patch("time.time", side_effect=itertools.count(0, 600)):
            self.assertFalse(vo.fetch_model(7, "http://x/y.gguf",
                                            "/root/m.gguf"))

    def test_without_python3_it_falls_back_to_curl(self):
        marks = {}
        full = str(vo.MODEL_BYTES)
        with mock.patch.object(vo, "scp_to", return_value=False), \
             mock.patch.object(vo, "ssh_run",
                               side_effect=["", "", "started", "0\n1000000",
                                            "0\n4000000000", f"{full}\n0",
                                            f"{full}\n0"]), \
             mock.patch("time.sleep"):
            self.assertTrue(vo.fetch_model(7, "http://x/y.gguf",
                                           "/root/m.gguf", marks=marks))
        self.assertEqual(marks["download_way"], "curl")

    def test_server_starts_from_the_local_file_not_from_hugging_face(self):
        seen = {}

        def catch(instance_id, command, timeout=60, **rest):
            if "pgrep" in command:
                return "4711"
            seen["cmd"] = command
            return None      # ssh hangs while the server runs - as it does

        with mock.patch.object(vo, "ssh_run", side_effect=catch), \
             mock.patch.object(vo, "token", return_value="secret"), \
             mock.patch("time.sleep"), \
             mock.patch.object(vo, "report"):
            self.assertTrue(vo.start_server(7, "/root/m.gguf", 8192))
        self.assertIn("-m /root/m.gguf", seen["cmd"])
        # Started from /app with its own libraries in reach, otherwise the
        # binary cannot find libllama-server-impl.so.
        self.assertIn("cd /app", seen["cmd"])
        self.assertIn("LD_LIBRARY_PATH=/app", seen["cmd"])
        self.assertIn("--parallel 1", seen["cmd"])

    def test_more_slots_are_passed_through(self):
        # A queue of short analysis jobs keeps eight slots busy; one slot
        # would serialise them.
        seen = {}

        def catch(instance_id, command, timeout=60, **rest):
            if "pgrep" in command:
                return "4711"
            seen["cmd"] = command
            return None

        with mock.patch.object(vo, "ssh_run", side_effect=catch), \
             mock.patch.object(vo, "token", return_value="secret"), \
             mock.patch("time.sleep"), \
             mock.patch.object(vo, "report"):
            vo.start_server(7, "/root/m.gguf", 32768, slots=8)
        self.assertIn("--parallel 8", seen["cmd"])
        self.assertNotIn("-hf", seen["cmd"])
        # Without a detached stdin the ssh call hangs until its timeout - that
        # is what made a working download look like "did not start".
        self.assertIn("< /dev/null", seen["cmd"])
        self.assertIn("setsid", seen["cmd"])

    def test_a_server_that_never_appears_is_a_failure(self):
        with mock.patch.object(vo, "ssh_run", return_value=""), \
             mock.patch.object(vo, "token", return_value="secret"), \
             mock.patch("time.sleep"), \
             mock.patch.object(vo, "report") as rep:
            self.assertFalse(vo.start_server(7, "/root/m.gguf", 8192))
        self.assertIn("did not come up", rep.call_args[0][0])


class BidsThatNeverStart(unittest.TestCase):
    """vast answers "success": false, hands out a contract, and the instance
    sits stopped. Six rentals looked like broken hardware because of it."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        for name, value in (("VAST_DIR", self.dir.name),
                            ("LOGFILE", os.path.join(self.dir.name, "o.log")),
                            ("STATE_FILE",
                             os.path.join(self.dir.name, "state.json"))):
            p = mock.patch.object(vo, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_a_running_instance_passes(self):
        with mock.patch.object(vo, "instances",
                               return_value=[instance(id_=7, cur_state="running")]):
            self.assertTrue(vo.ensure_running(7))

    def test_a_stopped_instance_is_started_once_then_given_up(self):
        stopped = instance(id_=7, cur_state="stopped", intended_status="stopped")
        marks = {}
        with mock.patch.object(vo, "instances", return_value=[stopped]), \
             mock.patch.object(vo, "vast", return_value="ok") as v, \
             mock.patch.object(vo, "report"), \
             mock.patch("time.sleep"):
            self.assertFalse(vo.ensure_running(7, marks=marks))
        self.assertEqual(v.call_count, 2)
        self.assertEqual(list(v.call_args[0]), ["start", "instance", "7"])
        self.assertIn("stayed stopped", marks["exit_state"])

    def test_scp_forces_the_old_protocol(self):
        done = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(vo, "ssh_target",
                               return_value=("h", "22")), \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch("subprocess.run", return_value=done) as r:
            self.assertTrue(vo.scp_to(7, "/tmp/x", "/root/x"))
        # sshd in these images has no sftp subsystem; without -O scp fails.
        self.assertEqual(r.call_args[0][0][:2], ["scp", "-O"])


class SeveralAttempts(unittest.TestCase):
    """One offer is not enough: machines hang, bids lose, downloads crawl."""

    class Args:
        vram, cap, no_bid, yes, attempts = 48, 2.0, False, True, 3
        context, model, no_ssh, task = 8192, "m", False, False
        url, project = "http://x/y.gguf", "/tmp/p"

    def test_the_next_offer_is_tried_after_a_failure(self):
        tried = []

        def one(best, a):
            tried.append(best["id"])
            return 0 if best["id"] == 3 else 1

        with mock.patch.object(vo, "offers",
                               return_value=[offer(id_=1), offer(id_=2),
                                             offer(id_=3), offer(id_=4)]), \
             mock.patch.object(vo, "rent_one", side_effect=one), \
             mock.patch.object(vo, "report"):
            self.assertEqual(vo.rent(self.Args()), 0)
        self.assertEqual(tried, [1, 2, 3])

    def test_it_stops_after_the_given_number_of_attempts(self):
        with mock.patch.object(vo, "offers",
                               return_value=[offer(id_=i) for i in range(9)]), \
             mock.patch.object(vo, "rent_one", return_value=1) as one, \
             mock.patch.object(vo, "report"):
            self.assertEqual(vo.rent(self.Args()), 1)
        self.assertEqual(one.call_count, 3)

    def test_without_yes_nothing_is_rented(self):
        args = self.Args()
        args.yes = False
        with mock.patch.object(vo, "offers", return_value=[offer(id_=1)]), \
             mock.patch.object(vo, "rent_one") as one, \
             mock.patch.object(vo, "report"):
            self.assertEqual(vo.rent(args), 0)
        one.assert_not_called()


class Destroying(unittest.TestCase):
    """An instance believed destroyed but still running is the worst case."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        for name, value in (("VAST_DIR", self.dir.name),
                            ("LOGFILE", os.path.join(self.dir.name, "o.log")),
                            ("STATE_FILE",
                             os.path.join(self.dir.name, "state.json"))):
            p = mock.patch.object(vo, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_destroy_confirms_and_verifies(self):
        with mock.patch.object(vo, "vast", return_value="destroying") as v, \
             mock.patch.object(vo, "instances", return_value=[]), \
             mock.patch("time.sleep"), \
             mock.patch.object(vo, "report") as rep:
            vo.destroy(4711)
        self.assertEqual(list(v.call_args[0]),
                         ["destroy", "instance", "4711", "-y"])
        self.assertIn("destroyed", rep.call_args[0][0])

    def test_a_surviving_instance_is_shouted_about(self):
        alive = instance(id_=4711, actual_status="exited")
        with mock.patch.object(vo, "vast", return_value="Aborted."), \
             mock.patch.object(vo, "instances", return_value=[alive]), \
             mock.patch("time.sleep"), \
             mock.patch.object(vo, "report") as rep:
            vo.destroy(4711)
        self.assertIn("WARNING", rep.call_args[0][0])
        self.assertIn("keeps costing money", rep.call_args[0][0])


class BadOffers(unittest.TestCase):
    """A machine that just died must not be rented again a minute later."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        for name, value in (("VAST_DIR", self.dir.name),
                            ("LOGFILE", os.path.join(self.dir.name, "o.log")),
                            ("STATE_FILE",
                             os.path.join(self.dir.name, "state.json"))):
            p = mock.patch.object(vo, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_a_noted_offer_is_skipped_by_the_search(self):
        vo.mark_bad_offer(4711, "exited")
        with mock.patch.object(vo, "vast", return_value=json.dumps(
                [offer(id_=4711, cards=2, dph=0.10),
                 offer(id_=4712, cards=2, dph=0.50)])):
            found = vo.offers(48, cap=2.0)
        self.assertEqual({o["id"] for o in found}, {4712})

    def test_the_machine_is_blacklisted_too(self):
        with mock.patch.object(vo, "report_machine", return_value=True) as rep:
            vo.mark_bad_offer(4711, "exited", machine_id=137575, report_it=True)
        st = json.load(open(vo.STATE_FILE))
        self.assertIn("4711", st["bad_offers"])
        self.assertIn("137575", st["bad_machines"])
        rep.assert_called_once()

    def test_offers_from_a_bad_machine_are_skipped_even_under_a_new_id(self):
        # An offer id changes as soon as somebody else rents the box; the
        # machine id does not.
        vo.mark_bad_offer(1, "exited", machine_id=137575)
        with mock.patch.object(vo, "vast", return_value=json.dumps(
                [offer(id_=99999, cards=2, dph=0.10, machine_id=137575),
                 offer(id_=99998, cards=2, dph=0.50, machine_id=222)])):
            found = vo.offers(48, cap=2.0)
        self.assertEqual({o["id"] for o in found}, {99998})

    def test_the_report_says_what_happened(self):
        seen = {}

        class Answer:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def catch(req, timeout=0):
            seen["url"] = req.full_url
            seen["body"] = json.loads(req.data)
            seen["auth"] = req.headers.get("Authorization")
            return Answer()

        with mock.patch("urllib.request.urlopen", side_effect=catch), \
             mock.patch("builtins.open", mock.mock_open(read_data="KEY")):
            self.assertTrue(vo.report_machine(137575, "died before ssh"))
        self.assertIn("/machines/137575/reports/", seen["url"])
        self.assertEqual(seen["body"]["machine_id"], 137575)
        self.assertIn("died before ssh", seen["body"]["reason"])
        self.assertIn("Bearer", seen["auth"])

    def test_a_refused_report_is_not_fatal(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.HTTPError(
                            "u", 405, "Method Not Allowed", {},
                            __import__("io").BytesIO(b"nope"))), \
             mock.patch("builtins.open", mock.mock_open(read_data="KEY")):
            self.assertFalse(vo.report_machine(1, "x"))

    def test_the_note_expires(self):
        now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
        old = (now - timedelta(hours=vo.BAD_OFFER_HOURS + 1)).isoformat()
        fresh = (now - timedelta(hours=1)).isoformat()
        st = {"bad_offers": {"1": old, "2": fresh}}
        self.assertEqual(vo.bad_offers(st, now), {2})

    def test_only_the_newest_notes_are_kept(self):
        for i in range(25):
            vo.mark_bad_offer(i, "exited")
        st = json.load(open(vo.STATE_FILE))
        self.assertEqual(len(st["bad_offers"]), 20)


class SetupLog(unittest.TestCase):
    """Every setup leaves a file with timestamps."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        for name, value in (("VAST_DIR", self.dir.name),
                            ("LOGFILE", os.path.join(self.dir.name, "o.log"))):
            p = mock.patch.object(vo, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_name_carries_time_and_offer(self):
        path = vo.setup_logfile(46751059)
        self.assertTrue(path.startswith(self.dir.name))
        self.assertIn("46751059", path)
        self.assertRegex(os.path.basename(path),
                         r"^setup_\d{8}-\d{6}_46751059\.log$")

    def test_waiting_writes_progress_with_timestamps(self):
        path = os.path.join(self.dir.name, "setup.log")
        loading = instance(id_=888, actual_status="loading",
                           status_msg="downloading model 42%")
        with mock.patch.object(vo, "instances", return_value=[loading]), \
             mock.patch.object(vo, "healthy", side_effect=[False, True]), \
             mock.patch.object(vo, "container_log", return_value="      | loading"), \
             mock.patch("time.sleep"):
            self.assertTrue(vo.wait_until_healthy(888, timeout_s=300,
                                                  logfile=path))
        text = open(path).read()
        self.assertIn("downloading model 42%", text)
        self.assertIn("loading", text)
        self.assertIn("healthy after", text)
        self.assertRegex(text, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")

    def test_an_exit_is_explained_before_the_instance_is_gone(self):
        # The instance is destroyed right after this, and with it the only
        # place that says why it died.
        path = os.path.join(self.dir.name, "setup.log")
        dead = instance(id_=888, actual_status="exited")
        marks = {}
        with mock.patch.object(vo, "instances", return_value=[dead]), \
             mock.patch.object(vo, "healthy", return_value=False), \
             mock.patch.object(vo, "container_log",
                               return_value="      | CUDA error: out of memory") as cl, \
             mock.patch("time.sleep"):
            self.assertFalse(vo.wait_until_healthy(888, timeout_s=300,
                                                   logfile=path, marks=marks))
        text = open(path).read()
        self.assertIn("CUDA error: out of memory", text)
        self.assertIn("last container output", text)
        self.assertEqual(marks["exit_state"], "exited")
        self.assertEqual(cl.call_args.kwargs.get("lines"), 80)

    def test_launch_command_and_answer_end_up_in_the_log(self):
        path = os.path.join(self.dir.name, "setup.log")
        done = mock.Mock(returncode=0, stdout='{"new_contract": 4242}', stderr="")
        with mock.patch("subprocess.run", return_value=done), \
             mock.patch.object(vo, "token", return_value="secret"):
            self.assertEqual(vo.launch(offer(id_=1), 8192, "m", path), 4242)
        text = open(path).read()
        self.assertIn("launch command", text)
        self.assertIn("new_contract", text)
        self.assertNotIn("secret", text)

    def test_the_token_is_redacted_in_entrypoint_mode(self):
        # There the token really is in the command line, as its own argument.
        path = os.path.join(self.dir.name, "setup2.log")
        done = mock.Mock(returncode=0, stdout='{"new_contract": 4242}', stderr="")
        with mock.patch("subprocess.run", return_value=done), \
             mock.patch.object(vo, "token", return_value="secret"):
            vo.launch(offer(id_=1), 8192, "m", path, with_ssh=False)
        text = open(path).read()
        self.assertIn("<token>", text)
        self.assertNotIn("secret", text)

    def test_an_empty_answer_becomes_an_error_not_an_instance(self):
        empty = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("subprocess.run", return_value=empty), \
             mock.patch.object(vo, "token", return_value="secret"):
            with self.assertRaises(RuntimeError):
                vo.launch(offer(id_=1), 8192, "m",
                          os.path.join(self.dir.name, "s.log"))

    def test_contract_number_also_from_a_sentence(self):
        sentence = mock.Mock(returncode=0,
                             stdout="Started. new_contract: 987654 ", stderr="")
        with mock.patch("subprocess.run", return_value=sentence), \
             mock.patch.object(vo, "token", return_value="secret"):
            self.assertEqual(vo.launch(offer(id_=1), 8192, "m",
                                       os.path.join(self.dir.name, "s.log")),
                             987654)


class Timings(unittest.TestCase):
    """Same procedure, comparable numbers: one row per rental."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        for name, value in (("VAST_DIR", self.dir.name),
                            ("LOGFILE", os.path.join(self.dir.name, "o.log"))):
            p = mock.patch.object(vo, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_marks_record_container_and_health_time(self):
        loading = instance(id_=888, actual_status="loading")
        running = instance(id_=888, actual_status="running")
        marks = {}
        with mock.patch.object(vo, "instances",
                               side_effect=[[loading], [running], [running]]), \
             mock.patch.object(vo, "healthy", side_effect=[False, False, True]), \
             mock.patch.object(vo, "container_log", return_value=""), \
             mock.patch("time.sleep"):
            self.assertTrue(vo.wait_until_healthy(888, timeout_s=600,
                                                  marks=marks))
        self.assertIn("container_s", marks)
        self.assertIn("healthy_s", marks)
        self.assertEqual(marks["address"], "http://203.0.113.7:41234")

    def test_times_csv_gets_a_header_and_a_row(self):
        o = offer(id_=5, cards=2, dph=0.44, geolocation="Norway, NO")
        marks = {"container_s": 41, "healthy_s": 322, "tok_s": 38.4,
                 "prompt_tok_s": 900.0}
        path = vo.write_times(vo.time_row(o, 888, marks, "m/x:Q4", "ready"))
        rows = list(csv.DictReader(open(path)))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["instance"], "888")
        self.assertEqual(rows[0]["gpus"], "2")
        self.assertEqual(rows[0]["healthy_s"], "322")
        self.assertEqual(rows[0]["tok_s"], "38.4")
        self.assertEqual(rows[0]["outcome"], "ready")
        # a second rental appends without a new header
        vo.write_times(vo.time_row(o, 999, marks, "m/x:Q4", "ready"))
        self.assertEqual(len(list(csv.DictReader(open(path)))), 2)

    def test_a_failure_is_recorded_too(self):
        path = vo.write_times(
            vo.time_row(offer(id_=5), 888, {"container_s": 30}, "m",
                        "never came up"))
        row = list(csv.DictReader(open(path)))[0]
        self.assertEqual(row["outcome"], "never came up")
        self.assertEqual(row["healthy_s"], "")

    def test_nvme_test_reads_both_dd_values(self):
        output = ("536870912 bytes (537 MB, 512 MiB) copied, 0,4 s, 1,3 GB/s\n"
                  "536870912 bytes (537 MB, 512 MiB) copied, 0,2 s, 2400 MB/s")
        with mock.patch.object(vo, "ssh_run", return_value=None), \
             mock.patch.object(vo, "vast", return_value=output):
            write, read, source = vo.measure_nvme(888, offer())
        self.assertAlmostEqual(write, 1.3 * 1024, places=1)
        self.assertAlmostEqual(read, 2400.0)
        self.assertEqual(source, "dd via vastai execute")

    def test_without_execute_the_vast_figure_counts(self):
        o = offer()
        o["disk_bw"] = 950.0
        with mock.patch.object(vo, "ssh_run", return_value=None), \
             mock.patch.object(vo, "vast",
                               side_effect=RuntimeError("no execute")):
            write, read, source = vo.measure_nvme(888, o)
        self.assertEqual((write, read), (950.0, 950.0))
        self.assertIn("disk_bw", source)

    def test_the_promised_bandwidth_is_recorded_next_to_the_real_one(self):
        o = offer(inet_down=1300.0)
        row = vo.time_row(o, 888, {"download_mbits": 1.0}, "m", "too slow")
        self.assertEqual(row["inet_claimed_mbits"], 1300.0)
        self.assertEqual(row["download_mbits"], 1.0)

    def test_the_forecast_is_logged_before_the_download(self):
        gesagt = []
        with mock.patch.object(vo, "scp_to", return_value=True), \
             mock.patch.object(vo, "ssh_run",
                               side_effect=["", "/usr/bin/python3", "started",
                                            "0\n1000000", "0\n6000000",
                                            "stuck"]), \
             mock.patch.object(vo, "report",
                               side_effect=lambda t, *a, **k: gesagt.append(t)), \
             mock.patch("time.sleep"):
            vo.fetch_model(7, "http://x/y.gguf", "/root/m.gguf",
                           claimed_mbits=1000.0)
        self.assertTrue(any("1000 Mbit/s ->" in z for z in gesagt))

    def test_nvme_values_appear_in_the_row(self):
        marks = {"healthy_s": 300, "nvme_write": 1331.2,
                 "nvme_read": 2400.0, "nvme_source": "dd"}
        path = vo.write_times(vo.time_row(offer(), 888, marks, "m", "ready"))
        row = list(csv.DictReader(open(path)))[0]
        self.assertEqual(row["nvme_write_mbs"], "1331.2")
        self.assertEqual(row["nvme_read_mbs"], "2400.0")
        self.assertEqual(row["nvme_source"], "dd")

    def test_the_row_carries_the_setup_log_and_the_last_words(self):
        marks = {"healthy_s": 0, "setup_log": "/home/gh/vast/setup_x.log",
                 "exit_log": "      | CUDA error: out of memory",
                 "exit_state": "exited"}
        row = vo.time_row(offer(), 888, marks, "m", "never came up")
        self.assertEqual(row["setup_log"], "/home/gh/vast/setup_x.log")
        self.assertIn("out of memory", row["exit_log"])
        # the CSV stays narrow, the extra columns only go to the database
        path = vo.write_times(row)
        self.assertEqual(list(csv.DictReader(open(path)))[0].keys().__len__(),
                         len(vo.TIMES_COLUMNS))

    def test_csv_is_written_even_when_the_database_is_missing(self):
        with mock.patch.dict("sys.modules", {"pymysql": None}):
            path = vo.write_times(vo.time_row(offer(), 1, {}, "m", "ready"))
        self.assertEqual(len(list(csv.DictReader(open(path)))), 1)

    def test_database_row_has_a_column_for_every_value(self):
        cur = mock.MagicMock()
        conn = mock.MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        conn.__enter__.return_value = conn
        fake = mock.MagicMock()
        fake.connect.return_value = conn
        with mock.patch.dict("sys.modules", {"pymysql": fake}):
            self.assertTrue(vo.db_write_times(
                vo.time_row(offer(), 888, {"healthy_s": 300}, "m", "ready")))
        befehle = [c[0][0] for c in cur.execute.call_args_list]
        self.assertIn("CREATE TABLE IF NOT EXISTS vast_rentals", befehle[0])
        # A table created before a new measurement existed lacks its column,
        # and every insert then fails - so missing columns are added first.
        self.assertTrue(any("ADD COLUMN" in b for b in befehle))
        insert = befehle[-1]
        values = cur.execute.call_args_list[-1][0][1]
        self.assertIn("INSERT INTO", insert)
        self.assertEqual(insert.count("%s"), len(values))
        self.assertEqual(len(values), len(vo.TIMES_COLUMNS) + 2)

    def test_measurement_reads_the_servers_timings(self):
        answer = mock.MagicMock()
        answer.read.return_value = json.dumps(
            {"timings": {"predicted_per_second": 42.5, "prompt_per_second": 800}}
        ).encode()
        answer.__enter__.return_value = answer
        with mock.patch("urllib.request.urlopen", return_value=answer), \
             mock.patch.object(vo, "token", return_value="t"):
            out, inp = vo.measure_speed("http://1.2.3.4:8080")
        self.assertAlmostEqual(out, 42.5)
        self.assertAlmostEqual(inp, 800.0)

    def test_a_broken_measurement_does_not_kill_the_rental(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("gone")), \
             mock.patch.object(vo, "token", return_value="t"):
            self.assertEqual(vo.measure_speed("http://1.2.3.4:8080"), (0.0, 0.0))


class RentalKey(unittest.TestCase):
    """Renting uses our own key, not the host's."""

    def test_key_comes_before_the_subcommand(self):
        with mock.patch.object(vo, "client_key",
                               return_value=["--api-key", "kkk"]), \
             mock.patch("subprocess.run",
                        return_value=mock.Mock(returncode=0, stdout="[]",
                                               stderr="")) as r:
            vo.vast("show", "instances", "--raw")
        args = r.call_args[0][0]
        self.assertEqual(args[1:4], ["--api-key", "kkk", "show"])

    def test_without_the_file_the_cli_key_stands(self):
        with mock.patch.object(vo, "CLIENT_KEY_FILE", "/does/not/exist"):
            self.assertEqual(vo.client_key(), [])

    def test_the_launch_command_uses_our_key_too(self):
        done = mock.Mock(returncode=0, stdout='{"new_contract": 7}', stderr="")
        with mock.patch.object(vo, "client_key",
                               return_value=["--api-key", "kkk"]), \
             mock.patch("subprocess.run", return_value=done) as r, \
             mock.patch.object(vo, "token", return_value="secret"), \
             mock.patch.object(vo, "report"):
            vo.launch(offer(id_=1), 8192, "m")
        args = r.call_args[0][0]
        self.assertEqual(args[1:3], ["--api-key", "kkk"])
        self.assertEqual(args[3], "create")


class Watchdog(unittest.TestCase):
    """An interruptible machine can be gone between two ticks."""

    class Args:
        vram, cap, no_bid, yes, attempts = 48, 2.0, False, True, 3
        context, model, no_ssh, task, analyze = 8192, "m", False, False, True
        url, project, slots = "http://x/y.gguf", "/tmp/p", 8

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        for name, value in (("VAST_DIR", self.dir.name),
                            ("LOGFILE", os.path.join(self.dir.name, "o.log")),
                            ("STATE_FILE",
                             os.path.join(self.dir.name, "state.json"))):
            p = mock.patch.object(vo, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_nothing_rented_leads_to_a_rental_and_a_worker(self):
        def fake_rent(a):
            vo.state_write({"endpoint": "http://1.2.3.4:8080"})
            return 0

        with mock.patch.object(vo, "running_instance", return_value=None), \
             mock.patch.object(vo, "rent", side_effect=fake_rent), \
             mock.patch.object(vo, "start_worker", return_value=True) as w, \
             mock.patch.object(vo, "report"):
            self.assertEqual(vo.watch(self.Args()), 0)
        w.assert_called_once_with("http://1.2.3.4:8080")

    def test_a_silent_instance_is_thrown_away(self):
        with mock.patch.object(vo, "running_instance",
                               return_value=instance(id_=7)), \
             mock.patch.object(vo, "healthy", return_value=False), \
             mock.patch.object(vo, "destroy") as d, \
             mock.patch.object(vo, "registry_upsert") as reg, \
             mock.patch.object(vo, "report"):
            self.assertEqual(vo.watch(self.Args()), 1)
        d.assert_called_once_with(7)
        # and it is deregistered, so nobody sends work to a dead address
        self.assertFalse(reg.call_args.kwargs["active"])

    def test_a_healthy_instance_restarts_only_a_missing_worker(self):
        with mock.patch.object(vo, "running_instance",
                               return_value=instance(id_=7)), \
             mock.patch.object(vo, "healthy", return_value=True), \
             mock.patch.object(vo, "worker_running", return_value=False), \
             mock.patch.object(vo, "start_worker", return_value=True) as w, \
             mock.patch.object(vo, "run_once", return_value=0), \
             mock.patch.object(vo, "report"):
            self.assertEqual(vo.watch(self.Args()), 0)
        w.assert_called_once_with("http://203.0.113.7:41234")

    def test_a_running_worker_is_left_alone(self):
        with mock.patch.object(vo, "running_instance",
                               return_value=instance(id_=7)), \
             mock.patch.object(vo, "healthy", return_value=True), \
             mock.patch.object(vo, "worker_running", return_value=True), \
             mock.patch.object(vo, "start_worker") as w, \
             mock.patch.object(vo, "run_once", return_value=0), \
             mock.patch.object(vo, "report"):
            vo.watch(self.Args())
        w.assert_not_called()

    def test_the_worker_gets_the_current_endpoint(self):
        with mock.patch("subprocess.run",
                        return_value=mock.Mock(returncode=0, stdout="started",
                                               stderr="")) as r, \
             mock.patch.object(vo, "report"):
            self.assertTrue(vo.start_worker("http://5.6.7.8:9999"))
        befehl = r.call_args[0][0][-1]
        self.assertIn("http://5.6.7.8:9999/v1/chat/completions", befehl)
        # an old run against a dead machine would only pile up errors
        self.assertIn("pkill -f", befehl)


class Registry(unittest.TestCase):
    """The llm_models entry is the interface to the rest of the house - but it
    must never matter more than the running instance."""

    def setUp(self):
        # switch_instance() writes a setup log. Without this redirection the
        # test litters /home/gh/vast with files named after fake offers - and
        # those then sit between the real rentals.
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        for name, value in (("VAST_DIR", self.dir.name),
                            ("LOGFILE", os.path.join(self.dir.name, "o.log")),
                            ("STATE_FILE",
                             os.path.join(self.dir.name, "state.json"))):
            p = mock.patch.object(vo, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_without_a_database_the_entry_fails_quietly(self):
        with mock.patch.dict("sys.modules", {"pymysql": None}):
            self.assertFalse(vo.registry_upsert(
                "http://203.0.113.7:41234", offer(cards=2), 888))

    def test_switching_registers_the_new_address(self):
        entries = []
        old = instance(id_=777, dph=1.00)
        v = vo.evaluate(old, [offer(id_=333, cards=2, dph=0.60)], 48, 2.0)
        with mock.patch.object(vo, "launch", return_value=888), \
             mock.patch.object(vo, "wait_until_healthy", return_value=True), \
             mock.patch.object(vo, "destroy"), \
             mock.patch.object(vo, "instances", return_value=[instance(id_=888)]), \
             mock.patch.object(vo, "state_write"), \
             mock.patch.object(vo, "registry_upsert",
                               side_effect=lambda *a, **k: entries.append(a) or True):
            self.assertTrue(vo.switch_instance(v, 8192, "m"))
        self.assertEqual(entries[0][0], "http://203.0.113.7:41234")
        self.assertEqual(entries[0][2], 888)


class RunningInstance(unittest.TestCase):

    def test_exited_instances_do_not_count(self):
        with mock.patch.object(vo, "vast", return_value=json.dumps(
                [instance(id_=1, actual_status="exited")])):
            self.assertIsNone(vo.running_instance())

    def test_the_most_expensive_one_drives_the_bill(self):
        with mock.patch.object(vo, "vast", return_value=json.dumps(
                [instance(id_=1, dph=0.30), instance(id_=2, dph=0.90)])):
            self.assertEqual(vo.running_instance()["id"], 2)

    def test_endpoint_from_the_port_mapping(self):
        self.assertEqual(vo.endpoint(instance()), "http://203.0.113.7:41234")

    def test_no_endpoint_without_a_mapping(self):
        i = instance()
        i["ports"] = {}
        self.assertIsNone(vo.endpoint(i))


if __name__ == "__main__":
    unittest.main(verbosity=2)
