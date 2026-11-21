#!/usr/bin/env bash
# OSB Faz 0 — compute host kontrolleri (SALT OKUR). cmp01-03 üzerinde root olarak çalıştırın.
#
# Ne yapar (hiçbir şeyi değiştirmez):
#   * FC HBA modeli/durumu/hızı (V-09)            -> /sys/class/fc_host, /sys/class/scsi_host
#   * multipath özeti (NETAPP yol durumları)      -> multipath -ll (yalnız okuma)
#   * libvirt domain'leri: QGA kanalı ve durumu, UEFI (nvram), vTPM, disk türleri/bus (V-12, V-16)
#                                                  -> virsh list / dumpxml (yalnız okuma)
#   * --ping verilirse: çalışan ve kanalı bağlı domain'lere QGA "guest-info" (yalnız okuma komutu)
#   * Ceph istemci paketleri, libvirt/qemu sürümü, OSA venv sürümleri (V-06, V-01)
#   * nova.conf / cinder.conf içinden yalnız beyaz listedeki GİZLİ OLMAYAN anahtarlar
#
# Gizlilik: WWPN ve multipath WWID'leri varsayılan olarak maskelenir (--no-mask ile açılır).
# Parola/transport_url/connection gibi anahtarlar hiçbir zaman yazdırılmaz (beyaz liste).
#
# Kullanım:  sudo ./host_checks.sh [--out DIZIN] [--ping] [--no-mask]
set -u
umask 077

OUT_DIR="./faz0-out"
DO_PING=0
MASK=1
while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT_DIR="$2"; shift 2 ;;
    --ping) DO_PING=1; shift ;;
    --no-mask) MASK=0; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "bilinmeyen argüman: $1" >&2; exit 2 ;;
  esac
done

HOST="$(hostname -s 2>/dev/null || hostname)"
TS="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT_DIR"
REPORT="$OUT_DIR/host-$HOST-$TS.txt"
CSV="$OUT_DIR/domains-$HOST-$TS.csv"

SAFE_KEYS="enforce_new_defaults,enforce_scope,images_type,images_rbd_pool,hw_disk_discard,rbd_user,\
enabled_backends,volume_backend_name,volume_driver,backup_driver,backup_use_same_host,backup_ceph_pool,\
backup_ceph_user,backup_compression_algorithm,rbd_pool,rbd_store_chunk_size,no_snapshot_gb_quota,\
netapp_storage_protocol,netapp_storage_family,netapp_transport_type,netapp_server_port,netapp_vserver,\
netapp_thin_provisioned,netapp_lun_space_reservation,use_multipath_for_image_xfer,enforce_multipath_for_image_xfer,\
volume_use_multipath,service_type,cpu_mode,virt_type"

mask() { # son 4 karakter hariç maskele
  local s="$1"
  if [ "$MASK" -eq 1 ] && [ "${#s}" -gt 4 ]; then
    printf '%s%s' "$(printf '%s' "${s%????}" | tr '0-9a-fA-F' 'x')" "${s: -4}"
  else
    printf '%s' "$s"
  fi
}

