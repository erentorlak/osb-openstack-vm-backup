#!/usr/bin/env python3
"""OSB Faz 0 — RBD envanteri (SALT OKUR).

Her imaj için: özellikler (features), bayraklar (object-map/fast-diff geçersiz mi), nesne boyutu,
Glance parent'ı, snapshot'lar (Cinder / cinder-backup / osb / diğer sınıflaması, korumalı mı, yaşı),
opsiyonel olarak tahsis edilmiş bayt (fast-diff) ve mevcut snapshot'a göre değişim hızı.

Yalnız imajları read_only=True açar; snapshot oluşturmaz, silmez, yazmaz.
Önerilen anahtar: client.osb-ro (TASARIM.md §11.3). Ceph Reef (18.2.x) python3-rados/python3-rbd ile
çalıştırın; en kolayı quay.io/ceph/ceph:v18.2.x container'ıdır (README.md).

Kapsadığı doğrulamalar: V-04 (kısmi), V-05, V-20 (tahsis), V-22 (RBD tarafı).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time

SCRIPT_VERSION = "0.1.0"

RE_CINDER_SNAP = re.compile(r"^snapshot-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
RE_BACKUP_SNAP = re.compile(r"^backup\.([a-z0-9\-]+?)\.snap\.(.+)$")   # cinder/backup/drivers/ceph.py kalıbı
RE_VOLUME_IMG = re.compile(r"^volume-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")


def log(msg: str) -> None:
    print(f"[rbd_survey] {msg}", file=sys.stderr, flush=True)


def classify_snap(name: str) -> str:
    if name.startswith("backup.osb-"):
        return "osb"
    if RE_BACKUP_SNAP.match(name):
        return "cinder_backup"
    if RE_CINDER_SNAP.match(name):
        return "cinder"
    if name.endswith(".clone_snap"):
        return "clone_snap"
    return "other"


def feature_names(rbd, features: int) -> list[str]:
    names = []
    for attr, label in (
        ("RBD_FEATURE_LAYERING", "layering"), ("RBD_FEATURE_STRIPINGV2", "striping"),
        ("RBD_FEATURE_EXCLUSIVE_LOCK", "exclusive-lock"), ("RBD_FEATURE_OBJECT_MAP", "object-map"),
        ("RBD_FEATURE_FAST_DIFF", "fast-diff"), ("RBD_FEATURE_DEEP_FLATTEN", "deep-flatten"),
        ("RBD_FEATURE_JOURNALING", "journaling"), ("RBD_FEATURE_DATA_POOL", "data-pool"),
        ("RBD_FEATURE_OPERATIONS", "operations"), ("RBD_FEATURE_MIGRATING", "migrating"),
    ):
        bit = getattr(rbd, attr, None)
        if bit is not None and features & bit:
            names.append(label)
    return names


def flag_names(rbd, flags: int) -> list[str]:
    names = []
    for attr, label in (("RBD_FLAG_OBJECT_MAP_INVALID", "object-map-invalid"),
                        ("RBD_FLAG_FAST_DIFF_INVALID", "fast-diff-invalid")):
        bit = getattr(rbd, attr, None)
        if bit is not None and flags & bit:
            names.append(label)
    return names


def sum_extents(img, size: int, from_snap: str | None, include_parent: bool) -> int:
    total = [0]

    def cb(offset, length, exists):
        if exists:
            total[0] += length
        return 0
    img.diff_iterate(0, size, from_snap, cb, include_parent=include_parent, whole_object=True)
    return total[0]


def parent_spec(img, rbd):
    try:
        spec = img.get_parent_image_spec()
        return {k: spec.get(k) for k in ("pool_name", "pool_namespace", "image_name", "image_id",
                                         "snap_name", "snap_id")}
    except rbd.ImageNotFound:
        return None
    except AttributeError:  # çok eski bağlama; Reef'te olmamalı
        try:
            pool, image, snap = img.parent_info()
            return {"pool_name": pool, "image_name": image, "snap_name": snap}
        except rbd.ImageNotFound:
            return None


def survey_image(ioctx, rbd, name: str, args, now: dt.datetime) -> dict:
    rec: dict = {"name": name}
    m = RE_VOLUME_IMG.match(name)
    rec["cinder_volume_id"] = m.group(1) if m else None
    with rbd.Image(ioctx, name, read_only=True) as img:
        size = img.size()
        st = img.stat()
        feats = img.features()
        flags = img.flags()
        rec.update({
            "id": img.id(), "size": size, "order": st.get("order"),
            "object_size": 1 << st["order"] if st.get("order") is not None else None,
            "features": feature_names(rbd, feats), "flags": flag_names(rbd, flags),
        })
        rec["parent"] = parent_spec(img, rbd)
        if rec["parent"] is not None:
            try:
                rec["parent_overlap"] = img.overlap()
            except Exception:  # noqa: BLE001
                rec["parent_overlap"] = None

        snaps = []
        for s in img.list_snaps():
            sname = s.get("name")
            item = {"name": sname, "id": s.get("id"), "size": s.get("size"), "class": classify_snap(sname),
                    "namespace": s.get("namespace")}
            try:
                item["protected"] = bool(img.is_protected_snap(sname))
            except Exception:  # noqa: BLE001 — namespace dışı snapshot'larda hata verebilir
                item["protected"] = None
            try:
                ts = img.get_snap_timestamp(s["id"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=dt.timezone.utc)
                item["created_at"] = ts.isoformat()
                item["age_days"] = round((now - ts).total_seconds() / 86400, 3)
            except Exception:  # noqa: BLE001
                item["created_at"] = None
            m2 = RE_BACKUP_SNAP.match(sname or "")
            if m2:
                item["backup_id"] = m2.group(1)
            snaps.append(item)
        rec["snaps"] = snaps

        fast = ("fast-diff" in rec["features"]) and not rec["flags"]
        rec["fast_diff_usable"] = fast
        if args.allocation and (fast or args.allow_slow):
            t0 = time.monotonic()
            rec["allocated_child"] = sum_extents(img, size, None, include_parent=False)
            if rec["parent"] is not None:
                rec["allocated_with_parent"] = sum_extents(img, size, None, include_parent=True)
            else:
                rec["allocated_with_parent"] = rec["allocated_child"]
            rec["allocation_seconds"] = round(time.monotonic() - t0, 3)

        if args.change_rate and (fast or args.allow_slow):
            # En yeni, yaşı bilinen ve kullanıcı namespace'indeki snapshot'a göre değişen bayt.
            cands = [s for s in snaps if s.get("age_days") is not None
                     and s.get("namespace") in (None, 0, "user")]
            cands.sort(key=lambda s: s["age_days"])
            cands = [s for s in cands if s["age_days"] >= args.min_snap_age_days]
            if cands:
                ref = cands[0]
                changed = sum_extents(img, size, ref["name"], include_parent=False)
                rec["change_since_snap"] = {
                    "snap": ref["name"], "class": ref["class"], "age_days": ref["age_days"],
                    "changed_bytes": changed,
                    "changed_bytes_per_day": round(changed / max(ref["age_days"], 1e-6)),
                }
    return rec


def write_private_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, default=str)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="OSB Faz 0 RBD envanteri (salt okur)")
    ap.add_argument("--conf", default="/etc/ceph/ceph.conf")
    ap.add_argument("--id", default="osb-ro", help="cephx kullanıcı adı, 'client.' öneki olmadan")
    ap.add_argument("--keyring", default=None, help="keyring yolu (varsayılan: ceph.conf/ortam)")
    ap.add_argument("--pool", action="append", default=None,
                    help="taranacak pool (tekrarlanabilir; varsayılan: cinder-volumes)")
    ap.add_argument("--prefix", default="", help="yalnız bu önekle başlayan imajlar (ör. volume-)")
    ap.add_argument("--limit", type=int, default=0, help="pool başına en fazla imaj (0 = hepsi)")
    ap.add_argument("--allocation", action="store_true", help="tahsis edilmiş baytı ölç (fast-diff ile hızlı)")
    ap.add_argument("--change-rate", action="store_true",
                    help="mevcut en yeni snapshot'a göre değişen baytı ölç (veri okumaz, object-map kullanır)")
    ap.add_argument("--min-snap-age-days", type=float, default=0.5,
                    help="değişim hızı için kullanılacak snapshot'ın asgari yaşı (gün)")
    ap.add_argument("--allow-slow", action="store_true",
                    help="fast-diff kullanılamayan imajlarda da diff hesapla (YAVAŞ, her nesneyi sorgular)")
    ap.add_argument("--sleep", type=float, default=0.05, help="imajlar arası bekleme (s)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    try:
        import rados  # noqa: PLC0415
        import rbd  # noqa: PLC0415
    except ImportError:
        log("python3-rados / python3-rbd bulunamadı (README: Ceph Reef container ile çalıştırın).")
        return 2

    pools = args.pool or ["cinder-volumes"]
    conf = {"keyring": args.keyring} if args.keyring else {}
    cluster = rados.Rados(conffile=args.conf, rados_id=args.id, conf=conf)
    cluster.connect(timeout=15)
    now = dt.datetime.now(dt.timezone.utc)
    out: dict = {
        "meta": {"tool": "osb-faz0-rbd-survey", "version": SCRIPT_VERSION, "collected_at": now.isoformat(),
                 "client_id": args.id, "allocation": args.allocation, "change_rate": args.change_rate},
        "pools": {}, "errors": [],
    }
    try:
        v = cluster.version()
        out["meta"]["librados_version"] = f"{v.major}.{v.minor}.{v.extra}"
    except Exception:  # noqa: BLE001
        pass
    try:
        ret, buf, _ = cluster.mon_command(json.dumps({"prefix": "versions", "format": "json"}), b"")
        out["meta"]["cluster_versions"] = json.loads(buf) if ret == 0 and buf else {"ret": ret}
    except Exception as e:  # noqa: BLE001 — mon yetkisi yetmeyebilir; kritik değil
        out["meta"]["cluster_versions"] = {"error": repr(e)}

    try:
        for pool in pools:
            log(f"pool {pool} açılıyor")
            ioctx = cluster.open_ioctx(pool)
            try:
                names = sorted(n for n in rbd.RBD().list(ioctx) if n.startswith(args.prefix))
                if args.limit:
                    names = names[: args.limit]
                images = []
                for i, name in enumerate(names, 1):
                    try:
                        images.append(survey_image(ioctx, rbd, name, args, now))
                    except rbd.ImageNotFound:
                        out["errors"].append({"pool": pool, "image": name, "error": "ImageNotFound (silinmiş olabilir)"})
                    except Exception as e:  # noqa: BLE001
                        out["errors"].append({"pool": pool, "image": name, "error": repr(e)})
                    if i % 25 == 0:
                        log(f"  {pool}: {i}/{len(names)}")
                    if args.sleep:
                        time.sleep(args.sleep)
                out["pools"][pool] = {"image_count": len(names), "images": images}
            finally:
                ioctx.close()
    finally:
        cluster.shutdown()

    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = args.out or os.path.join("faz0-out", f"rbd-survey-{ts}.json")
    write_private_json(path, out)
    log(f"yazıldı: {path} (hata: {len(out['errors'])})")
    return 0 if not out["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
