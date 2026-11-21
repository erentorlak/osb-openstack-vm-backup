"""Faz 0 betiklerinin Ceph/OpenStack gerektirmeyen birim testleri.

Çalıştırma:  python -m unittest discover -s faz0/tests -v
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import analyze  # noqa: E402
import ceph_latency_sampler as lat  # noqa: E402
import estimate  # noqa: E402
import inventory  # noqa: E402
import rbd_survey  # noqa: E402

CH = estimate.CHUNK


# ------------------------------------------------------------------ estimate
class TestEstimateCore(unittest.TestCase):
    def test_extents_to_chunks(self):
        ext = [(0, 10, True), (CH - 1, 2, True), (5 * CH, CH, True), (9 * CH, CH, False)]
        self.assertEqual(estimate.extents_to_chunks(ext, 10 * CH), [0, 1, 5])

    def test_extents_clamped_to_size(self):
        self.assertEqual(estimate.extents_to_chunks([(0, 100 * CH, True)], 2 * CH + 5), [0, 1, 2])

    def test_token_bucket_waits(self):
        t = [0.0]
        slept = []
        tb = estimate.TokenBucket(rate_bytes=CH, clock=lambda: t[0],
                                  sleep=lambda s: (slept.append(s), t.__setitem__(0, t[0] + s)))
        tb.take(CH)          # kova dolu başlar
        tb.take(CH)          # ~1 s beklemeli
        self.assertAlmostEqual(sum(slept), 1.0, places=3)

    def test_token_bucket_unlimited(self):
        tb = estimate.TokenBucket(0, sleep=lambda s: self.fail("beklememeli"))
        tb.take(10 * CH)

    def test_accounting_zero_dedup_compress(self):
        acc = estimate.Accounting(compress_every=1, compressor_name="zlib")
        per = {"chunks_read": 0, "bytes_read": 0, "zero_chunks": 0, "nonzero_chunks": 0,
               "unique_in_project_chunks": 0}
        a = b"A" * CH
        r = os.urandom(CH)
        for proj, data in (("p1", bytes(CH)), ("p1", a), ("p1", a), ("p2", a), ("p1", r)):
            acc.add_chunk(proj, data, per)
        t = acc.summary()["totals"]
        self.assertEqual(t["zero_chunks"], 1)
        self.assertEqual(t["nonzero_chunks"], 4)
        self.assertEqual(t["unique_project_chunks"], 3)   # p1:a, p2:a, p1:r
        self.assertEqual(t["unique_global_chunks"], 2)    # a, r
        ratios = acc.summary()["ratios"]
        self.assertAlmostEqual(ratios["dedup_project_factor"], 4 / 3, places=3)
        self.assertAlmostEqual(ratios["per_project_dedup_overhead"], 0.5, places=3)
        self.assertLess(ratios["compress_ratio"], 0.6)    # 'A'*4MiB çok iyi sıkışır, random sıkışmaz (≤1)

    def test_process_file_source(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "img.raw")
            with open(p, "wb") as f:
                f.write(bytes(CH))            # sıfır
                f.write(b"x" * CH)            # benzersiz
                f.write(b"x" * CH)            # kopya
                f.write(b"tail")              # kısa son chunk
            src = estimate.FileSource(p)
            try:
                acc = estimate.Accounting(compress_every=8, compressor_name="zlib")
                per = estimate.process_image(src, "file", acc, estimate.TokenBucket(0), threads=2,
                                             max_bytes=0, deadline=None)
            finally:
                src.close()
            self.assertEqual(per["chunks_read"], 4)
            self.assertEqual(per["zero_chunks"], 1)
            self.assertEqual(per["unique_in_project_chunks"], 2)
            self.assertEqual(per["bytes_read"], 3 * CH + 4)

    def test_main_with_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "img.raw")
            with open(p, "wb") as f:
                f.write(b"y" * (2 * CH))
            out = os.path.join(d, "est.json")
            rc = estimate.main(["--file", p, "--rate-mb", "0", "--compressor", "zlib", "--out", out])
            self.assertEqual(rc, 0)
            with open(out, encoding="utf-8") as f:
                res = json.load(f)
            self.assertEqual(res["totals"]["unique_project_chunks"], 1)
            self.assertIsNotNone(res["ratios"]["stored_fraction_of_read"])


# ------------------------------------------------------------------ inventory
class FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.content = json.dumps(body).encode() if body is not None else b""
        self.text = self.content.decode()

    def json(self):
        return self._body


class FakeProxy:
    def __init__(self, routes, ep=None):
        self.routes = routes
        self.calls = []
        self.ep = ep

    def get(self, path, **kw):
        self.calls.append((path, kw))
        status, body = self.routes.get(path, (404, {"error": "yok"}))
        return FakeResp(status, body)

    def get_endpoint_data(self):
        return self.ep

    def get_endpoint(self):
        return "http://192.0.2.10:8774/v2.1"


class TestInventoryHelpers(unittest.TestCase):
    def test_versions(self):
        self.assertEqual(inventory.parse_ver("2.96"), (2, 96))
        self.assertEqual(inventory.parse_ver((3, 71)), (3, 71))
        self.assertIsNone(inventory.parse_ver(None))
        self.assertEqual(inventory.pick_mv((2, 90), (2, 93)), "2.90")
        self.assertEqual(inventory.pick_mv((2, 90), (2, 60)), "2.60")
        self.assertEqual(inventory.pick_mv((3, 64), None), "3.64")

    def test_paged_follows_next_and_passes_microversion(self):
        px = FakeProxy({
            "/servers/detail?all_tenants=1": (200, {"servers": [{"id": "a"}],
                                                    "servers_links": [{"rel": "next", "href": "http://x/next"}]}),
            "http://x/next": (200, {"servers": [{"id": "b"}]}),
        })
        api = inventory.Api()
        res = api.paged(px, "/servers/detail?all_tenants=1", "servers", mv="2.90")
        self.assertEqual([s["id"] for s in res], ["a", "b"])
        self.assertTrue(all(kw.get("microversion") == "2.90" and kw.get("raise_exc") is False for _, kw in px.calls))

    def test_get_error_and_ok404(self):
        api = inventory.Api()
        px = FakeProxy({"/boom": (500, {"x": 1})})
        with self.assertRaises(inventory.ApiError):
            api.get(px, "/boom")
        self.assertIsNone(api.get(px, "/missing", ok404=True))

    def test_discover_root_fallback(self):
        ep = types.SimpleNamespace(service_url="http://192.0.2.10:8774/v2.1", api_version=(2, 1),
                                   min_microversion=None, max_microversion=None)
        px = FakeProxy({"http://192.0.2.10:8774/": (200, {"versions": [
            {"id": "v2.0", "version": ""}, {"id": "v2.1", "version": "2.96", "min_version": "2.1", "status": "CURRENT"}]})}, ep)
        info = inventory.discover(inventory.Api(), px, "v2.1")
        self.assertEqual(info["max_microversion"], "2.96")
        self.assertEqual(info["discovery_fallback"], "root-document")

    def test_discover_cinder_300(self):
        ep = types.SimpleNamespace(service_url="http://192.0.2.10:8776/v3/p", api_version=None,
                                   min_microversion=None, max_microversion=None)
        px = FakeProxy({"http://192.0.2.10:8776/": (300, {"versions": [
            {"id": "v3.0", "version": "3.71", "min_version": "3.0"}]})}, ep)
        self.assertEqual(inventory.discover(inventory.Api(), px, "v3.0")["max_microversion"], "3.71")

    def test_scrub_user_data(self):
        import base64
        s = {"OS-EXT-SRV-ATTR:user_data": base64.b64encode(b"#cloud-config\nusers: []\n").decode()}
        inventory.scrub_user_data(s, keep=False)
        self.assertNotIn("OS-EXT-SRV-ATTR:user_data", s)
        self.assertTrue(s["_osb_user_data"]["present"])
        self.assertTrue(s["_osb_user_data"]["starts_with_cloud_config"])
        s2 = {"OS-EXT-SRV-ATTR:user_data": None}
        inventory.scrub_user_data(s2, keep=False)
        self.assertFalse(s2["_osb_user_data"]["present"])


# ------------------------------------------------------------------ rbd_survey
class FakeRbdMod:
    RBD_FEATURE_LAYERING = 1
    RBD_FEATURE_EXCLUSIVE_LOCK = 4
    RBD_FEATURE_OBJECT_MAP = 8
    RBD_FEATURE_FAST_DIFF = 16
    RBD_FEATURE_DEEP_FLATTEN = 32
    RBD_FLAG_OBJECT_MAP_INVALID = 1
    RBD_FLAG_FAST_DIFF_INVALID = 2

    class ImageNotFound(Exception):
        pass

    def __init__(self, images):
        self.images = images
        mod = self

        class Image:
            def __init__(self, ioctx, name, snapshot=None, read_only=False):
                assert read_only, "survey yalnız read_only açmalı"
                self.d = mod.images[name]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def size(self):
                return self.d["size"]

            def stat(self):
                return {"order": 23}

            def features(self):
                return self.d["features"]

            def flags(self):
                return self.d.get("flags", 0)

            def id(self):
                return "abc123"

            def get_parent_image_spec(self):
                if not self.d.get("parent"):
                    raise mod.ImageNotFound()
                return self.d["parent"]

            def overlap(self):
                return 10 * 1024 ** 3

            def list_snaps(self):
                return iter(self.d.get("snaps", []))

            def is_protected_snap(self, name):
                return name.startswith("snapshot-")

            def get_snap_timestamp(self, sid):
                return dt.datetime(2026, 11, 20, 0, 0)

            def diff_iterate(self, off, length, from_snap, cb, include_parent=True, whole_object=False):
                assert whole_object
                if from_snap:
                    cb(0, 8 << 20, True)
                    return
                cb(0, 16 << 20, True)
                cb(16 << 20, 8 << 20, False)
                if include_parent:
                    cb(64 << 20, 8 << 20, True)
        self.Image = Image


class TestRbdSurvey(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(rbd_survey.classify_snap("backup.osb-1234.snap.1700000000"), "osb")
        self.assertEqual(rbd_survey.classify_snap("backup.0f3c6e2a-1b2c-4d5e-8f90-123456789abc.snap.1700"), "cinder_backup")
        self.assertEqual(rbd_survey.classify_snap("snapshot-0f3c6e2a-1b2c-4d5e-8f90-123456789abc"), "cinder")
        self.assertEqual(rbd_survey.classify_snap("volume-x.clone_snap"), "clone_snap")
        self.assertEqual(rbd_survey.classify_snap("manual"), "other")

    def test_survey_image(self):
        vid = "0f3c6e2a-1b2c-4d5e-8f90-123456789abc"
        fake = FakeRbdMod({f"volume-{vid}": {
            "size": 100 << 20, "features": 1 | 4 | 8 | 16 | 32,
            "parent": {"pool_name": "glance-images", "image_name": "img", "snap_name": "snap"},
            "snaps": [{"id": 7, "size": 100 << 20, "name": f"backup.{vid}.snap.1700", "namespace": 0}],
        }})
        args = types.SimpleNamespace(allocation=True, change_rate=True, allow_slow=False, min_snap_age_days=0.5)
        now = dt.datetime(2026, 11, 21, 12, 0, tzinfo=dt.timezone.utc)
        rec = rbd_survey.survey_image(None, fake, f"volume-{vid}", args, now)
        self.assertEqual(rec["cinder_volume_id"], vid)
        self.assertTrue(rec["fast_diff_usable"])
        self.assertEqual(rec["allocated_child"], 16 << 20)
        self.assertEqual(rec["allocated_with_parent"], 24 << 20)
        self.assertEqual(rec["snaps"][0]["class"], "cinder_backup")
        self.assertEqual(rec["snaps"][0]["backup_id"], vid)
        self.assertAlmostEqual(rec["snaps"][0]["age_days"], 1.5, places=2)
        self.assertEqual(rec["change_since_snap"]["changed_bytes"], 8 << 20)

    def test_invalid_flags_skip_allocation(self):
        fake = FakeRbdMod({"volume-x": {"size": 1 << 30, "features": 1 | 8 | 16, "flags": 1}})
        args = types.SimpleNamespace(allocation=True, change_rate=False, allow_slow=False, min_snap_age_days=0.5)
        rec = rbd_survey.survey_image(None, fake, "volume-x", args, dt.datetime.now(dt.timezone.utc))
        self.assertEqual(rec["flags"], ["object-map-invalid"])
        self.assertFalse(rec["fast_diff_usable"])
        self.assertNotIn("allocated_child", rec)


# ------------------------------------------------------------------ latency
class TestLatency(unittest.TestCase):
    SAMPLE1 = """# HELP x
