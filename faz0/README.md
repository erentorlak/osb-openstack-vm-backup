# OSB Faz 0 — Salt-okur doğrulama ve ölçüm araçları

Bu klasör, [TASARIM.md](../TASARIM.md) §22'deki **salt-okur** doğrulama maddelerini (V-xx) ve §12'deki kapasite ve performans varsayımlarını **üretimi değiştirmeden** ölçmek için hazırlanmış betikleri içerir. Hepsinin sonuçları `analyze.py` ile tek bir Markdown raporunda birleştirilir.

> **Garanti:** Betikler hiçbir kaynak oluşturmaz, değiştirmez ya da silmez.
> - OpenStack, ONTAP ve Prometheus tarafında yalnız GET isteği yapılır.
> - Ceph imajları her zaman `read_only=True` ile açılır; snapshot alınmaz.
> - Host'larda yalnız `virsh list/dumpxml`, `/sys` okuma ve `dpkg-query` çalışır.
>
> **Ön koşul olan değişiklikler** aşağıda ayrıca işaretlidir (⚠ÜD). Bunlar onaya tabidir.

## Araçlar

| Betik | Nerede çalışır | Ne okur | Çıktı | Kapsadığı |
|---|---|---|---|---|
| `control_checks.sh` | ctrl01 (ve drift için ctrl02/03), root | LXC konteynerlerinde sürümler, pbr git sha, beyaz listedeki config anahtarları, Cinder kaynak kodu satırları ve sha256 | `control-<host>-<ts>.txt` | V-01, V-02, V-06, V-18, V-19, V-21, V-25 |
| `host_checks.sh` | cmp01-03, root | FC HBA, multipath özeti, libvirt domain'leri (QGA kanalı, UEFI, vTPM, disk türleri), paketler | `host-<host>-<ts>.txt`, `domains-<host>-<ts>.csv` | V-06, V-09, V-12, V-16 |
| `inventory.py` | utility konteyneri (openstacksdk 3.0.0), admin openrc | Nova, Cinder, Neutron ve Keystone envanteri (GET) | `inventory-<ts>.json` | V-01, V-12, V-15, V-16, V-21, V-22 + §9.3 metadata dökümü |
| `rbd_survey.py` | Ceph Reef istemcisi olan bir host/container | Her RBD imajının özellikleri, bayrakları, parent'ı, snapshot'ları; opsiyonel tahsis ve değişim hızı (object-map, veri okumaz) | `rbd-survey-<ts>.json` | V-04, V-05, V-20, V-22 |
| `estimate.py` | aynı | Seçilen imajların dolu alanları **okunur** (hız sınırlı); sıfır, dedup ve sıkıştırma ölçülür; yükleme yapılmaz | `estimate-<ts>.json` | V-04, V-20 |
| `ceph_latency_sampler.py` | mgr:9283'e erişen herhangi bir host | ceph-mgr Prometheus metrikleri (OSD gecikmesi, IOPS) | `ceph-latency-<ts>.csv` | V-29 (ve V-20'nin üretim etkisi kısmı) |
| `ontap_readonly.py` | ONTAP yönetim adresine HTTPS erişimi olan host | ONTAP REST (GET): sürüm, lisanslar, FlexVol, LUN kullanımı, SnapMirror, FC LIF | `ontap-<ts>.json` | V-10 |
| `analyze.py` | herhangi bir yer (Python ≥ 3.8, çevrimdışı) | Yukarıdaki çıktılar | `faz0-rapor.md` (+ JSON özet) | Hepsinin özeti |

Lab gerektiren maddeler bu araçların kapsamı dışındadır: V-03, V-07, V-08, V-11, V-13, V-14, V-17, V-18 (davranış), V-23, V-24, V-26, V-27, V-28 ve V-30.

## Ön koşullar

