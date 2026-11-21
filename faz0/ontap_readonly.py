#!/usr/bin/env python3
"""OSB Faz 0 — NetApp ONTAP salt-okur envanteri (yalnız HTTP GET, ONTAP REST API).

Toplar (V-10): ONTAP sürümü, lisanslar, SVM, FlexVol'ler (boyut/kullanım/snapshot politikası, snapshot sayısı),
LUN'lar (boyut/kullanım; Cinder 'volume-<uuid>' adıyla eşlenebilir), SnapMirror ilişkileri, FC LIF'leri (WWPN maskeli).

Gereksinimler: Python >= 3.8, yalnız standart kütüphane. HTTPS (443) ile ONTAP yönetim adresine erişim.
Kimlik: SALT OKUR bir ONTAP hesabı (ör. 'readonly' rolü). Kullanıcı adı ONTAP_USER, parola ONTAP_PASSWORD
ortam değişkeninden okunur; yoksa parola sorulur. Kimlik bilgisi hiçbir dosyaya yazılmaz.

Bu betik yalnız GET yapar; POST/PATCH/DELETE kodu içermez.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import getpass
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

SCRIPT_VERSION = "0.1.0"


def log(msg: str) -> None:
    print(f"[ontap] {msg}", file=sys.stderr, flush=True)


def mask_wwpn(w: str | None, do_mask: bool) -> str | None:
    if not w or not do_mask:
        return w
    return "".join("x" if c.isalnum() else c for c in w[:-5]) + w[-5:]


class Ontap:
    def __init__(self, host: str, user: str, password: str, ctx: ssl.SSLContext, timeout: float):
        self.base = f"https://{host}"
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}", "Accept": "application/json"}
        self.ctx, self.timeout = ctx, timeout
        self.calls = 0

    def _get(self, url: str) -> dict:
        req = urllib.request.Request(url, headers=self.headers, method="GET")
        self.calls += 1
        with urllib.request.urlopen(req, context=self.ctx, timeout=self.timeout) as r:  # noqa: S310
            return json.loads(r.read().decode() or "{}")

    def get_all(self, path: str, fields: str | None, params: dict | None = None) -> list:
        """Sayfalamayı izler; 'fields' geçersizse 'fields=*' ile tekrar dener."""
        def build(fl):
            q = dict(params or {})
            if fl:
                q["fields"] = fl
            q.setdefault("max_records", "500")
            return f"{self.base}{path}?{urllib.parse.urlencode(q)}"
        try:
            body = self._get(build(fields))
        except urllib.error.HTTPError as e:
            if e.code == 400 and fields and fields != "*":
                log(f"{path}: alanlar reddedildi ({e.code}); fields=* ile tekrar")
                body = self._get(build("*"))
            else:
                raise
        records = list(body.get("records", []))
        nxt = (body.get("_links") or {}).get("next", {}).get("href")
        guard = 0
        while nxt and guard < 1000:
            guard += 1
            body = self._get(f"{self.base}{nxt}")
            records.extend(body.get("records", []))
            nxt = (body.get("_links") or {}).get("next", {}).get("href")
        return records

    def get_one(self, path: str, fields: str | None = None) -> dict:
        q = f"?fields={urllib.parse.quote(fields)}" if fields else ""
        return self._get(f"{self.base}{path}{q}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="OSB Faz 0 ONTAP salt-okur envanteri")
    ap.add_argument("--host", required=True, help="ONTAP küme yönetim adresi (ör. 198.51.100.50)")
    ap.add_argument("--svm", default=None, help="yalnız bu SVM (ör. svm-cinder)")
    ap.add_argument("--ca-file", default=None, help="ONTAP sertifikasını doğrulamak için CA dosyası")
    ap.add_argument("--insecure", action="store_true", help="TLS doğrulamasını kapat (yalnız lab / geçici)")
    ap.add_argument("--no-mask", action="store_true", help="WWPN'leri maskeleme")
    ap.add_argument("--snapshots", action="store_true", help="FlexVol başına snapshot listesini de al")
    ap.add_argument("--timeout", type=float, default=30)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    user = os.environ.get("ONTAP_USER") or input("ONTAP salt-okur kullanıcı: ")
    password = os.environ.get("ONTAP_PASSWORD") or getpass.getpass("ONTAP parola: ")
    if args.insecure:
        ctx = ssl._create_unverified_context()  # noqa: S323 — açıkça istendiğinde
        log("UYARI: TLS doğrulaması kapalı")
    else:
        ctx = ssl.create_default_context(cafile=args.ca_file)
    api = Ontap(args.host, user, password, ctx, args.timeout)
    svm_filter = {"svm.name": args.svm} if args.svm else {}

    out: dict = {"meta": {"tool": "osb-faz0-ontap", "version": SCRIPT_VERSION,
                          "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(), "svm": args.svm},
                 "errors": []}

    def step(name, fn):
        try:
            out[name] = fn()
            log(f"{name}: tamam")
        except Exception as e:  # noqa: BLE001
            out["errors"].append({"step": name, "error": repr(e)})
            log(f"{name}: HATA {e!r}")

    step("cluster", lambda: api.get_one("/api/cluster", "version,name"))
    step("licenses", lambda: api.get_all("/api/cluster/licensing/licenses", "name,state,scope"))
    step("svms", lambda: api.get_all("/api/svm/svms", "name,state,subtype"))
    step("volumes", lambda: api.get_all(
        "/api/storage/volumes",
        "name,svm.name,state,type,space.size,space.used,space.available,snapshot_policy.name,"
        "space.snapshot.reserve_percent,space.snapshot.used,efficiency.compression,efficiency.dedupe",
        svm_filter))
    step("luns", lambda: api.get_all(
        "/api/storage/luns", "name,svm.name,location.volume.name,location.logical_unit,space.size,"
        "space.used,os_type,status.state,status.mapped", svm_filter))
    step("snapmirror", lambda: api.get_all(
        "/api/snapmirror/relationships", "source.path,destination.path,state,healthy,policy.name,lag_time"))
    step("fc_interfaces", lambda: api.get_all("/api/network/fc/interfaces", "name,svm.name,state,wwpn,data_protocol",
                                              svm_filter))
    for fc in out.get("fc_interfaces", []) or []:
        fc["wwpn"] = mask_wwpn(fc.get("wwpn"), not args.no_mask)

    if args.snapshots:
        def snaps():
            res = {}
            for v in out.get("volumes", []) or []:
                uuid = v.get("uuid")
                if not uuid:
                    continue
                recs = api.get_all(f"/api/storage/volumes/{uuid}/snapshots", "name,create_time")
                times = sorted(r.get("create_time") for r in recs if r.get("create_time"))
                res[v.get("name")] = {"count": len(recs), "oldest": times[0] if times else None,
                                      "newest": times[-1] if times else None,
                                      "names_sample": [r.get("name") for r in recs[:5]]}
            return res
        step("volume_snapshots", snaps)

    luns = out.get("luns", []) or []
    out["summary"] = {
        "lun_count": len(luns),
        "lun_size_bytes": sum((l.get("space") or {}).get("size", 0) or 0 for l in luns),
        "lun_used_bytes": sum((l.get("space") or {}).get("used", 0) or 0 for l in luns),
        "cinder_named_luns": sum(1 for l in luns if "volume-" in (l.get("name") or "")),
        "api_calls": api.calls,
    }
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = args.out or os.path.join("faz0-out", f"ontap-{ts}.json")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)
    log(f"yazıldı: {path} (hata: {len(out['errors'])})")
    return 0 if not out["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
