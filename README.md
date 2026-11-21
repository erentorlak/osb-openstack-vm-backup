# OSB — OpenStack VM Backup/Restore

Bu depo iki şey içerir:
- OpenStack 2024.1 (Ceph RBD + NetApp FC) ortamı için bir VM yedekleme/geri yükleme uygulamasının tasarımı;
- tasarımın varsayımlarını üretimi değiştirmeden doğrulayan Faz 0 araçları.

| Dosya | İçerik |
|---|---|
| [TASARIM.md](TASARIM.md) | Tasarım dokümanı (v0.1 taslak). Bölümleri: mimari, veri modeli, backup ve restore akışları, güvenlik, kapasite, test, fazlar, riskler, açık kararlar ve doğrulanacak varsayımlar. |
| [faz0/](faz0/README.md) | Salt-okur doğrulama ve ölçüm betikleri, rapor üretici `analyze.py` ve birim testleri. |

Dokümandaki IP adresleri RFC 5737 örnek adresleridir (`192.0.2.0/24`, `198.51.100.0/24`); gerçek parola, tenant adı, VM adı ve UUID yoktur. Faz 0 betiklerinin çıktıları (`faz0-out/`) gerçek ortam verisi içerir. Bu yüzden `.gitignore` ile depo dışında tutulur.

```bash
python3 -m unittest discover -s faz0/tests -v
```