1. **Ceph okuma anahtarı.** Önerilen anahtar `client.osb-ro`'dur (TASARIM.md §11.3). Anahtarı oluşturmak **⚠ÜD-2**'dir; risk düşüktür ama onay gerekir.
   - Onay gelene kadar betikler mevcut bir anahtarla da çalışır; kod imajları yine `read_only=True` açar.
   - Ancak yazma yetkili bir anahtarı (ör. `client.cinder`) bu amaçla kullanmak da ayrı onaya tabidir.
2. **Ceph Reef bağlamaları.** Küme 18.2.2'dir; istemci de Reef olmalıdır. ceph-volume LXC'sindeki 20.2 istemcisini kullanmayın (V-06). En kolay yol, kümenin zaten kullandığı container imajıdır (cephadm ile kurulduysa yerelde vardır):
   ```bash
   podman run --rm --net=host -v /etc/ceph:/etc/ceph:ro -v "$PWD":/w -w /w \
     quay.io/ceph/ceph:v18.2.2 python3 rbd_survey.py --id osb-ro \
     --keyring /etc/ceph/ceph.client.osb-ro.keyring --allocation --change-rate
   ```
   İmaj yerelde yoksa indirmek bir dış indirmedir; kendi kararınızdır.
3. **ONTAP.** Salt okur bir hesap gerekir (ör. `readonly` rolü). Yoksa oluşturulması bir değişikliktir. Kimlik bilgisi `ONTAP_USER` ve `ONTAP_PASSWORD` ortam değişkenlerinden okunur ya da sorulur; dosyaya yazılmaz.
4. **Prometheus.** `ceph_latency_sampler.py` için ceph-mgr `prometheus` modülü açık olmalıdır. Durumu `ceph mgr module ls` ile kontrol edin; modülü açmak bir değişikliktir.
5. **Çıktıların izinleri.** Betikleri `umask 077` ile çalıştırın. Python betiklerinin JSON çıktıları zaten `0600` izinle yazılır.

## Önerilen çalışma sırası

Betikleri ilgili host'lara kopyalayın (ör. `scp -r faz0/ ctrl01:/root/osb-faz0/`). Sonra aşağıdaki sırayla çalıştırın.

```bash
# 1) Kontrol düğümü (her ctrl için ayrı; drift görünür)
sudo ./control_checks.sh --out ./faz0-out

# 2) Compute'lar (cmp01-03). --ping: kanalı bağlı VM'lere QGA "guest-info" (salt okur) gönderir
sudo ./host_checks.sh --out ./faz0-out --ping

# 3) Envanter (utility konteyneri içinde)
lxc-attach -n "$(lxc-ls -1 --running | grep utility | head -1)"
source /root/openrc
/openstack/venvs/utility-*/bin/python inventory.py --out faz0-out/inventory.json

# 4) RBD envanteri (Ceph Reef ortamında; yalnız object-map okur, hızlıdır)
python3 rbd_survey.py --id osb-ro --pool cinder-volumes --prefix volume- \
  --allocation --change-rate --out faz0-out/rbd-survey.json

# 5) Gecikme taban çizgisi: 30 dk (ayrı terminalde)
python3 ceph_latency_sampler.py --url http://198.51.100.11:9283/metrics --minutes 30 --label baseline

# 6) Estimate (gece penceresinde; 5. adımı --label estimate ile yeniden başlatın)
python3 estimate.py --id osb-ro --from-survey faz0-out/rbd-survey.json --sample 15 \
  --inventory faz0-out/inventory.json --rate-mb 100 --threads 4 --max-minutes 120 \
  --out faz0-out/estimate.json

# 7) NetApp
ONTAP_USER=<salt-okur-hesap> python3 ontap_readonly.py --host 198.51.100.50 --svm svm-cinder \
  --ca-file <ca.pem> --snapshots --out faz0-out/ontap.json

# 8) Rapor (çevrimdışı; anonim sürüm paylaşım içindir)
python3 analyze.py --inventory faz0-out/inventory.json --rbd-survey faz0-out/rbd-survey.json \
  --hosts-csv faz0-out/domains-cmp01-*.csv --hosts-csv faz0-out/domains-cmp02-*.csv \
  --hosts-csv faz0-out/domains-cmp03-*.csv --ontap faz0-out/ontap.json \
  --estimate faz0-out/estimate.json --no-snapshot-gb-quota false \
  --out faz0-out/faz0-rapor.md --json-out faz0-out/faz0-ozet.json
python3 analyze.py ...aynı girdiler... --anonymize --out faz0-out/faz0-rapor-anonim.md
```

