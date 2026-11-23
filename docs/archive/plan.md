# seo-probe-lib: Uygulama Planı

> Durum: uygulandı, henüz yayımlanmadı (ilk sürüm 0.1.0) · 2026-11-23 · Mimari inceleme: `docs/review.md` · Uygulamada netleşen kararlar: §11
> Paket: `seoprobe` (PyPI'da boş) · Repo ve dizin: `seo-probe-lib`
> Davranış kaynağı: `seo-analyzer-mvp` (`src/seoa`)

## 1. Amaç

`seoprobe`, SEO analizini başka Python projelerine (ör. bir SaaS'ın arka ucuna) eklemek için yazılmış bir **kütüphanedir**.

- Yalnızca kütüphanedir: web sunucusu, arayüz, CLI, veritabanı ve arka plan işçisi içermez.
- Her yetenek, tek başına çağrılabilen bir `async` fonksiyondur: `await seoprobe.techstack(url)`.
- Her fonksiyon aynı zarfı döner: `Result`. Zarf her zaman eksiksiz bir JSON'a serileşir.
- Hatalar sabit kodlarla döner, girdiler tiplidir, gereksinimler önceden bilinir.
- Yalnızca `async` API vardır; senkron katman yoktur.

### Kaynak projeyle ilişki

`seo-analyzer-mvp` bu paketin **davranış kaynağıdır, mimari kaynağı değildir**. Kurallar, eşikler, skor hesabı ve test örnekleri oradan taşınır; aynı girdi aynı bulguları ve aynı skoru vermelidir. Katmanlar, dosya bölümlemesi, servis/kuyruk/depo yapısı, HTTP API'ye özgü her şey ve LLM katmanı taşınmaz (§7.12). Kaynak dosyaların nereye gittiği **Ek A**'dadır.

## 2. Kullanım

```python
import seoprobe

# En basit kullanım: tek çağrı, kaynaklar otomatik açılıp kapanır
res = await seoprobe.techstack("https://example.com")
print(res.to_json(indent=2))

# Üretim kullanımı: bir oturum, paylaşılan bağlantılar ve sınırlar
config = seoprobe.Config(lang="tr", google_api_key="...")
async with seoprobe.Session(config) as s:
    page = await seoprobe.fetch("https://example.com", session=s)   # bir kez çekilir
    tech, prev = await asyncio.gather(
        seoprobe.techstack(page.unwrap(), session=s),
        seoprobe.previews(page.unwrap(), session=s),
    )

    if tech.ok:
        print([t.name for t in tech.data.items])
    else:
        print(tech.error.code)            # ErrorCode.FETCH_TIMEOUT

    audit = await seoprobe.audit_page("https://example.com", keywords=["seo analizi"], session=s)
    report = await seoprobe.report.build(audit.unwrap(), session=s)
    ops = await seoprobe.gsc.opportunities("sc-domain:example.com", session=s)
```

Her işlem **tek bir yerde, tek bir fonksiyon** olarak tanımlanır. `session=` verilmezse fonksiyon geçici bir oturum açar ve kapatır. Her işlemi bir sınıf metodu olarak ikinci kez tanımlayan bir `Client` yoktur.

## 3. İlkeler

### 3.1 Kullanıcının kuralları

**Kural 1: Okunabilir, yönetilebilir dosya mimarisi.**
- Kütüphanenin ortak temeli (`core/`) ile özellikler (`features/`) ayrı klasörlerdedir. Temeli görmek için tek klasör açılır.
- Her özellik paketi aynı küçük iç düzeni izler (§4.3). Birini bilen hepsini bilir.
- Dosya ağacı düz bir yığın değildir: birbirine bağımlı işlemler aynı özellik paketinde, bağımsız işlem grupları ayrı paketlerdedir.

**Kural 2: Özellikler yalıtılmış ve bağımsız yönetilebilir.**
- Bir özellik paketi yalnızca `core`'u ve **açıkça bildirdiği** başka özelliklerin genel API'sini (`__init__.py`) kullanabilir. Bir başka özelliğin iç dosyasını import edemez.
- Bağımlılıklar bir tabloyla belgelenir (§4.2) ve `lint-imports` ile testlerde denetlenir; döngü olamaz.
- Bir özelliği eklemek, silmek veya değiştirmek diğerlerine dokunmayı gerektirmez: kodu, çıktı modelleri, kuralları, metinleri, veri dosyaları ve testleri kendi klasöründedir.

**Kural 3: Temiz kod, tekrar yok, gereksiz dosya yok.**
- Ortak davranış (girdi doğrulama, gereksinim, zaman aşımı, hata eşleme, iz, log) yalnızca `core/feature.py`'deki `@operation`'dadır.
- Birden çok özelliğin kullandığı her şey `core`'dadır; bir özelliğin kullandığı her şey o özelliktedir. Aynı iş iki yerde yazılmaz.
- Bir dosya ancak kendi sorumluluğu varsa açılır. `utils.py`, `helpers.py`, `common.py`, `misc.py`, `constants.py`, özellik başına `exceptions.py` yasaktır.
- Yalnızca başka bir fonksiyonu çağıran sarmalayıcı yazılmaz; tek istisna yalnızca dışa açma yapan `__init__.py` dosyalarıdır.
- Kurallar, metinler (en/tr), eşikler ve şablonlar koda değil veri dosyalarına yazılır.

**Kural 4: Hatalar, girdiler ve çıktılar yönetilebilir.**
- Girdi: açık, tipli parametreler. Serbest `options: dict` yoktur. Geçersiz girdi işlem başlamadan reddedilir.
- Çıktı: her işlem `Result[T]` döner; `T` sürümlü bir pydantic modelidir ve JSON Schema'sı testle korunur.
- Hata: dış dünyadan gelen her hata (ağ, site, API) sabit bir `ErrorCode` ile `Result` içinde döner, exception fırlatılmaz.
- Takip: her `Result` bir `trace_id`, süre, maliyet ve yapılan isteklerin listesini taşır.

### 3.2 İyi bir Python kütüphanesi olarak

| İlke | Bu pakette |
|---|---|
| Import yan etkisizdir | `import seoprobe` ağa çıkmaz, dosya okumaz, ortam değişkeni okumaz, global durum kurmaz; veri dosyaları ilk ihtiyaçta okunur |
| Global durum yoktur | tüm durum `Session` içindedir; aynı süreçte farklı ayarlı oturumlar birlikte çalışır |
| Kütüphane log ayarlamaz | `logging.getLogger("seoprobe")` + `NullHandler`; seviye ve hedef kullanan projenindir |
| Ortamı kullanıcı yönetir | `Config` açıkça verilir; `Config.from_env()` yalnızca çağrılırsa ortamı okur |
| Bağımlılıklar azdır | çekirdek 5 paket; tarayıcı ve DNS isteğe bağlı ekstralar, yalnızca gerektiğinde import edilir |
| Tipler dışa açıktır | `py.typed`, `mypy --strict` |
| Genel API açıktır | yalnızca `seoprobe.__all__` semver kapsamındadır; `seoprobe.core.*` ve `seoprobe.features.*` alt modülleri iç koddur |
| Sürüm tek yerdedir | `pyproject.toml`; çalışma anında `importlib.metadata.version("seoprobe")` |
| Paket veri dosyaları güvenle okunur | `importlib.resources`; her özellik kendi verisini kendi paketinden okur, çalışma dizinine bağımlılık yoktur |
| Diske kendiliğinden yazmaz | paketin ürettiği her şey `Result` içinde döner; dosyaya yazmak kullanan projenin işidir |
| Her alt paketin `__init__.py`'si vardır | örtük namespace paketi kullanılmaz; mypy, pytest ve wheel davranışı öngörülebilir kalır |

### 3.3 Adlandırma

**Dil.** Koddaki her şey İngilizcedir: modül, fonksiyon, sınıf, parametre ve alan adları, JSON anahtarları, hata kodları, log mesajları, docstring'ler, veri dosyası adları, commit mesajları. Yalnızca kod yorumları bu kuralın dışındadır. Bulgu metinleri içeriktir; her özelliğin `catalog.json`'unda `en` ve `tr` olarak bulunur, varsayılan dil `en`'dir.

| Kural | Örnek |
|---|---|
| Bir şeyi inceleyen işlem bir **isimdir** | `robots`, `sitemap`, `hreflang`, `techstack`, `previews` |
| Bir şey yapan veya üreten işlem bir **fiildir** | `fetch`, `crawl`, `render`, `normalize`, `classify`, `suggest_schema` |
| Paket adını tekrar eden önek yok | `local_seo` var, `seo_audit` yok |
| Kısaltma yalnızca yerleşikse | `url`, `html`, `gsc`, `nap`; ama `cfg`, `res`, `mgr` yok |
| Tek kelime yetiyorsa iki kelime yok | `fetch`, `fetch_page` değil |
| Python: `snake_case` fonksiyon ve modül, `PascalCase` sınıf, `UPPER_CASE` sabit | `audit_page`, `ErrorCode`, `FEATURES` |
| Hata kodları, kural kimlikleri, şema sürümleri: kebab-case | `fetch-timeout`, `title-missing`, `techstack/1` |
| Veri dosyaları: kebab-case `.json` | `ctr-curve.json`, `hreflang-codes.json` |
| Birim, adın sonekidir | `duration_ms`, `size_bytes`, `timeout_s` |
| Mantıksal değerler soru gibi okunur | `is_spa`, `has_hreflang`, `allows_ai` |
| Özellik paketi adı, içindeki işlemlerin ortak konusudur | `site`, `crawl`, `search`, `monitoring` |

**Kaynak projeye göre değişen adlar.**

| Kaynakta | Yeni ad | Neden |
|---|---|---|
| `fetch_page` | `fetch` | tek kelime yeterli |
| `normalize_url` | `normalize` | girdi zaten URL |
| `check_page` | `onpage` | sektördeki adı (on-page SEO); diğer analizlerle aynı isim kalıbı |
| `validate_schema` | `schema` | inceleme işlemi isimdir; `suggest_schema` ile çifti belli |
| `detect` | `classify` | iş tipi ve dili sınıflandırır |
| `local` | `local_seo` | `local` tek başına anlamsız |
| `agentic` | `ai_access` | AI ajanlarının siteye erişimini inceler |
| `lab_performance` | `lab_metrics` | döndürdüğü şey ölçüm değerleri |
| `match_property` | `gsc.find_property` | eylemi açık |
| `suggest_keywords` | `gsc.top_queries` | uydurma öneri değil, gerçek sorgular döner |
| `gsc.report` | `gsc.summary` | `seoprobe.report` ile karışmaz |
| `action_plan` | `report.plan` | ad alanı zaten `report` |
| `test_integration` | `check_access` | anahtar ve OAuth erişimini sağlayıcıda dener |
| `AuditRecord` / `AuditReport` | `Audit` / `AuditReport` | ham sonuç ve işlenmiş rapor |
| `LocalAuditService`, `AuditRunner` | yok | `audit_page` / `audit_site` fonksiyonları |
| `Settings` | `Config` | kütüphanelerde yerleşik ad |
| analiz modülü (`AnalysisModule`) | özellik (`Feature`) | paket düzeyinde, işlem gruplarını kapsar |

## 4. Mimari

### 4.1 Dosya yapısı

```
seo-probe-lib/
├── pyproject.toml
├── README.md
├── CHANGELOG.md
├── LICENSE
├── docs/
│   ├── plan.md
│   ├── operations.md               # işlem kataloğu (testle koddan doğrulanır)
│   └── errors.md                   # hata kodları (testle ErrorCode ile doğrulanır)
├── src/seoprobe/
│   ├── __init__.py                 # genel API: işlemler, ad alanları, temel tipler; __all__
│   ├── py.typed
│   │
│   ├── core/                       # TEMEL: her özelliğin kullandığı ortak yapılar
│   │   ├── __init__.py             # temel tipleri dışa açar
│   │   ├── config.py               # Config
│   │   ├── session.py              # Session: bağlantılar, tarayıcı, istek sınırları, olay kancası
│   │   ├── result.py               # Result, Status, Meta, RequestLog, Event
│   │   ├── errors.py               # ErrorCode, ErrorInfo, SeoprobeError
│   │   ├── models.py               # Finding, NotChecked, Severity, Category, Requirements
│   │   ├── page.py                 # Page, fetch, normalize: tüm özelliklerin ortak girdisi
│   │   ├── feature.py              # Feature, @operation, Context: ortak davranışın tek yeri
│   │   ├── parse/                  # saf ayrıştırma; I/O yok, başka core modülü kullanmaz
│   │   │   ├── html.py             # HTML ayrıştırma
│   │   │   ├── urls.py             # normalizasyon
│   │   │   └── text.py             # metin, tarih ve kimlik işlemleri
│   │   ├── net/                    # ağ erişimi
│   │   │   ├── client.py           # SSRF-güvenli istemci; her isteği RequestLog'a yazar; RequestBudget
│   │   │   ├── safety.py           # URL/IP politikası, bağlantı anında adres doğrulama (SafeResolver)
│   │   │   ├── politeness.py       # host başına eşzamanlılık, aralık, devre kesici
│   │   │   ├── api.py              # dış JSON API'ler için ortak istemci (anahtar, kota, hata eşleme)
│   │   │   └── browser.py          # headless Playwright; her istekte SSRF koruması
│   │   ├── rules/
│   │   │   ├── catalog.py          # CheckDef; catalog.json yükleme, doğrulama, mesaj çevirisi
│   │   │   └── scoring.py          # deterministik skor, PageRecord
│   │   └── data/
│   │       ├── catalog.json        # çekirdek metinleri: hata mesajları (en/tr)
│   │       ├── bots.json           # bot kataloğu: AI tarayıcıları, ters DNS kuralları
│   │       └── stopwords.json
│   │
│   └── features/                   # ÖZELLİKLER: her paket yalıtılmış, bağımsız yönetilir
│       ├── __init__.py             # FEATURES: kayıtlı özelliklerin açık listesi
│       ├── page/                   # onpage, classify
│       │   ├── __init__.py
│       │   ├── models.py
│       │   ├── onpage.py
│       │   ├── classify.py
│       │   └── catalog.json
│       ├── content/                # readability, previews, schema, suggest_schema, local_seo
│       │   ├── __init__.py
│       │   ├── models.py
│       │   ├── readability.py
│       │   ├── previews.py
│       │   ├── schema.py           # schema, suggest_schema
│       │   ├── local_seo.py
│       │   ├── catalog.json
│       │   └── schema-templates.json
│       ├── site/                   # transport, site_setup, robots, sitemap, generate_sitemap, ai_access, techstack, dns_health
│       │   ├── __init__.py
│       │   ├── models.py
│       │   ├── transport.py
│       │   ├── setup.py            # site_setup
│       │   ├── robots.py
│       │   ├── sitemap.py          # sitemap, generate_sitemap
│       │   ├── ai_access.py
│       │   ├── techstack.py
│       │   ├── dns_health.py
│       │   ├── catalog.json
│       │   └── techstack.json
│       ├── crawl/                  # crawl, links, indexability, hreflang, suggest_links
│       │   ├── __init__.py
│       │   ├── models.py
│       │   ├── crawl.py
│       │   ├── links.py            # links, indexability
│       │   ├── hreflang.py
│       │   ├── suggest.py          # suggest_links
│       │   ├── catalog.json
│       │   └── hreflang-codes.json
│       ├── render/                 # render, screenshots, mobile, accessibility
│       │   ├── __init__.py
│       │   ├── models.py
│       │   ├── render.py
│       │   ├── screenshots.py
│       │   ├── mobile.py
│       │   ├── accessibility.py
│       │   ├── catalog.json
│       │   └── axe/                # axe.min.js + LICENSE, sabit sürüm
│       ├── performance/            # lab_metrics, pagespeed, field_data, web_vitals, carbon
│       │   ├── __init__.py
│       │   ├── models.py
│       │   ├── lab.py              # lab_metrics
│       │   ├── google.py           # pagespeed, field_data
│       │   ├── vitals.py           # web_vitals
│       │   ├── carbon.py
│       │   ├── catalog.json
│       │   └── carbon-model.json
│       ├── search/                 # seoprobe.gsc / ga4 / bing / indexnow
│       │   ├── __init__.py
│       │   ├── models.py
│       │   ├── oauth.py            # Google OAuth, TokenStore (gsc ve ga4 paylaşır)
│       │   ├── gsc.py
│       │   ├── opportunities.py    # gsc.opportunities
│       │   ├── ga4.py
│       │   ├── bing.py
│       │   ├── indexnow.py         # paketteki tek yazma işlemi
│       │   ├── catalog.json
│       │   └── ctr-curve.json
│       ├── offsite/                # authority, visibility, competitors
│       │   ├── __init__.py
│       │   ├── models.py
│       │   ├── authority.py
│       │   ├── visibility.py
│       │   ├── competitors.py
│       │   └── catalog.json
│       ├── monitoring/             # check_urls, check_redirects, snapshot, diff, analyze_logs
│       │   ├── __init__.py
│       │   ├── models.py
│       │   ├── batch.py            # check_urls, check_redirects
│       │   ├── snapshot.py         # snapshot, diff
│       │   ├── logs.py             # analyze_logs
│       │   └── catalog.json
│       ├── audit/                  # audit_page, audit_site, audit_stream, rerun
│       │   ├── __init__.py
│       │   ├── models.py           # Audit, AuditEvent
│       │   ├── pipeline.py
│       │   └── catalog.json
│       └── report/                 # seoprobe.report: build, score, plan, compare, findings
│           ├── __init__.py
│           ├── models.py           # AuditReport
│           ├── build.py
│           ├── scoring.py
│           ├── plan.py
│           ├── compare.py
│           └── catalog.json
└── tests/
    ├── conftest.py
    ├── fakes.py                    # FakeHttp, FakeBrowser, FakeGoogle
    ├── fixtures/                   # gerçek sayfa örnekleri (kaynak projenin altın örnekleri)
    ├── core/                       # src/seoprobe/core yapısını izler
    ├── features/                   # her özellik için bir klasör: tests/features/site/test_robots.py
    ├── contract/                   # JSON Schema anlık görüntüleri, bağımlılık sözleşmesi, katalog doğrulama
    └── integration/                # yerel HTTP sunucusuna karşı uçtan uca işlemler
```

Ağacın okunma biçimi: `core/` kütüphanenin temelidir, `features/` altındaki her klasör bir özelliktir. Özellik klasörlerinin içinde 4–10 dosya vardır ve hepsi aynı düzende sıralanır: `__init__.py`, `models.py`, işlem dosyaları, `catalog.json`, varsa veri dosyaları.

### 4.2 Özellikler ve bağımlılıkları

Bir özellik paketi, **birbirinin çıktısını veya iç mantığını kullanan** işlemleri bir araya getirir. Birbirinden bağımsız işlem grupları ayrı paketlerdir.

| Özellik | İşlemler | Neden bir arada | Bağımlı olduğu özellikler |
|---|---|---|---|
| `page` | `onpage`, `classify` | tek sayfanın temel kuralları ve sınıflandırması. `Page`, `fetch` ve `normalize` ise `core/page.py`'dedir, çünkü `core`'un kendisi (`ctx.page`) ve tüm özellikler onlara ihtiyaç duyar | — |
| `content` | `readability`, `previews`, `schema`, `suggest_schema`, `local_seo` | sayfa içeriğini ve yapılandırılmış veriyi inceler; `suggest_schema` ve `local_seo` aynı JSON-LD ayrıştırmasını kullanır | `page` |
| `site` | `transport`, `site_setup`, `robots`, `sitemap`, `generate_sitemap`, `ai_access`, `techstack`, `dns_health` | site düzeyindeki ayarlar; `ai_access` robots kurallarını, `generate_sitemap` sitemap analizini, `site_setup` transport sonuçlarını kullanır | `page` |
| `crawl` | `crawl`, `links`, `indexability`, `hreflang`, `suggest_links` | hepsi taranan sayfa kümesi ve aynı bağlantı grafiği üzerinde çalışır | `page`, `site` (robots, sitemap) |
| `render` | `render`, `screenshots`, `mobile`, `accessibility` | hepsi tarayıcıda açılmış bir sayfa üzerinde çalışır | `page` |
| `performance` | `lab_metrics`, `pagespeed`, `field_data`, `web_vitals`, `carbon` | `web_vitals` diğer üçünü birleştirir, `carbon` lab ölçümünün aktarım baytını kullanır | `page` |
| `search` | `gsc.*`, `ga4.*`, `bing.*`, `indexnow.*` | arama motorlarının kendi verisi; gsc ve ga4 aynı OAuth'u, `gsc.opportunities` ga4'ü kullanır | — |
| `offsite` | `authority`, `visibility`, `competitors` | sitenin dışarıdan görünümü; rakipler aynı hafif analizle karşılaştırılır | `page`, `content`, `site` |
| `monitoring` | `check_urls`, `check_redirects`, `snapshot`, `diff`, `analyze_logs` | URL listeleri ve zaman içindeki değişim; SaaS'ın izleme işleri | `page`, `site` (robots) |
| `audit` | `audit_page`, `audit_site`, `audit_stream`, `rerun` | tüm özellikleri tek denetimde birleştirir | `page`, `content`, `site`, `crawl`, `render`, `performance`, `search`, `offsite` |
| `report` | `report.build`, `score`, `plan`, `compare`, `findings` | denetim sonucu üzerinde çalışan saf fonksiyonlar | `audit` (yalnızca modeller) |

Katmanlar, alttan üste:

```
core
 └─ page
     ├─ content · site · render · performance               search (yalnızca core)
     │   └─ crawl (site'a bağlı)
     ├─ offsite · monitoring
     └─ audit  →  report
```

**Bağımlılık kuralları ve denetimi:**

- `core` hiçbir özelliği import etmez.
- Bir özellik yalnızca `core`'u ve tablodaki bağımlılıklarının **paket kökünü** import eder: `from seoprobe.features.site import robots`. `seoprobe.features.site.robots` gibi iç dosyaya erişim yasaktır.
- **Sözleşme `__all__`'dır.** Başka bir özellikten yalnızca onun `__all__`'ındaki adlar kullanılabilir: genel işlemler ve `models.py`'deki çıktı modelleri. Özellikler birbirini genel işlemleri üzerinden çağırır ve `Result` alır; aralarında Protocol veya adaptör katmanı yoktur.
- Döngü yoktur; `monitoring` gibi üst katmandaki bir özellik, `page` gibi alttakine bağımlı olabilir, tersi olamaz.
- **Denetim:**
  - `import-linter` katman ve bağımsızlık sözleşmeleri `lint-imports` ile çalışır.
  - `tests/contract/test_dependencies.py`, her özelliğin `FEATURE.depends_on` bildirimi ile gerçek import'larını karşılaştırır; iç dosya erişimini ve `__all__` dışındaki ad kullanımını yakalar.
- **Ortak model kuralı:** bir model `core/models.py`'ye ancak `core`'un kendisi kullanıyorsa ya da iki veya daha fazla özellik onu girdi olarak alıyorsa girer. Bir özelliğin çıktısı, başka biri tüketse bile üretenin `models.py`'sinde kalır (ör. `RobotsRules` → `site/models.py`).

### 4.3 Bir özellik paketinin iç düzeni

Her özellik paketi aynı düzeni izler. Bu listede olmayan bir dosya türü açılmaz.

| Dosya | Zorunlu mu | İçerik |
|---|---|---|
| `__init__.py` | evet | işlemleri dışa açar ve `FEATURE`'ı tanımlar; mantık içermez |
| `models.py` | evet | özelliğin tüm çıktı modelleri, yani JSON sözleşmesi; çıktısı değişen bir özellik için incelenecek tek dosya |
| işlem dosyaları (`robots.py`, ...) | en az bir | işlem mantığı. Bir dosyada konusu ortak 1–3 işlem bulunur |
| `catalog.json` | kural veya metin varsa | kural tanımları (kimlik, kategori, önem, kapsam, gereksinim) ve `en`/`tr` mesajları |
| veri dosyaları (`*.json`) | gerekirse | yalnızca bu özelliğin kullandığı veri: eşikler, sözlükler, şablonlar |
| alt klasör | yalnızca birden çok ilişkili veri dosyası varsa | `axe/` |

`__init__.py` örneği:

```python
from .robots import robots
from .sitemap import generate_sitemap, sitemap
from .transport import transport
...

FEATURE = Feature(
    name="site",
    operations=[transport, site_setup, robots, sitemap, generate_sitemap, ai_access, techstack, dns_health],
    depends_on=["page"],
)

__all__ = ["transport", "site_setup", "robots", "sitemap", "generate_sitemap", "ai_access", "techstack", "dns_health"]
```

`Feature`, `core/feature.py`'de tanımlıdır. Özelliğin `catalog.json`'unu ilk ihtiyaçta yükler, kural kimliklerinin tüm paket içinde benzersiz olduğunu doğrular ve `seoprobe.operations()`, `seoprobe.rules()`, `check_access` için kaydı tutar. Kayıt import yan etkisiyle yapılmaz; `features/__init__.py`'deki açık `FEATURES` listesindedir.

**Büyüme kuralı:** bir işlem dosyası ~400 satırı geçerse, aynı adla bir alt pakete bölünür (ör. `crawl/links.py` → `crawl/links/{graph,indexability}.py`). Derinlik en fazla `features/<özellik>/<konu>/` olur. Bölme ihtiyaçtan doğar, önceden yapılmaz.

### 4.4 Özellik ekleme ve çıkarma

Yeni bir özellik eklemek:

1. `features/<ad>/` klasörünü §4.3 düzeniyle aç.
2. İşlemleri `@operation` ile yaz, çıktı modellerini `models.py`'ye, kurallarını ve metinlerini `catalog.json`'a koy.
3. `FEATURE`'ı `depends_on` ile tanımla ve `features/__init__.py`'deki `FEATURES` listesine ekle.
4. Genel işlemleri `seoprobe/__init__.py`'de dışa aç.
5. `tests/features/<ad>/` altında testleri ve sözleşme anlık görüntülerini ekle.

Bir özelliği çıkarmak: klasörünü, `FEATURES`'taki satırını, `seoprobe/__init__.py`'deki dışa açmalarını ve test klasörünü silmek. Başka hiçbir dosya değişmez; değişmesi gerekiyorsa bağımlılık sözleşmesi bunu zaten yakalar.

Tek bir özellik tek başına test edilebilir: `pytest tests/features/site`.

### 4.5 Neden bu bölümleme

| Karar | Neden |
|---|---|
| `core/` ve `features/` ayrı | temel yapılar tek yerde görülür; özellik kodu temeli değiştirmeden gelişir |
| Özellik başına paket, işlem başına değil | bağımlı işlemler aynı yerde kalır; 50 işlem 50 klasör değil 11 paket olur |
| Her pakette aynı düzen | yeni bir geliştirici bir paketi öğrenince hepsini bilir; inceleme kolaylaşır |
| `models.py` her pakette ayrı | çıktı sözleşmesi mantıktan ayrı incelenir; sürüm değişikliği tek dosyada görünür |
| Kurallar ve metinler `catalog.json`'da | kural eklemek ve çeviri düzeltmek kod değişikliği değildir; özelliğin tüm metni tek dosyadadır |
| Veri dosyaları özelliğin yanında | özellik kendi verisiyle birlikte taşınır veya silinir; paylaşılan veri `core/data/`'dadır |
| `FEATURES` açık liste | hangi özelliklerin yüklü olduğu tek satırda okunur; import sırası davranışı değiştiremez |
| Genel API `seoprobe` kökünde | kullanıcı iç yapıyı bilmeden `seoprobe.<işlem>` çağırır; iç yapı sürüm baskısı olmadan değişebilir |

Dosya sayısı: yaklaşık 107 Python dosyası (kaynakta 146). Özellik yalıtımının bedeli, paket başına bir `__init__.py` ve bir `models.py`; karşılığında her özellik kendi başına okunur, test edilir ve değiştirilir.

## 5. Bağımlılıklar ve paketleme

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "seoprobe"
version = "0.1.0"
requires-python = ">=3.11"
license = "MIT"
dependencies = [
  "aiohttp>=3.9",
  "beautifulsoup4>=4.12",
  "lxml>=5.0",
  "pydantic>=2.7",
  "trafilatura>=2.0",
]

[project.optional-dependencies]
browser = ["playwright>=1.48"]    # render, screenshots, mobile, accessibility, lab_metrics
dns     = ["dnspython>=2.6"]      # dns_health
all     = ["seoprobe[browser,dns]"]

[dependency-groups]
dev = ["pytest>=8", "pytest-asyncio>=0.24", "mypy>=1.10", "ruff", "lxml-stubs", "import-linter"]
```

- Ekstra paketler yalnızca ilgili fonksiyonun içinde import edilir. Kurulu değilse fonksiyon `missing-extra` döner ve mesajda `pip install "seoprobe[browser]"` yazar.
- Geliştirici araçları `[dependency-groups]` altındadır, kullanıcıya kurulmaz.
- Paket içindeki `.json`, `.md` ve `.js` dosyaları wheel'e girer. Yayından önce wheel temiz bir venv'e kurulup her özelliğin verisinin okunduğu doğrulanır.
- axe-core, `features/render/axe/` altında sabit bir sürümle ve kendi lisans dosyasıyla gelir; Node.js gerekmez. MPL-2.0 değiştirilmeden dağıtıma izin verir; lisansı `features/render/axe/LICENSE`'ta durur.

## 6. Sözleşmeler

### 6.1 Result

```python
class Status(StrEnum):
    OK = "ok"            # işlem tam çalıştı
    PARTIAL = "partial"  # çalıştı, bazı kontroller yapılamadı (not_checked dolu)
    ERROR = "error"      # sonuç yok, error dolu

class Result(BaseModel, Generic[T]):
    operation: str                  # "techstack"
    feature: str                    # "site"
    status: Status
    data: T | None
    findings: list[Finding]
    not_checked: list[NotChecked]
    error: ErrorInfo | None
    meta: Meta

    @property
    def ok(self) -> bool: ...                # status != ERROR
    def unwrap(self) -> T: ...               # ERROR ise SeoprobeError fırlatır
    def to_dict(self) -> dict[str, Any]: ... # JSON uyumlu sözlük
    def to_json(self, indent: int | None = None) -> str: ...

class Meta(BaseModel):
    trace_id: str
    schema_version: str             # "techstack/1"
    package_version: str
    rule_version: str
    started_at: datetime
    duration_ms: int
    requests: list[RequestLog]      # url, method, status, duration_ms, size_bytes, error
```

`Finding`: `id`, `check_id`, `severity`, `category`, `scope`, `url`, `evidence`, `message_key`, `message`, `recommendation`, `effort`, `impact`, `is_heuristic`.
`NotChecked`: `check_id`, `reason` (ErrorCode veya gereksinim, ör. `missing-credential:google_api_key`), `message`.

### 6.2 JSON kuralları

- **Ayrıntılı:** hesaplanan her değer `data`'da yer alır: ham ölçümler, kanıtlar, kullanılan eşikler, ara sonuçlar.
- **Metinler hazır gelir:** bulgular hem sabit `message_key`'i hem `Config.lang` dilinde işlenmiş metni taşır.
- **İkili veri JSON içindedir:** ekran görüntüleri `{"mime": "image/png", "base64": "..."}`, sitemap XML metin olarak döner. Paket diske yazmaz.
- **Biçimler sabittir:** tarihler ISO 8601 UTC; süreler `_ms`, boyutlar `_bytes` sonekli tamsayı; oranlar 0–1.
- **`null` ile boş ayrıdır:** ölçülemeyen değer `null` olur ve nedeni `not_checked`'te yazar; boş liste "ölçüldü, yok" demektir.

### 6.3 Hata kodları

`ErrorCode` bir StrEnum'dur (`core/errors.py`); değerleri kebab-case'dir ve yalnızca major sürümde değişir. Özellikler kendi hata türünü tanımlamaz.

| Grup | Kodlar |
|---|---|
| Girdi | `invalid-url`, `unsupported-scheme`, `ssrf-blocked`, `invalid-input` |
| Ağ | `dns-failed`, `connect-failed`, `tls-error`, `fetch-timeout`, `too-many-redirects` |
| Politika | `rate-limited`, `request-budget-exceeded` |
| Gereksinim | `missing-extra`, `missing-credential`, `browser-unavailable` |
| Dış servis | `not-connected`, `no-permission`, `property-not-found`, `api-quota-exceeded`, `api-error` |
| Genel | `timeout`, `internal-error` |

- **Dış servis kodları servise özgü değildir.** Hangi servisten geldiği `details.service` alanında yazar (`gsc`, `ga4`, `bing`, `indexnow`, `pagespeed`, `crux`, `openpagerank`); yeni bir servis yeni hata kodu gerektirmez.
- **`ErrorInfo` alanları:** `code`, `message` (Config.lang dilinde), `retryable`, `details`.
- **`details` yalnızca güvenli alanları taşır:** maskelenmiş `url`, `status`, `service`, `exception` (yalnızca tür adı). Exception metni ve çözümlenmiş IP adresleri yalnızca `DEBUG` loguna yazılır; aksi halde SaaS'ın son kullanıcısı iç ağ bilgisini görebilir.
- **HTTP durumu veridir, hata değildir.** Hedef site bir yanıt döndürdüyse (404, 500 dahil) işlem `ok` olur; durum `Page.status`'ta ve ilgili kural bulgusunda yer alır. `error` yalnızca yanıt alınamadığında döner (DNS, bağlantı, TLS, zaman aşımı, SSRF reddi). Dış API'lerin hata durumları dış servis kodlarıyla döner (`api-error`, `api-quota-exceeded`, ...).
- **`internal-error`** beklenmeyen bir exception'dır: türü `details.exception`'a, izi log'a yazılır.

Exception yalnızca iki durumda fırlatılır:
- programcı hatası: yanlış tipte veya eksik parametre → `TypeError` / `ValueError`;
- `Result.unwrap()` hata içeren bir sonuçta çağrıldığında → `SeoprobeError` (`.code`, `.result` taşır). `internal-error` sonuçlarında özgün exception `__cause__` olarak zincirlenir; `Result` içinde serileşmeyen özel bir alanda tutulur.

`asyncio.CancelledError` her zaman yeniden fırlatılır; iptal yutulmaz.

### 6.4 Gereksinimler

```python
seoprobe.operations()                 # tüm işlemler: ad, özellik, açıklama, Requirements, çıktı şeması
seoprobe.requirements("pagespeed")    # Requirements(network=True, browser=False, extras=[], credentials=["google_api_key"])
await seoprobe.preflight("pagespeed", session=s)      # Preflight(ready=False, missing=["credential:google_api_key"])
await seoprobe.check_access("google", session=s)      # anahtarı zararsız tek istekle sağlayıcıda dener
                                                      # "google" | "openpagerank" | "search_console" | "ga4" | "bing"
```

- Zorunlu bir gereksinim eksikse işlem başlamaz, hemen `error` döner.
- İsteğe bağlı bir kaynak eksikse (ör. `audit_page` içinde PSI anahtarı) işlem sürer; o kısım `not_checked`'e girer ve durum `partial` olur.
- `check_access` her sağlayıcının testini o sağlayıcıyı kullanan özellikte bulur (`FEATURE.access_checks`); `core` hiçbir sağlayıcıyı tanımaz.

### 6.5 `@operation`: ortak davranışın tek yeri

```python
@operation("techstack", output=TechStack, requires=Requirements(network=True), timeout_s=20)
async def techstack(ctx: Context, target: str | Page) -> Outcome[TechStack]:
    page = await ctx.page(target)          # str ise çeker, Page ise yeniden kullanır
    ...
    return Outcome(data=TechStack(...), findings=[ctx.finding("techstack-outdated-cms", evidence=...)])
```

Dekoratör, kullanıcıya `async def techstack(target, *, session=None) -> Result[TechStack]` imzasını sunar ve sırasıyla:

1. Girdileri doğrular.
2. Oturumu alır ya da geçici bir oturum açar.
3. Gereksinimleri denetler.
4. `trace_id` üretir; `contextvars` ile HTTP ve tarayıcı katmanına iletir. Her istek `meta.requests`'e kaydedilir.
5. Zaman aşımını uygular.
6. Exception'ları `ErrorCode`'a eşler.
7. `seoprobe.<özellik>.<işlem>` logger'ına yazar, `Session(on_event=...)` kancasına `started` ve `finished` olaylarını gönderir.
8. `Result`'ı kurar (bulgu metinlerini özelliğin kataloğundan çevirerek) ve geçici oturumu kapatır.

İç uygulama tek bir büyük fonksiyon değildir: her aşama `core/feature.py`'de ayrı, küçük bir fonksiyondur ve sıra koddadır. Middleware zinciri yoktur. Metrik kütüphanesi de yoktur; `finished` olayı süreyi, istek sayısını ve baytı taşır, metrik sistemine bağlamak kullanan projenin işidir. İstek ve bayt bütçesi `HostLimiter`'da uygulanır; dekoratör yalnızca aşım exception'ını koda çevirir.

**İç içe işlemler** (`audit`, `crawl`, `monitoring` başka işlemleri çağırır):
- İç işlem dıştakinin oturumunu kullanır; geçici oturum açmaz.
- Son tarih `contextvars` ile aşağı akar. İç işlem `asyncio.timeout_at(min(kendi süresi, dıştakinin kalan süresi))` ile çalışır; hiçbir iç işlem dıştakinin süresini aşamaz.
- `trace_id` paylaşılır. İstekler en içteki işlemin `meta.requests`'ine kaydedilir; dıştaki işlem alt sonuçları kendi `data`'sında taşır.

Özellik kodunda `try/except`, zaman aşımı veya log tekrarlanmaz. `ctx.finding(...)` yalnızca özelliğin kataloğunda tanımlı bir kural kimliğini kabul eder; tanımsız kimlik testlerde hata verir.

### 6.6 Session ve Config

```python
class Session:
    def __init__(self, config: Config | None = None, *,
                 on_event: Callable[[Event], Awaitable[None] | None] | None = None,
                 token_store: TokenStore | None = None,
                 http: HttpClient | None = None,          # yalnızca testler için enjeksiyon noktası
                 browser: Browser | None = None) -> None: ...
    async def __aenter__(self) -> Session: ...    # AsyncExitStack: HttpClient + HostLimiter açılır
    async def browser(self) -> Browser: ...       # ilk ihtiyaçta, asyncio.Lock altında bir kez başlatılır
    async def __aexit__(self, *exc: object) -> None: ...   # ters sırada kapatır; iptalde de çalışır

@dataclass(frozen=True)
class Config:
    lang: Literal["en", "tr"] = "en"
    user_agent: str = "seoprobe/<sürüm> (+https://github.com/Arslanex/seo-probe-lib)"
    fetch_timeout_s: float = 20.0
    per_host_concurrency: int = 2
    request_delay_s: float = 0.4
    max_pages: int = 25
    max_pages_cap: int = 200
    max_external_link_checks: int = 40
    max_connections: int = 100           # oturumdaki toplam eşzamanlı bağlantı (TCPConnector limit)
    max_response_bytes: int = 10_485_760 # yanıt başına gövde sınırı (10 MB)
    max_total_bytes: int = 268_435_456   # işlem başına toplam indirme bütçesi (256 MB)
    max_batch_urls: int = 1000           # check_urls, check_redirects, snapshot
    browser_concurrency: int = 2         # aynı anda açık tarayıcı sayfası
    render: bool = True
    google_api_key: str | None = field(default=None, repr=False)      # PSI + CrUX
    openpagerank_key: str | None = field(default=None, repr=False)
    bing_api_key: str | None = field(default=None, repr=False)
    google_app: GoogleApp | None = None                                # Search Console ve GA4 OAuth

    @classmethod
    def from_env(cls, prefix: str = "SEOPROBE_") -> Config: ...
```

**Session kuralları:**
- `Session` üç somut nesneyi bir araya getirir: `HttpClient`, `HostLimiter`, `Browser` (tembel). Ayrı bir Manager, Registry veya Factory sınıfı yoktur.
- **Eşzamanlılık güvencesi:** tek bir olay döngüsü içinde birden çok görev aynı oturumu kullanabilir. Oturum iş parçacıkları veya olay döngüleri arasında paylaşılamaz (aiohttp `ClientSession` kuralıyla aynı).
- **SaaS kullanımı:** süreç başına tek, uzun ömürlü bir oturum önerilir. Böylece bağlantı havuzu, host başına nezaket kuralları ve tarayıcı tüm istekler arasında paylaşılır. Çağrı başına sınırlar (`max_pages`, zaman aşımı) parametredir.
- **Bellek sınırı:** bir işlemin indirdiği toplam bayt `max_total_bytes`'ı aşamaz. Aşılırsa kalan sayfalar `not_checked`'e `request-budget-exceeded` ile girer, durum `partial` olur.

Sırlar `repr`'de görünmez, log'a, `Result`'a ve `details`'e yazılmaz. `Config` her özelliğin ayarını tek yerde toplar; özellikler ortam değişkeni okumaz.

### 6.7 Ağ davranışı

| Konu | Karar |
|---|---|
| Bağlantı havuzu | oturum başına tek aiohttp `ClientSession`; toplam bağlantı `max_connections` ile sınırlı |
| Host başına | en fazla `per_host_concurrency` eşzamanlı istek, `request_delay_s` aralık, art arda hatada devre kesici |
| Dış API'ler | API host'ları da aynı `HostLimiter`'dan geçer |
| Yeniden deneme | yalnızca dış API'lerde (PSI, CrUX, Search Console, GA4, Bing, OpenPageRank, Wayback, Common Crawl, RDAP) ve yalnızca idempotent isteklerde; 429 ve 5xx'te en fazla 3 deneme, üstel geri çekilme + tam jitter, `Retry-After`'a uyulur |
| Denetlenen site | **yeniden denenmez**: nezaket ve davranış eşliği (kaynak proje de denemiyor) |
| Son tarih | her işlemin `timeout_s`'i; iç içe işlemlerde dıştakinin kalan süresi (§6.5) |
| İptal | `CancelledError` yutulmaz; tarayıcı context'leri ve bağlantılar `finally` / `AsyncExitStack` ile kapanır |
| CPU işleri | uzun süren CPU işleri (`analyze_logs` satır işleme) sabit boyutlu parçalar halinde `asyncio.to_thread` ile yapılır; olay döngüsü kilitlenmez |
| Önbellek | oturumlar arası önbellek yoktur. Denetim içinde paylaşım açıktır: `Page` bir kez çekilir ve işlemlere verilir |

### 6.8 Güvenlik

- **SSRF:** her istek ve her yönlendirme adımı URL politikasından geçer; yönlendirmeler elle, en fazla 5 adım izlenir; adres bağlantı anında doğrulayan çözümleyicide denetlenir (DNS rebinding'e karşı); IPv4-mapped IPv6, DNS64, CGNAT, link-local ve bulut metadata adresleri engellenir. Kural dış API çağrıları ve kullanıcının verdiği URL listeleri için de aynıdır. Kaynak projedeki uygulama olduğu gibi korunur.
- **Tarayıcı:** her render ayrı context'tedir; tüm alt istekler ve yönlendirmeler aynı politikadan geçer; service worker engelli, WebSocket bağlanmaz, indirme kapalı.
- **Anahtarlar URL'ye yazılmaz.** Google API anahtarı `X-Goog-Api-Key`, OpenPageRank anahtarı `API-OPR` başlığıyla gönderilir. Ek güvence olarak `RequestLog` URL'lerinde `key`, `api_key`, `access_token`, `token`, `code`, `state` parametreleri her zaman `***` ile maskelenir.
- **XML:** DOCTYPE reddedilir, `resolve_entities=False`.
- **Kaynak tüketimi:** yanıt gövdesi (`max_response_bytes`, açılmış bayt üzerinden), yönlendirme (5), sayfa (`max_pages_cap`), toplu URL (`max_batch_urls`), toplam bayt (`max_total_bytes`), tarayıcı eşzamanlılığı (`browser_concurrency`).
- **OAuth:** PKCE (S256) ve tek kullanımlık `state`. Kütüphane durumsuzdur (§7.8). Token'ların saklanması `TokenStore` ile kullanan projenindir; SaaS'ta şifreli depolama önerilir.
- **Çıktıdaki sayfa kaynaklı metinler güvenilmeyen veridir.** Title, açıklama, bağlantı metni gibi dizeler doğrudan siteden gelir. Kütüphane onları değiştirmez, yalnızca uzunluk sınırı uygular; gösterirken kaçış yapmak (XSS) kullanan projenin sorumluluğudur.

### 6.9 Dağıtım gereksinimleri

Tarayıcı kullanan işlemler (`render`, `screenshots`, `mobile`, `accessibility`, `lab_metrics`) sunucuda çalıştırılacaksa **ağ düzeyinde bir çıkış politikası zorunludur**: süreç özel ağ aralıklarına, loopback'e, link-local ve bulut metadata adreslerine bağlanamamalıdır (container network policy, güvenlik grubu veya egress firewall). Neden: Chromium DNS'i kendisi çözer; kütüphanenin istek kontrolü ile Chromium'un bağlantısı arasında kötü niyetli bir DNS sunucusunun kazanabileceği bir yarış kalır. HTTP istemcisinde bu yarış yoktur, çünkü adres bağlantı anında doğrulanır. Bu gereksinim, ilgili işlemlerin `Requirements` açıklamasında ve README'de yer alır.

## 7. İşlem kataloğu

Kaynak projedeki tüm yetenekler ve kaynakta olmayan yeni işlemler (§7.11) özelliklere göre aşağıdadır. Her işlem `seoprobe.<ad>` olarak çağrılır ve `Result` döner.

### 7.1 `core/page.py` ve `page`

`fetch` ve `normalize` `core/page.py`'de, `onpage` ve `classify` `features/page`'dedir. Genel API'de fark yoktur: hepsi `seoprobe.<ad>` olarak çağrılır.

| İşlem | Ne yapar | Gereksinim |
|---|---|---|
| `fetch(url)` | SSRF-güvenli çekme; yönlendirme zinciri, TTFB, başlıklar, ayrıştırılmış sayfa → `Page` (`core`) | ağ |
| `normalize(url)` | normalizasyon ve ret nedeni, ağsız (`core`) | — |
| `onpage(target)` | sayfa içi kurallar: title, meta, başlıklar, canonical, görseller, robots meta, URL biçimi, ... | ağ |
| `classify(target)` | iş tipi ve içerik dili | ağ |

### 7.2 `content`

| İşlem | Ne yapar | Gereksinim |
|---|---|---|
| `readability(target, keywords=)` | Flesch / Ateşman, hedef kelime denetimi, sık terimler | ağ |
| `previews(target)` | Google SERP önizlemesi, Open Graph/Twitter kartı, görsel boyut doğrulama | ağ |
| `schema(target)` | JSON-LD algılama ve doğrulama, eksik önemli alanlar | ağ |
| `suggest_schema(target)` | sayfa tipine göre kopyala-yapıştır JSON-LD, zengin sonuç uygunluğu | ağ |
| `local_seo(target)` | NAP çıkarma, tutarsızlık, `tel:` bağlantıları | ağ |

### 7.3 `site`

| İşlem | Ne yapar | Gereksinim |
|---|---|---|
| `transport(url)` | TLS sertifikası, güvenlik başlıkları, http→https, www birleştirme | ağ |
| `site_setup(url)` | yönlendirme matrisi, soft-404, özel 404, HTTP sürümü, sıkıştırma, `security.txt`, `ads.txt`, favicon, manifest | ağ |
| `robots(url)` | robots.txt ayrıştırma, AI tarayıcı kuralları, URL izin sorgusu | ağ |
| `sitemap(url)` | keşif (robots, yaygın yollar), index açma, analiz | ağ |
| `generate_sitemap(pages)` | örneklemden önerilen `sitemap.xml`, mevcutla karşılaştırma | ağ |
| `ai_access(url)` | `llms.txt`, Markdown erişimi, `Content-Signal`, WebMCP izi | ağ |
| `techstack(target)` | CMS, çerçeve, CDN, analitik, etiket yöneticisi, sunucu | ağ |

### 7.4 `crawl`

| İşlem | Ne yapar | Gereksinim |
|---|---|---|
| `crawl(url, max_pages=, urls=)` | keşif, tabakalı ve tohumlu örnekleme, nezaket kuralları → `list[Page]` | ağ |
| `links(pages)` | iç/dış bağlantı grafiği, kırık bağlantılar, yetim sayfalar, kopya başlık ve açıklamalar | ağ |
| `indexability(pages)` | indekslenebilirlik tablosu: noindex, canonical, robots, sitemap | ağ |
| `hreflang(pages)` | geçerli kodlar, kendine referans, karşılıklılık, x-default, canonical çakışması | ağ |

### 7.5 `render`

| İşlem | Ne yapar | Gereksinim |
|---|---|---|
| `render(target)` | headless render, SPA tespiti, ham/render farkı | ağ, `[browser]` |
| `screenshots(target)` | masaüstü ve mobil görüntü, ekran üstü analizi | ağ, `[browser]` |

### 7.6 `performance`

| İşlem | Ne yapar | Gereksinim |
|---|---|---|
| `lab_metrics(target)` | lab ölçümü, sayfa ağırlığı, kaynaklar, görsel analizi | ağ, `[browser]` |
| `pagespeed(url)` | PageSpeed Insights | ağ, `google_api_key` |
| `field_data(url)` | CrUX saha verisi (URL ve origin) | ağ, `google_api_key` |
| `web_vitals(url)` | üçünün birleşimi + performans kuralları | ağ; eksikler `partial` |

### 7.7 `offsite`

| İşlem | Ne yapar | Gereksinim |
|---|---|---|
| `authority(domain)` | OpenPageRank otorite tahmini (skora girmez) | ağ, `openpagerank_key` |
| `visibility(url)` | Common Crawl, Wayback geçmişi, RDAP alan adı yaşı | ağ |
| `competitors(url, competitors=)` | ≤3 rakibin giriş sayfasını aynı hafif analizle karşılaştırma | ağ |

### 7.8 `search`: `seoprobe.gsc`

| İşlem | Ne yapar |
|---|---|
| `gsc.auth_url(redirect_uri)` / `gsc.exchange_code(request, code)` | OAuth akışı. `auth_url` yetki URL'ini ve bir `AuthRequest` (state, PKCE verifier, redirect_uri, oluşturma zamanı) döner; çağıran onu saklar ve geri dönüşte `exchange_code`'a verir. Kütüphane bekleyen istekleri bellekte tutmaz, böylece çok örnekli bir SaaS'ta da çalışır. Token `TokenStore`'a yazılır |
| `gsc.properties()` | erişilebilir mülkler |
| `gsc.find_property(url)` | URL'e uyan mülk |
| `gsc.analytics(property, days=, dimensions=, query=)` | tıklama, gösterim, CTR, konum |
| `gsc.inspect(property, url)` | URL denetimi |
| `gsc.sitemaps(property)` | gönderilmiş sitemap'ler |
| `gsc.rankings(property, keywords=)` | kelime → konum ve sayfa |
| `gsc.top_queries(url)` | sitenin son 28 gündeki en çok gösterilen gerçek sorguları |
| `gsc.summary(url)` | yukarıdakilerin birleşimi + kurallar |

Hepsi `config.google_app` ve bağlı bir hesap gerektirir. `TokenStore` bir protokoldür; paket `MemoryTokenStore` ve `FileTokenStore` sunar, kullanan proje kendi deposunu verebilir.

### 7.9 `audit`

| İşlem | Ne yapar |
|---|---|
| `audit_page(url, keywords=, competitors=)` | tek sayfada tüm uygun işlemler → `Audit` |
| `audit_site(url, max_pages=, urls=, keywords=, competitors=)` | örneklenen sayfalarda aynısı |
| `audit_stream(url, mode=, ...)` | aynı denetim; `AsyncIterator[AuditEvent]` verir, son olay `Result[Audit]` taşır |
| `rerun(audit, operations=)` | bir denetimde yalnızca seçilen işlemleri yeniden çalıştırır |

- Bir işlem başarısız olursa denetim durmaz; işlem `not_checked`'e kendi `ErrorCode`'uyla girer, durum `partial` olur.
- İptal standart asyncio'dur: `task.cancel()`.
- `Audit`, tüm işlem çıktılarını, bulguları ve skoru içeren tam JSON'dur. Saklanması kullanan projeye aittir.

### 7.10 `report`: `seoprobe.report`

Saf fonksiyonlar; ağa çıkmaz.

| İşlem | Ne yapar |
|---|---|
| `report.build(audit)` | `Audit` → `AuditReport`: kategoriler, öne çıkanlar, kapsam, "ölçülmeyenler", kalite kapısı |
| `report.score(findings, ...)` | deterministik kategori ve genel skor, kapı, kapsam, güven etiketi |
| `report.plan(report)` | 4 fazlı aksiyon planı, hızlı kazanımlar |
| `report.compare(old, new)` | skor farkı, yeni / çözülen / süren bulgular, metrik değişimi |
| `report.findings(report, severity=, category=, scope=, check_id=, sort=)` | bulguları süzer; `sort="priority"` öncelik puanına göre |
| `seoprobe.rules()` | tüm özelliklerin kural kataloğu: id, özellik, kategori, önem, kapsam, gereksinim |

### 7.11 Kaynakta olmayan yeni işlemler

Kaynak projede olmayan 13 yeni işlem. Hepsi şu koşulları sağlar:

- **Yeni ücretli API yok.** Mevcut kaynaklar, ücretsiz resmî API'ler veya pakete gömülü açık kaynak bileşenler kullanılır.
- **Sunucuda çalışır.** Masaüstü veya kullanıcı tarayıcısı gerekmez; tarayıcı isteyenler headless Playwright ile çalışır.
- **Denetlenebilir.** Deterministiktir; her bulgu bir kural kimliği, kanıt ve kullanılan eşik değerini taşır. Eşikler parametredir, varsayılanları özelliğin veri dosyasındadır.

Bu işlemler `audit_page` / `audit_site` içine ve skora **girmez**. Her biri kendi `Result`'ını döner, kuralları `seoprobe.rules()`'da görünür. Böylece kaynaktan taşınan kuralların skoru kaynak projeyle aynı kalır. Denetime eklenmeleri, eşikler gerçek veriyle ayarlandıktan sonra verilecek ayrı bir karardır.

"Etki" sütunu, işlemin sıralamaya katkısını gösterir:
- **Doğrudan:** taramayı, indekslemeyi veya sıralamayı etkileyen bir sorunu ya da fırsatı bulur.
- **Dolaylı:** kullanıcı davranışı veya tarama verimliliği üzerinden etki eder.
- **Zayıf:** değeri SEO dışında.

| # | İşlem | Özellik | Ne yapar | Gereksinim | Etki |
|---|---|---|---|---|---|
| 1 | `gsc.opportunities(property, days=, compare_days=, ga4_property=)` | `search` | **striking:** 8–20. sıradaki yüksek gösterimli sorgular. **low_ctr:** konumuna göre beklenenden düşük CTR (beklenen eğri `ctr-curve.json`'da, bulgular `is_heuristic`). **cannibalization:** aynı sorguda gösterimi bölüşen birden çok sayfa. **declining:** iki dönem arasında tıklaması düşen sayfalar. `ga4_property` verilirse fırsatlar dönüşüm değerine göre sıralanır | OAuth | Doğrudan |
| 2 | `check_redirects(mapping)` | `monitoring` | eski→yeni URL listesinin her satırı için durum, adım sayısı, son URL, beklenen hedefe eşleşme, döngü | ağ | Doğrudan |
| 3 | `suggest_links(pages, targets=)` | `crawl` | az iç bağlantı alan sayfalara, konu olarak yakın kaynak sayfaları ve aday çapa metinlerini önerir. TF-IDF benzerliği kullanır; benzerlik puanı çıktıda yer alır | `crawl` çıktısı | Doğrudan |
| 4 | `analyze_logs(lines, format=, urls=)` | `monitoring` | erişim loglarından bot başına tarama sayısı, durum dağılımı, en çok taranan yollar, boşa giden tarama (parametreli URL, 404, yönlendirme), `urls` verilirse hiç taranmayan sayfalar. Botlar ters + ileri DNS ile "doğrulanmış / taklit" ayrılır. Okunamayan satır sayısı çıktıda yer alır | DNS | Doğrudan |
| 5 | `check_urls(urls)` | `monitoring` | URL başına durum, yönlendirme, canonical, noindex (meta ve başlık), robots izni, isteğe bağlı sitemap'te olup olmadığı | ağ | Doğrudan |
| 6 | `snapshot(url)` / `diff(old, new)` | `monitoring` | sayfanın SEO açısından önemli durumunun JSON görüntüsü: durum, son URL, title, açıklama, canonical, robots, H1, hreflang, schema tipleri, içerik özeti, kelime sayısı. `diff` değişiklikleri önem derecesiyle listeler (ör. noindex eklendi: kritik) | ağ | Doğrudan |
| 7 | `mobile(target)` | `render` | mobil görünümde dokunma hedefi boyutu ve çakışması, küçük yazı oranı, yatay taşma, viewport; masaüstü ile içerik ve bağlantı eşitliği | `[browser]` | Dolaylı |
| 8 | `ga4.properties()`, `ga4.landing_pages(property, days=, channel=)`, `ga4.summary(url)` | `search` | açılış sayfası başına organik oturum, etkileşim, anahtar olaylar ve gelir | OAuth (`analytics.readonly`) | Dolaylı |
| 9 | `indexnow.key_file(key)`, `indexnow.verify(host, key)`, `indexnow.submit(host, key, urls)` | `search` | Bing ve Yandex'e indeksleme bildirimi; anahtar dosyasının içeriğini üretir ve sitede yayında olduğunu doğrular | ağ; sitede anahtar dosyası | Dolaylı |
| 10 | `bing.sites()`, `bing.query_stats(site)`, `bing.crawl_stats(site)`, `bing.crawl_issues(site)`, `bing.url_info(site, url)` | `search` | Bing Webmaster verileri: sorgular, tarama istatistikleri, tarama sorunları, URL durumu | `bing_api_key` | Dolaylı |
| 11 | `accessibility(target, standard=)` | `render` | axe-core ile WCAG kontrolleri. İhlal başına kural kimliği, WCAG maddesi, etki derecesi, CSS seçici, HTML parçası; elle doğrulanması gerekenler ayrı listede | `[browser]` | Dolaylı |
| 12 | `dns_health(domain)` | `site` | SPF (varlık, geçerlilik, 10 sorgu sınırı), DMARC politikası, MX, CAA, DNSSEC, IPv6 (AAAA), ad sunucusu sayısı ve çeşitliliği | `[dns]` | Zayıf |
| 13 | `carbon(target)` | `performance` | aktarılan bayttan sayfa görüntüleme başına CO₂ tahmini (Sustainable Web Design modeli); model sürümü ve sabitler `carbon-model.json`'da, çıktıda da yazar | ağ | Zayıf |

**SaaS'ta güvenli çalıştırma:**

- **Toplu işlemlerin sınırı var.** `check_urls`, `check_redirects` ve toplu `snapshot` `Config.max_batch_urls`'ü aşamaz. Fazlası işlenmez; işlenmeyen sayısı çıktıda yazar.
- **Tüm giden istekler SSRF-güvenli istemciden geçer.** Kullanıcının verdiği URL listeleri ve yönlendirme hedefleri dahil, iç ağ adreslerine istek yapılamaz.
- **Log analizi sabit bellekle çalışır ve olay döngüsünü kilitlemez.** `analyze_logs` satırları akış olarak okur (`Iterable[str]` veya `AsyncIterable[str]`), dosyayı belleğe almaz; satırları sabit boyutlu parçalar halinde `asyncio.to_thread` ile işler. Ters DNS sonuçları oturum içinde önbelleğe alınır.
- **Tarayıcı kullanımı sınırlı.** Tarayıcı isteyen işlemler aynı tarayıcıyı paylaşır; eşzamanlı sayfa sayısı `Config.browser_concurrency` ile sınırlanır.
- **Tek yazma işlemi `indexnow.submit`'tir.** Hiçbir başka işlem, `audit` dahil, onu kendiliğinden çağırmaz.

### 7.12 Kapsam dışı

| Yok | Kullanan proje ne yapar |
|---|---|
| Web arayüzü, HTTP API, CLI, HTML/PDF rapor | `Result` JSON'unu kendi arayüzünde gösterir |
| Veritabanı, denetim listesi, site geçmişi | JSON'u saklar; `report.compare` ve `diff` iki sonucu alır |
| İş kuyruğu, worker, zamanlanmış tarama | işlemler coroutine'dir; kendi kuyruğunda ve zamanlayıcısında çalıştırır |
| Site profili, soru-cevap geçmişi | parametre olarak verir |
| Sayfalama ve SSE | `report.findings` tam liste döner; olaylar `audit_stream` ile gelir |
| LLM ile yorum, özet ve soru-cevap | `Result` ve `AuditReport` eksiksiz JSON olduğu için kendi LLM katmanına girdi olarak verir; paket hiçbir LLM'e bağlı değildir |

## 8. Test

- **Yapı:** `tests/core/` çekirdeği, `tests/features/<özellik>/` her özelliği test eder. Bir özelliğin testleri yalnızca o özelliği ve bağımlılıklarını yükler; `pytest tests/features/<özellik>` tek başına çalışır.
- **Birim:** ağsız; `tests/fakes.py` içindeki sahte HTTP, tarayıcı ve Google ile. `Session(http=..., browser=...)` sahteleri alır.
- **Ağ varsayılan olarak kapalı:** `conftest.py` yerel adresler dışındaki soket bağlantılarını engeller; yeni bağımlılık gerekmez.
- **Güvenlik:** SSRF derlemi (kaynak projedeki testler), yönlendirmeyle SSRF, sahte çözümleyiciyle DNS rebinding, anahtar maskeleme (hiçbir `Result` JSON'unda ve log kaydında anahtar değeri yok), `details`'te iç adres yok, XML entity reddi.
- **Eşzamanlılık:** iptalde açık bağlantı veya tarayıcı kalmıyor; iç işlem dıştakinin kalan süresini aşamıyor; tarayıcı tek kez başlatılıyor; `analyze_logs` sırasında olay döngüsü 100 ms'den uzun kilitlenmiyor.
- **Sözleşme (`tests/contract/`):**
  - **Çıktı şemaları:** her çıktı modelinin JSON Schema'sı anlık görüntüdür (`schemas/<özellik>/<işlem>.json`). Değişirse test kırılır; `schema_version` artırılır ve CHANGELOG'a yazılır.
  - **Bağımlılıklar:** her özelliğin bildirdiği bağımlılıklar gerçek import'larıyla aynıdır; başka bir özelliğin iç dosyasına ve `__all__` dışındaki adlarına erişim yoktur.
  - **Kataloglar:** her `catalog.json` şemaya uyar, kural kimlikleri paket genelinde benzersizdir ve her kuralın `en` ve `tr` metni vardır.
  - **Belgeler:** `ErrorCode` ile `docs/errors.md`, `seoprobe.operations()` ile `docs/operations.md` birebir aynıdır.
  - **Genel API:** `seoprobe.__all__` ve her genel fonksiyonun `inspect.signature`'ı anlık görüntüdür; değişirse test kırılır.
  - **Hata kodları:** her `ErrorCode` en az bir testte üretilir.
- **Davranış eşliği:** kaynak projenin altın örnekleri `tests/fixtures/`'a taşınır; aynı girdi kaynak projeyle aynı bulguları ve skoru vermelidir.
- **Paket:** yayından önce temiz venv'e wheel kurulur; ekstrasız `import seoprobe`, her özelliğin veri dosyasının okunması ve ekstrası olmayan işlemin `missing-extra` dönmesi denenir.
- **Kalite:** `ruff check`, `ruff format --check`, `mypy --strict`, `lint-imports`, `pytest`.

## 9. Sürümleme

- Semver. Sözleşme: `seoprobe.__all__`, genel fonksiyon imzaları, `ErrorCode` değerleri, çıktı şemaları.
- **Kullanımdan kaldırma:** bir genel ad en az bir minor sürüm boyunca `DeprecationWarning` ile çalışmaya devam eder, ardından major sürümde kaldırılır (PEP 387 yaklaşımı). CHANGELOG'da "Deprecated" başlığı altında yazılır.
- Her çıktı modelinin sürümü vardır (`techstack/1`). Alan eklemek minor, alan silmek veya anlamını değiştirmek major değişikliktir. Özellikler birbirinden bağımsız sürümlenir: `site`'ta bir şema değişikliği `crawl`'un şema sürümünü etkilemez.
- `rule_version` `Meta`'da raporlanır; aynı sürümde aynı girdi aynı skoru verir.
- CHANGELOG "Keep a Changelog" biçimindedir; girdiler özellik adıyla başlar (`site: ...`).

## 10. Uygulama aşamaları

Her aşamanın sonunda testler geçer ve paket kurulabilir durumdadır. Aşamalar özellik paketleri üzerinden ilerler; bir özellik bitmeden sonrakine geçilmez.

| # | Aşama | İçerik | Bitti sayılır, eğer |
|---|---|---|---|
| 0 | İskelet | `pyproject.toml`, README, LICENSE, boş `core/` ve `features/` | `pip install -e .` ve `import seoprobe` çalışıyor |
| 1 | Temel sözleşmeler | `core/{config,session,result,errors,models,feature,catalog}`, `core/data/catalog.json`, `docs/errors.md`, import-linter sözleşmeleri | `@operation`'ın 8 adımı ve katalog doğrulaması sahte bir özellikle test edilmiş |
| 2 | Temel altyapı | `core/{http,html,urls,text,page}`: güvenli HTTP, anahtar maskeleme, yeniden deneme, bayt bütçesi, `fetch`, `normalize` | SSRF, güvenlik ve ayrıştırıcı testleri geçiyor; hiçbir `Result`'ta anahtar yok |
| 3 | Örnek özellik: `page` | `onpage`, `classify` | §4.3 düzeni oturmuş; diğer özellikler için şablon; sözleşme testleri var |
| 4 | `content`, `site` | §7.2, §7.3 | davranış testleri geçiyor; bağımlılık sözleşmesi yeşil |
| 5 | `crawl` | §7.4 | davranış testleri geçiyor |
| 6 | `render`, `performance` | `core/browser`, §7.5, §7.6 | ekstra yokken `missing-extra` dönüyor |
| 7 | `search`, `offsite` | §7.7, §7.8 | anahtar veya OAuth yokken `missing-credential` dönüyor |
| 8 | `audit`, `report` | §7.9, §7.10 | davranış eşliği testleri kaynak projeyle aynı bulguları ve skoru veriyor |
| 9 | Belgeler | `docs/operations.md`, README örnekleri, CHANGELOG | temiz venv wheel testi geçiyor |
| 10 | `monitoring` (yeni özellik) | `check_urls`, `check_redirects`, `snapshot`, `diff`, `analyze_logs` | 1 milyon satırlık log sabit bellekle işleniyor; bot doğrulaması sahte DNS ile test edilmiş |
| 11 | `search` genişlemesi | `gsc.opportunities`, `ga4`, `bing`, `indexnow` | eşikler parametreli; `indexnow.submit` yalnızca açık çağrıyla çalışıyor |
| 12 | `crawl` ve `render` genişlemesi | `suggest_links`, `mobile`, `accessibility` | axe-core sürümü sabit; her ihlal bir WCAG maddesine bağlı |
| 13 | `site` ve `performance` genişlemesi | `dns_health`, `carbon`, CHANGELOG | temiz venv wheel testi geçiyor |

## 11. Uygulamada netleşen kararlar

Uygulama sırasında plandan ayrılan ya da plana eklenen kararlar. Kod bunları izler; plan metninin geri kalanıyla çeliştiği yerde bu bölüm geçerlidir.

| # | Karar | Neden |
|---|---|---|
| 1 | Skor hesabı `core/rules/scoring.py`'de (skor modelleri ve `PageRecord` dahil); `report.score` işlemi yok | skoru hem `audit` hesaplar hem `report` gösterir; `report`'ta olsaydı `audit → report → audit` döngüsü olurdu |
| 2 | `Page`, `fetch`, `normalize` `core/page.py`'de; `page` özelliği `onpage`, `classify` | `core`'un kendisi (`ctx.page`) sayfa çeker (review P0-1) |
| 3 | Özelliğin `__all__`'ı işlemler ve modellerin yanında, başka özelliklerin ihtiyaç duyduğu **saf yardımcı fonksiyonları** da içerebilir (ör. `run_page_checks`, `readability_scores`, `can_fetch`, `parse_robots`, `detect`, `header_directives`) | kopya kod yerine tek tanım; `audit` ve `offsite` kaynakla birebir aynı hesabı bu fonksiyonlarla yapar |
| 4 | `CheckDef.scored`: kaynak projede olmayan kurallar `"scored": false` | yeni kurallar raporlanır ama skora girmez; kaynaktan taşınan kuralların skoru kaynakla aynı kalır |
| 5 | İç içe işlemlerin istekleri dıştakinin `meta.requests`'ine de eklenir | bir işlemin kaydı, çalışırken yapılan bütün istekleri gösterir |
| 6 | Ölümcül denetim hatası (geçersiz/engelli URL, ulaşılamayan giriş sayfası) `status=ok` bir `Result` döner; `Audit.status = failed`, `Audit.error` yalnızca sabit kod, `Audit.error_code` | kaynak davranışı korunur; ayrıntı iç ağ bilgisi sızdırabileceği için yalnızca DEBUG logunda |
| 7 | `rerun(audit)` aynı URL, mod ve seçeneklerle yeni bir denetimdir (seçici işlem yok) | kaynaktaki `rerun` da böyleydi |
| 8 | `report.build(audit, previous=...)`: geçmiş bölümü önceki denetimle dolar | kütüphanede veritabanı yok; önceki denetimi çağıran verir |
| 9 | Büyüyen dosyalar alt pakete bölündü: `content/schema/`, `site/sitemap/`, `crawl/links/`, `report/sections/` | iki ayrı sorumluluk taşıyan dosyalar (kural 1) |
| 10 | Varsayılan dil `en`; Türkçe `Config(lang="tr")` | kod dili İngilizce (§3.3) |
| 11 | `ErrorCode.TOO_MANY_REDIRECTS` eklendi; `details` alanlarına `reason` (sabit kod) eklendi | yönlendirme döngüsü ayrı bir durum; OAuth ve girdi reddinin nedeni ayırt edilebilir |
| 12 | Bing Webmaster anahtarı URL'de (`apikey`) gider; `RequestLog`'da maskelenir | Bing API'si anahtarı başlıkta kabul etmiyor (resmî belge) |
| 13 | `site_setup` soft-404 yoklama yolu `-seoprobe-probe` ile biter (kaynakta `-seoa-probe`) | eski ürün adı hedef sitelere gönderilmez |
| 14 | `schema-none-found` kuralına metin eklendi (kaynakta metinsizdi, mesaj olarak anahtar görünüyordu) | bulgu ve skor değişmez, yalnızca okunur metin eklenir |
| 15 | Rapordan LLM bölümleri, sunucu API bağlantıları (`/v1/...`, indirme bağlantıları) ve arayüz metinleri çıkarıldı; 18 metin kütüphane terimleriyle yeniden yazıldı | kütüphanede LLM, API ve arayüz yok |
| 16 | Eşlik görüntüleri anahtarsız, ağsız ortamda üretilir (`tests/fixtures/parity/README.md`) | ilk üretimde kaynak `.env`'deki gerçek anahtarlarla canlı istek yapmıştı; görüntüler deterministik değildi |
| 17 | `core` amaca göre alt paketlere bölündü: `parse/` (html, text, urls), `net/` (http istemcisi, güvenlik, nezaket, API, tarayıcı), `rules/` (catalog, scoring). `config`, `errors`, `models`, `result`, `feature`, `session`, `page` kökte kalır. `core` içindeki katmanlar import-linter ile denetlenir: `feature : session : page` > `net \| rules` > `parse` > `config : errors : models : result`. `RequestBudget` `net/politeness.py`'den `net/client.py`'ye taşındı. `core/data/` yerinde kaldı | düz 14 dosyalık `core` birbirinden bağımsız sorumlulukları karıştırıyordu ve iç bağımlılıklar denetlenmiyordu; `RequestBudget` istemciye ait bir sarmalayıcıdır ve `politeness ↔ client` arasında gizli bir döngü yaratıyordu. Veri dosyaları birden çok özellik tarafından paket adıyla okunduğu için taşınmadı |
| 18 | `URLSafetyError.code` reddin nedenini taşır (`INVALID_URL`, `UNSUPPORTED_SCHEME`, diğer her şey `SSRF_BLOCKED`); `Context.page` girdi URL'sini çekmeden önce bu iki girdi hatasını kendisi döndürür, SSRF kararı istemcide kalır. `normalize` ve audit doğrulaması mesaj metninde `"scheme"` aramak yerine bu alanı kullanır | önceden `fetch("ftp://...")` ve `fetch("not a url")` `ssrf-blocked` dönüyordu; istemcinin hata metinleri (`target-rejected`) crawl, rapor ve rakip analizi tarafından tüketildiği için değiştirilmedi |
| 19 | `http-error`, `response-too-large`, `not-html`, `blocked-by-robots`, `cancelled` kodları kaldırıldı | hiçbir işlem üretmiyordu: büyük yanıt kesilir (`Page.truncated`), HTML olmayan yanıt ve HTTP durumu veridir, kütüphane kendi isteklerinde robots.txt uygulamaz, iptal `CancelledError` olarak yeniden fırlatılır; dış API hataları dış servis kodlarıyla döner. Paket yayımlanmadan kaldırmak major sürüm gerektirmez. `tests/contract/test_error_codes.py` her kodun bir testte üretildiğini denetler |
| 20 | Sayfa çekerken DNS hatası `dns-failed` (istemci metni `dns-failed`, kaynakta `unreachable`); audit'in https→http yedeği DNS hatasında denenmez | SaaS "alan adı yok" ile "sunucu kapalı"yı ayırabilsin; http aynı adı çözdüğü için yedek denemesi boşa istektir. `transport`'un `sibling_resolves` hesabı iki metni de "çözülmüyor" sayarak eski davranışı korur |
| 21 | Kütüphane bir bileşendir: durumsuz; async uygulama, thread havuzu ve süreç havuzu içinde değişmeden çalışır (README "Embedding it in your system"). HTML ayrıştırma `await Page.parse()` ile worker thread'de yapılır (`Context.page`, crawl, hreflang, audit, monitoring, classify, competitors) | 1 MB'lık sayfa olay döngüsünü ~140 ms, 5 MB'lık ~670 ms kilitliyordu; çağıranın döngüsünü (ör. bir web sunucusu) durdurmamalı |
| 22 | `Result` modül düzeyindeki `Result` sınıfıyla kurulur (`Result[Any]` değil) ve pickle'da tutulan exception düşer; `SeoprobeError` ve `OperationError` `__reduce__` tanımlar | çalışma anında parametrelenen pydantic sınıfı pickle edilemiyordu; süreç havuzu ve iş kuyrukları sonucu taşıyamıyordu |
| 23 | `Session` açıldığı olay döngüsünü hatırlar: başka döngüden kullanılırsa ya da açıkken yeniden açılırsa açık bir `RuntimeError` verir | aksi halde aiohttp ve asyncio nesneleri anlaşılmaz hatalar veriyor ya da takılıyordu |
| 24 | `TokenStore` async'tir; `FileTokenStore` dosya işini worker thread'de, okuma-değiştirme-yazmayı kilit altında yapar | senkron arayüz async bir veritabanı sürücüsüyle uygulanamıyordu; eşzamanlı yazmalarda 40 güncellemeden 39'u kayboluyordu |
| 25 | `trafilatura` logger seviyesi yalnızca uygulama ayarlamadıysa düşürülür; `catch_warnings` yalnızca tek seferlik import'ta çalışır | kütüphane uygulamanın logging/warnings ayarını değiştirmemeli; `catch_warnings` thread-safe değildir |
| 26 | Hiçbir sürüm yayımlanmadı: ilk sürüm 0.1.0'dır; `rule_version` ve her kuralın `since` alanı `0.1.0`'dır. Kural ve işlemler sürüm numarasıyla değil kökenleriyle ayrılır: kaynaktan taşınanlar skora girer, kaynakta olmayanlar `"scored": false` | kaynak projenin `1.0`'ı ve uygulama aşamalarının `0.2`'si yayımlanmış sürümler gibi görünüyordu. Parity bulguları `rule_version` karşılaştırmaz (`FINDING_KEYS`), skor etkilenmez |
| 27 | Ana metin `trafilatura.extract(..., fast=True)` ile, yedek algoritmalar olmadan çıkarılır | trafilatura 2.4 kısa makaleli sayfalarda yedek yoldan sayfanın tamamını (ortak menü dahil) alıyordu: aynı menüyü paylaşan sayfalar yanlışlıkla kopya içerik sayılıyordu. `fast=True` ile 2.3 ve 2.4 aynı sonucu verir, parity korunur, 39 fixture'da çıktı varsayılanla aynıdır ve çıkarma 2.3'te ~7 kat hızlıdır. `favor_precision=True` parity'yi bozduğu, `<2.4` üst sınırı kullanıcıya bağımlılık çakışması getirdiği için seçilmedi. Risk: yedek algoritmanın kurtardığı alışılmadık sayfalarda ana metin kısa çıkabilir; trafilatura hiçbir şey bulamazsa görünür metin kullanılır |
| 28 | Bağlantı sağlığı yoklamalarının bir süre bütçesi vardır: `Config.link_check_budget_s` (varsayılan `None`: işlemin süresi yettiği kadar), çağrı başına `audit_page` / `audit_site` / `links(..., link_check_budget_s=...)` ile değişir, `AuditOptions`'ta saklanır ve `rerun` tekrarlar. Yoklamalar işlemin kendi son tarihine asla ulaşmaz: kalan sürenin %20'si (en az 5 sn) sonraki aşamalara bırakılır. Süresinde yanıtlanmayan hedefler `unchecked` / `error="time-budget"` olur ve `LinkSummary.skipped_time_budget`'ta sayılır, asla kırık sayılmaz. Adet sınırları gibi bütçe de kuralları kontrol edilen hedefler üzerinden değerlendirir; kısmi kontrol skora girmeyen `not_checked` (`link-health`, `time-budget`) ile raporlanır, yalnızca o türden hiçbir hedef kontrol edilemediyse kural `not_checked` (`broken-*-link`) olur. Böylece skor bütçeye bağlı değildir (canlı: python.org 4 sn, 8 sn ve bütçesiz aynı skor; ilk uygulama kuralın tamamını dışladığı için bağlantı kategorisini 57'den 25'e düşürüyordu). `max_internal_link_checks` ve `link_check_concurrency` `Config`'e taşındı; `Config.from_env` artık `float | None` gibi alanları tipine göre okur | tek sayfalık audit gerçek sitelerde 35–45 sn sürüyordu (60 iç yoklama × 0,4 sn nezaket aralığı); süre ve hedef siteye binen yük çağıranın kararı olmalı: hızlı yanıt isteyen kısa bütçe, bekleyebilen sınırsız bütçe verir. Varsayılanlar değişmediği için parity korunur. README'de ölçülmüş maliyet tablosu |
| 29 | `visibility`'nin genel sorguları (RDAP, Wayback, Common Crawl) da aynı dinamik bütçeye sahiptir: `Config.visibility_budget_s`, çağrı başına `visibility(..., budget_s=...)` ve `audit_page` / `audit_site(..., visibility_budget_s=...)`; `AuditOptions`'ta saklanır, `rerun` tekrarlar. Bütçe dolunca yalnızca bitmemiş sorgular `error="time-budget"` olur, yanıt verenler verisini korur; sonuç `partial`, `not_checked` (`visibility`, `time-budget`) audit'e de geçer. Bütçe kuralı tek yerdedir: `Context.budget_deadline` (kalan sürenin %20'si, en az 5 sn işleme bırakılır), bağlantı yoklamaları da onu kullanır | bilgi amaçlı ve skora girmeyen bu sorgular audit'in kritik yolundaydı: canlı ölçümde 17–35 sn, en kötü 120 sn. İki bütçe 5 sn iken react.dev audit'i 52 sn'den 18 sn'ye indi, skor değişmedi |
| 30 | Wayback ilk ve son kayıt tarihleri iki hızlı tam-host CDX sorgusuyla (`limit=1`, `limit=-1`) alınır; alan adı genelindeki URL örneklemi paralel ve kendi 12 sn sınırıyla çalışır, geç kalırsa tarihler geçerli kalır (`sample-timeout`) | canlı ölçüm: alan adı genelindeki `sort=reverse` sorgusu 25–52 sn sürüyor, 30 sn sınırını aşıyordu; python.org'da hiçbir Wayback verisi dönmüyordu. Alan adı genelinde `fastLatest` yanlış (2013) sonuç veriyor, kullanılmadı |

**Bilinen sınırlamalar:**
- `render.mobile` kuralları ile `performance.lab_metrics`'in kaynaktan gelen mobil kuralları (küçük yazı, yatay kaydırma, dokunma hedefi) benzer sorunları ayrı kimliklerle raporlayabilir. Yeni kurallar skora girmediği için eşlik etkilenmez.
- `gsc.opportunities`'in beklenen CTR eğrisi (`ctr-curve.json`) kamuya açık CTR çalışmalarından derlenmiş yaklaşık değerlerdir; ilgili bulgular `heuristic` işaretlidir.
- `dns_health` kayıtlı alan adını kendisi bulamaz (public suffix listesi yok); çağıran kayıtlı alan adını vermelidir.

## Ek A: Taşıma eşlemesi

Bu ek yalnızca taşıma süresince kullanılır. Davranışın nereden alınacağını gösterir; yeni yapının nasıl olacağını belirlemez. Taşıma sırasında kontrol listesidir: kaynak projedeki 146 Python dosyasının her biri bir satıra karşılık gelir ve hiçbiri işaretsiz kalmadan `0.1.0` yayınlanmaz.

Kaynak yollar `src/seoa/` altındadır.

### A.1 Davranışın kaynağı

| Hedef | Kaynak |
|---|---|
| `core/config.py` | `config.py` (ortam okumayan yeni sürüm) |
| `core/models.py` | `domain/models.py` (paylaşılanlar; özelliğe özgü modeller ilgili `models.py`'ye) |
| `core/feature.py` | `application/analysis/plugin.py`, `application/analysis/types.py` |
| `core/rules/catalog.py` | `domain/rules/registry.py`, `application/rules_loader.py`, `domain/i18n/catalog.py` (yükleyici kısmı) |
| `core/parse/html.py` | `domain/parsing/html.py` |
| `core/parse/urls.py` | `domain/urls.py` |
| `core/parse/text.py` | `domain/text.py`, `domain/ids.py` |
| `core/net/browser.py` | `infrastructure/rendering/browser.py`, `domain/devices.py` |
| `core/net/` | `infrastructure/http/{fetcher,resolver}.py`, `domain/url_safety.py`, `application/crawl/politeness.py`, `infrastructure/integrations/_public_http.py` |
| `core/data/` | repo kökündeki `data/rules/{ai-crawlers,stopwords}.json` |
| `core/page.py` | `infrastructure/http/fetcher.py` (`fetch` → `Page`), `domain/urls.py` (`normalize`), `domain/models.py` (`FetchResult`, `Hop`) |
| `features/page` | `domain/rules/{rules_page,page_checks,messages_page}.py`, `application/crawl/detect.py` |
| `features/content` | `application/analysis/modules/{readability,textstats,previews,schema_suggest,schema_templates,local}.py`, `domain/rules/schema_checks.py` |
| `features/site` | `infrastructure/http/transport.py`, `application/site/robots.py`, `application/crawl/sitemap.py`, `application/analysis/modules/{site_setup,agentic,techstack,techstack_db,sitemap_gen}.py`, `domain/rules/{rules_site,messages_site}.py`, `data/rules/techstack.json` |
| `features/crawl` | `application/crawl/sampling.py`, `application/analysis/links/{graph,duplicates,indexability,rules,messages}.py`, `application/analysis/modules/hreflang.py`, `data/rules/hreflang-codes.json` |
| `features/render` | `infrastructure/rendering/{spa,js_gap}.py`, `application/analysis/modules/screenshots.py` |
| `features/performance` | `infrastructure/rendering/perf.py`, `infrastructure/integrations/{psi,crux}.py`, `application/analysis/performance/{checks,rules,messages,web_vitals}.py` |
| `features/search` | `infrastructure/integrations/{gsc,google_oauth}.py`, `application/analysis/search_console/{collector,property,rankings,rules,messages}.py`, `application/audit/service.py:182` (`suggest_keywords`) |
| `features/offsite` | `infrastructure/integrations/{openpagerank,visibility,commoncrawl,wayback,rdap}.py`, `application/analysis/modules/{authority,competitors}.py` |
| `features/audit` | `application/audit/{orchestrator,rules}.py`, `application/audit/service.py:261` (`rerun`) |
| `features/report` | `application/reporting/{builder,schema,deep,suite,extra,rankings,scope,quality,actionplan,history}.py`, `application/analysis/compare.py`, `domain/scoring/score.py`, `presentation/api/routers/reports.py:50` (süzme ve öncelik sıralaması) |
| her özelliğin `catalog.json`'u | `domain/i18n/{catalog,app,extra,rankings,suite}.py` ve tüm `messages*.py` (metinler ilgili özelliğe dağıtılır) |
| `check_access` (her özelliğin `access_checks`'i) | `presentation/api/routers/meta.py:61` (LLM testi hariç) |

### A.2 Taşınmayan, yerine başka bir şey gelen

| Kaynak | Yeni yapıda |
|---|---|
| `bootstrap.py`, `application/ports.py`, `application/audit/protocol.py` | `Session` kaynakları kurar; sahteler `tests/fakes.py`'de |
| `infrastructure/adapters.py` (geri kalanı) | ince sarmalayıcılar; özellikler birbirini genel API'leri üzerinden çağırır |
| `application/audit/service.py` (kuyruk, worker, olay deposu) | kapsam dışı (§7.12) |
| `infrastructure/persistence/{repo,schema}.py` | kapsam dışı (§7.12) |
| `presentation/` (FastAPI, Jinja, HTML rapor), `domain/palette.py`, `scripts/` | kapsam dışı (§7.12) |
| LLM katmanı: `infrastructure/llm/{client,budget,cache,input,validators,known_facts,eeat,ai_readiness,synthesize,qa}.py`, `infrastructure/llm/prompts/*.md`, `data/rules/{eeat-signals,known-wrong-facts}.json`, `infrastructure/adapters.py` (`run_advisory`, `run_synthesis`, `_llm_pages`, `_ai_facts`, `_scores_summary`, `llm_ping`, `llm_cost`) | kapsam dışı (§7.12). Denetimde skordan sonra çalışıyordu; hiçbir bulgu ve skor ona bağlı değildi. Rapordaki danışma ve özet bölümleri de çıkar |

### A.3 Kaynakta tekrarlanan ve tek yere toplanan kod

| Kaynakta | Yeni yerde |
|---|---|
| `_Budget`: `modules/agentic.py:286`, `modules/site_setup.py:338` | `RequestBudget`, `core/net/client.py` |
| `_date()`: `integrations/crux.py:30`, `integrations/wayback.py:20` | `core/parse/text.py` |
| ortamdan anahtar okuma: `integrations/psi.py:19`, `integrations/openpagerank.py:35` | `Config` |
| global `get_settings()`: 8 dosya | `Session` üzerinden `Context` |
| `ROOT / "data"` ile dosya okuma: `modules/hreflang.py:45`, `modules/techstack_db.py:22` | özelliğin kendi paketinden `importlib.resources` |
| import anında `load_dotenv`: `config.py:12` | yok; `Config.from_env()` isteğe bağlı |
| mesajların 9 dosyaya dağılması | her özelliğin tek `catalog.json`'u |
| her modülde ayrı zaman aşımı, hata yakalama, log | `@operation` |
| her analizin sayfayı yeniden çekmesi | paylaşılan `Page` |
