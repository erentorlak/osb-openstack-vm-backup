#!/usr/bin/env bash
# OSB Faz 0 — kontrol düğümü / LXC konteyner kontrolleri (SALT OKUR). ctrl01 üzerinde root olarak çalıştırın
# (her ctrl için ayrı çalıştırmak drift'i de gösterir).
#
# Ne yapar (hiçbir şeyi değiştirmez; konteynerlere yalnız okuma komutlarıyla lxc-attach olur):
#   * nova/cinder/neutron/glance/keystone konteynerlerinde OSA venv paket sürümleri ve pbr.json git sha (V-01)
#   * Cinder kaynak kodu: RBD sürücüsünün volume silerken backup snapshot'larını temizleyen kodu ve
#     cinder-backup Ceph sürücüsünün snapshot ad kalıbı + dosya sha256 (V-02, V-25; kontrat testi taban çizgisi)
#   * Beyaz listedeki GİZLİ OLMAYAN config anahtarları: enforce_new_defaults/enforce_scope (V-19),
#     no_snapshot_gb_quota (V-21), backup_use_same_host, keystone_authtoken service_type (V-18), ...
#   * Konteynerlerdeki Ceph istemci paket sürümleri (V-06)
#   * policy override dosyalarının varlığı ve satır sayısı
#
# Kullanım:  sudo ./control_checks.sh [--out DIZIN] [--match REGEX]
set -u
umask 077

OUT_DIR="./faz0-out"
MATCH='(nova|cinder|neutron|glance|keystone|ceph|utility)'
while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT_DIR="$2"; shift 2 ;;
    --match) MATCH="$2"; shift 2 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "bilinmeyen argüman: $1" >&2; exit 2 ;;
  esac
done

HOST="$(hostname -s 2>/dev/null || hostname)"
TS="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT_DIR"
REPORT="$OUT_DIR/control-$HOST-$TS.txt"

# Konteyner içinde çalışacak salt-okur betik (stdin üzerinden verilir).
read -r -d '' INNER <<'EOF_INNER'
set -u
SAFE_KEYS="enforce_new_defaults,enforce_scope,policy_file,no_snapshot_gb_quota,backup_use_same_host,\
backup_driver,backup_ceph_pool,backup_ceph_user,backup_compression_algorithm,enabled_backends,\
volume_backend_name,volume_driver,rbd_pool,rbd_user,rbd_store_chunk_size,rbd_flatten_volume_from_snapshot,\
rbd_max_clone_depth,rbd_ceph_conf,quota_snapshots,quota_backups,quota_backup_gigabytes,quota_gigabytes,\
quota_volumes,osapi_max_limit,max_limit,service_type,images_type,images_rbd_pool,hw_disk_discard,\
default_store,stores,show_image_direct_url,show_multiple_locations,netapp_storage_protocol,\
netapp_storage_family,netapp_transport_type,netapp_server_port,netapp_vserver,netapp_thin_provisioned,\
token_expiration,expiration,allow_expired_window"

safe_conf() {
  [ -r "$1" ] || return 0
  awk -v keys="$SAFE_KEYS" -v fname="$1" '
    BEGIN { n = split(keys, arr, ","); for (i = 1; i <= n; i++) want[arr[i]] = 1; sec = "DEFAULT" }
    /^[ \t]*[#;]/ { next }
    /^[ \t]*\[/ { sec = $0; gsub(/^[ \t]*\[|\][ \t]*$/, "", sec); next }
    {
      if (match($0, /^[ \t]*[A-Za-z0-9_]+[ \t]*=/)) {
        k = substr($0, RSTART, RLENGTH); gsub(/[ \t=]/, "", k)
        if (k in want) { v = $0; sub(/^[^=]*=[ \t]*/, "", v); print "  " fname ": [" sec "] " k " = " v }
      }
    }' "$1"
}

echo "-- pbr / sürüm"
for f in $(find /openstack/venvs -maxdepth 6 -name pbr.json -path '*dist-info*' 2>/dev/null \
          | grep -E '/(nova|cinder|neutron|glance|keystone|os_brick|openstacksdk|python_openstackclient)-[^/]*dist-info/'); do
  echo "  $(basename "$(dirname "$f")"): $(cat "$f")"
done
for d in $(find /openstack/venvs -maxdepth 6 -type d -name '*.dist-info' 2>/dev/null \
          | grep -E '/(nova|cinder|neutron|glance|keystone|os_brick|openstacksdk)-[0-9]'); do
  echo "  dist: $(basename "$d")"
done

echo "-- config (yalnız beyaz liste)"
for f in /etc/nova/*.conf /etc/cinder/*.conf /etc/neutron/*.conf /etc/glance/*.conf /etc/keystone/*.conf; do
  [ -e "$f" ] && safe_conf "$f"
done

echo "-- policy override dosyaları"
for f in /etc/nova/policy.yaml /etc/nova/policy.json /etc/cinder/policy.yaml /etc/cinder/policy.json \
         /etc/neutron/policy.yaml /etc/neutron/policy.json /etc/glance/policy.yaml /etc/keystone/policy.yaml; do
  [ -e "$f" ] && echo "  $f: $(grep -cvE '^[[:space:]]*(#|$)' "$f") kural satırı"
done

for d in $(find /openstack/venvs -maxdepth 5 -type d -path '*site-packages/cinder' 2>/dev/null); do
  echo "-- cinder kaynak kontrolü: $d"
  rbdpy="$d/volume/drivers/rbd.py"; cephpy="$d/backup/drivers/ceph.py"
  [ -r "$rbdpy" ] && sha256sum "$rbdpy" | sed 's/^/  /'
  [ -r "$cephpy" ] && sha256sum "$cephpy" | sed 's/^/  /'
  echo "  [rbd.py] backup snapshot temizliği / VolumeIsBusy:"
  [ -r "$rbdpy" ] && grep -nE '_delete_backup_snaps|_get_backup_snaps|get_backup_snaps|VolumeIsBusy|ImageHasSnapshots|clone_snap' "$rbdpy" | head -25 | sed 's/^/    /'
  echo "  [ceph.py] snapshot ad kalıbı ve en son snapshot seçimi:"
  [ -r "$cephpy" ] && grep -nE 'search_key|backup\\\.|def get_backup_snaps|_get_most_recent_snap|_get_new_snap_name|snapshot_id' "$cephpy" | head -30 | sed 's/^/    /'
done

echo "-- ceph istemci paketleri"
dpkg-query -W -f='  ${Package} ${Version}\n' ceph-common librbd1 librados2 python3-rbd python3-rados 2>/dev/null
EOF_INNER

{
  echo "OSB Faz 0 control_checks  host=$HOST  zaman=$TS"
  echo
  echo "===== Host (konteyner dışı) ====="
  printf '%s\n' "$INNER" | bash -s 2>&1
  if command -v lxc-ls >/dev/null 2>&1; then
    for c in $(lxc-ls -1 --running 2>/dev/null | grep -E "$MATCH"); do
      echo
      echo "===== konteyner: $c ====="
      printf '%s\n' "$INNER" | lxc-attach -n "$c" -- bash -s 2>&1
    done
  else
    echo "lxc-ls bulunamadı; yalnız host kontrol edildi"
  fi
} > "$REPORT"

echo "yazıldı: $REPORT"
