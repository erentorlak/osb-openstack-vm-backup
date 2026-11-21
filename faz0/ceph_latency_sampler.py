#!/usr/bin/env python3
"""OSB Faz 0 — Ceph OSD gecikme örnekleyicisi (SALT OKUR; V-29 ve V-20'nin üretim etkisi kısmı).

ceph-mgr 'prometheus' modülünün /metrics çıktısını periyodik okur ve aralık başına şunları CSV'ye yazar:
  * ceph_osd_commit_latency_ms / ceph_osd_apply_latency_ms (OSD'ler arası ortalama ve en kötü)
  * ceph_osd_op_r_latency / ceph_osd_op_w_latency sayaçlarından aralık ortalaması (ms)
  * ceph_osd_op_r / ceph_osd_op_w sayaçlarından IOPS

Kullanım: estimate.py çalışmadan önce (taban çizgisi), çalışırken ve sonra açık tutun; karşılaştırın.
Prometheus modülü kapalıysa bu betik çalışmaz; modülü açmak bir üretim değişikliğidir (TASARIM.md ⚠ÜD).
Yalnız standart kütüphane kullanır.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import re
import sys
import time
import urllib.request

LINE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([-+0-9.eEInfNa]+)')
LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def parse_metrics(text: str, wanted: set[str]) -> dict[str, dict[str, float]]:
    """{metrik: {ceph_daemon: değer}} — yalnız istenen metrikler."""
    res: dict[str, dict[str, float]] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = LINE.match(line)
        if not m or m.group(1) not in wanted:
            continue
        labels = dict(LABEL.findall(m.group(2) or ""))
        key = labels.get("ceph_daemon") or labels.get("osd") or "_"
        try:
            res.setdefault(m.group(1), {})[key] = float(m.group(3))
        except ValueError:
            continue
    return res


GAUGES = ("ceph_osd_commit_latency_ms", "ceph_osd_apply_latency_ms")
COUNTERS = ("ceph_osd_op_r_latency_sum", "ceph_osd_op_r_latency_count",
            "ceph_osd_op_w_latency_sum", "ceph_osd_op_w_latency_count", "ceph_osd_op_r", "ceph_osd_op_w")


def interval_row(prev: dict, cur: dict, secs: float) -> dict:
    row: dict = {}
    for g in GAUGES:
        vals = list((cur.get(g) or {}).values())
        row[f"{g}_avg"] = round(sum(vals) / len(vals), 2) if vals else None
        row[f"{g}_max"] = round(max(vals), 2) if vals else None

    def delta(name):
        a, b = prev.get(name) or {}, cur.get(name) or {}
        return sum(max(0.0, b[k] - a.get(k, b[k])) for k in b)
    for op in ("r", "w"):
        s, c = delta(f"ceph_osd_op_{op}_latency_sum"), delta(f"ceph_osd_op_{op}_latency_count")
        row[f"op_{op}_latency_ms"] = round(s / c * 1000, 3) if c else None   # sum saniye cinsindendir
        row[f"op_{op}_iops"] = round(delta(f"ceph_osd_op_{op}") / secs, 1) if secs > 0 else None
    return row


def fetch(url: str, timeout: float) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 — operatörün verdiği iç adres
        return r.read().decode("utf-8", "replace")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Ceph mgr prometheus gecikme örnekleyicisi (salt okur)")
    ap.add_argument("--url", required=True, help="ör. http://198.51.100.11:9283/metrics (aktif mgr)")
    ap.add_argument("--interval", type=float, default=15)
    ap.add_argument("--minutes", type=float, default=60, help="toplam süre (0 = Ctrl-C'ye kadar)")
    ap.add_argument("--label", default="", help="CSV'ye yazılacak etiket (ör. baseline, estimate-run)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    wanted = set(GAUGES) | set(COUNTERS)
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = args.out or os.path.join("faz0-out", f"ceph-latency-{ts}.csv")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    end = time.monotonic() + args.minutes * 60 if args.minutes else None
    prev, prev_t = parse_metrics(fetch(args.url, 10), wanted), time.monotonic()
    fields = ["time", "label"] + [f"{g}_{s}" for g in GAUGES for s in ("avg", "max")] + \
             ["op_r_latency_ms", "op_w_latency_ms", "op_r_iops", "op_w_iops"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        try:
            while end is None or time.monotonic() < end:
                time.sleep(args.interval)
                try:
                    cur, now = parse_metrics(fetch(args.url, 10), wanted), time.monotonic()
                except Exception as e:  # noqa: BLE001 — mgr failover sırasında geçici hata
                    print(f"[latency] okuma hatası: {e!r}", file=sys.stderr)
                    continue
                row = interval_row(prev, cur, now - prev_t)
                row.update({"time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                            "label": args.label})
                w.writerow(row)
                f.flush()
                prev, prev_t = cur, now
        except KeyboardInterrupt:
            pass
    print(f"[latency] yazıldı: {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
