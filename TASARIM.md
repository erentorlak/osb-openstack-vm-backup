# OSB — OpenStack 2024.1 VM Backup/Restore Tasarımı

> **Sürüm:** v0.1 (taslak) · **Tarih:** 21 Kasım 2026 · **Girdi:** Ortam brief'i (21.11.2026, sanitize)
>
> **Durum:** Tasarım önerisi. Brief'teki tüm sayılar bir snapshot'tır.
> **⚠ÜD** ile işaretli her madde bir **üretim değişikliğidir**; lab doğrulaması ve değişiklik onayı olmadan uygulanmaz.
> `V-xx` = §22'deki canlı doğrulama maddesi · `D-xx` = §21'deki açık karar · `B-xx` = §1.3'teki bulgu.
> Birimler: **s** = saniye, **sa** = saat, **dk** = dakika.
>
> **Gizlilik:** Gerçek IP, parola, tenant ya da VM adı yoktur. Tüm IP'ler RFC 5737 dokümantasyon aralıklarından seçilmiş örnek adreslerdir ve gerçek adreslerle benzerlik taşımaz:
> - `192.0.2.0/24` yönetim/API ağını temsil eder;
> - `198.51.100.0/24` Ceph public ağını temsil eder.
>
> **Kod adı:** OSB (OpenStack Backup). `osbak` prototipinden kod alınmaz. Yalnız manifest/chunk fikri referans alınır, çünkü prototip mevcut pool ve NetApp FC yapısıyla uyumsuz.

---

## 1. Yönetici özeti

### 1.1 Bugünkü durum

- Ortam: 181 VM, 330 volume ve 22 proje.
  - **Ceph:** 197 volume, 36,3 TB allocate, ~13 TiB gerçek veri.
  - **NetApp FC:** 132 volume, 9,6 TB allocate; gerçek kullanım bilinmiyor.
