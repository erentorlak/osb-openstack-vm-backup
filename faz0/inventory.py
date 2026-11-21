#!/usr/bin/env python3
"""OSB Faz 0 — OpenStack envanter toplayıcı (SALT OKUR).

Yalnız HTTP GET yapar; hiçbir kaynak oluşturmaz, değiştirmez, silmez.
Çıktı: tek bir JSON dosyası (izinler 0600). analyze.py bu dosyayı okur.

Gereksinimler:
  * openstacksdk (utility container'daki 3.0.0 yeterli), Python >= 3.8
  * admin yetkili kimlik: clouds.yaml (--cloud) veya OS_* ortam değişkenleri (openrc)

Kapsadığı doğrulamalar (TASARIM.md §22): V-01, V-12 (API tarafı), V-15, V-16, V-21, V-22 (Cinder tarafı)
ve §9.3'teki "hızlı kazanç" metadata dökümü.

Gizlilik: user_data varsayılan olarak SAKLANMAZ (yalnız var/yok, boyut ve sha256 tutulur);
--include-user-data ile açıkça istenirse saklanır. Çıktıda iç IP'ler ve proje adları bulunur;
dosyayı paylaşmayın, paylaşılacak rapor için analyze.py --anonymize kullanın.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import sys
import time
from urllib.parse import quote, urlparse, urlunparse

SCRIPT_VERSION = "0.1.0"

# Tasarımın ihtiyaç duyduğu en yüksek mikro sürümler; ortamın max'ı daha düşükse ona inilir.
NOVA_SERVERS_MV = (2, 90)      # hostname (2.90), server_groups (2.71), user_data/root_device_name (2.3, admin)
NOVA_ATTACH_MV = (2, 89)       # attachment_id/bdm_uuid (2.89), delete_on_termination (2.79), tag (2.70)
NOVA_FLAVOR_MV = (2, 61)       # flavor detail içinde extra_specs
NOVA_SG_MV = (2, 64)           # server group policy/rules
NOVA_KEYPAIR_MV = (2, 10)      # başka kullanıcının keypair'i (user_id)
CINDER_MV = (3, 64)            # volume_type_id (3.63), encryption_key_id (3.64)


class ApiError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(f"[inventory] {msg}", file=sys.stderr, flush=True)


def parse_ver(v) -> tuple[int, int] | None:
    """'2.96', (2, 96), [2, 96] veya None -> (2, 96) | None."""
    if v is None:
        return None
    if isinstance(v, (tuple, list)):
        if len(v) >= 2 and v[0] is not None and v[1] is not None:
            try:
                return int(v[0]), int(v[1])
            except (TypeError, ValueError):
                return None
        return None
    s = str(v).strip()
    if "." in s:
        a, b = s.split(".", 1)
        try:
            return int(a), int(b)
        except ValueError:
            return None
    return None


def fmt_ver(t: tuple[int, int] | None) -> str | None:
    return None if t is None else f"{t[0]}.{t[1]}"


def pick_mv(want: tuple[int, int], max_v: tuple[int, int] | None) -> str:
    if max_v is None:
        return fmt_ver(want)
    return fmt_ver(min(want, max_v))


class Api:
    """openstacksdk proxy'leri üzerinden ham GET; mikro sürüm ve sayfalama yönetimi."""

    def __init__(self, sleep: float = 0.0):
        self.calls = 0
        self.sleep = sleep

    def get(self, proxy, path: str, mv: str | None = None, mv_type: str | None = None,
            ok404: bool = False, ok_status=()):
        kwargs = {"raise_exc": False}
        if mv:
            kwargs["microversion"] = mv
        if mv_type:
            kwargs["microversion_service_type"] = mv_type
        self.calls += 1
        resp = proxy.get(path, **kwargs)
        if self.sleep:
            time.sleep(self.sleep)
        code = resp.status_code
        if code == 404 and ok404:
            return None
        if code >= 400 and code not in ok_status:
            raise ApiError(f"GET {path} -> HTTP {code}: {(resp.text or '')[:300]}")
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as e:
            raise ApiError(f"GET {path} -> JSON değil: {e}") from e

    def paged(self, proxy, path: str, key: str, mv: str | None = None, mv_type: str | None = None):
        items, url, seen = [], path, set()
        while url:
            if url in seen:
                break
            seen.add(url)
            body = self.get(proxy, url, mv, mv_type) or {}
            items.extend(body.get(key, []) or [])
            nxt = None
            for link in body.get(f"{key}_links", []) or []:
                if link.get("rel") == "next":
                    nxt = link.get("href")
            url = nxt
        return items


