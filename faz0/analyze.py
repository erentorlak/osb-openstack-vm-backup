#!/usr/bin/env python3
"""OSB Faz 0 — toplanan çıktılardan Faz 0 raporunu (Markdown) üretir. Çevrimdışı çalışır.

Girdiler (yalnız --inventory zorunlu):
  --inventory     inventory.py çıktısı
  --rbd-survey    rbd_survey.py çıktısı (önerilir: --allocation --change-rate ile alınmış)
  --hosts-csv     host_checks.sh domain CSV'leri (tekrarlanabilir)
  --ontap         ontap_readonly.py çıktısı
  --estimate      estimate.py çıktısı
Çıktı: Markdown rapor (+ opsiyonel JSON özet). --anonymize: proje/VM adları ve UUID'ler takma adlarla
değiştirilir, IP yazılmaz; raporu paylaşmadan önce yine de gözden geçirin.

Yalnız standart kütüphane; Python >= 3.8.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import statistics
import sys
from collections import Counter, defaultdict

SCRIPT_VERSION = "0.1.0"
TIB = 1024 ** 4
GIB = 1024 ** 3
RE_UUID_IN = re.compile(r"volume-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
RE_SNAP_IMG = re.compile(r"^snapshot-([0-9a-f-]{36})$")
TRUE = {"yes", "true", "1", "on", True}


def tib(b) -> str:
    return "—" if b is None else f"{b / TIB:,.2f} TiB"


def gib(b) -> str:
    return "—" if b is None else f"{b / GIB:,.1f} GiB"


def pct(a, b) -> str:
    return "—" if not b else f"%{100 * a / b:.0f}"


def ver_tuple(s):
    try:
        a, b = str(s).split(".", 1)
        return int(a), int(b)
    except (ValueError, AttributeError):
        return None


class Anon:
    def __init__(self, enabled: bool, projects: list[dict]):
        self.enabled = enabled
        self.pmap = {}
        for i, p in enumerate(sorted(projects, key=lambda p: p.get("id") or ""), 1):
            self.pmap[p["id"]] = f"proj-{i:02d}" if enabled else (p.get("name") or p["id"])
        self.smap: dict[str, str] = {}

    def project(self, pid) -> str:
        if pid not in self.pmap:
            self.pmap[pid] = f"proj-x{len(self.pmap) + 1:02d}" if self.enabled else str(pid)
        return self.pmap[pid]

    def server(self, s: dict) -> str:
        sid = s.get("id")
        if not self.enabled:
            return f"{s.get('name')} ({sid})"
        if sid not in self.smap:
            self.smap[sid] = f"vm-{len(self.smap) + 1:03d}"
        return self.smap[sid]

    def ident(self, x) -> str:
        return "‹gizli›" if self.enabled else str(x)


class Model:
    """inventory.json üzerinde türetilmiş görünümler."""

    def __init__(self, inv: dict, ceph_backends: set[str], netapp_backends: set[str]):
        self.inv = inv
        self.ceph_backends, self.netapp_backends = ceph_backends, netapp_backends
        self.volumes = {v["id"]: v for v in inv.get("volumes", []) if v.get("id")}
        self.servers = [s for s in inv.get("servers", [])]
        self.flavors = {f["id"]: f for f in inv.get("flavors", []) if f.get("id")}
        self.backup_ids = {b["id"] for b in inv.get("backups", []) if b.get("id")}
        self.snapshot_ids = {s["id"] for s in inv.get("snapshots", []) if s.get("id")}
        self.ports_by_dev = defaultdict(list)
        for p in inv.get("ports", []):
            self.ports_by_dev[p.get("device_id")].append(p)
        self.fip_by_port = {f.get("port_id"): f for f in inv.get("floatingips", []) if f.get("port_id")}

    @staticmethod
    def backend_name(vol: dict) -> str:
        host = vol.get("os-vol-host-attr:host") or ""
        if "@" not in host:
            return ""
        return host.split("@", 1)[1].split("#", 1)[0]

    def kind(self, vol: dict) -> str:
        b = self.backend_name(vol)
        if b in self.ceph_backends or "rbd" in b.lower():
            return "ceph"
        if b in self.netapp_backends or "netapp" in b.lower():
            return "netapp"
        return "other"

    @staticmethod
    def vproject(vol: dict):
        return vol.get("os-vol-tenant-attr:tenant_id") or vol.get("project_id")

    @staticmethod
    def sproject(s: dict):
        return s.get("tenant_id") or s.get("project_id")

    def attachments(self, s: dict) -> list[dict]:
        att = (self.inv.get("attachments") or {}).get(s["id"])
        if att is not None:
            return [{"volume_id": a.get("volumeId"), "device": a.get("device"),
                     "delete_on_termination": a.get("delete_on_termination")} for a in att]
        return [{"volume_id": a.get("id"), "device": None, "delete_on_termination": a.get("delete_on_termination")}
                for a in s.get("os-extended-volumes:volumes_attached", []) or []]

    def root_volume(self, s: dict):
        atts = self.attachments(s)
        rdn = s.get("OS-EXT-SRV-ATTR:root_device_name")
        for a in atts:
            if rdn and a["device"] == rdn:
                return self.volumes.get(a["volume_id"]), "root_device_name"
        boot = [self.volumes.get(a["volume_id"]) for a in atts
                if str((self.volumes.get(a["volume_id"]) or {}).get("bootable")).lower() == "true"]
        if len(boot) == 1:
            return boot[0], "bootable"
        return None, "belirsiz" if atts else "volume_yok"

    def flavor(self, s: dict) -> dict:
        f = s.get("flavor") or {}
        if "vcpus" in f or "original_name" in f:
            return f
        return self.flavors.get(f.get("id"), f)

    def vm_class(self, s: dict) -> str:
        kinds = {self.kind(self.volumes[a["volume_id"]]) for a in self.attachments(s)
                 if a["volume_id"] in self.volumes}
        if not kinds:
            return "volume_yok"
        if kinds == {"ceph"}:
            return "yalnız_ceph"
        if kinds == {"netapp"}:
            return "yalnız_netapp"
        return "karışık"


def vim(vol: dict | None) -> dict:
    return (vol or {}).get("volume_image_metadata") or {}


def load_json(path):
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_hosts(paths: list[str]) -> dict[str, dict]:
    res = {}
    for p in paths or []:
        with open(p, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("uuid"):
                    row["_file"] = p
                    res[row["uuid"].strip().lower()] = row
    return res


def quota_entry(q: dict, key: str) -> tuple[int | None, int]:
    v = q.get(key)
    if isinstance(v, dict):
        lim = v.get("limit")
        used = (v.get("in_use") or 0) + (v.get("reserved") or 0)
        return (None if lim in (None, -1) else int(lim)), int(used)
    if isinstance(v, (int, float)):
        return (None if v == -1 else int(v)), 0
    return None, 0


def build_report(a) -> tuple[str, dict]:
    inv = load_json(a.inventory)
    survey = load_json(a.rbd_survey)
    ontap = load_json(a.ontap)
    est = load_json(a.estimate)
    hosts = load_hosts(a.hosts_csv)
    m = Model(inv, set(a.ceph_backends.split(",")), set(a.netapp_backends.split(",")))
    an = Anon(a.anonymize, inv.get("projects", []))
    L: list[str] = []
    S: dict = {"verdicts": {}}

    def h(t):
        L.append("")
        L.append(t)
        L.append("")

    def verdict(vid, status, text):
        S["verdicts"][vid] = {"status": status, "text": text}

    meta = inv.get("meta", {})
    L.append("# OSB Faz 0 — Doğrulama raporu")
    L.append("")
    L.append(f"- Üretildi: {dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')} (analyze.py {SCRIPT_VERSION})")
    L.append(f"- Envanter zamanı: {meta.get('collected_at')} · API çağrısı: {meta.get('api_calls')} · "
             f"kullanılan mikro sürümler: `{json.dumps(meta.get('microversions_used'))}`")
    L.append(f"- Girdiler: inventory{' + rbd_survey' if survey else ''}{' + hosts(' + str(len(a.hosts_csv)) + ')' if hosts else ''}"
             f"{' + ontap' if ontap else ''}{' + estimate' if est else ''} · anonim: {'evet' if a.anonymize else 'HAYIR'}")
    L.append("- Durum etiketleri: **TAMAM** · **DİKKAT** (karar/aksiyon gerekir) · **VERİ YOK** (girdi eksik)")
    L.append("- Referanslar TASARIM.md'deki V-xx / B-xx / D-xx maddeleridir.")

    # ------------------------------------------------------------ V-01
    h("## V-01 — API sürümleri")
    eps = inv.get("endpoints") or {}
    rows = []
    for name, ep in eps.items():
        ep = ep or {}
        rows.append(f"| {name} | {ep.get('min_microversion')} | {ep.get('max_microversion')} | "
                    f"{ep.get('discovery_fallback') or 'keystoneauth'} |")
    L += ["| Servis | min | max | keşif |", "|---|---|---|---|"] + rows
    nmax = ver_tuple((eps.get("compute") or {}).get("max_microversion"))
    if nmax is None:
        verdict("V-01", "VERİ YOK", "Nova max microversion okunamadı.")
    elif nmax >= (2, 96):
        verdict("V-01", "TAMAM", f"Nova max {nmax[0]}.{nmax[1]} → Caracal (2024.1) kod seviyesi; '26.1.0.dev' bir sürüm dizesi artefaktı.")
    elif nmax >= (2, 93):
        verdict("V-01", "DİKKAT", f"Nova max {nmax[0]}.{nmax[1]} → Zed–Bobcat aralığı; brief'teki 'Caracal' varsayımı Nova için doğru değil. Tasarım ≤2.90 ile çalışır; yükseltme planı gözden geçirilmeli.")
    else:
        verdict("V-01", "DİKKAT", f"Nova max {nmax[0]}.{nmax[1]} < 2.93; bazı alanlar (2.89/2.90) yok olabilir.")
    L.append("")
    L.append(f"**{S['verdicts']['V-01']['status']}** — {S['verdicts']['V-01']['text']}")

    # ------------------------------------------------------------ envanter
    h("## Envanter dağılımı")
    servers = m.servers
    st = Counter(s.get("status") for s in servers)
    vmc = Counter(m.vm_class(s) for s in servers)
    vols = list(m.volumes.values())
    vk = Counter(m.kind(v) for v in vols)
    size_by_kind = defaultdict(int)
    for v in vols:
        size_by_kind[m.kind(v)] += int(v.get("size") or 0)
    multi = [s for s in servers if len(m.attachments(s)) > 1]
    max_vpv = max((len(m.attachments(s)) for s in servers), default=0)
    L.append(f"- Sunucu: **{len(servers)}** ({', '.join(f'{k}: {v}' for k, v in st.most_common())})")
    L.append(f"- VM sınıfı: {', '.join(f'{k}: {v}' for k, v in vmc.most_common())}")
    L.append(f"- Volume: **{len(vols)}** — " + ", ".join(
        f"{k}: {vk[k]} / {size_by_kind[k]:,} GB" for k in ("ceph", "netapp", "other") if vk[k]))
    L.append(f"- Volume tipleri: {', '.join(f'{k}: {v}' for k, v in Counter(v.get('volume_type') for v in vols).most_common())}")
    L.append(f"- Çok diskli VM: {len(multi)} · VM başına en fazla volume: {max_vpv}")
    roots = Counter(m.root_volume(s)[1] for s in servers)
    L.append(f"- Kök volume tespiti (§9.3): {', '.join(f'{k}: {v}' for k, v in roots.most_common())}")
    if roots.get("belirsiz"):
        L.append(f"  - **DİKKAT:** {roots['belirsiz']} VM'de kök volume `root_device_name` ile eşlenemedi; restore planında elle doğrulanmalı.")
    S["inventory"] = {"servers": len(servers), "vm_class": dict(vmc), "volumes": dict(vk),
                      "size_gb": dict(size_by_kind), "multi_volume_vms": len(multi), "max_vols_per_vm": max_vpv}

    # ------------------------------------------------------------ V-12
    h("## V-12 — Tutarlılık hazırlığı (QGA)")
    qga_api = Counter()
    for s in servers:
        rv, _ = m.root_volume(s)
        if rv is None:
            qga_api["kök_volume_yok"] += 1
        elif str(vim(rv).get("hw_qemu_guest_agent", "")).lower() in TRUE:
            qga_api["image_metadata_yes"] += 1
        else:
            qga_api["image_metadata_yok"] += 1
    L.append(f"- Kök volume `volume_image_metadata.hw_qemu_guest_agent` (API, vekil gösterge): "
             f"{', '.join(f'{k}: {v}' for k, v in qga_api.most_common())}")
    if hosts:
        joined = [hosts.get(str(s.get("id")).lower()) for s in servers]
        seen = [r for r in joined if r]
        ch = sum(1 for r in seen if r.get("qga_channel") == "yes")
        con = sum(1 for r in seen if r.get("qga_state") == "connected")
        frz = sum(1 for r in seen if r.get("qga_fsfreeze") == "enabled")
        L.append(f"- Host verisi: {len(seen)}/{len(servers)} VM eşleşti · QGA kanalı var: **{ch}** · "
                 f"ajan bağlı: **{con}**" + (f" · fsfreeze etkin: {frz}" if any(r.get('qga_fsfreeze') for r in seen) else ""))
        status = "TAMAM" if con >= 0.8 * max(1, len(servers)) else "DİKKAT"
        verdict("V-12", status, f"Ajanı bağlı VM oranı {pct(con, len(servers))}. Kalanlar en iyi ihtimalle crash-consistent (B-05, D-08).")
    else:
        verdict("V-12", "VERİ YOK", "host_checks.sh CSV'leri verilmedi; yalnız API vekil göstergesi var.")
    L.append("")
    L.append(f"**{S['verdicts']['V-12']['status']}** — {S['verdicts']['V-12']['text']}")
    S["qga"] = dict(qga_api)

    # ------------------------------------------------------------ V-15 / V-16 / B-08
    h("## V-15 / V-16 / B-08 — Özel durumlar")
    enc = [v for v in vols if v.get("encrypted")]
    mat = [v for v in vols if v.get("multiattach")]
    enc_types = [t.get("name") for t in inv.get("volume_types", []) if t.get("_osb_encryption")]
    uefi = win = tpm = eph = ud = 0
    for s in servers:
        rv, _ = m.root_volume(s)
        meta_i = vim(rv)
        fl = m.flavor(s)
        specs = fl.get("extra_specs") or {}
        if str(meta_i.get("hw_firmware_type", "")).lower() == "uefi" or (hosts.get(str(s.get("id")).lower()) or {}).get("uefi_nvram") == "yes":
            uefi += 1
        if str(meta_i.get("os_type", "")).lower() == "windows" or "windows" in str(meta_i.get("os_distro", "")).lower():
            win += 1
        if specs.get("hw:tpm_version") or meta_i.get("hw_tpm_version") or (hosts.get(str(s.get("id")).lower()) or {}).get("vtpm") == "yes":
            tpm += 1
        if int(fl.get("ephemeral") or 0) > 0 or int(fl.get("swap") or 0) > 0:
            eph += 1
        if (s.get("_osb_user_data") or {}).get("present"):
            ud += 1
    L.append(f"- Şifreli volume: **{len(enc)}** (şifreli tipler: {', '.join(enc_types) or 'yok'}) — MVP'de hariç (§7.4)")
    L.append(f"- Multiattach volume: **{len(mat)}**")
    L.append(f"- UEFI: **{uefi}** · Windows: **{win}** · vTPM: **{tpm}** (B-10)")
    L.append(f"- Ephemeral/swap diskli flavor kullanan VM: **{eph}** (bu diskler yedeklenmez)")
    L.append(f"- user_data taşıyan VM: **{ud}** (restore'da varsayılan verilmez; B-08)")
    verdict("V-15", "DİKKAT" if enc else "TAMAM", f"{len(enc)} şifreli, {len(mat)} multiattach volume.")
    verdict("V-16", "DİKKAT" if (tpm or eph) else "TAMAM",
            f"UEFI {uefi}, vTPM {tpm}, ephemeral/swap {eph}; UEFI/vTPM tatbikat kapsamına alınmalı.")
    S["special"] = {"encrypted": len(enc), "multiattach": len(mat), "uefi": uefi, "windows": win,
                    "vtpm": tpm, "ephemeral_or_swap": eph, "user_data": ud}

    # ------------------------------------------------------------ V-21 kota
    h("## V-21 — Mode A (kalıcı Cinder snapshot) olsaydı kota durumu")
    L.append(f"Varsayım: eşzamanlılık {a.concurrency} VM; `no_snapshot_gb_quota` = {a.no_snapshot_gb_quota} "
             "(control_checks.sh çıktısından teyit edin). Mode B bu kotaları hiç kullanmaz.")
    L.append("")
    L.append("| Proje | Ceph vol | snapshot limit | kullanımda | gerekli | GB limit | GB kullanımda | GB gerekli | sonuç |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---|")
    quotas = inv.get("cinder_quotas") or {}
    per_proj_vols = defaultdict(list)
    for v in vols:
        per_proj_vols[m.vproject(v)].append(v)
    per_proj_vpv = defaultdict(int)
    for s in servers:
        n = sum(1 for a2 in m.attachments(s) if m.kind(m.volumes.get(a2["volume_id"], {})) == "ceph")
        per_proj_vpv[m.sproject(s)] = max(per_proj_vpv[m.sproject(s)], n)
    fails = 0
    count_gb = str(a.no_snapshot_gb_quota).lower() not in TRUE
    for pid in sorted(per_proj_vols, key=lambda p: an.project(p)):
        cv = [v for v in per_proj_vols[pid] if m.kind(v) == "ceph"]
        if not cv:
            continue
        q = quotas.get(pid) or {}
        slim, sused = quota_entry(q, "snapshots")
        glim, gused = quota_entry(q, "gigabytes")
        need_s = sused + len(cv) + per_proj_vpv[pid] * a.concurrency
        cgb = sum(int(v.get("size") or 0) for v in cv)
        need_g = gused + (cgb if count_gb else 0)
        ok = (slim is None or slim >= need_s) and (glim is None or glim >= need_g)
        fails += 0 if ok else 1
        L.append(f"| {an.project(pid)} | {len(cv)} | {slim if slim is not None else '∞'} | {sused} | {need_s} | "
                 f"{glim if glim is not None else '∞'} | {gused} | {need_g} | {'TAMAM' if ok else '**YETERSİZ**'} |")
    verdict("V-21", "DİKKAT" if fails else "TAMAM",
            f"Mode A ile {fails} proje kotaya takılır (sayı veya GB). Mode B'yi destekleyen bir bulgu (B-01/B-02).")
    L.append("")
    L.append(f"**{S['verdicts']['V-21']['status']}** — {S['verdicts']['V-21']['text']}")
    dot = sum(1 for s in servers for a2 in m.attachments(s) if a2.get("delete_on_termination"))
    L.append(f"- `delete_on_termination=true` bağlı volume: **{dot}** — Mode A'da VM silinince bunlar artık volume olarak kalır (B-01).")

    # ------------------------------------------------------------ V-22 snapshot / backup
    h("## V-22 — Mevcut snapshot ve yedekler")
    snaps = inv.get("snapshots", [])
    backups = inv.get("backups", [])
    L.append(f"- Cinder snapshot: **{len(snaps)}** ({', '.join(f'{k}: {v}' for k, v in Counter(x.get('status') for x in snaps).most_common())})")
    L.append(f"- Cinder backup: **{len(backups)}** ({', '.join(f'{k}: {v}' for k, v in Counter(x.get('status') for x in backups).most_common())}); "
             f"incremental: {sum(1 for b in backups if b.get('is_incremental'))}; "
             f"toplam {sum(int(b.get('size') or 0) for b in backups):,} GB")
    svc = Counter((x.get("binary"), x.get("state")) for x in inv.get("cinder_services", []))
    if svc:
        L.append(f"- Cinder servisleri: {', '.join(f'{b} {s}: {n}' for (b, s), n in sorted(svc.items(), key=str))}")

    # ------------------------------------------------------------ RBD
    h("## V-05 / V-22 — RBD sağlığı ve çapraz kontrol")
    if survey:
        imgs = [i for pd in survey.get("pools", {}).values() for i in pd.get("images", [])]
        vol_imgs = {i["cinder_volume_id"]: i for i in imgs if i.get("cinder_volume_id")}
        ceph_ids = {vid for vid, v in m.volumes.items() if m.kind(v) == "ceph"}
        missing_img = sorted(ceph_ids - set(vol_imgs))
        orphan_img = sorted(set(vol_imgs) - set(m.volumes))
        invalid = [i for i in imgs if i.get("flags")]
        nofast = [i for i in imgs if "fast-diff" not in (i.get("features") or [])]
        sc = Counter(sn["class"] for i in imgs for sn in i.get("snaps", []))
        stale_bk = [sn for i in imgs for sn in i.get("snaps", [])
                    if sn["class"] == "cinder_backup" and sn.get("backup_id") not in m.backup_ids]
        rbd_snap_ids = {RE_SNAP_IMG.match(sn["name"]).group(1) for i in imgs for sn in i.get("snaps", [])
                        if sn["class"] == "cinder" and RE_SNAP_IMG.match(sn["name"])}
        cinder_ceph_snaps = {x["id"] for x in snaps if m.kind(m.volumes.get(x.get("volume_id"), {})) == "ceph"}
        parents = Counter((i.get("parent") or {}).get("pool_name") for i in imgs if i.get("parent"))
        L.append(f"- Taranan imaj: **{len(imgs)}** · `volume-<uuid>` adlı: {len(vol_imgs)} · "
                 f"Cinder'daki Ceph volume'larıyla eşleşen: {len(ceph_ids & set(vol_imgs))}")
        L.append(f"- Cinder'da olup RBD'de bulunamayan Ceph volume: **{len(missing_img)}** · RBD'de olup Cinder'da olmayan `volume-*`: **{len(orphan_img)}**")
        L.append(f"- Glance parent'lı (klon) imaj: {sum(parents.values())} ({', '.join(f'{k}: {v}' for k, v in parents.items())})")
        L.append(f"- object-map/fast-diff **geçersiz** bayraklı imaj: **{len(invalid)}** · fast-diff özelliği olmayan: **{len(nofast)}**")
        L.append(f"- RBD snapshot sınıfları: {', '.join(f'{k}: {v}' for k, v in sc.most_common()) or 'yok'}")
        L.append(f"- Sahibi Cinder backup listesinde olmayan `backup.*.snap.*` (artık): **{len(stale_bk)}** — temizlik adayı (§18, ⚠ÜD)")
        L.append(f"- Cinder snapshot ↔ RBD `snapshot-*`: Cinder'da olup RBD'de yok {len(cinder_ceph_snaps - rbd_snap_ids)}, "
                 f"RBD'de olup Cinder'da yok {len(rbd_snap_ids - m.snapshot_ids)}")
        verdict("V-05", "DİKKAT" if (invalid or nofast) else "TAMAM",
                f"{len(invalid)} geçersiz bayrak, {len(nofast)} fast-diff'siz imaj; bunlar yavaş diff yoluna düşer (ÜD-12).")
        verdict("V-22", "DİKKAT" if (stale_bk or orphan_img or missing_img) else "TAMAM",
                f"{len(stale_bk)} artık backup snapshot, {len(orphan_img)} yetim imaj, {len(missing_img)} eksik imaj.")
        S["rbd"] = {"images": len(imgs), "invalid_flags": len(invalid), "no_fast_diff": len(nofast),
                    "snap_classes": dict(sc), "stale_backup_snaps": len(stale_bk), "orphan_images": len(orphan_img),
                    "missing_images": len(missing_img)}
    else:
        verdict("V-05", "VERİ YOK", "rbd_survey.py çıktısı verilmedi.")
        verdict("V-22", "DİKKAT" if backups else "VERİ YOK", "Yalnız Cinder tarafı görüldü; RBD artıkları için rbd_survey gerekir.")
        L.append("_rbd_survey.py çıktısı verilmedi._")

    # ------------------------------------------------------------ V-10 NetApp
    h("## V-10 — NetApp")
    netapp_used = None
    if ontap:
        cl = ontap.get("cluster") or {}
        ver = (cl.get("version") or {}).get("full") or (cl.get("version") or {}).get("generation")
        sm = ontap.get("snapmirror") or []
        lic = [x.get("name") for x in (ontap.get("licenses") or []) if str(x.get("state", "")).lower() in ("compliant", "installed", "")]
        summ = ontap.get("summary") or {}
        netapp_used = summ.get("lun_used_bytes")
        pol = Counter((v.get("snapshot_policy") or {}).get("name") for v in ontap.get("volumes", []) or [])
        L.append(f"- ONTAP: {ver} · lisanslar: {', '.join(sorted(set(filter(None, lic)))) or '—'}")
        L.append(f"- LUN: {summ.get('lun_count')} · boyut {tib(summ.get('lun_size_bytes'))} · **kullanılan {tib(netapp_used)}** · Cinder adlı: {summ.get('cinder_named_luns')}")
        L.append(f"- FlexVol snapshot politikaları: {', '.join(f'{k}: {v}' for k, v in pol.items()) or '—'}")
        L.append(f"- SnapMirror ilişkisi: {len(sm)}")
        verdict("V-10", "TAMAM", f"NetApp gerçek kullanım {tib(netapp_used)}; SnapMirror ilişkisi {len(sm)} (D-06).")
    else:
        verdict("V-10", "VERİ YOK", "ontap_readonly.py çıktısı yok; kapasitede varsayım kullanıldı.")
        L.append("_ontap_readonly.py çıktısı verilmedi._")
    pools = inv.get("pools") or []
    for p in pools:
        cap = p.get("capabilities") or {}
        if cap.get("total_capacity_gb") is not None:
            L.append(f"- Cinder pool `{an.ident(p.get('name'))}`: toplam {cap.get('total_capacity_gb')} GB, boş {cap.get('free_capacity_gb')} GB, "
                     f"provisioned {cap.get('provisioned_capacity_gb')} GB")

    # ------------------------------------------------------------ V-20 kapasite
    h("## V-20 — Kapasite tahmini (§12.4 ile karşılaştırın)")
    ceph_src, ceph_src_label = None, "varsayım (brief: 13 TiB + ~1,5 TiB parent)"
    change_frac, change_label = a.daily_change, "varsayım"
    if survey:
        alloc = [i.get("allocated_with_parent") for pd in survey.get("pools", {}).values() for i in pd.get("images", [])
                 if i.get("cinder_volume_id") in m.volumes and i.get("allocated_with_parent") is not None]
        if alloc:
            ceph_src, ceph_src_label = sum(alloc), f"rbd_survey ({len(alloc)} imaj, parent dahil)"
        rates = [i["change_since_snap"]["changed_bytes_per_day"] / i["allocated_child"]
                 for pd in survey.get("pools", {}).values() for i in pd.get("images", [])
                 if i.get("change_since_snap") and i.get("allocated_child") and i["change_since_snap"]["age_days"] <= 14]
        if len(rates) >= 3:
            change_frac, change_label = statistics.median(rates), f"rbd_survey medyan ({len(rates)} imaj; snapshot yaşı ≤ 14 gün)"
    if ceph_src is None:
        ceph_src = 14.5 * TIB
    if netapp_used is None:
        netapp_src, netapp_label = a.netapp_used_tib * TIB, f"varsayım ({a.netapp_used_tib} TiB)"
    else:
        netapp_src, netapp_label = netapp_used, "ontap_readonly"
    frac, frac_label = a.reduction, "varsayım"
    if est and (est.get("ratios") or {}).get("stored_fraction_of_read") is not None:
        frac, frac_label = est["ratios"]["stored_fraction_of_read"], "estimate.py ölçümü"
    src = ceph_src + netapp_src
    first = src * frac
    daily = src * change_frac * frac
    local = first + a.local_days * daily
    ec = local * (a.ec_k + a.ec_m) / a.ec_k
    rep3 = local * 3
    raw = a.cluster_raw_tib * TIB
    used = a.cluster_used_tib * TIB
    ceiling = a.full_ratio * (a.osd_hosts - 1) / a.osd_hosts * raw
    L.append("| Kalem | Değer | Kaynak |")
    L.append("|---|---|---|")
    L.append(f"| Ceph kaynak veri | {tib(ceph_src)} | {ceph_src_label} |")
    L.append(f"| NetApp kaynak veri | {tib(netapp_src)} | {netapp_label} |")
    L.append(f"| Depolanan / okunan oranı | {frac:.2f} | {frac_label} |")
    L.append(f"| Günlük değişim | %{change_frac * 100:.2f} | {change_label} |")
    L.append(f"| İlk full sonrası (mantıksal) | {tib(first)} | — |")
    L.append(f"| Yerel tier {a.local_days} gün (mantıksal) | {tib(local)} | — |")
    L.append(f"| EC {a.ec_k}+{a.ec_m} raw | {tib(ec)} → küme %{100 * (used + ec) / raw:.0f} | tavan %{100 * ceiling / raw:.0f} |")
    L.append(f"| 3× replika raw (kıyas) | {tib(rep3)} → küme %{100 * (used + rep3) / raw:.0f} | — |")
    ok = used + ec <= ceiling
    if est or survey:
        v20 = "TAMAM" if ok else "DİKKAT"
    else:
        v20 = "VERİ YOK"
    measured = "ölçüme dayalı" if est else "kısmen varsayım (estimate.py çalıştırılmadı)"
    verdict("V-20", v20, f"EC {a.ec_k}+{a.ec_m} ile küme kullanımı %{100 * (used + ec) / raw:.0f} "
                         f"(tavan %{100 * ceiling / raw:.0f}); {measured}.")
    if est:
        r = est.get("ratios") or {}
        lat = est.get("read_latency_ms") or {}
        L.append("")
        L.append(f"- estimate.py: okunan {gib((est.get('totals') or {}).get('bytes_read'))}, hız {est.get('overall_read_mb_s')} MB/s, "
                 f"sıfır oranı {r.get('zero_fraction')}, proje içi dedup ×{r.get('dedup_project_factor')}, global dedup ×{r.get('dedup_global_factor')}, "
                 f"sıkıştırma {r.get('compress_ratio')} ({est.get('compressor')}), okuma gecikmesi p50/p95/p99 {lat.get('p50')}/{lat.get('p95')}/{lat.get('p99')} ms")
        if r.get("per_project_dedup_overhead") is not None:
            L.append(f"- Proje bazlı dedup'ın global'e göre ek alanı: %{100 * r['per_project_dedup_overhead']:.1f} (A7 kararının maliyeti)")
    S["capacity"] = {"source_bytes": src, "fraction": frac, "daily_change": change_frac, "first_full": first,
                     "local": local, "ec_raw": ec, "rep3_raw": rep3, "ceiling": ceiling}

    # ------------------------------------------------------------ restore hazırlığı
    h("## Restore hazırlığı (§9.3)")
    sports = [p for s in servers for p in m.ports_by_dev.get(s["id"], [])]
    L.append(f"- VM portu: {len(sports)} · FIP'li: {sum(1 for p in sports if p.get('id') in m.fip_by_port)} · "
             f"port_security kapalı: {sum(1 for p in sports if p.get('port_security_enabled') is False)} · "
             f"allowed_address_pairs: {sum(1 for p in sports if p.get('allowed_address_pairs'))} · "
             f"vnic_type≠normal: {sum(1 for p in sports if p.get('binding:vnic_type') not in (None, 'normal'))} · "
             f"QoS'lu: {sum(1 for p in sports if p.get('qos_policy_id'))}")
    sgs = inv.get("security_groups", [])
    remote = sum(1 for g in sgs for r in g.get("security_group_rules", []) if r.get("remote_group_id"))
    L.append(f"- Security group: {len(sgs)} · `remote_group_id` referanslı kural: {remote} (yeniden oluşturmada çözülmeli)")
    sg_used = [g for g in inv.get("server_groups", []) if g.get("members")]
    L.append(f"- Üyesi olan server group: {len(sg_used)} ({', '.join(f'{k}: {v}' for k, v in Counter(g.get('policy') or ','.join(g.get('policies', [])) for g in sg_used).items()) or '—'})")
    kps = inv.get("keypairs", [])
    L.append(f"- Keypair referansı: {len(kps)} · artık bulunamayan: **{sum(1 for k in kps if k.get('missing'))}**")
    deleted_flavor = sum(1 for s in servers if (s.get("flavor") or {}).get("original_name")
                         and not any(f.get("name") == s["flavor"]["original_name"] for f in m.flavors.values()))
    L.append(f"- Flavor'ı artık listede olmayan VM: {deleted_flavor} (embedded flavor ile yeniden kurulabilir)")

    # ------------------------------------------------------------ özet
    h("## Doğrulama özeti")
    L.append("| Madde | Durum | Not |")
    L.append("|---|---|---|")
    for vid in sorted(S["verdicts"]):
        v = S["verdicts"][vid]
        L.append(f"| {vid} | **{v['status']}** | {v['text']} |")
    L.append("")
    L.append("Bu araçlarla kapsanmayan (lab gerektiren) maddeler: V-03, V-07, V-08, V-11, V-13, V-14, V-17, V-18, V-23, "
             "V-24, V-26, V-27, V-28, V-30. V-02/V-19/V-25 için control_checks.sh çıktısını, V-06/V-09 için "
             "host_checks.sh çıktısını, V-29 için ceph_latency_sampler.py CSV'sini doğrudan inceleyin.")

    errs = inv.get("errors") or []
    if errs or (survey or {}).get("errors") or (ontap or {}).get("errors") or (est or {}).get("errors"):
        h("## Toplama hataları")
        for src_name, e_list in (("inventory", errs), ("rbd_survey", (survey or {}).get("errors")),
                                 ("ontap", (ontap or {}).get("errors")), ("estimate", (est or {}).get("errors"))):
            for e in (e_list or [])[:50]:
                L.append(f"- {src_name}: `{e.get('step') or e.get('image')}` — {str(e.get('error'))[:200] if not a.anonymize else 'ayrıntı gizlendi'}")

    return "\n".join(L) + "\n", S


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="OSB Faz 0 rapor üretici (çevrimdışı)")
    ap.add_argument("--inventory", required=True)
    ap.add_argument("--rbd-survey")
    ap.add_argument("--hosts-csv", action="append", default=[])
    ap.add_argument("--ontap")
    ap.add_argument("--estimate")
    ap.add_argument("--ceph-backends", default="cinder_rbd")
    ap.add_argument("--netapp-backends", default="netapp_fc")
    ap.add_argument("--concurrency", type=int, default=4, help="eşzamanlı yedeklenen VM (kota hesabı için)")
    ap.add_argument("--no-snapshot-gb-quota", default="false", help="cinder.conf değeri (control_checks çıktısı)")
    ap.add_argument("--cluster-raw-tib", type=float, default=140)
    ap.add_argument("--cluster-used-tib", type=float, default=43)
    ap.add_argument("--osd-hosts", type=int, default=4)
    ap.add_argument("--full-ratio", type=float, default=0.85)
    ap.add_argument("--ec-k", type=int, default=2)
    ap.add_argument("--ec-m", type=int, default=2)
    ap.add_argument("--local-days", type=int, default=7)
    ap.add_argument("--daily-change", type=float, default=0.02, help="ölçüm yoksa günlük değişim oranı")
    ap.add_argument("--reduction", type=float, default=0.6, help="ölçüm yoksa depolanan/okunan oranı")
    ap.add_argument("--netapp-used-tib", type=float, default=5, help="ONTAP verisi yoksa NetApp gerçek kullanım")
    ap.add_argument("--anonymize", action="store_true")
    ap.add_argument("--out", default="faz0-out/faz0-rapor.md")
    ap.add_argument("--json-out", default=None)
    a = ap.parse_args(argv)
    report, summary = build_report(a)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(report)
    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=1, default=str)
    print(f"[analyze] yazıldı: {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