- Korunan veri: yalnız 8 cinder backup (1'i `error`) ve 39 snapshot.
  - VM düzeyinde yedek yok.
  - Offsite kopya yok.
  - Restore tatbikatı yapılmamış.
- Bugün bir VM silinirse port, SG, flavor, FIP ve keypair bilgisi kaybolur. Verisi de çoğu durumda geri gelmez.
- Ceph kümesi kaybedilirse üretim ve yedekler birlikte gider.
- `cinder-backup` tek başına yeterli değildir:
  1. Ceph'te tutarlılık ve incremental aynı anda alınamaz (`--snapshot` kullanılırsa yedek full olur).
  2. Mevcut sürücüyle NetApp'te yalnız full kopya alınabilir.
  3. VM metadata'sı yedeklenmez.
  4. Yedekler aynı Ceph kümesinde durur.

### 1.2 Öneri (tek paragraf)

**OSB**, OpenStack dışında çalışan bağımsız bir uygulamadır. İşleyişi şöyledir:
- Her VM'i bir **tutarlılık grubu** olarak ele alır.
- Hypervisor'da çalışan küçük bir ajanla VM'i QGA üzerinden dondurur (freeze).
- Tüm volume'ların anlık noktasını alır ve VM'i hemen çözer (thaw).
- Veriyi sonradan **noktadan noktaya diff** ile okur:
  - Ceph'te `diff_iterate` ve fast-diff kullanılır.
  - NetApp'te tam okuma yapılır ve dedup uygulanır.
- Okunan veriyi **4 MiB'lık, içerik adresli ve proje bazında şifreli chunk'lara** böler. Bu chunk'lar iki yere yazılır:
  - RGW üzerinde ayrı bir EC placement'a (yerel, kısa retention);
  - **pull modeliyle Object Lock'lu offsite S3'e** (uzun retention).
- VM metadata'sını aynı yedek setine koyar.
- Restore'u **plan/apply** akışıyla yapar. Varsayılan restore modu tahribatsızdır.

Ceph ile NetApp arasındaki fark yalnız **provider** katmanında kalır. Katalog, retention, restore ve güvenlik ortaktır.

**Temel kararlar:**
1. Tek uygulama ve provider eklentileri: `ceph_rbd` ve `block_attach` (NetApp FC). Faz 3'te koşullu olarak `ontap_vault` eklenir.
2. Ceph'te `cinder-backup` kullanılmaz. Akış freeze → nokta → `diff_iterate` → chunk şeklindedir. Böylece **tutarlılık ve incremental birlikte** sağlanır.
3. Ceph için hedef nokta tipi **doğrudan RBD snapshot**'tır: `backup.osb-<set>.snap.<ts>`.
   - Kiracıya görünmez, kota tüketmez ve volume silmeyi engellemez.
   - Milisaniyeler içinde alınır.
   - Lab'da doğrulanana kadar fallback olarak Cinder snapshot kullanılır (§7.2, B-01).
4. NetApp'te Cinder snapshot alınır. Bundan geçici bir klon volume oluşturulur ve FC'li compute üzerinde salt-okur attach edilir. Ardından tam okuma ve dedup yapılır. **Tam okuma, tam depolama anlamına gelmez:** her gün yalnız değişen chunk'lar saklanır.
5. Her manifest kendi başına yeterlidir (self-contained). Chunk'lar mark-and-sweep GC ile temizlenir. **Yedekler arasında zincir yoktur.**
6. Veri düzlemi salt-okurdur:
   - Yazma yetkisi yalnız izole **restore-helper**'dadır.
   - Snapshot yetkisi yalnız hypervisor'lardadır. Bu host'larda `client.cinder` anahtarı zaten bulunur.
7. Şifreleme istemci tarafında yapılır (AES-256-GCM). Ana anahtar OpenStack dışında tutulur ve emanete (escrow) verilir. Bu sayede HTTP üzerinden çalışan RGW'de bile veri gizli kalır.
8. Katalog için PostgreSQL kullanılır. **Tek doğruluk kaynağı S3'tür;** katalog S3'ten yeniden kurulabilir.

**Kapasite:**
- İlk full yedek sonrasında yerel tier ~10–15 TiB mantıksal veri tutar.
- EC 2+2 ile bu ~20–30 TiB raw eder. Küme %31'den ~%45–52'ye çıkar.
- 4 host'lu kümede güvenli tavan **~%64**'tür (B-07).
- Bu yüzden yerel retention kısa tutulur; uzun retention offsite'tadır.

**Takvim (kaba):**

| Faz | Süre | Kapsam |
|---|---|---|
| Faz 0 | 2–3 hafta | Doğrulama ve ölçüm |
| Faz 1 | 8–10 hafta | Ceph MVP ve pilot |
| Faz 2 | 8–10 hafta | NetApp, offsite, GC ve tüm filo |
| Faz 3 | — | HA, özel RBAC rolleri, self-servis, SnapVault |

### 1.3 Brief'te yer almayan ama tasarımı belirleyen bulgular

| # | Bulgu | Etki |
|---|---|---|
| **B-01** | Cinder, snapshot'ı olan bir volume'u silmez. Nova da `delete_on_termination` silmesi başarısız olursa hatayı loglayıp devam eder. Volume `available` kalır ve faturalanmaya devam eder. | "Volume başına kalıcı Cinder base snapshot" stratejisi kiracının volume silmesini bozar. Bu yüzden Ceph'te doğrudan RBD snapshot hedeflenir. |
| **B-02** | Cinder snapshot'ları varsayılanda yalnız `snapshots` sayı kotasından değil, projenin `gigabytes` kotasından da düşer (`no_snapshot_gb_quota=False`). | Kalıcı base snapshot tutmak projenin GB kullanımını iki katına çıkarır. Kotaya yakın projelerde yedek başarısız olur. |
| **B-03** | `cinder-backup`'ın chunked sürücüleri (S3/NFS/Posix) NetApp volume'ları için de sha-blok karşılaştırmalı incremental yapabilir. "NetApp'te yalnız full" kısıtı mevcut Ceph backup sürücüsüne özgüdür. | Geçiş döneminde bir seçenek olabilir. Yine de VM metadata'sı, tutarlılık, şifreleme ve immutability sorunlarını çözmez. |
| **B-04** | Nova sürüm dizesi `26.1.0.dev779`. 26.x Zed serisidir; Caracal 29.x'tir. Diğer servislerin sürümleri Caracal ile tutarlı. | Pbr/etiket artefaktı olabilir. Gerçek kod seviyesi max microversion ile doğrulanmalı (Caracal 2.96, Zed 2.93; V-01). Tasarım en fazla 2.90'a dayanır. |
| **B-05** | QGA freeze için guest'te ajan kurulu olması yetmez. Instance'ın kayıtlı imaj özelliğinde `hw_qemu_guest_agent=yes` olmalı ve VM yeniden başlatılmalıdır. Volume'un image metadata'sını değiştirmek çalışan instance'ı etkilemez. Bunun için `nova-manage image_property set` ile VM SHUTOFF iken güncelleme yapılmalıdır. | Tutarlı yedek oranı bu operasyonel yaygınlaştırmaya bağlıdır (D-08). |
| **B-06** | Windows VSS, yazmaları ~10 saniyeden uzun tutamaz. | Freeze penceresi içinde tüm snapshot'ların bitmesi gerekir. Cinder API gecikmesi (1–5 s) risklidir; RBD snapshot ms seviyesindedir. |
| **B-07** | 4 OSD host'lu kümede bir host kaybedildiğinde veri kalan 3 host'a yeniden dağılır. Bu yüzden raw kullanım, nearfull eşiğine çarpmamak için ≲ 0,85 × 3/4 ≈ **%64** olmalıdır. | Yerel yedek tier'ı küçük tutulmalı ve EC kullanmalıdır (§12). |
| **B-08** | Restore edilen VM yeni bir instance UUID'si alır. Cloud-init bunu "yeni instance" sayar; per-instance modüller ve user_data yeniden çalışır, SSH host key'leri de yenilenir. | user_data varsayılan olarak restore edilmez (opt-in). |
| **B-09** | `swap_volume` bir restore aracı değildir: eski volume'un verisini yenisine kopyalar. | Kök volume için in-place restore başka yoldan yapılır (§9.5). |
| **B-10** | UEFI NVRAM compute host'un yerel diskinde, vTPM durumu ise host ve Barbican'da tutulur. İkisi de volume yedeğinde yer almaz. | UEFI ve vTPM'li VM'ler tatbikatta özellikle test edilir (V-16, V-27). |
| **B-11** | RGW'nin henüz gerçek bir kullanıcısı yok ve bucket oluşturulmamış. | TLS'i açmak ve ayrı bir EC placement kurmak **şimdi** düşük risklidir. Data pool tipi ilk bucket oluşmadan seçilmelidir. |
| **B-12** | Compute arızasında veri zaten paylaşımlı depodadır (Ceph veya SAN). | Host kaybında doğru araç restore değil, manuel `nova evacuate`'tir. Masakari evacuation çalışmadığı için runbook gerekir. |

### 1.4 Kapsam

**Kapsam içi:**
- Cinder volume'lu VM'ler: yalnız Ceph (~130), yalnız NetApp (~50) ve karışık (~1).
- SHUTOFF VM'ler (11).
- VM metadata'sı.
- Restore türleri: yeni volume, yeni VM, replace, in-place ve OpenStack'siz ham export.
- Offsite kopya, retention, GC, restore tatbikatı ve raporlama.

**Kapsam dışı (MVP):**
- Kubernetes (cluster düzeyinde ayrı bir araç gerekir).
- Şifreli Cinder volume'lar. Bunlar tespit edilir ve MVP'de hariç tutulur; ele alınmaları Faz 3'tedir.
- Volume'suz 3 VM (`worker-*`). Bunlar için yalnız metadata yedeği alınır.
- Flavor ephemeral/swap diskleri. Bunlar uyarıyla raporlanır.
- Dosya düzeyinde restore (Faz 3).
- Trove, Octavia ve Heat nesneleri.
- Kontrol düzlemi DR'ı. Ancak Galera dump'ı ve OSA/Ceph config'inin offsite'a taşınması Faz 1'de ek iş olarak önerilir.
- Kiracı self-servis arayüzü (Faz 3).
- Glance imaj yedeği (Faz 3, opsiyonel).

---

## 2. Temel kararlar (ADR özeti)

| # | Konu | Karar | Reddedilen alternatif | Gerekçe |
|---|---|---|---|---|
| A1 | Uygulama yapısı | Tek uygulama ve capability modelli provider eklentileri | Ceph ve NetApp için iki ayrı ürün | Karışık VM'ler tek tutarlılık grubu olarak ele alınmalı. Katalog, restore ve retention tek kalmalı. Fark yalnız "nokta al / değişeni bul / oku / yaz" katmanındadır. |
| A2 | Ceph veri yolu | Nokta + `diff_iterate` (fast-diff) + 4 MiB chunk → S3 | `cinder-backup` Ceph sürücüsü | `--snapshot` full yedek üretir. Metadata, dedup, şifreleme ve immutability yoktur. Yedek aynı kümede RBD olarak durur. |
| A3 | Ceph nokta tipi | **Mode B:** doğrudan RBD snap `backup.osb-<set>.snap.<epoch>` (hedef). **Mode A:** Cinder snapshot (fallback) | Kalıcı Cinder base snapshot | B-01, B-02, proj-B/C kotaları ve B-06. Bu ad kalıbı, Cinder RBD sürücüsünün volume silerken temizlediği backup-snapshot kalıbıdır (V-02). |
| A4 | NetApp veri yolu | Cinder snapshot → geçici klon volume → FC'li cmp'de salt-okur attach → tam okuma, sıfır atlama ve dedup | `cinder-backup` full kopya; ONTAP SnapDiff (LUN blok düzeyinde yok) | Her gün yalnız değişen chunk'lar depolanır. Kalıcı snapshot olmadığı için kota ve silme sorunu oluşmaz. İkincil ONTAP varsa Faz 3'te SnapVault eklenir (D-06). |
| A5 | Tutarlılık | Hypervisor'daki `osb-freeze-agent`: freeze → RBD snap → thaw işlemlerini yerelde ve deadline ile yapar | Nova `createImage` (server snapshot) | `createImage` quiesce sonucunu raporlamaz. `os_require_quiesce` boot anındaki imaj özelliğine bağlıdır. Glance'te kayıt artığı ve kalıcı Cinder snapshot'ları bırakır. |
| A6 | Manifest | Self-contained manifest + mark-and-sweep GC | Delta zinciri | Zincir olmadığı için her yedek bağımsız olarak restore edilir ve silinir. Restore tek manifest okur. Bedeli: 4 MiB başına 32 B. |
| A7 | Dedup alanı | Proje bazında (HMAC chunk id) | Global dedup | Kiracı izolasyonu sağlanır, yan kanal oluşmaz ve proje silinince kripto-silme yapılabilir. Kayıp yalnız projeler arası ortak Glance tabanlarıdır. |
| A8 | Depo | Yerel: RGW, ayrı `osb-ec` placement (EC 2+2) ve kota. Offsite: S3 + Object Lock, pull | `cinder-backups` RBD pool'u; yalnız yerel depo | 4 MiB chunk RGW'de tek bir RADOS nesnesi olur. Kapasite için EC şarttır (§12). Yerel tier tek başına DR değildir. |
| A9 | Katalog | PostgreSQL 16 (katalog ve iş kuyruğu) | SQLite; OpenStack Galera'sı | Eşzamanlı worker'lar ve `SKIP LOCKED` kuyruğu gerekir. Galera arızası yedek sistemini düşürmemelidir. Katalog S3'ten yeniden kurulabilir. |
| A10 | Yerleşim | OpenStack dışında ayrı host (`backup01`) | ctrl LXC'si; OpenStack içinde VM | Bulut çökünce de restore yapılabilmeli. 36 privileged LXC ve cgroup sorunu var. Kaynak izolasyonu gerekir. |
| A11 | Kimlik | Servis kullanıcısı + application credential; okuma ve yazma kimlikleri ayrı; Faz 3'te policy override ile özel roller | Domain admin; paylaşılan admin parolası | Caracal'da `admin` rolü pratikte bulut-admin demektir. Gerçek en az yetki policy override gerektirir (§11.2). |
| A12 | Şifreleme | İstemci tarafı AES-256-GCM, proje DEK'i, OpenStack dışında ana anahtar ve escrow | RGW SSE; Barbican | HTTP RGW'de gizlilik sağlanır. DR anında Barbican'a bağımlılık oluşmaz. |
| A13 | İş kuyruğu | PostgreSQL tabanlı kuyruk, worker'lar dışarıdan bağlanır (mTLS long-poll) | OpenStack RabbitMQ'su | RabbitMQ 3.11 EOL ve kontrol düzlemine bağlı. Compute'ta dinleyen port açılmaz, firewall değişikliği gerekmez. |

---

## 3. Mimari

### 3.1 Genel görünüm

```
                  ┌──────────── OSB kontrol düzlemi — backup01 (OpenStack dışı) ─────────────┐
 Operatör ─CLI/API─► osb-api ── osb-scheduler ── osb-orchestrator ── osb-gc ── osb-verify   │
                  │        └──────────── PostgreSQL 16 (katalog + iş kuyruğu) ─────────────┘ │
                  │   Keystone/Nova/Cinder/Neutron ← iç VIP 192.0.2.10 (app credential)      │
                  │   osb-worker(ceph) ── client.osb-ro (RBD salt-okur) ─────────────────┐   │
                  └──────────────────────────────▲──────────────────────────────────────│───┘
                     mTLS long-poll (dışa doğru) │                                       │
        ┌────────────────────────────────────────┼───────────────────────┐               │
        ▼                                        ▼                       ▼               │
 osb-freeze-agent (cmp01-03)          osb-worker(block) (cmp01-03)   osb-restore-helper  │
 libvirt/QGA freeze/thaw              Cinder attachment + os-brick   (restore01, izole)  │
 client.osb-snap (yalnız snap)        FC multipath, salt-okur        client.osb-restore  │
        │ rbd snap create/rm                 │ FC okuma                  │ RBD/FC yazma  │
        ▼                                    ▼                           ▼               ▼
  Ceph cinder-volumes / glance-images   NetApp SVM LUN'ları        (yalnız restore)  RBD okuma
        │
        │  S3: chunks / manifests / sets  (istemci tarafı şifreli)
        ▼
  RGW yerel tier ── placement "osb-ec" (EC 2+2, pool kotası) ── ceph01-04 (TLS 8443 önerilir)
        │  pull, salt-okur S3 kimliği
        ▼
  osb-replicator @ offsite ──► Offsite S3 (Object Lock) + anahtar/katalog escrow kopyası
```

### 3.2 Yerleşim

IP'lerin tamamı RFC 5737 örnek adresleridir.

| Host | Bileşenler | Ağ | IP |
|---|---|---|---|
| `backup01` | osb-api, scheduler, orchestrator, gc, verify, PostgreSQL, osb-worker(ceph) | mgmt + Ceph public (storage bond, MTU 9000) | `192.0.2.31` / `198.51.100.31` |
| `restore01` | osb-restore-helper | mgmt + Ceph public | `192.0.2.33` / `198.51.100.33` |
| `backup02` (Faz 2–3) | 2. worker, PG streaming replika, pasif orchestrator | mgmt + Ceph public | `192.0.2.32` / `198.51.100.32` |
| `cmp01-03` | osb-freeze-agent, osb-worker(block) | mevcut | `192.0.2.21-23` |
| offsite | osb-replicator, S3 hedefi | WAN/VPN | D-03 |

**`backup01` önerilen boyutu:** 32 çekirdek, 128 GiB RAM, 2×25 GbE (LACP, MTU 9000) ve 2×NVMe (OS ve PG). Veri S3'e gittiği için büyük disk gerekmez.

**`restore01`:** Ayrı fiziksel host olması tercih edilir. Kısıt varsa `backup02` üzerinde ayrı Unix kullanıcısı ve container ile çalışabilir. Önemli olan, yazma anahtarının yedek worker'larıyla aynı süreç alanında bulunmamasıdır.

**Ara çözüm (D-04):** Ayrı donanım gelene kadar ctrl03'te cgroup ile sınırlanmış tek bir LXC kullanılabilir: 8 vCPU, 32 GiB RAM ve IO limiti. Bu durumda worker throughput'u bilinçli olarak düşük tutulur (≤ 300 MB/s).

**Faz 1'de tek controller vardır.** Controller ölürse yedekler durur ama mevcut yedekler S3'te güvende kalır. `osb-cli` ile restore yapılabilir (§9.8). HA Faz 3'te gelir: Patroni, ikinci orchestrator ve PG advisory lock ile lider seçimi.

### 3.3 Tasarım ilkeleri

1. **S3 doğruluk kaynağıdır.** Katalog yeniden üretilebilir bir indekstir.
2. **En az yetki dağılımı:**
   - Yedek worker'ları salt-okurdur.
   - Snapshot yetkisi yalnız hypervisor'lardadır.
   - Yazma yetkisi yalnız restore-helper'dadır.
3. **Yedekler bağımsızdır.** Zincir yoktur; herhangi bir yedek silinebilir ya da tek başına restore edilebilir.
4. **Kiracı davranışı değişmez.** Noktalar görünmezdir, kota tüketmez ve volume silmeyi engellemez.
5. **Üretim önceliklidir.** Throttle, kapasite guard'ı ve kill switch (`osb pause`) vardır.
6. **Her adım idempotent ve yeniden başlatılabilirdir.**
7. **Restore varsayılanı tahribatsızdır.** Tahribatlı modlar dört göz onayı gerektirir.
8. **OpenStack ve katalog olmadan da restore yapılabilir.** Bunun için manifest ve escrow'daki anahtar yeterlidir.

---

## 4. Bileşenler

| Bileşen | Çalıştığı yer | Sorumluluk | Kimlik | Not |
|---|---|---|---|---|
| `osb-api` | backup01 | REST API ve `osb` CLI: politika, anlık yedek, restore plan/apply, raporlar | Operatörler Keystone ile girer. Roller: `osb_viewer`, `osb_operator`, `osb_admin`. | Tahribatlı restore modları ikinci bir onaycı gerektirir. |
| `osb-scheduler` | backup01 | Politikalardan zamanı gelen işleri üretir. EDF (earliest deadline first) ile proje bazında adil sıralar. | — | Global kill switch, pencere ve throttle burada uygulanır. |
| `osb-orchestrator` | backup01 | VM state machine'ini yürütür: envanter, metadata yakalama, freeze koordinasyonu, nokta, transfer dağıtımı, commit ve base rotasyonu. | `osb-svc` app credential(lar)ı | Faz 3'te aktif/pasif çalışır. |
| `osb-freeze-agent` | cmp01-03 | QGA `guest-ping`, libvirt `fsFreeze/fsThaw`, RBD snap create/rm. **Yerel hard deadline ile kesin thaw yapar.** | `client.osb-snap`; mTLS istemci sertifikası | Yalnız o host'taki domain'in **gerçekten bağlı** RBD disklerinde işlem yapar (libvirt XML ile doğrular). Yalnız `backup.osb-*` adlı snapshot'ları siler. Port dinlemez. |
| `osb-worker(ceph)` | backup01 (+backup02) | RBD okur; chunk'lar, HMAC alır, zstd ile sıkıştırır, AES-GCM ile şifreler ve S3'e PUT eder. | `client.osb-ro`, S3 `osb-writer` | Container imajı `quay.io/ceph/ceph:v18.2.x` tabanlıdır; librbd Reef'e sabitlenir. |
| `osb-worker(block)` | cmp01-03 | Geçici klon volume'u Cinder attachment ve os-brick ile FC üzerinden salt-okur bağlar, okur ve aynı pipeline'a verir. Restore modunda yazar. | Orchestrator'dan gelen attachment bilgisi; S3 `osb-writer` | systemd cgroup sınırları: `CPUQuota`, `MemoryMax`, `IOReadBandwidthMax`. |
| `osb-restore-helper` | restore01 | Restore verisini RBD'ye yazar. İmzalı **write grant** olmadan hiçbir imajı RW açmaz. | `client.osb-restore`, S3 `osb-reader` | Grant'i Cinder API'den kendisi de doğrular (§9.4). |
| `osb-replicator` | offsite | Yeni nesneleri yerel RGW'den çeker, offsite'a yazar, doğrular ve lock uygular. | Yerelde `osb-reader`; offsite'ta writer | Prod tarafı offsite'ta silme yapamaz. |
| `osb-gc` | backup01 | Retention sonu işlemleri, mark-and-sweep, base nokta mutabakatı ve yetim kaynak temizliği. | S3 `osb-gc`; snap silme için agent'a istek | Günlük silme bütçesi uygular (§14). |
| `osb-verify` | backup01 | Scrub (örnek chunk okuma), derin doğrulama ve restore tatbikatı. | `osb-reader`; drill projesinde `osb-svc` | — |
| PostgreSQL 16 | backup01 | Katalog, kuyruk ve audit | Yerel | Günlük şifreli dump S3'e alınır. |

---

## 5. Provider modeli

```python
class SourceProvider(Protocol):
    name: str  # "ceph_rbd" | "block_attach" | "ontap_vault"
    def supports(self, vol: VolumeInfo) -> Caps | None: ...
    def create_points(self, vm: VmCtx, vols: list[VolumeInfo], freeze: FreezeHandle) -> list[PointRef]: ...
    def changed_extents(self, point: PointRef, base: PointRef | None) -> Iterator[Extent]: ...
        # base None ise tüm dolu alanları döndürür; Extent(offset, length, exists)
    def open_reader(self, point: PointRef) -> BlockReader: ...
    def finalize(self, point: PointRef, keep_as_base: bool) -> None: ...  # base rotasyonu / temizlik

class RestoreTarget(Protocol):
    def create_volume(self, spec: VolumeSpec, project: str) -> VolumeRef: ...
    def open_writer(self, vol: VolumeRef, grant: WriteGrant) -> BlockWriter: ...  # yalnız restore-helper / block worker
```

`Caps` alanları:
- `incremental: bool`
- `point_latency: "ms" | "s"`
- `placement: "any" | "fc_host" | "vm_host"`
- `persistent_base: bool`
- `data_location: "chunkstore" | "external"`

| Yetenek | `ceph_rbd` | `block_attach` (NetApp FC) | `ontap_vault` (Faz 3, koşullu) |
|---|---|---|---|
| Nokta | Mode B: RBD snap (agent, ms) · Mode A: Cinder snapshot (1–5 s) | Cinder snapshot (ONTAP LUN klonu; V-08) | ONTAP FlexVol snapshot |
| Değişen blok | `diff_iterate(whole_object=True)`, fast-diff | Yok: tam okuma + sıfır atlama + dedup (opsiyonel: SCSI GET LBA STATUS, V-11) | SnapMirror blok deltası (ONTAP içinde) |
| Okuma yeri | Ceph public ağına erişen her worker | Yalnız FC HBA'lı cmp | ONTAP → ONTAP |
| Kalıcı base | 1 RBD snap / volume | Yok (okuma sonrası silinir) | ONTAP politikası |
| Veri yeri | Chunk store | Chunk store | Dış ONTAP (katalogda referans) |
| Restore yazma | restore-helper (RBD) | block worker (FC) | SnapMirror restore + `cinder manage` |

---

## 6. Veri modeli

### 6.1 Katalog (PostgreSQL, özet şema)

```sql
project(id uuid pk, keystone_id text unique, dedup_domain uuid, dek_key_id text, state text)
policy(id uuid pk, name text unique, spec jsonb, version int, enabled bool)
binding(id uuid pk, policy_id uuid, selector jsonb /* project | server | tag */, enforced bool, priority int)
protected_server(server_id uuid pk, project_id uuid, policy_id uuid, last_success_at timestamptz,
                 last_set_id uuid, state text)
backup_set(id uuid pk, server_id uuid, project_id uuid, policy_id uuid, point_time timestamptz,
           consistency text  /* application | filesystem | crash | offline */, freeze_ms int,
           state text        /* planned | pointed | transferring | committed | failed | expired | deleted */,
           set_key text, metadata jsonb, bytes_logical bigint, bytes_read bigint, bytes_new bigint,
           expires_local timestamptz, expires_offsite timestamptz, legal_hold bool,
           offsite_state text, created_at timestamptz, committed_at timestamptz)
volume_backup(id uuid pk, set_id uuid fk, volume_id uuid, provider text, point_ref jsonb,
              base_set_id uuid null, mode text /* full | incremental | fullread */,
              size bigint, chunk_size int, manifest_key text, manifest_digest bytea,
              chunks_total int, chunks_new int, bytes_read bigint, state text)
base_point(volume_id uuid pk, set_id uuid, provider text, point_ref jsonb, rbd_image_id text, created_at timestamptz)
chunk(domain uuid, id bytea /* 32 B */, size_plain int, size_stored int, created_at timestamptz,
      last_ref_at timestamptz, state text /* live | condemned */, condemned_at timestamptz,
      primary key (domain, id))
task(id bigserial pk, kind text, payload jsonb, state text, priority int, not_before timestamptz,
     attempts int, lease_owner text, lease_until timestamptz, idem_key text unique)
restore_op(id uuid pk, set_id uuid, mode text, target_project uuid, plan jsonb, plan_hash bytea,
           requested_by text, approved_by text[], state text, created_at timestamptz)
restore_step(op_id uuid, seq int, kind text, idem_key text, resource_id text, state text, detail jsonb)
audit(id bigserial pk, at timestamptz, actor text, action text, target text, detail jsonb,
      prev_hash bytea, hash bytea)  -- hash zinciri; append-only
drill(id uuid pk, set_id uuid, started_at timestamptz, rto_s int, digest_ok bool, boot_ok bool, notes text)
```

### 6.2 Nesne deposu düzeni

Her proje (dedup alanı) için ayrı bir bucket açılır; 22 proje için 22 bucket olur. Bucket'ların sahibi `osb-owner` kullanıcısıdır.

```
s3://osb-<domain>/
  chunks/<id[0:2]>/<id[2:4]>/<id-hex>       şifreli ve sıkıştırılmış chunk
  manifests/<server_id>/<set_id>/<volume_id>.osbm
  sets/<server_id>/<set_id>.osbs            VM metadata + manifest listesi = COMMIT işareti
  keys/<key_id>.dek                         ana anahtarla sarılmış proje DEK'i
s3://osb-system/
  catalog/<yyyy-mm-dd>.sql.zst.enc          günlük katalog dökümü
  audit/<yyyy-mm-dd>.jsonl.zst.enc
  platform/...                              opsiyonel: Galera dump, OSA/Ceph config yedekleri
```

### 6.3 Chunk formatı

```
K_id, K_enc, K_man = HKDF-SHA256(DEK_project, info = "osb/id" | "osb/enc" | "osb/man")
chunk_id = HMAC-SHA256(K_id, plaintext)                      # 32 B; dedup anahtarı, içeriği ifşa etmez
object   = "OSC1" | key_id(16) | flags(1) | nonce(12, rastgele) | AES-256-GCM(K_enc, zstd?(plaintext)) | tag(16)
```

- **4 MiB sabit boyut, ofsete hizalı.**
  - Cinder volume'larında RBD nesne boyutu 8 MiB'tır (`rbd_store_chunk_size=8`). Değişen bir nesne 2 chunk eder; değişmeyen yarı dedup ile atlanır.
  - RGW, 4 MiB'a kadar olan bir nesneyi tek bir RADOS head nesnesinde tutar. 8 MiB'lık chunk ise head ve tail olarak iki nesneye bölünürdü.
- **Sıfır chunk saklanmaz.** Manifestte `ZERO` olarak işaretlenir.
- **Sıkıştırma:** zstd seviye 3 kullanılır. Kazanç %5'in altındaysa chunk ham saklanır (flag ile işaretlenir).
- **Dedup kararı katalog aramasıyla verilir** (chunk id var mı?). Bu yüzden nonce rastgele olabilir; deterministik şifreleme gerekmez.
- **Restore sırasında iki kontrol yapılır:** GCM tag'i doğrulanır ve `HMAC(plaintext) == chunk_id` kontrol edilir. Böylece uçtan uca bütünlük sağlanır ve nesne değiştirme saldırısı yakalanır.

### 6.4 Manifest formatı

Her manifest self-contained'dir; volume'un o andaki **tam** görüntüsünü tarif eder.

```
header : "OSM1" | set_id | volume_id | provider | size | chunk_size | n_chunks | key_id | created_at
         | source{pool, image, image_id, point_name, parent_spec}
body   : n_chunks × 32 B   (chunk_id veya ZERO = 0x00…00)
footer : volume_digest = SHA-256(body) ; mac = HMAC(K_man, header ‖ body)
```

- Manifestin tamamı zstd ile sıkıştırılır ve AES-GCM(K_man) ile şifrelenir.
- 200 GiB'lık bir volume 51.200 girişe karşılık gelir; ham boyutu ~1,6 MiB'tır. Sıfır bölgeleri neredeyse yer kaplamaz.
- Tüm filonun günlük manifest yükü sıkıştırma sonrası ~150 MB'tır; 30 günde ~4,5 GB eder.
- Faz 3 iyileştirmesi: manifestler 1024 girişlik içerik adresli sayfalara bölünür. Değişmeyen sayfalar yedekler arasında paylaşılır ve manifest yükü 10–50 kat azalır.

### 6.5 Commit protokolü ve değişmezler

**Commit sırası:**
1. Chunk'lar yüklenir. Bir chunk'a ancak yüklendikten sonra referans verilir.
2. Manifestler yüklenir.
3. `sets/.../<set_id>.osbs` yüklenir. **Bu, commit noktasıdır.**
4. Katalog satırı `committed` olarak işaretlenir.

Adım 3 ile 4 arasında bir çökme olursa reconciler, S3'teki set nesnesini kataloğa aktarır.

| Değişmez | Tanım |
|---|---|
| **I1** | Bir yedek seti, ancak set nesnesi S3'te var ve MAC'i doğrulanıyorsa vardır. |
| **I2** | Commit edilmiş bir manifestin referans verdiği her chunk depoda bulunur. |
| **I3** | GC, commit edilmiş ya da uçuşta olan bir manifestin referans verdiği chunk'ı silmez (§14). |
| **I4** | Katalog, S3 ve escrow'daki anahtarlar kullanılarak `osb catalog rebuild` ile yeniden kurulabilir. |
| **I5** | Bir volume'un base noktası, ancak daha yeni bir set commit edildikten sonra silinir. |

### 6.6 Politika modeli

```yaml
policy: gold
rpo: 24h                         # scheduler EDF son tarihi: last_success + rpo
window: "20:00-07:00 Europe/Istanbul"
consistency: prefer              # require | prefer | crash  (SHUTOFF VM'ler otomatik "offline")
retention:
  local:   { daily: 7 }
  offsite: { daily: 14, weekly: 8, monthly: 12 }
offsite: required
deep_verify: monthly             # tam okuma + manifest karşılaştırma
priority: 10
---
binding:
  - { selector: { project: proj-A }, policy: silver, enforced: true }
  - { selector: { server_tag: "osb:policy=gold" }, policy: gold }      # kiracı opt-in (chargeback ile)
  - { selector: { server_tag: "osb:exclude" }, action: exclude, enforced_override: false }
```

---

## 7. Backup akışı

### 7.1 VM düzeyinde sıra

```
T0  Planlama: VM ve volume listesi, volume başına provider seçimi; kapasite, sağlık ve kota guard'ları
T1  Metadata yakalama (Nova/Neutron/Cinder/Keystone, §9.3) → set taslağı (freeze öncesi; pencereyi uzatmaz)
T2  Agent: QGA guest-ping (3 s timeout) → tutarlılık modu kararı
T3  ┌ FREEZE  (agent, yerel hard deadline: Linux 20 s / Windows 8 s)
T4  │ Noktalar: Ceph volume'lar → agent yerelde rbd snap create (paralel, ms)
    │           NetApp volume'lar → orchestrator Cinder snapshot (force/in-use), "available" bekler
T5  └ THAW    (her koşulda; hata, timeout veya bağlantı kopması dahil)
T6  Doğrulama: attachment listesi T1 ile aynı mı? Değiştiyse noktalar bırakılır ve yeniden denenir.
T7  Transfer (uzun ve kritik pencere dışında): volume başına changed_extents → chunk → dedup → S3
T8  Manifestler → set nesnesi (COMMIT) → katalog
T9  Base rotasyonu: yeni nokta base olur, eskisi silinir (I5). NetApp geçici kaynakları temizlenir.
T10 Offsite kuyruğu, metrikler, SLA güncellemesi
```

**Freeze penceresi hedefleri:**
- Yalnız Ceph volume'lu VM'de p95 < 2 s.
- NetApp ya da karışık VM'de p95 < 8 s.

Pencere süresi her yedek setinde `freeze_ms` olarak kaydedilir.

### 7.2 Ceph (`ceph_rbd`)

**Mode B (hedef):**
1. Agent, domain'in libvirt XML'inde `cinder-volumes/volume-<vid>` diskini doğrular.
2. Snapshot'ı alır: `rbd snap create cinder-volumes/volume-<vid>@backup.osb-<set_id>.snap.<epoch>`
   - Bu ad kalıbı `cinder-backup` Ceph sürücüsünün kalıbıyla (`^backup\.([a-z0-9\-]+?)\.snap\.(.+)$`) aynıdır.
   - Cinder RBD sürücüsü volume silerken bu kalıba uyan snapshot'ları temizler. Bu yüzden kiracının silme işlemi bozulmaz.
   - Bu davranış **V-02 ve V-03 ile bu Cinder build'inde doğrulanmalıdır.**
3. Snapshot kiracıya görünmez ve Cinder kotası tüketmez.
4. Sınır: Bu yöntem Cinder'ın iç davranışına dayanır. Her OpenStack yükseltmesinde kontrat testi çalıştırılır (§17). Test geçmezse Mode A'ya dönülür.

**Mode A (fallback):** Cinder snapshot alınır (`osb-<set>-<vid>`, `metadata osb:managed=true`) ve RBD tarafında `snapshot-<snap_uuid>` okunur. Kalıcı base snapshot yan etkileri §10'da ele alınır (B-01, B-02).

**Transfer (iki mod için ortak):**

```python
img = rbd.Image(ioctx, f"volume-{vid}", snapshot=new_point, read_only=True)   # client.osb-ro
img.diff_iterate(0, img.size(), from_snapshot=base_point_or_None, iterate_cb=cb,
                 include_parent=True, whole_object=True)
# cb(offset, length, exists): exists=False → ZERO; değilse 4 MiB sınırlarına genişlet → oku
```

1. **İlk yedekte** `from_snapshot=None` ve `include_parent=True` kullanılır. Glance klon parent'ının verisi de okunur ve yedek self-contained olur.
   - Aynı imajdan türeyen VM'ler proje içinde dedup ile tek kopyaya iner.
   - Parent verisi `glance-images` pool'undan okunduğu için `client.osb-ro` bu pool'da okuma yetkisine sahiptir.
2. **Sonraki yedeklerde** yalnız değişen 8 MiB'lık nesneler okunur. Yeni manifest, base manifestin kopyası üzerine değişen girişler yazılarak oluşturulur.
3. **Base geçerlilik kontrolü** her çalışmada yapılır:
   - base snapshot hâlâ var mı;
   - `img.id()` base kaydıyla aynı mı (retype veya migrate imajı değiştirmiş olabilir);
   - boyut büyüdü mü.

   Herhangi biri bozuksa **tam okuma** yapılır. Dedup sayesinde yükleme yine düşük kalır.
4. **object-map/fast-diff geçersizse** (`flags: object map invalid`) librbd yavaş diff yoluna düşer. Sonuç doğrudur ama yavaştır. Bu durum raporlanır; `rbd object-map rebuild` ayrıca planlanır (⚠ÜD, V-05).
5. **Throttle:**
   - `client.osb-ro` için librbd QoS uygulanır (`rbd_qos_read_bps_limit`, imaj başına).
   - Global bir token bucket vardır.
   - Ceph istemci gecikmesine göre AIMD geri çekilme uygulanır (§12.3).
6. **Derin doğrulama (aylık):** Volume'un noktası baştan sona okunur ve chunk id'leri yeniden hesaplanıp manifestle karşılaştırılır. Diff tabanlı sistemlerde olası object-map hatalarına karşı emniyet ağıdır.

### 7.3 NetApp FC (`block_attach`)

1. **Nokta:** Freeze penceresinde Cinder snapshot alınır (in-use, microversion 3.66+). Cinder NetApp sürücüsü bunu aynı FlexVol içinde bir LUN klonu olarak uygular (V-08).
2. **Geçici volume:** `osb-service` projesinde, admin kimliğiyle snapshot'tan bir volume oluşturulur (`osb-tmp-<set>-<vid>`). Böylece kiracının volume ve GB kotası etkilenmez (V-07).
3. **Salt-okur attach:** Block worker, FC'li bir cmp'de çalışır ve yük dengesine göre seçilir. İşlem sırası:
   - os-brick `get_connector_properties` ile FC WWPN'leri alınır.
   - Cinder attachment oluşturulur (`mode=ro`, microversion 3.54).
   - Connector ile attachment güncellenir.
   - os-brick `connect_volume` çağrılır → `/dev/dm-N`.
   - Attachment tamamlanır (`attachment complete`).

   Mevcut başarısız yedeğin `wwpns` hatası büyük olasılıkla FC'siz ctrl LXC'ye düşmesinden kaynaklanıyordu. Bu tasarımda yerleşim `placement=fc_host` capability'si ile zorunludur.
4. **Okuma:** O_DIRECT ile 4 MiB'lık okumalar yapılır (kuyruk derinliği 4–8).
   - Sıfır kontrolü `memcmp` ile yapılır; ucuzdur.
   - Sıfır olmayan chunk'lar HMAC'lenir. Katalogda yoksa sıkıştırılıp şifrelenir ve PUT edilir.
   - Opsiyonel: GET LBA STATUS ile tahsis edilmemiş bölgeler atlanabilir (V-11).
5. **Temizlik:** `disconnect_volume` → attachment silme → geçici volume silme → **snapshot silme**. Kalıcı nokta tutulmaz.
6. **Yük:** Günlük tam okuma, dolu LUN alanı kadar SAN ve host yükü getirir (§12.2). Faz 0'da ölçülür (V-20). Kabul edilemezse NetApp VM'leri için RPO 48–72 saate çekilir ya da yalnız Tier-1 günlük alınır (D-06).
7. **Ara önlem (Faz 1, NetApp henüz OSB'de değilken):**
   - `cinder-backup` için `backup_use_same_host=True` ayarlanır (⚠ÜD). Böylece NetApp yedekleri FC'li cmp üzerindeki backup servisine gider.
   - Kritik NetApp volume'ları için haftalık full yedek alınır.

### 7.4 Özel durumlar

| Durum | Davranış |
|---|---|
| SHUTOFF VM (11) | Freeze yapılmaz; tutarlılık `offline` kaydedilir. Nokta yine alınır, çünkü VM yedek sırasında başlatılabilir. Değişiklik yoksa diff boş döner ve işlem çok ucuzdur. |
| Karışık VM (~1) | Tek freeze altında Ceph noktası (ms) ve NetApp Cinder snapshot'ı (s) alınır. Pencere hedefi < 8 s'dir. |
| Volume'suz VM (3) | Yalnız metadata yedeği alınır (`metadata_only`). Kubernetes worker'ları için politika varsayılanı `exclude`'dur (D-16). |
| Multiattach volume | Set başına bir kez yedeklenir ve `shared_with` alanı tutulur. Restore'da çoklu attach yapılır. |
| Şifreli volume (`encryption_key_id`) | MVP'de **atlanır** ve raporlanır. Ham okuma yalnız LUKS şifreli metni verir; anahtar kopyalama akışı Faz 3'tedir (V-15). |
| Flavor ephemeral/swap > 0 | Volume verisi yedeklenir; ephemeral veri yedeklenmez. Durum uyarıyla raporlanır. |
| Volume büyütüldü | Manifest yeni boyuta uzatılır. Diff yeni bölgeyi kapsar. |
| Retype / migrate | `image_id` ya da provider değişir; tam okuma yapılır. |
| VM migrate oldu | Agent domain'i bulamaz. Host yeniden çözülür ve deneme tekrarlanır. |
| Yedek sırasında volume veya VM silindi | Set `skipped` olarak işaretlenir. Metadata kaydı korunur; yetim retention başlar (§14). |
| Bir volume'un transferi başarısız oldu | **Set bütünüyle başarısız sayılır.** Kısmi commit yapılmaz. Yeni noktalar bırakılır, base korunur ve VM tekrar kuyruğa girer. |

---

## 8. Tutarlılık politikası

| Politika | Davranış | Kullanım |
|---|---|---|
| `require` | Freeze başarısız olursa üç kez denenir (10 dk arayla). Sonra yedek **başarısız** sayılır ve alarm üretilir. Crash-consistent'a düşülmez. | Veri ve WAL'ı ayrı disklerde olan DB VM'leri, çok diskli uygulamalar |
| `prefer` (varsayılan) | Freeze denenir; olmazsa crash-consistent alınır ve `consistency=crash` ile uyarı yazılır. | Genel filo |
| `crash` | Freeze denenmez. | QGA'sız veya gecikmeye hassas VM'ler |
| `offline` (otomatik) | VM SHUTOFF ise kendiliğinden uygulanır. | — |

**Linux ön koşulları:**
1. Guest'te `qemu-guest-agent` kurulu ve çalışır olmalı.
2. Instance'ın kayıtlı imaj özelliği `hw_qemu_guest_agent=yes` olmalı. Bu, VM SHUTOFF iken `nova-manage image_property set <uuid> --property hw_qemu_guest_agent=yes` ile ayarlanır, ardından VM başlatılır (B-05, ⚠ÜD).
3. Aynı özellik kök volume'un `volume_image_metadata`'sına da yazılmalı. Böylece restore edilen VM'ler kanalı doğrudan alır.
4. Uygulama hook'ları için `/etc/qemu/fsfreeze-hook` ve `fsfreeze-hook.d/` kullanılır:
   - MySQL/MariaDB: flush ve read lock alan hook.
   - PostgreSQL: WAL sayesinde dosya sistemi tutarlılığı yeterlidir; hook opsiyoneldir.

**Windows ön koşulları:**
1. virtio-win guest tools kurulmalı: QGA ve "QEMU Guest Agent VSS Provider" servisi.
2. VSS writer'ları uygulama tutarlılığını sağlar (ör. SQL Server).
3. VSS, I/O'yu ~10 s'den uzun tutamaz (B-06). Bu yüzden Windows VM'lerde Mode B pratikte zorunludur; Mode A'da pencere ölçülmelidir (V-13, V-14).

**Kesin thaw garantisi:**
1. Agent, freeze'i başlatırken yerel bir zamanlayıcı kurar. Deadline dolunca orchestrator'dan bağımsız olarak thaw yapar.
2. Agent dondurduğu domain'leri `/var/lib/osb-agent/frozen.json` dosyasına yazar. Yeniden başladığında bu listedekileri `guest-fsfreeze-status` ile kontrol edip çözer. Başka araçların dondurduğu domain'lere dokunmaz.
3. Freeze süresi bir histogram olarak izlenir. Deadline aşımında alarm üretilir.

**Çok diskli VM uyarısı:** Freeze olmadan alınan noktalar diskler arasında ms ile saniye arasında farklı anlara düşer. Veri ve WAL ayrı disklerdeyse bu bir risktir. Bu tür VM'ler `require` politikasına alınmalıdır. RBD group snapshot ile atomik nokta alma Faz 3'te araştırılacaktır.

**Kayıt:** Her set şu alanları taşır: `consistency`, `freeze_ms`, `qga_version` ve hata nedeni (`no_channel`, `agent_timeout`, `freeze_error`, `vss_timeout`).

---

## 9. Restore akışı

### 9.1 Restore modları

| Mod | Ne yapar | Tahribat | Onay |
|---|---|---|---|
| **R1** `volume-new` | Volume(lar)ı hedef projede yeni volume olarak oluşturur. | Yok | Operatör |
| **R2** `instance-new` (varsayılan) | Yedekten yeni bir VM kurar (aynı ya da izole ağda). | Yok | Operatör |
| **R3** `instance-replace` | R2'yi uygular ve orijinal VM'in **portlarını taşır** (IP, MAC, FIP ve SG korunur). Eski VM SHUTOFF olarak geri dönüş için bekletilir. | Düşük (geri alınabilir) | Dört göz |
| **R4** `volume-inplace` (veri diski) | Yeni volume'a restore eder; eskisini detach edip yenisini attach eder. Eski volume N gün saklanır. | Düşük (geri alınabilir) | Dört göz |
| **R5** `root-inplace-ceph` | VM'i durdurur, `osb-prerestore` güvenlik snapshot'ı alır, kök RBD'nin üzerine yazar ve VM'i başlatır. **Instance UUID aynı kalır.** | Orta (güvenlik snapshot'ı ile geri alınabilir) | Dört göz |
| **R6** `raw-export` | Manifesti ham ya da qcow2 dosyaya çıkarır. OpenStack ve katalog gerektirmez (§9.8). | Yok | `osb_admin` |
| **R7** `file-level` (Faz 3) | Restore edilen volume'u kiracı projesindeki bir yardımcı VM'e salt-okur bağlar. | Yok | Operatör |

### 9.2 Plan / apply

**Plan girdisi:**
- `set_id`, mod ve hedef proje.
- Ağ eşlemesi: orijinal ağ → hedef ağ.
- IP politikası: `keep_if_free` ya da `new`.
- FIP politikası: `reassociate_if_free`, `allocate` ya da `none`.
- Flavor politikası: `same_id`, `match_spec` ya da `create_private`.
- `keypair: none | recreate`
- `user_data: omit | include` — varsayılan `omit` (B-08).
- `server_group: same_if_exists | recreate | none`
- Ad soneki (`-restored-<tarih>`).
- `start_after`.

**Plan çıktısı:**
- Oluşturulacak ve değiştirilecek kaynakların tam listesi.
- Kota kontrolü:
  - Cinder: volume, gigabytes ve tip bazında gigabytes.
  - Nova: instance, çekirdek ve RAM.
  - Neutron: port ve FIP.
- Çakışmalar: IP veya MAC kullanımda, FIP başka projede, SG/flavor/keypair eksik.
- Tahmini süre: yazılacak bayt / throughput.
- `plan_hash`

**Apply kuralları:**
- Apply yalnız aynı `plan_hash` ile ve onaylarla çalışır. Ortam değiştiyse yeniden planlama istenir.
- Planın ömrü 24 saattir.
- Her adım `restore_step` journal'ına yazılır.
- Her adımın bir idempotency anahtarı vardır.
- Oluşturulan her kaynak `osb-op=<op_id>` ile etiketlenir (metadata ya da tag).
- Hata durumunda iki seçenek vardır:
  - kaldığı yerden devam;
  - `rollback`: oluşturulanlar ters sırada silinir.

### 9.3 Instance metadata matrisi (neler alınabilir, neler alınamaz)

| Öğe | Kaynak | Mikro sürüm / yetki | Restore'da kullanımı | Not |
|---|---|---|---|---|
| Flavor tam spec + extra_specs | Nova server show `flavor` | 2.47+ | Aynı id varsa o; yoksa spec eşleşmesi; o da yoksa private flavor | Flavor silinmiş olabilir. |
| Kök disk ve boot sırası | `OS-EXT-SRV-ATTR:root_device_name` ile attachment `device` eşleşmesi | 2.3+, admin | Kök → `boot_index 0`, diğerleri −1 | `boot_index` API'de yoktur; root_device_name eşleşmesinden türetilir. |
| Volume, device, tag, delete_on_termination, attachment_id | `os-volume_attachments` | 2.70 / 2.79 / 2.89 | BDM v2 ve **orijinal device sırasıyla** attach | fstab'da UUID yoksa cihaz sırası önemlidir. |
| Volume tipi, boyut, bootable, metadata | Cinder volume show | 3.63+ | Aynı tip ya da backend'in kanonik tipi | `cinder-rbd`/`cinder_rbd` backend adına göre normalize edilir. |
| `volume_image_metadata` (hw_*, os_type, firmware, machine type, disk bus, QGA) | Cinder volume | — | `os-set_image_metadata` ile kök volume'a yazılır | **Eksikse UEFI ve Windows (disk bus) VM'ler açılmayabilir.** |
| user_data | `OS-EXT-SRV-ATTR:user_data` | 2.3+, admin | Varsayılan **verilmez** | Cloud-init yeniden çalışır (B-08). |
| Keypair public key | `GET /os-keypairs/{name}?user_id=` | 2.10+, admin | Opt-in; yeni ad | Keypair kullanıcıya aittir ve silinmiş olabilir. Anahtar zaten diskin içindedir. |
| Server metadata, tags, description, hostname | Server show | 2.26 / 2.19 / 2.90 | Aynen | — |
| Server group (policy ve üyelik) | Server show `server_groups` + `os-server-groups` | 2.71+ | Grup varsa ona; yoksa yeniden oluşturulur | 3 host'ta anti-affinity kısıtlıdır. |
| AZ, host | Server show | admin | AZ (`nova`); host yalnız bilgi amaçlı | — |
| config_drive | Server show | — | Aynı | İçeriği Nova yeniden üretir. |
| Port: ağ, subnet, fixed IP, MAC, SG, vnic_type, port_security, allowed_address_pairs, qos_policy_id, dns_name, extra_dhcp_opts | Neutron `ports?device_id=` | admin | R2'de yeni port (IP boşsa aynı); R3'te port taşıma | MAC ayarlamak admin politikası gerektirir. Orijinal port yaşarken aynı MAC **kullanılmaz**. |
| SG tanımları ve kuralları | Neutron security-groups/rules | — | Yoksa yeniden oluşturulur (`osb-` önekiyle) | `remote_group_id` referansları çözülmelidir. |
| Floating IP | Neutron `floatingips?port_id=` | — | Boştaysa aynı FIP; değilse yeni ya da yok | FIP başka bir projeye geçmiş olabilir. |
| Volume QoS | Volume type `qos_specs` | admin | Tip üzerinden | — |
| trusted certs, lock durumu | Server show | 2.63 / 2.73 | Bilgi amaçlı | — |
| Ephemeral/swap verisi | — | — | **Yedeklenmez** (MVP) | Uyarı verilir. |
| UEFI NVRAM, vTPM | Host ve Barbican | — | **Alınamaz** | Tatbikatta test edilir (B-10). |
| Diğer scheduler hint'leri, libvirt XML, PCI atamaları | API'de yok | — | Flavor extra_specs'ten yeniden türetilir | — |

**Metadata'nın tutarlılığı:**
- Metadata, freeze öncesinde (T1) yakalanır ve noktadan sonra (T6) attachment'lar yeniden karşılaştırılır.
- SG ve flavor gibi paylaşılan nesneler proje bazında günlük olarak ayrıca dökülür. Böylece silinmiş bir SG bile yeniden kurulabilir.
- **Hızlı kazanç (Faz 0):** Tüm server, port, volume, flavor, SG ve keypair envanteri günlük JSON olarak şifreli S3'e alınır. Veri yedeği başlamadan bile manuel kurtarmayı mümkün kılar.

### 9.4 Yeni instance (R2) adımları

1. **Kimlik:** Hedef projeye scope'lanmış bir token alınır. `osb-svc` kullanıcısının hedef projede rolü olmalıdır (§11.2).
2. **Volume'lar oluşturulur:** Tip ve boyut orijinalle aynıdır; ad `<orig>-osb-<op>`, metadata `osb-op`.
3. **Veri yazılır:**
   - **Ceph:** Orchestrator, restore-helper'a imzalı bir *write grant* verir: `{op_id, volume_id, rbd_image, size, plan_hash, exp}`.
     - Helper Cinder'dan iki şeyi kendisi doğrular: volume `available` durumunda mı ve `osb-op` metadata'sı eşleşiyor mu.
     - Ardından imajı RW açar ve `lock_acquire(EXCLUSIVE)` alır.
     - S3'ten paralel GET → decrypt → doğrulama yapar; aio ile yazar.
     - Yeni RBD imajı thin olduğu için ZERO chunk'ları atlar.
   - **NetApp:** Block worker yeni LUN'u FC üzerinden RW bağlar ve yazar. Yeni thin LUN'da ZERO bölgeler atlanabilir (V-11).
4. **Doğrulama:** Decrypt sırasında her chunk'ın id'si kontrol edilir. Yazılan alandan %1 örnek geri okunup karşılaştırılır. Tatbikatta tam geri okuma yapılır ve `volume_digest` eşleşmesi aranır.
5. **Kök volume:** `bootable=true` ve kayıtlı `volume_image_metadata` ayarlanır.
6. **Portlar** oluşturulur: ağ ve subnet, uygunsa fixed IP, SG'ler (yoksa yeniden oluşturulur), vnic_type, QoS, allowed_address_pairs ve port_security.
7. **Opsiyonel:** Keypair ve server group.
8. **Server create:** Flavor; BDM v2 (kök `boot_index 0`, `source=volume`); port id'leri; metadata, tag ve description; config_drive; AZ; grup hint'i. Veri diskleri orijinal device sırasıyla attach edilir.
9. **ACTIVE beklenir.** FIP politikaya göre ilişkilendirilir.
10. **Açılış kontrolü:**
    - Konsol logunda kernel/boot işareti aranır.
    - QGA varsa `guest-ping` yapılır.
    - Opsiyonel olarak izole ağda TCP/22 veya 3389 kontrol edilir.
11. **Rapor:** Süre, bayt, doğrulama sonucu ve oluşturulan kaynaklar raporlanır.

### 9.5 Replace ve in-place

**R3 — instance-replace:**
1. Yeni VM izole bir porta sahip olarak ya da portsuz kurulur.
2. Eski VM durdurulur.
3. Portları `interface detach` ile eski VM'den alınıp `interface attach` ile yeni VM'e bağlanır. IP, MAC, FIP ve SG korunur.
4. Eski VM `osb-replaced` etiketiyle SHUTOFF olarak N gün bekletilir.
5. Geri dönüş: portlar ters yönde taşınır.

Yalnız instance UUID değişir. Faturalama ve izleme referansları için not düşülür.

**R4 — volume-inplace (veri diski):** Yeni volume'a restore edilir → eski volume detach edilir → yenisi aynı device'a attach edilir → eski volume N gün saklanır. Üzerine yazma yapılmaz.

**R5 — root-inplace (yalnız Ceph):**
1. Nova `lock` uygulanır (`locked_reason`, 2.73).
2. VM durdurulur.
3. Kök imajın güncel hâli `backup.osb-prerestore-<op>.snap.<ts>` adıyla agent tarafından snapshot'lanır.
4. Helper imajı açar ve `lock_acquire(EXCLUSIVE)` alır. Yazma sırasında manifestteki `ZERO` aralıklarına `write_zeroes`/discard uygular; **in-place yazmada sıfırlar atlanmaz.**
5. VM başlatılır ve `unlock` yapılır.
6. Geri alma, güvenlik snapshot'ına rollback ile yapılır.

**Kök volume değiştirmek için kullanılmayanlar:**
- `swap_volume` (B-09).
- Nova 2.93 rebuild (BFV): Önce yedeği bir Glance imajına çevirmeyi gerektirir. Yavaştır, çift alan kaplar ve Glance'e bağımlıdır; reddedildi.
- NetApp kök volume için in-place yapılmaz; R3 kullanılır.

### 9.6 Boot sırası

- **VM içinde:** `root_device_name` ile eşleşen volume `boot_index 0` olur. Veri diskleri orijinal `device` sırasıyla eklenir.
- **VM'ler arasında (Faz 2):** `restore_group` tanımlanır. Örnek: `[db-*] → wait(qga_ping | tcp:5432) → [app-*] → [lb/web-*]`. Her adımın sağlık kapısı ve timeout'u vardır. Grup planı tek bir onayla uygulanır.

### 9.7 RTO hedefi ve hız

200 GiB'lık bir VM için kabaca:
- S3 paralel GET (32 akış): ~500–1000 MB/s.
- RBD aio yazma: ~500 MB/s.
- Toplam: ~7–10 dk veri ve 2–5 dk boot. Plan ve onayla birlikte **RTO < 1 saat** hedeflenir.

Restore işleri yedeklerden önceliklidir ve bant genişliğini yedeklerin önüne geçerek kullanır.

### 9.8 OpenStack'siz ve katalogsuz restore (DR)

```
osb-cli restore-raw --bucket s3://osb-<domain> --set <set_id> --volume <volume_id> \
                    --key-escrow /secure/mk.age --out /restore/vol.raw   # veya --to-rbd pool/image
```

- Gereken tek şeyler S3 erişimi ve escrow'daki ana anahtardır.
- Bu yol kontrol düzlemi ya da Ceph kümesi kaybında kullanılır.
- `osb catalog rebuild` komutu set ve manifest nesnelerini tarayıp katalogu yeniden kurar.

---

## 10. Kısıtların çözümü

| Kısıt | Etki | Çözüm |
|---|---|---|
| Snapshot sayı kotası (proj-B: 58 volume / 50 snapshot; proj-C: 24 / 10) | Base snapshot stratejisi kotaya takılır. | **Mode B kota tüketmez.** Mode A gerekirse kota en az şu kadar olmalı: `N_volume + (VM başına azami volume × eşzamanlı VM)` (⚠ÜD, D-05). NetApp'te snapshot geçicidir; tepe değeri eşzamanlı volume sayısı kadardır. |
| Snapshot GB kotası (B-02) | Kalıcı base snapshot GB kullanımını iki katına çıkarır. | Mode B. Mode A'da `no_snapshot_gb_quota=True` (⚠ÜD; global semantik değişikliği, faturalamayla birlikte değerlendirilmeli) ya da GB kotası artırımı. |
| Volume silme engeli (B-01) | Kiracı volume'u silemez; Nova sessizce artık volume bırakır. | Mode B; Cinder silme sırasında temizler (V-02). Mode A'da: reconciler silinen VM'lerin `delete_on_termination` volume'larındaki OSB snapshot'ını siler ve kiracıya rapor verir. Volume'a dokunmaz. |
| Cinder incremental zincir koruması | Parent backup silinemez. | OSB zincir kullanmaz; manifestler self-contained'dir. |
| FC'siz host'a düşen iş / `backup_use_same_host=False` | NetApp attach hatası (`wwpns`) | OSB'de block worker'lar yalnız `fc_host` capability'li cmp'lerde çalışır. Geçiş döneminde `cinder-backup` için `backup_use_same_host=True` (⚠ÜD). |
| Glance klon parent'ı | Restore'un parent'a bağımlı kalma riski | `include_parent=True`; restore edilen volume self-contained (flatten edilmiş) olur. Faz 3: parent imaj hâlâ ve aynı haliyle duruyorsa (`parent_spec` + imaj checksum) önce klon alınıp yalnız fark yazılır. |
| Glance NFS askıda | Upload veya scrubber kilitlenebilir. | OSB, Glance API'sine ne yedekte ne restore'da bağımlıdır; NFS'e dokunmaz. |
| Ceph istemcisi 20.2 / küme 18.2.2 | Uyumsuzluk riski | OSB worker'ları **Reef 18.2.x container imajını** kullanır; LXC'deki istemci kullanılmaz (V-06). Mevcut LXC'deki uyumsuzluk ayrı bir platform maddesidir. |
| Dev build'ler (B-04) | API davranışı sürümden sapabilir. | Açılışta version discovery yapılır; mikro sürümler sabitlenir (≤ 2.90 Nova, ≤ 3.66 Cinder). Her gün salt-okur kontrat testi çalışır (§17). |
| RabbitMQ EOL | — | OSB'nin AMQP bağımlılığı yoktur. |
| Monasca kapalı | Alarm yok | Prometheus exporter + Alertmanager. Loglar mevcut OpenSearch'e gider. |
| HAProxy drift (ctrl02/03) | VIP failover'da API kopabilir. | OSB iç VIP'i kullanır; retry ve devre kesici vardır. Bakım penceresinde failover testi yapılır (V-23). |
| RGW için HAProxy frontend'i yok | — | İstemci tarafında 4 RGW endpoint'i sağlık kontrolüyle round-robin kullanılır. Üretim değişikliği gerekmez. |
| Masakari evacuation çalışmıyor | Host arızasında otomatik kurtarma yok | B-12: önce `nova evacuate` runbook'u; restore son çaredir. |
| 36 privileged LXC, cgroup limitsiz | Kaynak çekişmesi | OSB LXC'de çalışmaz (A10). Ara çözüm zorunluysa sıkı cgroup limitleri uygulanır. |
| Şifreli ve multiattach volume'lar | — | Tespit edilir ve raporlanır. Şifreli volume'lar MVP'de hariçtir; multiattach desteklenir. |
| Volume tipi adı tutarsızlığı | — | Tip, `volume_backend_name` üzerinden normalize edilir. Cinder host string'indeki `#cinder_rbd` pool değil backend/pool adıdır; RBD pool'u `cinder-volumes`'tur. |

---

## 11. Güvenlik

### 11.1 Tehdit modeli (özet)

| Tehdit | Kontrol |
|---|---|
| VM içinde fidye yazılımı | Yedekler kiracının erişim alanı dışındadır; RGW Keystone'a bağlı değildir. Geçmiş noktalar değişmez ve offsite'ta Object Lock vardır. |
| mgmt ağında HTTP dinleme (token ve kimlik bilgisi sızıntısı) | Application credential, kısa token ömrü, yalnız iç VIP. S3 SigV4 secret göndermez. Veri istemci tarafında şifrelidir. HAProxy TLS projesi (⚠ÜD, D-15). |
| `backup01` ele geçirilmesi | Tüm tenant verisi okunabilir (yedeklemenin doğası gereği). **Ceph'te yazma ya da silme yapılamaz** (salt-okur anahtar). Offsite silinemez (pull + Object Lock). Yerelde yalnız `osb-gc` silebilir; kimliği ayrı tutulur ve silme bütçesiyle sınırlanır. |
| Hypervisor ele geçirilmesi | Mevcut durumdan farklı değildir (`client.cinder` zaten oradadır). Agent yalnız `backup.osb-*` snapshot'larını siler. |
| İçeriden kötüye kullanım | Dört göz onayı, hash zincirli audit, offsite immutability. |
| Anahtar kaybı | İki ayrı yerde escrow; üç ayda bir açma tatbikatı. |

### 11.2 Keystone ve RBAC

```bash
# ⚠ÜD (düşük risk) — örnek; değerler kurulumda belirlenir
openstack project create osb-service --description "OSB servis projesi"
openstack user create osb-svc --project osb-service --password-prompt   # parola yalnız kurulumda
openstack role add --user osb-svc --project osb-service admin
openstack role add --user osb-svc --project <korunan-proje> admin       # her korunan proje için (Faz 1)
openstack application credential create osb-<proje> --role admin       # proje başına; --unrestricted YOK
```

- **Gerçekçi not:** Caracal'da (legacy ya da yeni default'larla) herhangi bir projedeki `admin` rolü pratikte bulut-admin demektir. `all_tenants` listesi, `user_data`, başka kullanıcının keypair'i ve projeler arası snapshot'tan volume oluşturma bu yetkiyi gerektirir. Faz 1'de bu kabul edilir. Telafi edici kontroller şunlardır:
  - Kimlik bilgisi yalnız `backup01`'dedir (`0600`).
  - Proje başına app credential kullanılır; ayrı ayrı döndürülebilir ve iptal edilebilir.
  - Her çağrı audit'e yazılır.
- **Faz 3:** oslo.policy override'ları (`nova_policy_overrides`, `cinder_policy_overrides`, `neutron_policy_overrides`) ile iki rol tanımlanır:
  - `osb_reader`: envanter ve metadata okuma;
  - `osb_restore`: oluşturma.

  Restore kimliği ayrı tutulur ve yalnız onaylı bir operasyon sırasında açılır. Önce `enforce_new_defaults`/`enforce_scope` durumu doğrulanmalıdır (V-19). App credential access rules, keystonemiddleware'de `service_type` ayarı olmadan çalışmayabilir (V-18).
- **Operatör girişi:** Keystone (keystonemiddleware) kullanılır. `osb-service` projesinde `osb_viewer`, `osb_operator` ve `osb_admin` rolleri bulunur. Tahribatlı restore için ikinci bir `osb_admin` onayı gerekir.

### 11.3 Ceph cephx

```bash
# ⚠ÜD (düşük risk) — lab'da doğrulandıktan sonra
# Yedek worker'ları: SALT OKUR (backup01/02)
ceph auth get-or-create client.osb-ro \
  mon 'profile rbd' \
  osd 'profile rbd-read-only pool=cinder-volumes, profile rbd-read-only pool=glance-images, profile rbd-read-only pool=nova-vms'

# Nokta oluşturma/silme (Mode B): yalnız cmp01-03'teki freeze-agent
ceph auth get-or-create client.osb-snap \
  mon 'profile rbd' \
  osd 'profile rbd pool=cinder-volumes'

# Restore yazma: yalnız restore01
ceph auth get-or-create client.osb-restore \
  mon 'profile rbd' \
  osd 'profile rbd pool=cinder-volumes, profile rbd-read-only pool=glance-images'
```

- **Admin keyring hiçbir OSB host'unda bulunmaz.**
- Keyring'ler `0600` izinlidir ve bileşenin kendi Unix kullanıcısına aittir.
- `client.osb-snap` RW bir anahtardır. Ancak yalnız zaten `client.cinder` tutan hypervisor'larda durur ve kullanımı agent'ın allowlist'iyle sınırlanır (bağlı disk + `backup.osb-*` adı).
- Daha dar bir cap varyantı lab'da denenecek: yalnız header ve object-map nesnelerine yazabilen, data nesnelerine yazamayan bir cap (V-30).
- **Anahtar rotasyonu:** Yılda bir ya da şüphe anında `ceph auth caps`/yeniden oluşturma yapılır. Rotasyon için hazırlanan runbook'ta servis kesintisi yoktur.

### 11.4 RGW (S3)

```bash
# ⚠ÜD — RGW'nin henüz kullanıcısı yok (B-11)
radosgw-admin user create --uid=osb-owner  --display-name="OSB bucket owner"   # anahtarı çevrimdışı saklanır
radosgw-admin user create --uid=osb-writer --display-name="OSB writer"
radosgw-admin user create --uid=osb-reader --display-name="OSB reader (restore/replicator)"
radosgw-admin user create --uid=osb-gc     --display-name="OSB GC"
radosgw-admin quota set --quota-scope=user --uid=osb-owner --max-size=<D-02 sonrası>
radosgw-admin quota enable --quota-scope=user --uid=osb-owner
```

- **Bucket'ların sahibi `osb-owner`'dır.** Diğer yetkiler bucket policy ile verilir:
  - `osb-writer`: Put, Get ve Head (List'siz); **Delete yok** ve PutBucketPolicy yok.
  - `osb-reader`: Get ve List.
  - `osb-gc`: Delete ve List.

  Writer ele geçirilse bile silme yapamaz ve policy'yi değiştiremez.
- **Ayrı placement:** `osb-ec` placement'ı (`osb.rgw.buckets.data` EC 2+2, failure-domain=host; index ve non-ec pool'ları replicated). Pool'a `max_bytes` kotası konur. Default placement'a dokunulmaz (V-17).
- **TLS:** Beast frontend'e `ssl_port=8443` eklenir. OSB'nin 8080'e bağımlılığı olmaz.
- **Keystone'a object-store endpoint kaydı yapılmaz.** Kiracılar RGW'ye bu yoldan erişemez.

### 11.5 Şifreleme ve anahtar yönetimi

```
MK  (ana anahtar, 256 bit) — backup01: /etc/osb/secrets/mk (0400); escrow: 2 kasa (çevrimdışı) + offsite
 └─ DEK(project)  — MK ile sarılı; katalogda ve s3://osb-<domain>/keys/ altında
     └─ HKDF → K_id (dedup), K_enc (chunk), K_man (manifest/set)
```

- **MK rotasyonu:** Yalnız DEK'ler yeniden sarılır; ucuzdur.
- **DEK rotasyonu:** Yeni chunk'lar yeni `key_id` ile yazılır. Dedup alanı değiştiği için bir sonraki yedek tam yükleme yapar. Bu yüzden nadiren uygulanır.
- **Proje silinince:** Retention dolduktan sonra DEK imha edilir (kripto-silme).
- **Barbican MK için kullanılmaz.** Sebepleri:
  - DR anında OpenStack'e bağımlılık oluşur.
  - simple_crypto KEK'i config dosyasında durur.
- **Tatbikat:** MK escrow açma testi üç ayda bir yapılır.

### 11.6 Ağ ve host sertleştirme

- **OSB host'ları (`backup01`, `restore01`):**
  - nftables ile gelen trafik varsayılan olarak reddedilir. İzin verilenler: bastion'dan SSH ve operatör ağından API.
  - SSH yalnız anahtarla yapılır; parola kapalıdır.
  - `unattended-upgrades` açıktır.
- **systemd sandbox:** `ProtectSystem=strict`, `NoNewPrivileges`, `PrivateTmp`, en dar `CapabilityBoundingSet` ve bileşen başına ayrı Unix kullanıcısı.
- **Compute'taki agent ve block worker:** Port dinlemez; yalnız dışa doğru mTLS ile bağlanır. Firewall değişikliği gerekmez.
- **ONTAP:** OSB'nin ONTAP REST kullanımı (Faz 3) yalnız HTTPS ve SVM kapsamlı, dar bir rolle yapılır. Mevcut Cinder sürücüsünün HTTP:80 kullanımı ayrı bir platform maddesidir.
- **Audit:**
  - Her API çağrısı, restore, anahtar kullanımı ve silme `audit` tablosuna yazılır (hash zinciri).
  - Kayıtlar OpenSearch'e gönderilir ve günlük olarak offsite'a kopyalanır.

---

## 12. Ölçek, performans, kapasite

### 12.1 İş yükü

| Kalem | Değer |
|---|---|
| Korunacak VM / volume | ~178 / ~329 |
| Ceph gerçek veri | ~13 TiB (+ ilk geçişte ~1–2 TiB Glance parent okuması) |
| NetApp gerçek veri | Bilinmiyor; 9,6 TB allocate. Varsayım: 4–6 TiB (V-10) |
| Günlük değişim (varsayım) | %1–5 → Ceph'te 130–650 GiB okuma |
| OSD | 20 × ~6,99 TiB. Bu boyut 7,68 TB SSD sınıfına işaret eder (çıkarım; doğrulanmalı). Bu durumda sınırlayıcı etken donanım değil, üretim etkisi olur. |

### 12.2 Süreler (tahmin; V-20 ile ölçülecek)

| İş | Hesap | Süre |
|---|---|---|
| İlk Ceph geçişi (~15 TiB okuma) | 500 MB/s ↔ 1 GB/s | ~9 sa ↔ ~4,5 sa → **5–7 gece, proje dalgalarıyla** (gecede ~2–3 TiB) |
| Günlük Ceph incremental | 130–650 GiB / 500 MB/s | 5–25 dk |
| NetApp günlük tam okuma (9,6 TB'ın tamamı okunursa) | 3 host × 500 MB/s | ~1,8 sa. Tek host ile ~5,3 sa. Sıfır bölgeleri CPU'yu yormaz. |
| 200 GiB restore | §9.7 | ~10–15 dk + onay → < 1 sa |
| Offsite ilk seed (10–15 TiB) | 1 Gbps ↔ 10 Gbps | ~28–42 sa ↔ ~3–5 sa |
| Offsite'tan tam DR (15 TiB) | 1 Gbps | ~42 sa+ → **link bant genişliği DR RTO'sunu belirler** (D-03) |

### 12.3 Eşzamanlılık ve throttle (başlangıç değerleri)

| Parametre | Değer |
|---|---|
| Eşzamanlı freeze | Toplam 4 VM; host başına 2 |
| Mode A Cinder snapshot çağrısı | Backend başına 8 eşzamanlı |
| Ceph okuma | Worker başına 8 volume × 4 uçuşta 8 MiB okuma (~256 MiB tampon) |
| S3 PUT | Worker başına 64 eşzamanlı |
| Global bant | Gündüz 300 MB/s, gece 1 GB/s (D-17). AIMD: Ceph istemci p99 gecikmesi eşiği aşarsa yarıya iner. |
| Block worker (cmp) | Host başına 1 süreç, 2 volume. ≤ 300 MB/s, 4 çekirdek, 4 GiB RAM (cgroup). |
| CPU bütçesi | 1 GB/s için: HMAC-SHA256 (SHA-NI ile ~1,5–2 GB/s/çekirdek) 1 çekirdek; zstd-3 (~400–500 MB/s/çekirdek) 2–3 çekirdek; AES-GCM 0,3 çekirdek |
| Kapasite ve sağlık guard'ı | `HEALTH_ERR`, `nearfull` ya da raw > %60 olduğunda **yeni yedek başlatılmaz**. Restore ve GC'ye izin verilir. %55'te uyarı. |

### 12.4 Kapasite modeli (tahmin)

| Kalem | Tahmin | Not |
|---|---|---|
| Veri azaltma (proje içi dedup, zstd, sıfır) | %30–50 | V-20 (estimate modu) |
| İlk full sonrası mantıksal (Ceph + NetApp) | 9–13 TiB | (13 + 5) × 0,5–0,7 |
| 7 günlük yerel retention ilavesi | +0,7–2 TiB | Günde 100–300 GiB benzersiz yeni veri |
| **Yerel tier mantıksal** | **10–15 TiB** | — |
| EC 2+2 raw (×2) | 20–30 TiB | 3× replikasyonla 30–45 TiB olurdu; tavana dayanır |
| Küme raw kullanımı (sonrası) | 43 + 20–30 = 63–73 TiB / 140 → **%45–52** | — |
| Güvenli tavan (B-07) | ~%64 ≈ 89 TiB | — |
| Üretim büyümesi için kalan | 16–26 TiB raw | Pool kotası ile korunur |
| RGW nesne sayısı | ~3–4,5 milyon (+25–75 bin/gün) | Büyük bucket'lar önceden shard'lanır |
| Katalog `chunk` satırı | ~3–4,5 milyon | PostgreSQL için küçük bir hacim |

**Sonuç:**
- Yerel tier 7 (en fazla 14) günle sınırlanır. Uzun retention offsite'tadır.
- EC 2+2 kullanılmazsa yerel retention 3–5 güne düşmelidir.
- **Chargeback:** Cinder'ın `backups` ve `backup_gigabytes` kotaları OSB'ye uygulanmaz, çünkü OSB Cinder backup nesnesi oluşturmaz. OSB proje bazında korunan mantıksal boyutu ve depolanan baytı raporlar. Faturalama entegrasyonu D-09'dadır.

---

## 13. Hata yönetimi, idempotency, retry

### 13.1 Görevler ve kilitler

- **Görev kiralama (lease):** Lease 60 s'dir, heartbeat 15 s'de bir gönderilir. Lease düşerse görev yeniden kuyruğa girer. En fazla 5 deneme yapılır, sonra dead-letter kuyruğuna alınır ve alarm üretilir.
- **Kilitler:**
  - Volume başına PG advisory lock vardır; aynı volume'da yedek ile restore çakışmaz.
  - VM başına freeze kilidi vardır.
- **Retry:** Üstel geri çekilme ve jitter uygulanır. Hatalar sınıflandırılır:
  - **Geçici:** 5xx, 409, timeout, EAGAIN.
  - **Kalıcı:** 404, kota, yetki.

  Servis başına devre kesici vardır (Cinder, Nova, RGW).

### 13.2 Idempotency anahtarları

| Adım | Anahtar ve tekrar davranışı |
|---|---|
| RBD noktası | `backup.osb-<set_id>.snap.*` ada göre aranır; varsa yeniden kullanılır. |
| Cinder snapshot / geçici volume | `osb-<set>-<vid>`, `osb-tmp-<set>-<vid>` ve metadata `osb:set`; ada göre aranır. |
| Chunk | İçerik adreslidir; yeniden PUT zararsızdır. Önce katalogda var mı diye bakılır. |
| Manifest / set | `set_id` anahtarı; aynı içerik yeniden yazılabilir. |
| Restore adımları | `restore_step.idem_key` ve kaynak etiketi `osb-op`. Kaynak varsa oluşturulmaz, bulunur. |

### 13.3 Hata senaryoları

| Senaryo | Davranış |
|---|---|
| Agent'a ulaşılamıyor / QGA yanıt vermiyor | `prefer` → crash-consistent devam. `require` → 3 deneme, sonra başarısız ve alarm. |
| Freeze başarılı, nokta alınamadı | Thaw yapılır (her koşulda); oluşan noktalar geri alınır; görev yeniden denenir. |
| Freeze sırasında orchestrator öldü | Agent deadline'da thaw yapar. Yeni lider, lease bitince görevi alır ve noktaları adlarından bulur. |
| Worker transfer ortasında öldü | Başka bir worker devralır. Yüklenmiş chunk'lar dedup sayesinde yeniden yüklenmez. |
| RGW düğümü düştü | Endpoint listesinden çıkarılır ve retry yapılır. Hepsi düşerse transfer bekler; noktalar korunur ve TTL'e kadar bekletilir. |
| Ceph degraded / nearfull | Guard devreye girer: yeni yedek alınmaz; restore'a izin verilir. |
| Mode A kota aşımı | Yedek başarısız olur ve alarm üretilir. **Otomatik kota artırımı yapılmaz.** |
| Base noktası kayıp | Tam okuma yapılır (dedup ile); uyarı verilir. |
| Katalog DB erişilemiyor | Scheduler durur. Çalışan transferler commit adımında bekler; S3 tutarlı kalır. |
| Katalog kaybı | `osb catalog rebuild` ile S3'ten kurulur, ardından mutabakat yapılır. |
| Offsite erişilemiyor | Yerelde devam edilir. Lag alarmı üretilir; yerel GC, offsite'a kopyalanmamış setleri silmez. |
| Restore yarıda kaldı | Journal'dan devam edilir ya da `rollback` yapılır. |
| Chunk bütünlük hatası (GCM veya HMAC) | Restore otomatik olarak offsite kopyadan okur. Scrub alarmı üretilir ve olay incelenir. |
| Uzun süre bekleyen nokta | Nokta TTL'i 24 saattir. Reconciler, katalogda karşılığı olmayan `backup.osb-*` snapshot'larını siler (yalnız `osb-` önekli olanları). |

---

## 14. Retention ve GC

1. **Süre dolumu (günlük):**
   - GFS kuralına göre süresi dolan setler `expired` olur.
   - Yerel silme için koşul: set offsite'a kopyalanmış olmalıdır (`offsite: required` ise).
   - Set nesnesi ve manifestler silinir (`osb-gc` kimliğiyle).
2. **Mark-and-sweep:**
   - **Mark:** Yaşayan tüm manifestler ve uçuştaki setler taranır; referans verilmeyen chunk'lar `condemned` olarak işaretlenir (`condemned_at`).
   - **Diriliş:** Dedup araması sırasında `condemned` bir chunk'a rastlanırsa, aynı transaction içinde chunk `live`'a döndürülür ve `last_ref_at` güncellenir.
   - **Sweep:** 48 saatten uzun süredir `condemned` olan ve yaşı en eski uçuştaki setin başlangıcından büyük olan chunk'lar silinir.
3. **Emniyet kuralları:**
   - Var olan bir VM'in son başarılı seti, süresi dolsa bile silinmez.
   - Silinen VM'ler için yetim retention uygulanır: en az 30 gün (politikaya göre 90). Restore ihtiyacının en büyük nedeni zaten VM silinmesidir.
   - `legal_hold` işaretli setler silinmez.
   - **Günlük silme bütçesi:** Depolanan baytın en fazla %10'u günlük olarak silinebilir. Aşım manuel onay gerektirir. Bu kural hatalı yazılmış bir retention'a ya da saldırıya karşı sigortadır.
   - GC'nin her zaman dry-run raporu vardır.
4. **Base nokta mutabakatı:** RBD'deki `backup.osb-*` snapshot'ları ile katalogdaki `base_point` kayıtları karşılaştırılır; yetim noktalar silinir.
5. **Offsite GC:** Ayrı ve offsite'ta çalışır. Object Lock süresi dolmadan silme yapılamaz. Lock uzatma taraması haftalıktır (§15).

---

## 15. Offsite ve DR

- **Model:** Pull. `osb-replicator` offsite'ta çalışır ve yerel RGW'yi yalnız `osb-reader` kimliğiyle okur. **Prod tarafında offsite'ta silme yetkisi olan bir kimlik yoktur.**
- **Immutability:** Offsite bucket'ında Object Lock (governance ya da compliance; D-03) vardır. Yüklemede `retain_until = now + L` uygulanır (L ≈ en uzun yerel retention + 7 gün).
  - Haftalık bir tarama, yaşayan setlerin referans verdiği chunk'ların lock'unu `now + L`'ye uzatır. Bu yaklaşık 3–4 milyon `PutObjectRetention` çağrısı eder (~6 çağrı/s sürekli).
  - Böylece eski ama hâlâ referans verilen chunk'lar da korunur.
- **Sıra:** Önce chunk'lar, sonra manifestler, en son set kopyalanır. Sonra doğrulama yapılır (HEAD + örnekli GET), `offsite_state=replicated` işaretlenir ve lag metriği güncellenir.
- **Hedef seçenekleri (D-03):**
  1. İkinci bir sahada MinIO ya da küçük bir Ceph/RGW (önerilen).
  2. Yurt içi bulut S3 (Object Lock'lu). **KVKK ve veri yerelliği değerlendirilmeli.**
  3. RGW multisite: realm/zonegroup dönüşümü gerektirir ve hedefin de Ceph olmasını şart koşar; bu yüzden tercih edilmez.
- **Escrow:** MK, katalog dökümleri ve audit kayıtları offsite'ta da bulunur (veri kimliklerinden ayrı erişimle).
- **Platform eki (Faz 1):** ctrl02'deki günlük Galera dump'ı, `/etc/openstack_deploy`, Ceph `ceph.conf`, monmap ve keyring'ler (şifreli) ile ONTAP config yedeği `s3://osb-system/platform/` altına alınır.

| Senaryo | Kaynak | RPO | RTO (hedef) |
|---|---|---|---|
| Tek VM silindi veya bozuldu | Yerel | ≤ 24 sa (politika) | < 1 sa |
| Fidye yazılımı (VM içi) | Yerel; daha eskisi offsite | Temiz son nokta | < 1–4 sa |
| Ceph pool veya küme kaybı | Offsite | ≤ 24 sa + replikasyon gecikmesi (p95 < 24 sa) | Linke bağlı: Tier-1 önce; 10 Gbps'te ~1 gün |
| Compute host kaybı | — (veri paylaşımlı depoda) | 0 | `nova evacuate` runbook'u (B-12) |
| Kontrol düzlemi kaybı | Platform yedeği + OSB ham restore | — | OSA yeniden kurulum; ayrı DR planı |

---

## 16. Gözlemlenebilirlik ve operasyon

- **Metrikler (Prometheus):**
  - Yedek durumu:
    - `osb_last_success_timestamp{server,project,policy}`
    - `osb_rpo_violation`
  - Throughput ve verim:
    - `osb_bytes_read_total`
    - `osb_bytes_uploaded_total`
    - `osb_dedup_ratio`
    - `osb_chunkstore_bytes{tier}`
  - Freeze:
    - `osb_freeze_duration_seconds` (histogram)
    - `osb_freeze_failures_total{reason}`
    - `osb_consistency_ratio{policy}`
  - Kapasite: `osb_ceph_raw_used_ratio` (mgr prometheus modülünden).
  - Offsite: `osb_offsite_lag_seconds`.
  - GC: `osb_gc_reclaimed_bytes`, `osb_gc_delete_budget_used`.
  - Restore ve tatbikat:
    - `osb_restore_duration_seconds`
    - `osb_drill_success`
    - `osb_scrub_errors_total`
- **Alarmlar:**
  - RPO ihlali (son başarılı yedek > 1,5 × RPO).
  - Freeze deadline aşımı.
  - Tutarlı yedek oranında düşüş.
  - Offsite lag > 36 sa.
  - Raw kullanım > %55.
  - Scrub hatası.
  - Dead-letter kuyruğu.
  - Katalog ile S3 arasında uyuşmazlık.
  - Kontrat testi hatası.
- **Raporlar:**
  - Haftalık koruma raporu: proje bazında korunan ve korunmayan VM'ler, tutarlılık dağılımı, QGA eksikleri.
  - Aylık tatbikat raporu.
- **Kill switch:** `osb pause [--project X]` komutu yeni işleri anında durdurur. Uçuştaki işler güvenli noktada durur ve noktalar TTL ile temizlenir.

---

## 17. Test, kabul, restore tatbikatı

### 17.1 Ortamlar

- **Lab:** OSA 29.0.2, Ceph 18.2.2 (en az 3 OSD host) ve tercihen ONTAP Select/Simulator.
  - FC lab'ı yoksa NetApp testleri üretim ONTAP'ta **ayrı bir test SVM ve test projesi** ile yapılır (⚠ÜD).
  - Lab'daki Nova sürümü, üretimdeki gerçek sürümle eşlenir (V-01).
- **Kontrat testleri (günlük, üretimde salt-okur):**
  - Kullanılan mikro sürümler ve alanlar mevcut mu?
  - Cinder RBD sürücü kaynak kodunda `_delete_backup_snaps` ve kalıp değişmiş mi? (venv dosya hash'i izlenir.)
  - RGW'de bucket policy ve placement durumu.

### 17.2 Test matrisi

- **Guest:** Ubuntu ve RHEL türevi (ext4, xfs, LVM); Windows Server 2019/2022 (VSS, SQL Server); MySQL ve PostgreSQL VM'leri.
- **Donanım ve önyükleme:** BIOS ve UEFI; virtio ve scsi disk bus.
- **Volume türleri:** Glance klonu ve flatten edilmiş volume; çok diskli VM; NetApp; karışık VM; multiattach; şifreli volume (tespit testi).
- **Yaşam döngüsü:** SHUTOFF VM; büyütülmüş volume; retype edilmiş volume; yedek sırasında migrate edilen VM; yedek sırasında silinen VM.
- **Kiracı işlemleri (Mode B'nin yan etkileri):** Volume silme, Nova `delete_on_termination`, extend, retype, migrate, revert, klon (V-03).
- **Restore:** R1–R6 tüm modlar. Cloud-init davranışı (V-26), UEFI açılışı (V-27), port taşıma (V-28).

### 17.3 Chaos testleri

- Freeze sırasında orchestrator ve agent `kill -9` edilir; ağ kesilir. **Beklenen: guest ≤ deadline içinde çözülür.**
- Transfer ortasında worker öldürülür.
- RGW düğümleri tek tek düşürülür.
- OSD `out` yapılır ve degraded durum oluşturulur.
- PostgreSQL yeniden başlatılır.
- Cinder ve Nova'ya 5xx enjekte edilir.
- Kota aşımı oluşturulur.
- Katalog silinir ve S3'ten yeniden kurulur.
- MK escrow'dan geri getirilir.
- Offsite kesintisi oluşturulur.
- GC'nin silme bütçesi tetiklenir.

### 17.4 Restore tatbikatı

- **Ortam:** Ayrı `osb-drill` projesi ve izole ağ (FIP yok, dışarı çıkış yok).
- **Haftalık otomatik tatbikat:** Rastgele seçilen ~%5 VM için R2 uygulanır:
  - digest ve açılış kontrolü yapılır;
  - VM otomatik silinir;
  - sonuç `drill` tablosuna yazılır.
- **Kapsama:** Her VM çeyrekte en az bir kez restore edilir. Tier-1 VM'ler ayda bir.
- **Yarıyıllık DR tatbikatı:** Offsite'tan ham restore (R6) ve katalogsuz senaryo.

### 17.5 Kabul ölçütleri (öneri; D-01 ile kesinleşir)

| Ölçüt | Hedef |
|---|---|
| Planlanan yedek başarı oranı (haftalık) | ≥ %99 |
| RPO uyumu (VM bazında) | ≥ %99 |
| QGA'lı VM'lerde tutarlı yedek oranı | ≥ %95 |
| Freeze penceresi p95 | Ceph < 2 s · NetApp/karışık < 8 s · Windows < 6 s |
| Freeze deadline aşımı | 0 |
| 200 GiB tek VM RTO p95 | < 1 sa |
| Tatbikat başarısı (açılış + digest) | %100; başarısız olan her vaka için kök neden analizi |
| Offsite lag p95 | < 24 sa |
| Scrub bütünlük hatası | 0 |
| Üretim etkisi (yedek penceresinde Ceph istemci p99 gecikme artışı) | < %20 |
| Kiracı tarafında yan etki (silme engeli, kota) | 0 |

---

## 18. `cinder-backup`'tan geçiş

1. **Paralel çalışma:** OSB `cinder-backup`'ı devralmaz; kiracıların self-servis Cinder backup'ı (Horizon/Skyline) açık kalır. Gelecekteki durumu D-13 ile belirlenir.
2. **Mevcut 7 yedek:** Faz 0'da her biri izole projede restore edilerek **doğrulanır.** Bu aynı zamanda ilk restore tatbikatıdır. Yedekler, OSB kapsamı retention süresini doldurana kadar korunur.
3. **Hatalı yedek (150 GB, `wwpns`):** Kök nedeni kaydedilir; `backup-reset-state` + silme uygulanır (⚠ÜD).
4. **Artık snapshot'lar:** `backup.<uuid>.snap.*` snapshot'ları envanterlenir (V-22). Yalnız karşılığı olan Cinder backup'ı silinmiş olanlar temizlenir. **`backup.osb-*` önekine asla dokunulmaz;** `cinder-backup`'ın kendi snapshot'larına da dokunulmaz.
5. **Birlikte çalışma:** Aynı volume'da kiracının `cinder-backup` incremental'ı ile OSB Mode B'nin birbirini etkilemediği kod incelemesi ve lab ile doğrulanır (V-25). Ceph sürücüsü en son snapshot'ı kendi base imajında arar; risk düşük beklenir.
6. **Ara önlem:** NetApp için `backup_use_same_host=True` ayarlanır (⚠ÜD). OSB NetApp provider'ı devreye girene kadar kritik NetApp volume'ları haftalık full yedeklenir.

---

## 19. Fazlar, efor, üretim değişiklikleri

| Faz | Takvim | Kapsam | Çıkış ölçütü | Efor (kaba, adam-hafta) |
|---|---|---|---|---|
| **0 — Doğrulama ve ölçüm** | 2–3 hafta | V-01…V-30; lab kurulumu; **estimate modu** (salt-okur okuma ve hash, yükleme yok): 10–20 volume'da throughput, dedup/sıkıştırma ve üretim etkisi; hızlı kazançlar (günlük metadata JSON envanteri, Galera dump offsite, mevcut 7 yedeğin restore testi); D-01…D-08 kararları | Kararlar imzalandı; kapasite ölçüldü; Mode B kararı verildi | 4–6 |
| **1 — Ceph MVP ve pilot** | 8–10 hafta | api/CLI, scheduler, orchestrator, freeze-agent, `ceph_rbd`, metadata, yerel chunk store, R1/R2/R6, metrikler ve alarmlar, audit; QGA yaygınlaştırmasının başlangıcı; pilot 1–2 proje (10–20 VM: en az 1 Windows, 1 DB, 1 çok diskli) | 4 hafta pilotta ≥ %99 başarı; 3 tatbikat başarılı; freeze p95 hedefte | 16–22 |
| **2 — NetApp, offsite, ölçek** | 8–10 hafta | `block_attach`, replicator + Object Lock, GC ve GFS retention, R3/R4/R5, restore group, Windows VSS; tüm projelere dalgalar halinde yayılım; `cinder-backup` geçiş adımları | Tier-1/2 VM'lerin tamamı korunuyor; offsite lag < 24 sa; aylık tatbikat düzeni | 16–22 |
| **3 — Sertleştirme, HA, self-servis** | 8–12 hafta | HA (Patroni, ikinci orchestrator); policy override rolleri; kiracı API'si ve Skyline/Horizon eklentisi; `ontap_vault` (ikincil ONTAP varsa); sayfalı manifest; parent-aware restore; şifreli volume desteği; dosya düzeyinde restore; Glance yedeği | Tek nokta arızası yok; en az yetki modeli | 16–30 |

**Ekip varsayımı:** 2 backend/altyapı mühendisi, 0,5 FTE depolama (Ceph/NetApp), 0,25 FTE güvenlik ve operasyon desteği. Efor rakamları ±%40 belirsizlik içerir.

**Üretim değişiklikleri listesi (⚠ÜD):**

| # | Değişiklik | Faz | Risk |
|---|---|---|---|
| ÜD-1 | `osb-service` projesi, kullanıcı, rol atamaları, app credential'lar | 1 | Düşük |
| ÜD-2 | cephx kullanıcıları `osb-ro`, `osb-snap`, `osb-restore` | 1 | Düşük |
| ÜD-3 | RGW: `osb-ec` placement ve pool'lar, kullanıcılar, bucket policy, pool kotası, TLS 8443 (RGW restart) | 1 | Düşük–orta |
| ÜD-4 | cmp01-03'e freeze-agent (Faz 1) ve block worker (Faz 2) kurulumu | 1–2 | Orta |
| ÜD-5 | QGA: `nova-manage image_property set` + VM stop/start; kök volume image metadata | 1–2 | Orta (kiracı bakım penceresi) |
| ÜD-6 | `backup_use_same_host=True` (cinder-backup, rolling restart) | 0–1 | Düşük–orta |
| ÜD-7 | Snapshot kota artırımları (yalnız Mode A) | 1 | Düşük |
| ÜD-8 | `no_snapshot_gb_quota=True` (yalnız Mode A) | 1 | Orta |
| ÜD-9 | Opsiyonel `[libvirt] hw_disk_discard=unmap` (guest TRIM → daha küçük yedek) | 2 | Orta |
| ÜD-10 | HAProxy TLS (platform projesi) | D-15 | Yüksek |
| ÜD-11 | Policy override'ları | 3 | Orta |
| ÜD-12 | `rbd object-map rebuild` (geçersiz bayraklı imajlar) | 1–2 | Düşük–orta |

---

## 20. Riskler

| # | Risk | Olasılık / etki | Azaltım |
|---|---|---|---|
| R-01 | Yedek I/O'su üretim Ceph performansını düşürür. | O / Y | Throttle, AIMD, pencere, librbd QoS, Faz 0 ölçümü, kill switch |
| R-02 | Guest donmuş kalır. | D / Y | Yerel deadline, journal, yeniden başlatmada thaw, chaos testi, alarm |
| R-03 | Mode B, Cinder iç davranışına bağlıdır; bir yükseltmede kırılabilir. | O / O | Kontrat testi, kod hash izleme, Mode A fallback |
| R-04 | Paylaşımlı Ceph'in kapasitesi tükenir. | O / Y | EC 2+2, pool ve RGW kotası, %60 guard, kısa yerel retention |
| R-05 | MK kaybolur. | D / Kritik | 2 kasa + offsite escrow, üç ayda bir açma testi |
| R-06 | `backup01` ele geçirilir. | D / Y | Salt-okur Ceph anahtarı, sertleştirme, pull offsite, Object Lock, ayrı GC kimliği |
| R-07 | QGA yaygınlaşmaz; yedeklerin çoğu crash-consistent kalır. | Y / O | Raporlama, kiracı iletişimi, DB VM'leri için `require`, ÜD-5 planı |
| R-08 | NetApp tam okuma yükü kabul edilemez çıkar. | O / O | Kademeli sıklık, LBA status, Faz 3 SnapVault, Ceph'e taşıma seçeneği (D-06) |
| R-09 | Dev build'lerde API sapması | O / O | Mikro sürüm sabitleme, kontrat testleri, V-01 |
| R-10 | HTTP üzerinden kimlik sızıntısı | O / Y | Yalnız mgmt ağı, app credential, kısa token, TLS projesi |
| R-11 | Restore çakışması (IP/MAC kopyası) | O / O | Plan kontrolleri; R3'te port taşıma; canlı orijinalin MAC'i kullanılmaz |
| R-12 | GC hatası gerekli chunk'ları siler. | D / Kritik | Mark-sweep + grace + diriliş, silme bütçesi, offsite immutability, scrub, derin doğrulama |
| R-13 | Katalog kaybı | D / O | S3'ten yeniden kurma, günlük döküm |
| R-14 | Ekip kapasitesi yetmez / kapsam büyür. | O / O | Faz disiplini, MVP sınırı, D-14 |
| R-15 | Mode A'da kiracı volume silemez. | Y (Mode A'da) / O | Mode B; reconciler; iletişim |
| R-16 | Offsite link DR RTO'sunu karşılamaz. | O / Y | Ölçüm, katmanlı restore önceliği, D-03 |
| R-17 | Restore sonrası UEFI veya vTPM'li VM açılmaz. | O / O | Tatbikat kapsamı, envanter (V-16), runbook |

Olasılık ve etki ölçeği: D = düşük, O = orta, Y = yüksek.

---

## 21. Açık kararlar

| # | Karar | Seçenekler | Öneri |
|---|---|---|---|
| D-01 | Tier bazında RPO ve RTO | — | Tier-1: RPO 24 sa (ileride 4–12 sa), tek VM RTO 1 sa. Tier-2: 24 sa / 4 sa. Tier-3: 72 sa / 1 iş günü. |
| D-02 | Retention | — | Yerel 7 günlük. Offsite gold: 14 günlük + 8 haftalık + 12 aylık; silver: 14 + 4; bronze: 7 + 2. |
| D-03 | Offsite hedefi, yeri, lock modu, link, bütçe | İkinci saha MinIO/RGW · yurt içi bulut S3 · multisite | İkinci saha + governance lock (Tier-1 için compliance); KVKK değerlendirmesi |
| D-04 | Kontrol düzlemi host'u | Ayrı sunucu · ctrl LXC (ara çözüm) | Ayrı sunucu (`backup01` + `restore01`) |
| D-05 | Ceph nokta modu | Mode B · Mode A | V-02 ve V-03 geçerse Mode B |
| D-06 | NetApp stratejisi | Tam okuma sıklığı · ikincil ONTAP + SnapVault · uzun vadede Ceph'e taşıma | Faz 2'de tam okuma (ölçüme göre sıklık); ikincil ONTAP varsa Faz 3 |
| D-07 | Pilot kapsamı | — | Operasyon ekibinin kendi projesi + işbirliğine açık 1 kiracı |
| D-08 | QGA yaygınlaştırmasının sahipliği | — | Operasyon metadata ve restart'ı planlar; kiracı ajanı kurar (iletişim planı gerekir) |
| D-09 | Self-servis ve chargeback (safirbilling) | — | Faz 3; Faz 1'den itibaren raporlama |
| D-10 | Faz 1'de admin rolünün kabulü | — | Telafi edici kontrollerle kabul; Faz 3'te override |
| D-11 | MK escrow prosedürü ve emanetçiler | — | 2 kişi, 2 kasa, offsite kopya |
| D-12 | Dedup alanı | Proje bazında · global | Proje bazında |
| D-13 | `cinder-backup`'ın geleceği | — | Kiracı self-servisi olarak kalır; operasyonel yedek OSB'dir |
| D-14 | Ekip ve bütçe | — | §19 varsayımı |
| D-15 | HAProxy TLS zamanlaması | — | Faz 2 ile paralel bir platform projesi |
| D-16 | Kubernetes worker'ları ve volume'suz VM'ler | — | Yalnız metadata; K8s için ayrı araç |
| D-17 | Yedek penceresi ve throttle limitleri | — | Gece 20:00–07:00 · 1 GB/s; gündüz 300 MB/s |

---

## 22. Canlı doğrulanması gereken varsayımlar (Faz 0)

"Salt-okur" işaretli maddeler üretimde değişiklik yapmadan yürütülür. "Lab" maddeleri üretimden önce lab'da yapılır.
Salt-okur maddeler için betikler ve çalışma sırası: [faz0/README.md](faz0/README.md).

| # | Doğrulanacak | Yöntem | Etkilediği |
|---|---|---|---|
| V-01 | Nova'nın gerçek kod seviyesi (B-04) | `GET /v2.1/` → `version.version` (max microversion); venv'de `pbr.json` git sha | Mikro sürüm planı |
| V-02 | Cinder RBD sürücüsü volume silerken `backup.*.snap.*` kalıbını temizliyor mu? | **Salt-okur:** venv'de `cinder/volume/drivers/rbd.py` ve `cinder/backup/drivers/ceph.py` incelemesi. **Lab:** silme testi. | D-05 |
| V-03 | Mode B'nin Cinder işlemlerine etkisi (silme, Nova `delete_on_termination`, extend, retype, migrate, revert, klon) | Lab | D-05 |
| V-04 | `client.osb-ro` ile snapshot açma, `diff_iterate` ve `include_parent` | Lab, ardından üretimde tek volume (salt-okur) | A2 |
| V-05 | 197 imajın object-map/fast-diff bayrakları | Salt-okur (`rbd info`, osb-ro) | ÜD-12 |
| V-06 | Reef 18.2.x istemci container'ı ile küme uyumu | Lab + salt-okur bağlantı | Worker imajı |
| V-07 | Admin'in `osb-service` projesinde kiracı snapshot'ından volume oluşturabilmesi | Lab | §7.3 |
| V-08 | NetApp Cinder snapshot uygulaması ve süresi; klon volume süresi; attachment `mode=ro`; os-brick FC attach | Lab / test projesi | §7.3, B-06 |
| V-09 | Tüm cmp'lerin FC zoning'i; cmp02/03 HBA modeli | Salt-okur (`/sys/class/fc_host`, multipath) | Block worker yerleşimi |
| V-10 | NetApp gerçek kullanım (LUN başına `size-used`), ONTAP sürümü, SnapMirror lisansı, FlexVol snapshot politikası | Salt-okur ONTAP erişimi | §12, D-06 |
| V-11 | ONTAP LUN'larda GET LBA STATUS / LBPRZ desteği | Lab / test LUN | NetApp okuma yükü |
| V-12 | QGA envanteri: kayıtlı imaj özelliği, libvirt XML'de `org.qemu.guest_agent.0`, `guest-ping` | Salt-okur | D-08, R-07 |
| V-13 | Freeze penceresi ölçümü (Mode A ve B; Linux ve Windows) | Lab | §8 |
| V-14 | Windows VSS + QGA sürümü davranışı | Lab | B-06 |
| V-15 | Şifreli volume tipleri ve multiattach envanteri | Salt-okur | Kapsam |
| V-16 | Ephemeral/swap, vTPM (`hw:tpm_*`) ve UEFI flavor/imaj envanteri | Salt-okur | R-17 |
| V-17 | RGW TLS, placement ekleme, kullanıcılar arası bucket policy, Object Lock | Lab | ÜD-3 |
| V-18 | App credential access rules uygulanıyor mu? (`service_type`) | Lab | §11.2 |
| V-19 | `enforce_new_defaults` / `enforce_scope` (OSA override'ları) | Salt-okur config incelemesi | §11.2 |
| V-20 | **Estimate modu:** 10–20 volume'da okuma hızı, dedup/sıkıştırma oranı, Ceph gecikme etkisi | Salt-okur (osb-ro) | §12, D-02, D-17 |
| V-21 | `no_snapshot_gb_quota` ve proje GB kota doluluğu | Salt-okur | B-02 |
| V-22 | 39 snapshot ve `backup.*.snap.*` artıklarının envanteri ve sahipliği | Salt-okur | §18 |
| V-23 | HAProxy VIP failover sırasında OSB'nin davranışı | Bakım penceresi | R-09 |
| V-24 | Offsite hedefi ve link bant genişliği | Ölçüm | D-03 |
| V-25 | `cinder-backup` Ceph sürücüsünün yabancı `backup.*` snapshot'larıyla davranışı | Kod incelemesi + lab | §18 |
| V-26 | Restore edilen VM'de cloud-init (user_data, SSH host key) | Lab | B-08 |
| V-27 | UEFI guest'in yeni NVRAM ile açılması | Lab | B-10 |
| V-28 | Neutron'da admin'in belirli MAC ve IP ile port açması; port taşıma (R3) | Lab | §9.5 |
| V-29 | Yedek penceresinde mevcut Ceph istemci gecikme taban çizgisi | Salt-okur (mgr prometheus) | AIMD eşiği |
| V-30 | `client.osb-snap` için data yazmasız, daraltılmış cap varyantı | Lab | §11.3 |

---

## Ek A — Brief §10'daki 18 soruya kısa cevaplar

| # | Soru | Cevap | Bölüm |
|---|---|---|---|
| 1 | Tek uygulama mı, modüler mi? | Tek uygulama ve capability modelli provider'lar: `ceph_rbd`, `block_attach`, ileride `ontap_vault`. | §2 A1, §5 |
| 2 | Ceph: `cinder-backup` mı, kendi yolumuz mu? | Kendi yolumuz: nokta + `diff_iterate` + chunk. `cinder-backup`'ta tutarlılık ile incremental birlikte yok; metadata, dedup, şifreleme ve immutability de yok. | §2 A2, §7.2 |
| 3 | NetApp: SnapVault, `cinder-backup` full, yoksa sürücü mü? | Faz 2: snapshot → klon → FC tam okuma + dedup. SnapVault yalnız ikincil ONTAP varsa (Faz 3). `cinder-backup` chunked sürücüsü bir ara seçenek (B-03). | §7.3, D-06 |
| 4 | Tutarlılık ve incremental aynı anda nasıl? | Freeze → noktalar (ms–s) → thaw; diff noktadan noktaya alınır; transfer pencere dışında yapılır. | §7.1, §8 |
| 5 | Snapshot kotası yetmezse? | Mode B kota tüketmez. Mode A'da kota formülü + `no_snapshot_gb_quota`. | §10 |
| 6 | Chunk store nerede tutulmalı? | Yerelde RGW'de ayrı bir EC placement; offsite'ta Object Lock'lu S3 (pull). `cinder-backups` pool'u kullanılmaz. | §2 A8, §15 |
| 7 | Manifest self-contained mı, zincir mi? | Self-contained; Faz 3'te sayfalı. | §6.4 |
| 8 | Katalog tasarımı nasıl olmalı? | PostgreSQL ve mark-and-sweep (refcount yok). Katalog S3'ten yeniden kurulabilir. | §6, §14 |
| 9 | Restore aynı instance'a mı, yeniye mi? | Varsayılan olarak yeni instance. Kimliği korumak için port taşıma (R3). Ceph kökünde in-place (R5). `swap_volume` restore aracı değildir. | §9.1, §9.5 |
| 10 | Hangi metadata alınabilir? | Matris §9.3'te. NVRAM, vTPM ve scheduler hint'leri alınamaz. | §9.3 |
| 11 | Çok kiracılı yetki nasıl kurulmalı? | Proje başına app credential. Faz 1'de admin + telafi edici kontroller; Faz 3'te policy override. | §11.2 |
| 12 | Güvenlik önlemleri neler? | Salt-okur veri düzlemi; yazma izole helper'da; istemci tarafı şifreleme; RGW TLS; pull offsite; audit. | §11 |
| 13 | Scheduler ve eşzamanlılık? | EDF, lease'ler, advisory lock'lar, AIMD throttle; başlangıç değerleri §12.3'te. | §12.3, §13 |
| 14 | Offsite ve DR? | Pull + Object Lock + lock uzatma; RPO/RTO tablosu; link bant genişliği belirleyici. | §15 |
| 15 | Test ve kabul? | Lab, kontrat testleri, chaos, haftalık otomatik tatbikat; KPI tablosu. | §17 |
| 16 | Controller nerede çalışmalı? | OpenStack dışında ayrı host; LXC yalnız sınırlı bir ara çözüm. | §3.2, D-04 |
| 17 | Mevcut `cinder-backup` ile geçiş? | Paralel çalışma; mevcut yedekler doğrulanıp saklanır; `backup_use_same_host` ara önlemi. | §18 |
| 18 | Açık kararlar neler? | D-01…D-17. | §21 |