safe_conf() { # $1 = dosya; yalnız beyaz listedeki anahtarları "[bölüm] anahtar = değer" olarak yazar
  [ -r "$1" ] || return 0
  awk -v keys="$SAFE_KEYS" -v fname="$1" '
    BEGIN { n = split(keys, arr, ","); for (i = 1; i <= n; i++) want[arr[i]] = 1; sec = "DEFAULT" }
    /^[ \t]*[#;]/ { next }
    /^[ \t]*\[/ { sec = $0; gsub(/^[ \t]*\[|\][ \t]*$/, "", sec); next }
    {
      if (match($0, /^[ \t]*[A-Za-z0-9_]+[ \t]*=/)) {
        k = substr($0, RSTART, RLENGTH); gsub(/[ \t=]/, "", k)
        if (k in want) { v = $0; sub(/^[^=]*=[ \t]*/, "", v); print fname ": [" sec "] " k " = " v }
      }
    }' "$1"
}

section() { printf '\n===== %s =====\n' "$1"; }

{
  echo "OSB Faz 0 host_checks  host=$HOST  zaman=$TS  mask=$MASK"
  section "Sistem"
  uname -r
  [ -r /etc/os-release ] && grep -E '^(PRETTY_NAME|VERSION_ID)=' /etc/os-release

  section "FC HBA (V-09)"
  if [ -d /sys/class/fc_host ] && [ -n "$(ls -A /sys/class/fc_host 2>/dev/null)" ]; then
    for h in /sys/class/fc_host/host*; do
      n="$(basename "$h")"
      wwpn="$(cat "$h/port_name" 2>/dev/null)"
      state="$(cat "$h/port_state" 2>/dev/null)"
      speed="$(cat "$h/speed" 2>/dev/null)"
      model="$(cat "/sys/class/scsi_host/$n/modelname" 2>/dev/null || cat "/sys/class/scsi_host/$n/model_name" 2>/dev/null)"
      sym="$(cat "$h/symbolic_name" 2>/dev/null)"
      echo "$n wwpn=$(mask "$wwpn") state=$state speed=$speed model=${model:-?} symbolic=${sym:-?}"
    done
  else
    echo "FC HBA bulunamadı"
  fi

  section "Multipath özeti"
  if command -v multipath >/dev/null 2>&1; then
    mp="$(multipath -ll 2>/dev/null)"
    echo "NETAPP map sayısı: $(printf '%s\n' "$mp" | grep -c 'NETAPP')"
    echo "aktif yol (active ready running): $(printf '%s\n' "$mp" | grep -c 'active ready running')"
    echo "sorunlu yol (failed|faulty|offline): $(printf '%s\n' "$mp" | grep -Ec 'failed|faulty|offline')"
    printf '%s\n' "$mp" | grep -E 'NETAPP' | head -5 | while read -r line; do
      wwid="$(printf '%s' "$line" | awk '{print $1}')"
      echo "  örnek: $(mask "$wwid") ${line#* }"
    done
  else
    echo "multipath komutu yok"
  fi

  section "Paket ve sürümler (V-06, V-01)"
  dpkg-query -W -f='${Package} ${Version}\n' ceph-common librbd1 librados2 python3-rbd python3-rados \
    libvirt-daemon-system qemu-system-x86 multipath-tools 2>/dev/null
  command -v virsh >/dev/null 2>&1 && virsh version 2>/dev/null | sed 's/^/  /'
  for f in $(find /openstack/venvs -maxdepth 6 -name pbr.json -path '*dist-info*' 2>/dev/null \
            | grep -E '/(nova|cinder|os_brick)-[^/]*dist-info/'); do
    echo "$f: $(cat "$f")"
  done

  section "Güvenli config anahtarları"
  for f in /etc/nova/nova.conf /etc/nova/nova-compute.conf /etc/cinder/cinder.conf; do safe_conf "$f"; done
} > "$REPORT" 2>&1

# ---------------------------------------------------------------- libvirt domain tablosu
echo "uuid,libvirt_name,state,qga_channel,qga_state,uefi_nvram,vtpm,rbd_disks,block_disks,other_disks,disk_buses,qga_version,qga_fsfreeze" > "$CSV"
if command -v virsh >/dev/null 2>&1; then
  virsh list --all --name 2>/dev/null | sed '/^$/d' | while read -r dom; do
    uuid="$(virsh domuuid "$dom" 2>/dev/null | head -1)"
    state="$(virsh domstate "$dom" 2>/dev/null | head -1 | tr ' ' '_')"
    xml="$(virsh dumpxml "$dom" 2>/dev/null)"
    parsed="$(printf '%s' "$xml" | python3 -c '
import sys, xml.etree.ElementTree as ET
try:
    root = ET.fromstring(sys.stdin.read())
except ET.ParseError:
    print("?,?,?,?,0,0,0,?"); sys.exit(0)
qga, qstate = "no", ""
for ch in root.iter("channel"):
    t = ch.find("target")
    if t is not None and t.get("name") == "org.qemu.guest_agent.0":
        qga, qstate = "yes", t.get("state", "")
uefi = "yes" if root.find("./os/nvram") is not None or (root.find("./os/loader") is not None and "OVMF" in (root.find("./os/loader").text or "")) else "no"
tpm = "yes" if root.find("./devices/tpm") is not None else "no"
rbd = blk = other = 0
buses = set()
for d in root.iter("disk"):
    if d.get("device") not in (None, "disk"):
        continue
    src = d.find("source"); tgt = d.find("target")
    if tgt is not None and tgt.get("bus"):
        buses.add(tgt.get("bus"))
    if src is not None and src.get("protocol") == "rbd":
        rbd += 1
    elif d.get("type") == "block":
        blk += 1
    else:
        other += 1
print(",".join([qga, qstate, uefi, tpm, str(rbd), str(blk), str(other), "|".join(sorted(buses))]))
')"
    qver=""; qfreeze=""
    if [ "$DO_PING" -eq 1 ] && [ "$state" = "running" ] && printf '%s' "$parsed" | grep -q '^yes,connected'; then
      info="$(virsh qemu-agent-command --timeout 3 "$dom" '{"execute":"guest-info"}' 2>/dev/null)"
      if [ -n "$info" ]; then
        qver="$(printf '%s' "$info" | python3 -c 'import sys,json; print(json.load(sys.stdin)["return"].get("version",""))' 2>/dev/null)"
        qfreeze="$(printf '%s' "$info" | python3 -c '
import sys, json
cmds = {c["name"]: c.get("enabled", False) for c in json.load(sys.stdin)["return"].get("supported_commands", [])}
print("enabled" if cmds.get("guest-fsfreeze-freeze") else "disabled")' 2>/dev/null)"
      else
        qver="no_response"
      fi
    fi
    echo "$uuid,$dom,$state,$parsed,$qver,$qfreeze" >> "$CSV"
  done
fi

{
  section "Domain özeti (V-12, V-16)"
  total=$(($(wc -l < "$CSV") - 1))
  echo "domain: $total"
  echo "QGA kanalı var:        $(awk -F, 'NR>1 && $4=="yes"' "$CSV" | wc -l)"
  echo "QGA kanalı bağlı:      $(awk -F, 'NR>1 && $5=="connected"' "$CSV" | wc -l)"
  echo "UEFI (nvram/OVMF):     $(awk -F, 'NR>1 && $6=="yes"' "$CSV" | wc -l)"
  echo "vTPM:                  $(awk -F, 'NR>1 && $7=="yes"' "$CSV" | wc -l)"
  echo "RBD disk toplam:       $(awk -F, 'NR>1 {s+=$8} END {print s+0}' "$CSV")"
  echo "block (FC) disk toplam: $(awk -F, 'NR>1 {s+=$9} END {print s+0}' "$CSV")"
  [ "$DO_PING" -eq 1 ] && echo "fsfreeze etkin (guest-info): $(awk -F, 'NR>1 && $13=="enabled"' "$CSV" | wc -l)"
} >> "$REPORT"

echo "yazıldı: $REPORT"
echo "yazıldı: $CSV"