def discover(api: Api, proxy, version_id: str) -> dict:
    """Uç noktanın max/min mikro sürümünü bulur (V-01). Önce keystoneauth keşfi, sonra kök belge."""
    info: dict = {}
    try:
        ep = proxy.get_endpoint_data()
        if ep is not None:
            info.update({
                "service_url": getattr(ep, "service_url", None) or getattr(ep, "url", None),
                "api_version": fmt_ver(parse_ver(getattr(ep, "api_version", None))),
                "min_microversion": fmt_ver(parse_ver(getattr(ep, "min_microversion", None))),
                "max_microversion": fmt_ver(parse_ver(getattr(ep, "max_microversion", None))),
            })
    except Exception as e:  # noqa: BLE001 — keşif hatası envanteri durdurmamalı
        info["discovery_error"] = repr(e)

    if not info.get("max_microversion"):
        try:
            base = info.get("service_url") or proxy.get_endpoint()
            p = urlparse(base)
            root = urlunparse((p.scheme, p.netloc, "/", "", "", ""))
            body = api.get(proxy, root, ok_status=(300,)) or {}
            versions = body.get("versions") or ([body["version"]] if "version" in body else [])
            for v in versions:
                if v.get("id") == version_id:
                    info["max_microversion"] = v.get("version") or None
                    info["min_microversion"] = v.get("min_version") or None
                    info["root_doc_status"] = v.get("status")
                    break
            info["discovery_fallback"] = "root-document"
        except Exception as e:  # noqa: BLE001
            info["root_discovery_error"] = repr(e)
    return info


def scrub_user_data(server: dict, keep: bool) -> None:
    raw = server.get("OS-EXT-SRV-ATTR:user_data")
    meta = {"present": bool(raw)}
    if raw:
        try:
            decoded = base64.b64decode(raw)
        except (ValueError, TypeError):
            decoded = raw.encode() if isinstance(raw, str) else b""
        meta["bytes"] = len(decoded)
        meta["sha256"] = hashlib.sha256(decoded).hexdigest()
        meta["starts_with_cloud_config"] = decoded.lstrip().startswith(b"#cloud-config")
    server["_osb_user_data"] = meta
    if not keep:
        server.pop("OS-EXT-SRV-ATTR:user_data", None)


