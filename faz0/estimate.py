#!/usr/bin/env python3
"""OSB Faz 0 — "estimate modu" (SALT OKUR): OSB'nin veri yolunu yüklemesiz çalıştırır.

Seçilen RBD imajlarını read_only açar, dolu alanları (diff_iterate, include_parent=True) 4 MiB'lık
hizalı chunk'lar halinde okur ve şunları ölçer:
  * okuma hızı ve okuma gecikmesi (p50/p95/p99) — üretim etkisi için ceph_latency_sampler.py ile birlikte
  * sıfır chunk oranı
  * proje içi dedup (OSB tasarımı, A7) ve global dedup (kıyas: proje bazlı dedup'ın maliyeti)
  * sıkıştırma oranı (zstd varsa seviye 3, yoksa zlib seviye 6; örnekleme ile)
Hiçbir yere yükleme yapmaz, snapshot almaz, yazmaz. Okuma hız sınırlıdır (--rate-mb).

Not: Snapshot olmadan canlı (head) imaj okunur; veri tutarlı bir an değildir ama oran tahmini için yeterlidir.
--snap ile mevcut bir snapshot okunabilir.

Kapsadığı doğrulamalar: V-04, V-20 (TASARIM.md §12).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import random
import sys
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor

SCRIPT_VERSION = "0.1.0"
CHUNK = 4 * 1024 * 1024
ZERO = bytes(CHUNK)


def log(msg: str) -> None:
    print(f"[estimate] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- çekirdek (test edilebilir, Ceph'siz)

class TokenBucket:
    """Bayt/saniye sınırlayıcı; rate <= 0 ise sınırsız."""

    def __init__(self, rate_bytes: float, burst_bytes: float | None = None, clock=time.monotonic,
                 sleep=time.sleep):
        self.rate = rate_bytes
        self.capacity = burst_bytes if burst_bytes is not None else max(rate_bytes, CHUNK)
        self.tokens = self.capacity
        self.clock, self.sleep = clock, sleep
        self.last = clock()
        self.lock = threading.Lock()

    def take(self, n: int) -> None:
        if self.rate <= 0:
            return
        while True:
            with self.lock:
                now = self.clock()
                self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= n or self.tokens >= self.capacity:
                    self.tokens -= n
                    return
                wait = (n - self.tokens) / self.rate
            self.sleep(min(wait, 1.0))


def extents_to_chunks(extents, size: int, chunk: int = CHUNK) -> list[int]:
    """(offset, length, exists) listesini, dolu alanları kapsayan sıralı chunk indekslerine çevirir."""
    idx = set()
    for off, length, exists in extents:
        if not exists or length <= 0:
            continue
        first = off // chunk
        last = min(off + length - 1, size - 1) // chunk
        idx.update(range(first, last + 1))
    return sorted(idx)


def make_compressor(name: str):
    if name == "zstd":
        import zstandard  # noqa: PLC0415
        c = zstandard.ZstdCompressor(level=3)
        return "zstd-3", c.compress
    return "zlib-6", lambda b: zlib.compress(b, 6)


class Accounting:
    """Tüm imajlar için ortak sayaçlar; thread-safe."""

    def __init__(self, compress_every: int, compressor_name: str = "auto", latency_reservoir: int = 100_000):
        if compressor_name == "auto":
            try:
                self.comp_label, self._compress = make_compressor("zstd")
            except ImportError:
                self.comp_label, self._compress = make_compressor("zlib")
        else:
            self.comp_label, self._compress = make_compressor(compressor_name)
        self.compress_every = max(1, compress_every)
        self.lock = threading.Lock()
        self.seen_project: dict[str, set] = {}
        self.seen_global: set = set()
        self.unique_counter = 0
        self.t = {"chunks_read": 0, "bytes_read": 0, "zero_chunks": 0, "zero_bytes": 0,
                  "nonzero_chunks": 0, "nonzero_bytes": 0, "unique_project_chunks": 0,
                  "unique_project_bytes": 0, "unique_global_chunks": 0, "unique_global_bytes": 0,
                  "compress_sample_plain": 0, "compress_sample_stored": 0}
        self.lat: list[float] = []
        self.lat_n = 0
        self.lat_cap = latency_reservoir
        self.rng = random.Random(42)

    def record_latency(self, ms: float) -> None:
        with self.lock:
            self.lat_n += 1
            if len(self.lat) < self.lat_cap:
                self.lat.append(ms)
            else:
                j = self.rng.randrange(self.lat_n)
                if j < self.lat_cap:
                    self.lat[j] = ms

    def add_chunk(self, project: str, data: bytes, per_image: dict) -> None:
        n = len(data)
        is_zero = data == ZERO[:n]
        digest = None if is_zero else hashlib.sha256(data).digest()
        do_compress = False
        with self.lock:
            self.t["chunks_read"] += 1
            self.t["bytes_read"] += n
            per_image["chunks_read"] += 1
            per_image["bytes_read"] += n
            if is_zero:
                self.t["zero_chunks"] += 1
                self.t["zero_bytes"] += n
                per_image["zero_chunks"] += 1
                return
            self.t["nonzero_chunks"] += 1
            self.t["nonzero_bytes"] += n
            per_image["nonzero_chunks"] += 1
            seen = self.seen_project.setdefault(project, set())
            if digest not in seen:
                seen.add(digest)
                self.t["unique_project_chunks"] += 1
                self.t["unique_project_bytes"] += n
                per_image["unique_in_project_chunks"] += 1
                self.unique_counter += 1
                do_compress = (self.unique_counter % self.compress_every) == 1 or self.compress_every == 1
            if digest not in self.seen_global:
                self.seen_global.add(digest)
                self.t["unique_global_chunks"] += 1
                self.t["unique_global_bytes"] += n
        if do_compress:
            stored = min(len(self._compress(data)), n)  # OSB: kazanç yoksa ham saklar
            with self.lock:
                self.t["compress_sample_plain"] += n
                self.t["compress_sample_stored"] += stored

    def summary(self) -> dict:
        t = dict(self.t)
        nz = t["nonzero_bytes"] or 1
        comp = (t["compress_sample_stored"] / t["compress_sample_plain"]) if t["compress_sample_plain"] else None
        ratios = {
            "zero_fraction": round(t["zero_bytes"] / t["bytes_read"], 4) if t["bytes_read"] else None,
            "dedup_project_factor": round(nz / (t["unique_project_bytes"] or 1), 4),
            "dedup_global_factor": round(nz / (t["unique_global_bytes"] or 1), 4),
            "compress_ratio": round(comp, 4) if comp is not None else None,
        }
        if comp is not None and t["bytes_read"]:
            ratios["stored_fraction_of_read"] = round(t["unique_project_bytes"] * comp / t["bytes_read"], 4)
            ratios["per_project_dedup_overhead"] = round(
                (t["unique_project_bytes"] - t["unique_global_bytes"]) / (t["unique_global_bytes"] or 1), 4)
        lat = sorted(self.lat)

        def pct(p):
            if not lat:
                return None
            return round(lat[min(len(lat) - 1, int(p / 100 * len(lat)))], 2)
        return {"totals": t, "ratios": ratios, "compressor": self.comp_label,
                "compress_every": self.compress_every,
                "read_latency_ms": {"samples": self.lat_n, "p50": pct(50), "p95": pct(95), "p99": pct(99),
                                    "max": round(lat[-1], 2) if lat else None}}


# ---------------------------------------------------------------- kaynaklar

class RbdSource:
    def __init__(self, ioctx, rbd_mod, name: str, snap: str | None):
        self.img = rbd_mod.Image(ioctx, name, snapshot=snap, read_only=True)
        self.size = self.img.size()

    def extents(self):
        ext = []

        def cb(offset, length, exists):
            ext.append((offset, length, bool(exists)))
            return 0
        self.img.diff_iterate(0, self.size, None, cb, include_parent=True, whole_object=True)
        return ext

    def read(self, off: int, length: int) -> bytes:
        return self.img.read(off, length)

    def close(self) -> None:
        self.img.close()


class FileSource:
    """Yerel test ve kuru çalıştırma için ham dosya kaynağı (tüm dosya 'dolu' sayılır)."""

    def __init__(self, path: str):
        self.f = open(path, "rb")  # noqa: SIM115
        self.size = os.path.getsize(path)
        self.lock = threading.Lock()

    def extents(self):
        return [(0, self.size, True)] if self.size else []

    def read(self, off: int, length: int) -> bytes:
        with self.lock:
            self.f.seek(off)
            return self.f.read(length)

    def close(self) -> None:
        self.f.close()


def process_image(src, project: str, acct: Accounting, bucket: TokenBucket, threads: int,
                  max_bytes: int, deadline: float | None) -> dict:
    per = {"size": src.size, "chunks_read": 0, "bytes_read": 0, "zero_chunks": 0, "nonzero_chunks": 0,
           "unique_in_project_chunks": 0, "truncated": False}
    t0 = time.monotonic()
    ext = src.extents()
    per["extent_bytes"] = sum(l for _, l, e in ext if e)
    chunks = extents_to_chunks(ext, src.size)
    per["chunks_planned"] = len(chunks)
    if max_bytes:
        limit = max(1, max_bytes // CHUNK)
        if len(chunks) > limit:
            chunks, per["truncated"] = chunks[:limit], True

    def work(ci: int):
        off = ci * CHUNK
        length = min(CHUNK, src.size - off)
        bucket.take(length)
        r0 = time.monotonic()
        data = src.read(off, length)
        acct.record_latency((time.monotonic() - r0) * 1000)
        acct.add_chunk(project, data, per)

    stop = False
    with ThreadPoolExecutor(max_workers=max(1, threads)) as pool:
        pending = []
        for ci in chunks:
            if deadline is not None and time.monotonic() > deadline:
                per["truncated"], stop = True, True
                break
            pending.append(pool.submit(work, ci))
            if len(pending) >= threads * 4:
                pending.pop(0).result()
        for f in pending:
            f.result()
    secs = time.monotonic() - t0
    per["seconds"] = round(secs, 2)
    per["read_mb_s"] = round(per["bytes_read"] / 1e6 / secs, 1) if secs > 0 else None
    per["stopped_by_deadline"] = stop
    return per


def load_project_map(inventory_path: str | None) -> dict[str, str]:
    """inventory.json -> {volume_id: project_id}."""
    if not inventory_path:
        return {}
    with open(inventory_path, encoding="utf-8") as f:
        inv = json.load(f)
    res = {}
    for v in inv.get("volumes", []):
        pid = v.get("os-vol-tenant-attr:tenant_id") or v.get("project_id")
        if v.get("id") and pid:
            res[v["id"]] = pid
    return res


def write_private_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, default=str)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="OSB Faz 0 estimate modu (salt okur, yüklemesiz)")
    ap.add_argument("--conf", default="/etc/ceph/ceph.conf")
    ap.add_argument("--id", default="osb-ro")
    ap.add_argument("--keyring", default=None)
    ap.add_argument("--image", action="append", default=[], help="pool/imaj (tekrarlanabilir)")
    ap.add_argument("--images-file", help="her satırda pool/imaj")
    ap.add_argument("--sample", type=int, default=0,
                    help="--from-survey içinden rastgele N volume seç (tohum: --seed)")
    ap.add_argument("--from-survey", help="rbd_survey.py çıktısı (örnekleme için)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--snap", default=None, help="tüm imajlarda bu adlı snapshot'ı oku (yoksa head)")
    ap.add_argument("--inventory", help="inventory.json (volume -> proje eşlemesi; proje içi dedup için)")
    ap.add_argument("--rate-mb", type=float, default=100.0, help="toplam okuma sınırı MB/s (0 = sınırsız)")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--max-gib-per-image", type=float, default=0, help="imaj başına okuma sınırı (0 = yok)")
    ap.add_argument("--max-minutes", type=float, default=0, help="toplam süre sınırı (0 = yok)")
    ap.add_argument("--compress-every", type=int, default=8, help="her N benzersiz chunk'tan birini sıkıştır")
    ap.add_argument("--compressor", choices=["auto", "zstd", "zlib"], default="auto")
    ap.add_argument("--file", action="append", default=[],
                    help="Ceph yerine yerel ham dosya oku (test / kuru çalıştırma)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    targets: list[tuple[str, str]] = []
    for item in args.image:
        pool, _, name = item.partition("/")
        targets.append((pool, name))
    if args.images_file:
        with open(args.images_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    pool, _, name = line.partition("/")
                    targets.append((pool, name))
    if args.sample and args.from_survey:
        with open(args.from_survey, encoding="utf-8") as f:
            sv = json.load(f)
        cands = [(p, img["name"]) for p, pd in sv.get("pools", {}).items() for img in pd.get("images", [])
                 if img.get("cinder_volume_id")]
        random.Random(args.seed).shuffle(cands)
        targets.extend(cands[: args.sample])
    if not targets and not args.file:
        ap.error("hedef yok: --image, --images-file, --sample/--from-survey veya --file verin")

    pmap = load_project_map(args.inventory)
    acct = Accounting(args.compress_every, args.compressor)
    bucket = TokenBucket(args.rate_mb * 1e6)
    deadline = time.monotonic() + args.max_minutes * 60 if args.max_minutes else None
    max_bytes = int(args.max_gib_per_image * 1024 ** 3)
    started = dt.datetime.now(dt.timezone.utc)
    images_out, errors = [], []
    log(f"sıkıştırıcı: {acct.comp_label}; hız sınırı: {args.rate_mb} MB/s; hedef: {len(targets) + len(args.file)}")

    for path in args.file:
        src = FileSource(path)
        try:
            per = process_image(src, "file", acct, bucket, args.threads, max_bytes, deadline)
            images_out.append({"pool": None, "name": path, "project": "file", **per})
        finally:
            src.close()

    if targets:
        try:
            import rados  # noqa: PLC0415
            import rbd  # noqa: PLC0415
        except ImportError:
            log("python3-rados / python3-rbd bulunamadı (README: Ceph Reef container ile çalıştırın).")
            return 2
        conf = {"keyring": args.keyring} if args.keyring else {}
        cluster = rados.Rados(conffile=args.conf, rados_id=args.id, conf=conf)
        cluster.connect(timeout=15)
        ioctxs: dict = {}
        try:
            for i, (pool, name) in enumerate(targets, 1):
                if deadline is not None and time.monotonic() > deadline:
                    log("süre sınırı doldu; kalan imajlar atlandı")
                    break
                vid = name[len("volume-"):] if name.startswith("volume-") else None
                project = pmap.get(vid or "", "unknown")
                try:
                    if pool not in ioctxs:
                        ioctxs[pool] = cluster.open_ioctx(pool)
                    src = RbdSource(ioctxs[pool], rbd, name, args.snap)
                    try:
                        per = process_image(src, project, acct, bucket, args.threads, max_bytes, deadline)
                    finally:
                        src.close()
                    images_out.append({"pool": pool, "name": name, "project": project, **per})
                    log(f"[{i}/{len(targets)}] {pool}/{name}: {per['bytes_read'] / 2**30:.2f} GiB, "
                        f"{per['read_mb_s']} MB/s")
                except Exception as e:  # noqa: BLE001
                    errors.append({"pool": pool, "image": name, "error": repr(e)})
                    log(f"[{i}/{len(targets)}] {pool}/{name}: HATA {e!r}")
        finally:
            for io in ioctxs.values():
                io.close()
            cluster.shutdown()

    finished = dt.datetime.now(dt.timezone.utc)
    summ = acct.summary()
    dur = (finished - started).total_seconds()
    out = {
        "meta": {"tool": "osb-faz0-estimate", "version": SCRIPT_VERSION, "chunk_size": CHUNK,
                 "started": started.isoformat(), "finished": finished.isoformat(),
                 "duration_s": round(dur, 1), "rate_limit_mb_s": args.rate_mb, "threads": args.threads,
                 "snap": args.snap, "client_id": args.id},
        **summ,
        "overall_read_mb_s": round(summ["totals"]["bytes_read"] / 1e6 / dur, 1) if dur > 0 else None,
        "images": images_out, "errors": errors,
    }
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = args.out or os.path.join("faz0-out", f"estimate-{ts}.json")
    write_private_json(path, out)
    log(f"yazıldı: {path}; oranlar: {json.dumps(summ['ratios'])}")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