IP'ler RFC 5737 örnek adresleridir; kendi ortamınızın değerlerini kullanın. `--no-snapshot-gb-quota` değerini 1. adımın çıktısından alın.

## Güvenlik ve etki notları

- **`estimate.py` gerçekten veri okur.** Varsayılan sınır 100 MB/s ve 4 thread'dir. İlk çalıştırmayı gece penceresinde, 5. adımdaki gecikme ölçümüyle birlikte yapın.
  - Ceph istemci gecikmesi taban çizgisine göre belirgin artarsa `--rate-mb` değerini düşürün.
  - Süreyi `--max-minutes` ile sınırlayın. Ctrl-C ile durdurulan çalıştırma sonuç yazmaz.
- **`estimate.py` snapshot almaz.** Canlı (head) imajı okur; veri tutarlı bir an değildir ama oran tahmini için yeterlidir. Mevcut bir snapshot'ı okumak için `--snap` kullanın.
- **Değişim hızı alt sınırdır.** `rbd_survey.py --change-rate`, mevcut en yeni snapshot'tan bu yana değişen baytı object-map ile hesaplar. Aynı blok defalarca yazılsa da bir kez sayılır. En anlamlı sonuç 1–7 gün yaşındaki snapshot'larla alınır.
- **Gizli değerler yazdırılmaz.** `control_checks.sh` ve `host_checks.sh` config dosyalarından yalnız beyaz listedeki, gizli olmayan anahtarları yazdırır. `password`, `transport_url` ve `connection` gibi anahtarlar hiçbir zaman çıktıya girmez.
  - WWPN ve multipath WWID'leri varsayılan olarak maskelenir.
- **user_data saklanmaz.** `inventory.py` yalnız var olup olmadığını, boyutunu ve sha256'sını tutar. İçeriği saklamak için `--include-user-data` gerekir; önerilmez.
- **Çıktılar iç topoloji bilgisi içerir** (IP, proje adı, UUID). Dışarıya yalnız `--anonymize` ile üretilen raporu ve onu da gözden geçirdikten sonra verin.

## Sonuçlar hangi kararları besler

| Ölçüm | Karar |
|---|---|
| V-02 kod satırları + V-21 kota tablosu + `delete_on_termination` sayısı | D-05 (Mode B mi, Mode A mı) |
| estimate oranları + rbd_survey tahsisi + ONTAP kullanımı | D-02 (retention), §12.4 kapasite, EC kararı |
| estimate hızı + gecikme CSV'si | D-17 (pencere ve throttle) |
| QGA kanal/bağlantı oranı (host_checks) | D-08 (QGA yaygınlaştırma planı), R-07 |
| ONTAP kullanımı, SnapMirror, lisanslar | D-06 (NetApp stratejisi) |
| Nova max microversion | B-04 / V-01 (yükseltme ve mikro sürüm planı) |

## Testler

Ceph ve OpenStack gerektirmeyen birim testleri:

```bash
python3 -m unittest discover -s faz0/tests -v
```

Testler şunları sahte (fake) nesnelerle doğrular:
- estimate çekirdeği: chunk hizalama, sıfır/dedup/sıkıştırma sayımı, hız sınırlayıcı;
- inventory yardımcıları: sayfalama, mikro sürüm başlığı, Nova/Cinder kök belge keşfi, user_data temizliği;
- rbd_survey'in imaj işleme mantığı;
- Prometheus ayrıştırıcısı;
- analyze raporu: sayımlar, kararlar ve anonimleştirme.

Gerçek API'lerle uçtan uca test **yapılmadı**. Önce lab'da ya da tek bir projede (`--limit` / `--image`) deneyin.