ceph_osd_commit_latency_ms{ceph_daemon="osd.0"} 2.0
ceph_osd_commit_latency_ms{ceph_daemon="osd.1"} 4.0
ceph_osd_apply_latency_ms{ceph_daemon="osd.0"} 1.0
ceph_osd_op_r_latency_sum{ceph_daemon="osd.0"} 10.0
ceph_osd_op_r_latency_count{ceph_daemon="osd.0"} 1000.0
ceph_osd_op_r{ceph_daemon="osd.0"} 1000.0
ceph_osd_op_w_latency_sum{ceph_daemon="osd.0"} 5.0
ceph_osd_op_w_latency_count{ceph_daemon="osd.0"} 100.0
ceph_osd_op_w{ceph_daemon="osd.0"} 100.0
unrelated_metric 5
"""
    SAMPLE2 = SAMPLE1.replace("op_r_latency_sum{ceph_daemon=\"osd.0\"} 10.0", "op_r_latency_sum{ceph_daemon=\"osd.0\"} 12.0") \
                     .replace("op_r_latency_count{ceph_daemon=\"osd.0\"} 1000.0", "op_r_latency_count{ceph_daemon=\"osd.0\"} 2000.0") \
                     .replace("ceph_osd_op_r{ceph_daemon=\"osd.0\"} 1000.0", "ceph_osd_op_r{ceph_daemon=\"osd.0\"} 2500.0")

    def test_parse_and_interval(self):
        wanted = set(lat.GAUGES) | set(lat.COUNTERS)
        a = lat.parse_metrics(self.SAMPLE1, wanted)
        b = lat.parse_metrics(self.SAMPLE2, wanted)
        self.assertNotIn("unrelated_metric", a)
        row = lat.interval_row(a, b, 10.0)
        self.assertEqual(row["ceph_osd_commit_latency_ms_avg"], 3.0)
        self.assertEqual(row["ceph_osd_commit_latency_ms_max"], 4.0)
        self.assertEqual(row["op_r_latency_ms"], 2.0)       # 2 s / 1000 op
        self.assertEqual(row["op_r_iops"], 150.0)
        self.assertIsNone(row["op_w_latency_ms"])          # sayaç değişmedi


# ------------------------------------------------------------------ analyze
def synthetic_inventory():
    def uid(n):
        return f"00000000-0000-4000-8000-{n:012d}"
    projects = [{"id": "pA", "name": "gercek-proje-adi-A"}, {"id": "pB", "name": "gercek-proje-adi-B"}]
    vols, servers, att = [], [], {}
    # pA: 3 Ceph VM (biri 2 diskli), pB: 1 NetApp VM + 1 volume'suz VM
    for i in range(3):
        sid = uid(100 + i)
        root = {"id": uid(i), "size": 50, "bootable": "true", "volume_type": "cinder-rbd",
                "os-vol-host-attr:host": "ctrl01-ceph-volume@cinder_rbd#cinder_rbd",
                "os-vol-tenant-attr:tenant_id": "pA",
                "volume_image_metadata": {"hw_qemu_guest_agent": "yes" if i == 0 else "no",
                                          "hw_firmware_type": "uefi" if i == 1 else "bios"},
                "encrypted": False, "multiattach": False}
        vols.append(root)
        att[sid] = [{"volumeId": root["id"], "device": "/dev/vda", "delete_on_termination": True}]
        if i == 2:
            data = dict(root, id=uid(50), bootable="false", volume_image_metadata=None, size=200)
            vols.append(data)
            att[sid].append({"volumeId": data["id"], "device": "/dev/vdb", "delete_on_termination": False})
        servers.append({"id": sid, "name": f"gizli-vm-{i}", "status": "ACTIVE", "tenant_id": "pA",
                        "OS-EXT-SRV-ATTR:root_device_name": "/dev/vda",
                        "flavor": {"original_name": "m1", "vcpus": 2, "ram": 2048, "ephemeral": 0, "swap": 0,
                                   "extra_specs": {}},
                        "_osb_user_data": {"present": i == 0}, "key_name": "k1", "user_id": "u1"})
    nv = {"id": uid(60), "size": 100, "bootable": "true", "volume_type": "netapp-thin",
          "os-vol-host-attr:host": "cmp01@netapp_fc#POOL_A", "os-vol-tenant-attr:tenant_id": "pB",
          "volume_image_metadata": {"os_type": "windows"}, "encrypted": True, "multiattach": False}
    vols.append(nv)
    servers.append({"id": uid(200), "name": "gizli-win", "status": "SHUTOFF", "tenant_id": "pB",
                    "OS-EXT-SRV-ATTR:root_device_name": "/dev/vda",
                    "flavor": {"original_name": "silinmis", "ephemeral": 10, "swap": 0, "extra_specs": {"hw:tpm_version": "2.0"}}})
    att[uid(200)] = [{"volumeId": nv["id"], "device": "/dev/vda", "delete_on_termination": False}]
    servers.append({"id": uid(300), "name": "worker-1", "status": "ACTIVE", "tenant_id": "pB",
                    "flavor": {"original_name": "m1", "ephemeral": 0, "swap": 0}})
    att[uid(300)] = []
    return {
        "meta": {"collected_at": "2026-11-21T10:00:00+00:00", "api_calls": 42,
                 "microversions_used": {"servers": "2.90"}},
        "endpoints": {"compute": {"min_microversion": "2.1", "max_microversion": "2.96"},
                      "block_storage": {"min_microversion": "3.0", "max_microversion": "3.71"}},
        "projects": projects, "servers": servers, "attachments": att, "volumes": vols,
        "flavors": [{"id": "f1", "name": "m1"}],
        "snapshots": [{"id": uid(900), "volume_id": uid(0), "status": "available"}],
        "backups": [{"id": uid(800), "status": "available", "size": 50, "is_incremental": False}],
        "cinder_quotas": {"pA": {"snapshots": {"limit": 3, "in_use": 1, "reserved": 0},
                                 "gigabytes": {"limit": 1000, "in_use": 300, "reserved": 0}},
                          "pB": {"snapshots": {"limit": 10, "in_use": 0}, "gigabytes": {"limit": -1, "in_use": 100}}},
        "ports": [{"id": "port1", "device_id": uid(100), "port_security_enabled": True, "binding:vnic_type": "normal"}],
        "floatingips": [{"id": "fip1", "port_id": "port1"}],
        "security_groups": [{"id": "sg1", "security_group_rules": [{"remote_group_id": "sg1"}]}],
        "server_groups": [], "keypairs": [{"user_id": "u1", "name": "k1", "missing": True}],
        "volume_types": [{"name": "netapp-thin", "_osb_encryption": {"provider": "luks"}}],
        "cinder_services": [{"binary": "cinder-backup", "state": "up"}], "pools": [], "errors": [],
    }


def synthetic_survey(inv):
    vids = [v["id"] for v in inv["volumes"] if "rbd" in v["os-vol-host-attr:host"]]
    imgs = []
    for n, vid in enumerate(vids):
        imgs.append({"name": f"volume-{vid}", "cinder_volume_id": vid, "features": ["layering", "fast-diff"],
                     "flags": ["object-map-invalid"] if n == 0 else [],
                     "parent": {"pool_name": "glance-images"} if n < 2 else None,
                     "allocated_child": 10 * analyze.GIB, "allocated_with_parent": 20 * analyze.GIB,
                     "change_since_snap": {"age_days": 1.0, "changed_bytes_per_day": analyze.GIB // 5,
                                           "changed_bytes": analyze.GIB // 5},
                     "snaps": [{"name": "backup.deadbeef-0000-4000-8000-000000000000.snap.1", "class": "cinder_backup",
                                "backup_id": "deadbeef-0000-4000-8000-000000000000"}] if n == 0 else []})
    imgs.append({"name": "volume-11111111-1111-4111-8111-111111111111",
                 "cinder_volume_id": "11111111-1111-4111-8111-111111111111", "features": [], "flags": [], "snaps": []})
    return {"pools": {"cinder-volumes": {"images": imgs}}, "errors": []}


class TestAnalyze(unittest.TestCase):
    def run_report(self, extra=(), with_survey=True, with_hosts=False, anonymize=True):
        inv = synthetic_inventory()
        with tempfile.TemporaryDirectory() as d:
            ip = os.path.join(d, "inv.json")
            with open(ip, "w", encoding="utf-8") as f:
                json.dump(inv, f)
            argv = ["--inventory", ip, "--out", os.path.join(d, "r.md"), "--json-out", os.path.join(d, "s.json")]
            if with_survey:
                sp = os.path.join(d, "sv.json")
                with open(sp, "w", encoding="utf-8") as f:
                    json.dump(synthetic_survey(inv), f)
                argv += ["--rbd-survey", sp]
            if with_hosts:
                hp = os.path.join(d, "dom.csv")
                with open(hp, "w", encoding="utf-8") as f:
                    f.write("uuid,libvirt_name,state,qga_channel,qga_state,uefi_nvram,vtpm,rbd_disks,block_disks,"
                            "other_disks,disk_buses,qga_version,qga_fsfreeze\n")
                    f.write(f"{inv['servers'][0]['id']},instance-1,running,yes,connected,no,no,1,0,0,virtio,,\n")
                argv += ["--hosts-csv", hp]
            if anonymize:
                argv.append("--anonymize")
            argv += list(extra)
            self.assertEqual(analyze.main(argv), 0)
            with open(os.path.join(d, "r.md"), encoding="utf-8") as f:
                md = f.read()
            with open(os.path.join(d, "s.json"), encoding="utf-8") as f:
                summ = json.load(f)
        return md, summ, inv

    def test_counts_and_verdicts(self):
        md, s, _ = self.run_report(with_hosts=True)
        self.assertEqual(s["inventory"]["servers"], 5)
        self.assertEqual(s["inventory"]["vm_class"], {"yalnız_ceph": 3, "yalnız_netapp": 1, "volume_yok": 1})
        self.assertEqual(s["inventory"]["max_vols_per_vm"], 2)
        self.assertEqual(s["verdicts"]["V-01"]["status"], "TAMAM")
        self.assertEqual(s["verdicts"]["V-21"]["status"], "DİKKAT")    # pA: snapshot limit 3 < gerekli
        self.assertEqual(s["special"]["encrypted"], 1)
        self.assertEqual(s["special"]["vtpm"], 1)
        self.assertEqual(s["special"]["uefi"], 1)
        self.assertEqual(s["special"]["windows"], 1)
        self.assertEqual(s["special"]["ephemeral_or_swap"], 1)
        self.assertEqual(s["rbd"]["stale_backup_snaps"], 1)
        self.assertEqual(s["rbd"]["orphan_images"], 1)
        self.assertEqual(s["rbd"]["invalid_flags"], 1)
        self.assertEqual(s["verdicts"]["V-12"]["status"], "DİKKAT")    # 1/5 bağlı
        self.assertIn("**YETERSİZ**", md)
        self.assertAlmostEqual(s["capacity"]["daily_change"], 0.02, places=3)   # 0.2 GiB/gün / 10 GiB

    def test_anonymize_hides_names_and_ids(self):
        md, _, inv = self.run_report()
        self.assertNotIn("gercek-proje-adi", md)
        self.assertNotIn("gizli-vm", md)
        for s in inv["servers"]:
            self.assertNotIn(s["id"], md)
        self.assertNotIn("192.0.2", md)
        self.assertIn("proj-01", md)

    def test_without_optional_inputs(self):
        _, s, _ = self.run_report(with_survey=False)
        self.assertEqual(s["verdicts"]["V-05"]["status"], "VERİ YOK")
        self.assertEqual(s["verdicts"]["V-20"]["status"], "VERİ YOK")

    def test_zed_nova_flagged(self):
        inv = synthetic_inventory()
        inv["endpoints"]["compute"]["max_microversion"] = "2.93"
        with tempfile.TemporaryDirectory() as d:
            ip = os.path.join(d, "inv.json")
            with open(ip, "w", encoding="utf-8") as f:
                json.dump(inv, f)
            out = os.path.join(d, "r.md")
            js = os.path.join(d, "s.json")
            analyze.main(["--inventory", ip, "--out", out, "--json-out", js])
            with open(js, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["verdicts"]["V-01"]["status"], "DİKKAT")


if __name__ == "__main__":
    unittest.main()