def collect(conn, args) -> dict:
    api = Api(sleep=args.sleep)
    errors: list[dict] = []
    out: dict = {
        "meta": {
            "tool": "osb-faz0-inventory",
            "version": SCRIPT_VERSION,
            "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "include_user_data": bool(args.include_user_data),
        },
        "errors": errors,
    }

    def step(name, fn):
        t0 = time.monotonic()
        try:
            res = fn()
            log(f"{name}: tamam ({time.monotonic() - t0:.1f} s)")
            return res
        except Exception as e:  # noqa: BLE001 — kısmi envanter, hiç yoktan iyidir
            log(f"{name}: HATA {e!r}")
            errors.append({"step": name, "error": repr(e)})
            return None

    # --- V-01: sürüm keşfi
    endpoints = {
        "compute": step("discover compute", lambda: discover(api, conn.compute, "v2.1")),
        "block_storage": step("discover block-storage", lambda: discover(api, conn.block_storage, "v3.0")),
    }
    out["endpoints"] = endpoints
    nova_max = parse_ver((endpoints.get("compute") or {}).get("max_microversion"))
    cinder_max = parse_ver((endpoints.get("block_storage") or {}).get("max_microversion"))
    mv = {
        "servers": pick_mv(NOVA_SERVERS_MV, nova_max),
        "attachments": pick_mv(NOVA_ATTACH_MV, nova_max),
        "flavors": pick_mv(NOVA_FLAVOR_MV, nova_max),
        "server_groups": pick_mv(NOVA_SG_MV, nova_max),
        "keypairs": pick_mv(NOVA_KEYPAIR_MV, nova_max),
        "cinder": pick_mv(CINDER_MV, cinder_max),
    }
    out["meta"]["microversions_used"] = mv

    # --- Keystone
    def projects():
        res = []
        for p in conn.identity.projects():
            res.append({
                "id": p.id, "name": getattr(p, "name", None), "domain_id": getattr(p, "domain_id", None),
                "enabled": getattr(p, "is_enabled", None), "parent_id": getattr(p, "parent_id", None),
            })
        return res
    out["projects"] = step("keystone projects", projects) or []

    # --- Nova
    nova = conn.compute
    servers = step("nova servers", lambda: api.paged(
        nova, "/servers/detail?all_tenants=1", "servers", mv["servers"])) or []
    for s in servers:
        scrub_user_data(s, args.include_user_data)
    out["servers"] = servers

    attachments: dict[str, list] = {}

    def attach_all():
        for i, s in enumerate(servers, 1):
            sid = s["id"]
            try:
                body = api.get(nova, f"/servers/{sid}/os-volume_attachments", mv["attachments"], ok404=True)
                attachments[sid] = (body or {}).get("volumeAttachments", [])
            except ApiError as e:
                errors.append({"step": "nova attachments", "server": sid, "error": str(e)})
            if i % 50 == 0:
                log(f"  attachments {i}/{len(servers)}")
        return attachments
    step("nova attachments", attach_all)
    out["attachments"] = attachments

    out["flavors"] = step("nova flavors", lambda: api.paged(
        nova, "/flavors/detail?is_public=None", "flavors", mv["flavors"])) or []
    out["server_groups"] = step("nova server groups", lambda: (api.get(
        nova, "/os-server-groups?all_projects=True", mv["server_groups"]) or {}).get("server_groups", [])) or []

    def keypairs():
        res, seen = [], set()
        for s in servers:
            name, uid = s.get("key_name"), s.get("user_id")
            if not name or not uid or (uid, name) in seen:
                continue
            seen.add((uid, name))
            body = api.get(nova, f"/os-keypairs/{quote(name, safe='')}?user_id={quote(uid, safe='')}",
                           mv["keypairs"], ok404=True)
            if body is None:
                res.append({"user_id": uid, "name": name, "missing": True})
            else:
                kp = body.get("keypair", {})
                res.append({"user_id": uid, "name": name, "missing": False, "type": kp.get("type"),
                            "fingerprint": kp.get("fingerprint"), "public_key": kp.get("public_key")})
        return res
    out["keypairs"] = step("nova keypairs", keypairs) or []

    # --- Cinder
    cinder = conn.block_storage
    cmv = mv["cinder"]
    out["volumes"] = step("cinder volumes", lambda: api.paged(
        cinder, "/volumes/detail?all_tenants=1", "volumes", cmv, "volume")) or []
    out["snapshots"] = step("cinder snapshots", lambda: api.paged(
        cinder, "/snapshots/detail?all_tenants=1", "snapshots", cmv, "volume")) or []
    out["backups"] = step("cinder backups", lambda: api.paged(
        cinder, "/backups/detail?all_tenants=1", "backups", cmv, "volume")) or []
    out["cinder_services"] = step("cinder services", lambda: (api.get(
        cinder, "/os-services", cmv, "volume") or {}).get("services", [])) or []
    out["pools"] = step("cinder pools", lambda: (api.get(
        cinder, "/scheduler-stats/get_pools?detail=True", cmv, "volume") or {}).get("pools", [])) or []

    def volume_types():
        types = (api.get(cinder, "/types?is_public=None", cmv, "volume") or {}).get("volume_types", [])
        for t in types:
            try:
                enc = api.get(cinder, f"/types/{t['id']}/encryption", cmv, "volume", ok404=True)
                t["_osb_encryption"] = enc or {}
            except ApiError as e:
                t["_osb_encryption_error"] = str(e)
        return types
    out["volume_types"] = step("cinder volume types", volume_types) or []
    out["qos_specs"] = step("cinder qos specs", lambda: (api.get(
        cinder, "/qos-specs", cmv, "volume") or {}).get("qos_specs", [])) or []

    def quotas():
        res = {}
        for p in out["projects"]:
            try:
                body = api.get(cinder, f"/os-quota-sets/{p['id']}?usage=True", cmv, "volume")
                res[p["id"]] = (body or {}).get("quota_set", {})
            except ApiError as e:
                errors.append({"step": "cinder quotas", "project": p["id"], "error": str(e)})
        return res
    out["cinder_quotas"] = step("cinder quotas", quotas) or {}

    # --- Neutron (mikro sürüm yok)
    net = conn.network
    out["ports"] = step("neutron ports", lambda: api.paged(net, "/ports", "ports")) or []
    out["floatingips"] = step("neutron floating ips", lambda: api.paged(net, "/floatingips", "floatingips")) or []
    out["security_groups"] = step("neutron security groups", lambda: api.paged(
        net, "/security-groups", "security_groups")) or []
    out["networks"] = step("neutron networks", lambda: api.paged(net, "/networks", "networks")) or []
    out["subnets"] = step("neutron subnets", lambda: api.paged(net, "/subnets", "subnets")) or []
    out["qos_policies"] = step("neutron qos policies", lambda: (api.get(
        net, "/qos/policies", ok404=True) or {}).get("policies", [])) or []

    out["meta"]["api_calls"] = api.calls
    out["meta"]["counts"] = {k: len(v) for k, v in out.items() if isinstance(v, (list, dict)) and k not in ("meta",)}
    return out


def write_private_json(path: str, data) -> None:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=False, default=str)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="OSB Faz 0 envanter toplayıcı (salt okur)")
    ap.add_argument("--cloud", help="clouds.yaml içindeki bulut adı (yoksa OS_* ortam değişkenleri)")
    ap.add_argument("--out", default=None, help="çıktı JSON yolu (varsayılan: faz0-out/inventory-<zaman>.json)")
    ap.add_argument("--include-user-data", action="store_true",
                    help="user_data içeriğini de sakla (gizli bilgi içerebilir; varsayılan: saklama)")
    ap.add_argument("--sleep", type=float, default=0.02, help="her API çağrısından sonra bekleme (s)")
    args = ap.parse_args(argv)

    try:
        import openstack  # noqa: PLC0415 — yalnız çalışırken gerekli
    except ImportError:
        log("openstacksdk bulunamadı; utility container'da çalıştırın (lxc-attach ... utility).")
        return 2

    conn = openstack.connect(cloud=args.cloud) if args.cloud else openstack.connect()
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = args.out or os.path.join("faz0-out", f"inventory-{ts}.json")
    data = collect(conn, args)
    write_private_json(out_path, data)
    log(f"yazıldı: {out_path}  (API çağrısı: {data['meta']['api_calls']}, hata: {len(data['errors'])})")
    return 0 if not data["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
