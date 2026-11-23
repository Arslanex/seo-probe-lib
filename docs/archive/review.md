# Mimari İnceleme: seo-probe-lib

> Tarih: 2026-11-23 · İncelenen: `docs/plan.md` v0.5 · Davranış kaynağı: `seo-analyzer-mvp/src/seoa`
> Kapsam: yalnızca analiz ve tasarım. `seo-probe-lib`'de henüz kod yoktur. Kod düzeyindeki bulgular, taşınacak kaynak kod üzerinde doğrulanmıştır ve dosya:satır ile verilmiştir. Planla ilgili bulgular plan metnine dayanır.

Etiketler: **P0** kritik (güvenlik, doğruluk, mimari kural ihlali) · **P1** önemli · **P2** ertelenebilir iyileştirme.

## 1. Genel değerlendirme: 74 / 100

| Alan | Puan | Not |
|---|---|---|
| Paket sınırları ve modülerlik | 16 / 20 | `core` / `features` ayrımı ve açık bağımlılık tablosu iyi. Ancak `core`, `features/page`'e örtük olarak bağımlı (§3, P0-1) ve özellikler arası sözleşme tanımsız |
| Sonuç ve hata modeli | 16 / 20 | `Result[T]` ve sabit `ErrorCode` doğru. HTTP durumu ile hata arasındaki sınır ve exception zincirleme eksik |
| Kaynak yönetimi ve eşzamanlılık | 12 / 20 | Session yaşam döngüsü, iç içe işlemler, son tarih (deadline), genel bağlantı sınırı ve bellek sınırı tanımsız |
| Güvenlik | 15 / 20 | Kaynak koddaki SSRF koruması güçlü. Anahtar sızıntısı, tarayıcı DNS rebinding açığı ve hata ayrıntılarında iç adres sızıntısı ele alınmamış |
| Paketleme ve test | 15 / 20 | src düzeni, ekstralar, sözleşme testleri iyi. Genel API uyumluluk testi, ağ yasağı ve kullanımdan kaldırma politikası eksik |

Plan, kullanıcının kurallarını büyük ölçüde karşılıyor. Sorunlar mimarinin temelinde değil, sınırlarda: özellikler arası sözleşme, iç içe işlem davranışı ve SaaS ortamına özgü güvenlik ayrıntıları.

## 2. Korunacak kararlar

| Karar | Neden korunmalı |
|---|---|
| `seoprobe` (genel API) / `core` / `features` üçlüsü | modüler monolit için doğru ölçek; kullanıcı iç yapıyı görmez |
| Tek tanım, tek fonksiyon; `session=` parametresi | aynı işlemin ikinci bir tanımı yok; `Client` sınıfının yarattığı tekrar yok |
| `Result[T]` zarfı ve `ErrorCode` | dış dünya hataları beklenen sonuçtur; exception'la akış kontrolü yapılmaz |
| `@operation` ile ortak davranışın tek yerde toplanması | zaman aşımı, log ve hata eşleme özellik kodunda tekrarlanmaz |
| Özellik başına aynı küçük iç düzen, `catalog.json` | birini bilen hepsini bilir; kurallar ve metinler veri olarak kalır |
| Açık `FEATURES` listesi, import yan etkisi yok | kayıt sırası davranışı değiştiremez |
| Kaynak koddaki SSRF tasarımı | yönlendirmeler elle, her adım doğrulanıyor (`infrastructure/http/fetcher.py:88-100`); adres bağlantı anında çözümleyicide doğrulanıyor (`resolver.py:1-15`); IPv4-mapped, DNS64, CGNAT ve metadata host'ları engelleniyor (`domain/url_safety.py:29-110`); dış API çağrıları da aynı kurala tabi (`_public_http.py:1-5`) |
| Tarayıcı yalıtımı | her render ayrı context; tüm istekler ve yönlendirmeler aynı politikadan geçiyor; service worker engelli, WebSocket hiç bağlanmıyor (`rendering/browser.py:1-10, 184-284, 328-340`) |
| Güvenli XML | DOCTYPE reddi, `resolve_entities=False` (`crawl/sitemap.py:8, 233`) |
| OAuth güvenliği | PKCE (S256), tek kullanımlık `state`, süresi dolan bekleyen istekler temizleniyor (`google_oauth.py:9, 308, 345`) |
| Gövde sınırı | 10 MB, sıkıştırması açılmış bayt üzerinden sayılıyor (`fetcher.py:111-115`) |
| Yalnızca `async` API | kullanıcı kararı; I/O ağırlıklı bir kütüphane için doğru |
| LLM'in kaldırılması | skor ve bulgular ona bağlı değildi (`orchestrator.py:211-215`: SCORE adımı ADVISE'dan önce); kaldırmak davranış eşliğini bozmaz |

## 3. Kritik sorunlar

### P0-1: `core` örtük olarak bir özelliğe bağımlı

`core/feature.py`'deki `ctx.page(target)` bir URL verildiğinde sayfayı çeker. Ama planda `fetch` işlemi `features/page/fetch.py`'dedir. Böylece `core` → `features/page` bağımlılığı oluşur ve planın "core hiçbir özelliği import etmez" kuralı çiğnenir.

**Çözüm:** `Page` modeli zaten `core`'da ve diğer tüm özelliklerin ortak girdisi. Sayfa çekme ve URL normalizasyonu da altyapıdır. `fetch` ve `normalize`, `Page` ile birlikte `core/page.py`'ye taşınır. `features/page` yalnızca sayfa kurallarını (`onpage`, `classify`) taşır. Genel API değişmez: `seoprobe.fetch` aynı kalır. → ADR-1

### P0-2: API anahtarları sonuçlara ve loglara sızar

PageSpeed ve CrUX anahtarı sorgu dizesinde gönderiliyor: `psi.py:36` (`params={..., "key": key}`), `crux.py:75` (`params={"key": key}`). Plan her isteğin URL'ini `Result.meta.requests`'e yazıyor. Sonuç JSON'u SaaS'ın son kullanıcısına gösterilirse kurumun Google anahtarı sızar.

**Çözüm:**
- Google API'leri anahtarı `X-Goog-Api-Key` başlığıyla kabul eder; anahtar URL'den çıkarılır.
- Ek güvenlik için `RequestLog` URL'lerinde hassas sorgu parametreleri (`key`, `api_key`, `access_token`, `token`, `code`, `state`) her durumda `***` ile maskelenir.

→ ADR-7

### P0-3: Tarayıcıda DNS rebinding açığı kalıyor

Kaynak kod bunu açıkça belgeliyor (`rendering/browser.py:9-11`): Chromium DNS'i kendisi çözer, bu yüzden kötü niyetli bir DNS sunucusu route kontrolü ile bağlantı arasındaki yarışı kazanabilir. Yerel tek kullanıcılık MVP'de kabul edilebilir bir riskti; SaaS'ta kullanıcı URL'leri sunucuda render edilir.

**Çözüm:**
- Kütüphane bunu tek başına tam kapatamaz. Ağ düzeyinde bir çıkış politikası (egress firewall / network policy: özel ağlara ve metadata adreslerine çıkış yasak) bir **dağıtım gereksinimi** olarak belgelenir.
- `render` ve tarayıcı kullanan işlemlerin `Requirements`'ında bu not yer alır.
- Tarayıcı trafiğini kütüphanenin kendi doğrulayan çözümleyicisinden geçiren yerel bir proxy, gerçek bir ihtiyaç doğarsa ele alınacak bir P2 iyileştirmesidir.

→ ADR-8

### P1-1: Özellikler arası sözleşme tanımsız

Plan "yalnızca paket kökünden import" diyor. Ama paket kökü her şeyi dışa açabildiği için bu kural tek başına bağımsızlık sağlamaz.

**Çözüm:**
- Bir özelliğin başka bir özellikten kullanabileceği şeyler yalnızca o özelliğin `__all__`'ındaki adlardır: işlemler ve `models.py`'deki çıktı modelleri.
- Özellikler birbirini **genel işlemleri üzerinden** çağırır ve `Result` alır. Ek bir arayüz katmanı (Protocol, adaptör) gerekmez.
- Test, içe aktarılan her adın hedef özelliğin `__all__`'ında olduğunu doğrular.

→ ADR-2

### P1-2: İç içe işlemlerin davranışı tanımsız

`audit`, `crawl` ve `monitoring` başka işlemleri çağırır. Şunlar tanımsız:
- iç işlemin zaman aşımının dış işlemin kalan süresiyle ilişkisi;
- isteklerin hangi `Result`'a kaydedildiği;
- iç işlemin geçici oturum açıp açmayacağı.

**Çözüm:**
- Son tarih (deadline) `contextvars` ile aşağı akar; iç işlem `min(kendi süresi, dışın kalan süresi)` ile çalışır (`asyncio.timeout_at`).
- İç işlem dıştaki oturumu kullanır.
- İstekler en içteki işleme kaydedilir; `audit` alt sonuçları kendi `Result`'ında taşır.

→ ADR-4

### P1-3: Session yaşam döngüsü ve SaaS kullanım biçimi belirsiz

Plan, `Session`'ın eşzamanlı kullanım için güvenli olduğunu söylüyor; ama kapanış sırası, tarayıcının tembel başlatılmasındaki yarış ve iptal davranışı tanımlı değil. Ayrıca nezaket kuralları (`HostLimiter`, `politeness.py:13-50`) oturum başına tutuluyor. SaaS her istek için yeni bir oturum açarsa, aynı siteye giden istekler arasında nezaket kuralı işlemez.

**Çözüm:**
- `Session`, `contextlib.AsyncExitStack` ile üç somut nesneyi sırayla açar ve ters sırayla kapatır: HTTP istemcisi, istek sınırlayıcı ve tarayıcı.
- Tarayıcı `asyncio.Lock` altında tembel başlatılır.
- Önerilen kullanım: süreç başına tek, uzun ömürlü bir oturum.

→ ADR-3

### P1-4: Genel eşzamanlılık ve bellek sınırı yok

- **Bağlantı sınırı:** yalnızca host başına sınır var (`politeness.py`). Uzun ömürlü bir oturumda yüzlerce eşzamanlı işlem sınırsız bağlantı açabilir.
- **Bellek:** kaynak kodda denetim, tüm sayfaların gövdesini bellekte tutuyor (`ctx["fetches"]`, `adapters.py:272`). En kötü durumda 200 sayfa × 10 MB = 2 GB.

**Çözüm:**
- Bağlantı sınırı: aiohttp `TCPConnector(limit=Config.max_connections)`. Yeni kod gerektirmez.
- Bellek: işlem başına toplam bayt bütçesi. Kaynak koddaki istek bütçesi (`_Budget`) bayt sayacıyla genişletilir. Bütçe aşılırsa kalan sayfalar `not_checked`'e `request-budget-exceeded` ile girer.

→ ADR-5

### P1-5: Dış API'lerde yeniden deneme yok

Kaynak kodda hiçbir istek yeniden denenmiyor. PSI, CrUX, Search Console ve GA4 kota (429) ve geçici (5xx) hata döner.

**Çözüm:**
- Yalnızca **dış API'lerde** ve yalnızca idempotent isteklerde yeniden deneme yapılır: en fazla 3 deneme, üstel geri çekilme + tam jitter, `Retry-After` başlığına uyulur.
- **Denetlenen site yeniden denenmez.** Bunun iki nedeni var: nezaket ve davranış eşliği; kaynak kod da yeniden denemiyor.

→ ADR-6

### P1-6: OAuth bekleyen istekleri süreç belleğinde

`GoogleOAuth._pending` bir sözlük (`google_oauth.py:284`). Birden çok sunucu örneğiyle çalışan bir SaaS'ta yetki isteğini bir örnek başlatıp geri dönüşü başka bir örnek karşılarsa akış kırılır.

**Çözüm:** kütüphane durumsuz olur. `gsc.auth_url(...)` bir `AuthRequest` (state, verifier, redirect_uri, created_at) döner; çağıran bunu saklar. `gsc.exchange_code(request, code)` bu nesneyi geri alır. Kütüphane PKCE ve `state` doğrulamasını yapmaya devam eder. → ADR-9

### P1-7: Hata ayrıntılarında iç ağ bilgisi

Kaynak kod exception metnini hata dizesine ekliyor: `f"target-rejected: {exc}"`, `f"{code}: {inner}"` (`fetcher.py:130-155`). Bu metin engellenen çözümlenmiş adresi içerebilir. SaaS'ta bir saldırgan bu bilgiyle iç DNS'i yoklayabilir.

**Çözüm:** `ErrorInfo.details` yalnızca güvenli alanları taşır (`url`, `status`, `service`). Exception metni ve çözümlenmiş adresler yalnızca `DEBUG` loguna yazılır. → ADR-7

### P1-8: HTTP durumu ile hata arasındaki sınır belirsiz

Hedef sayfa 404 dönerse bunun `error` mı, yoksa bulgulu bir `ok` mu olduğu tanımlı değil.

**Çözüm:**
- Bir yanıt alındıysa durum kodu veridir: `status=ok`, `Page.status=404`, ilgili kural bulgusu.
- Yanıt alınamadıysa (DNS, bağlantı, zaman aşımı, SSRF reddi) `error` döner.
- `http-error` kodu yalnızca dış API'ler için kullanılır.

→ ADR-10

### P1-9: Uzun CPU işleri olay döngüsünü kilitler

`analyze_logs` 1 milyon satırı tek bir döngüde işlerse olay döngüsü saniyelerce kilitlenir. SaaS'ta bu, aynı süreçteki tüm istekleri durdurur.

**Çözüm:** satırlar sabit boyutlu parçalar halinde `asyncio.to_thread` ile işlenir. HTML ayrıştırma için de aynısı, ancak ölçümle karar verilir (P2-3). → ADR-5

### P1-10: Genel API uyumluluğu ve kullanımdan kaldırma politikası yok

**Çözüm:**
- **Uyumluluk testi:** `seoprobe.__all__` ve her genel fonksiyonun `inspect.signature`'ı anlık görüntü olarak tutulur.
- **Kullanımdan kaldırma:** bir ad en az bir minor sürüm boyunca `DeprecationWarning` ile yaşar, ardından kaldırılır (PEP 387 yaklaşımı).

### P1-11: Çıktıdaki sayfa kaynaklı metinler güvenilmeyen veridir

Title, açıklama, alıntılar ve bağlantı metinleri doğrudan siteden gelir. SaaS bunları HTML'e kaçışsız yazarsa XSS açığı oluşur.

**Çözüm:** belgede açık kural: `Result` içindeki tüm sayfa kaynaklı dizeler güvenilmeyen veridir; gösterirken kaçış yapmak kullanan projenin sorumluluğudur. Kütüphane metni değiştirmez; yalnızca uzunluk sınırı uygular.

### P2 iyileştirmeleri

| # | İyileştirme | Neden ertelenebilir |
|---|---|---|
| P2-1 | `competitors` `offsite`'tan `audit`'e taşınır; böylece `offsite` yalnızca `core`'a bağımlı kalır | `competitors` aslında rakip sayfalarda küçük bir denetimdir, yani bir orkestrasyon işi. Ama mevcut hali de döngü yaratmıyor |
| P2-2 | `Result`'ta serileşmeyen özel bir `_exception` alanı; `unwrap()`, `raise SeoprobeError(...) from result._exception` ile zincir kurar | hata ayıklamayı kolaylaştırır; JSON sözleşmesini etkilemez |
| P2-3 | Büyük HTML'lerin `asyncio.to_thread` ile ayrıştırılması | lxml ayrıştırma sırasında GIL'i bırakır; ama önce 200 sayfalık bir denetimde ölçülmeli |
| P2-4 | `audit_stream` için sınırlı kuyruk (`asyncio.Queue(maxsize=...)`) | yavaş tüketicide doğal geri basınç sağlar; olay sayısı zaten az |
| P2-5 | URL güvenliği ve normalizasyon için property-based test (`hypothesis`, yalnızca geliştirme bağımlılığı) | mevcut testler ana durumları kapsıyor; sınır durumlar için değerli |
| P2-6 | Tarayıcı için doğrulayan yerel proxy (P0-3'ün kütüphane içi çözümü) | karmaşık; ağ politikası aynı riski daha basit kapatır |
| P2-7 | Raporun kalite kapısı (`reporting/quality.py`) HTML rapor üzerinde çalışıyordu; JSON rapora uyarlanır | HTML rapor kaldırıldığı için kapsamı daralır; kalan kontroller (mesaj anahtarı eksikliği vb.) JSON'a uygulanır |

## 4. Hedef mimari

Planın yapısı korunur. Değişenler `+` (yeni), `~` (değişen) ve `−` (çıkan) ile işaretlidir.

```
src/seoprobe/
├── __init__.py                 genel API ve __all__
├── py.typed
├── core/
│   ├── __init__.py
│   ├── config.py               ~ max_connections, max_total_bytes eklenir
│   ├── session.py              ~ AsyncExitStack; HttpClient + HostLimiter + Browser (tembel)
│   ├── result.py
│   ├── errors.py
│   ├── models.py               ~ yalnızca ≥2 özelliğin kullandığı tipler (kural: §6.4)
│   ├── page.py                 + Page, fetch, normalize (features/page'den taşındı)
│   ├── feature.py              ~ Feature, @operation, Context; aşamalar küçük fonksiyonlar
│   ├── catalog.py
│   ├── html.py · urls.py · text.py · browser.py
│   ├── http/
│   │   ├── client.py           ~ RequestLog maskeleme; güvenli hata ayrıntıları
│   │   ├── safety.py
│   │   ├── politeness.py       ~ istek + bayt bütçesi
│   │   └── api.py              ~ dış API'ler: başlıkta anahtar, yeniden deneme politikası
│   └── data/
└── features/
    ├── __init__.py             FEATURES
    ├── page/                   ~ onpage, classify (fetch ve normalize core'a geçti)
    ├── content/
    ├── site/
    ├── crawl/
    ├── render/
    ├── performance/
    ├── search/                 ~ oauth.py durumsuz (AuthRequest)
    ├── offsite/                ~ authority, visibility (competitors → audit, P2-1)
    ├── monitoring/
    ├── audit/                  ~ + competitors.py (P2-1)
    └── report/
```

Yeni dosya yalnızca bir tane: `core/page.py`. O da `features/page/fetch.py`'nin yeni yeri, yani toplam dosya sayısı değişmiyor. Yeni bağımlılık eklenmiyor.

## 5. Bağımlılık mimarisi

```
core                      (hiçbir özelliğe bağımlı değil)
 ├─ page · content · site · render · performance · search · offsite   (yalnızca core, page)
 │   └─ crawl → site
 ├─ monitoring → site
 └─ audit → page, content, site, crawl, render, performance, search, offsite
     └─ report → audit (yalnızca modeller)
```

| Kural | Nasıl denetlenir |
|---|---|
| `core` hiçbir `features.*` modülünü import etmez | import-linter `forbidden` sözleşmesi |
| Katman sırası ve döngüsüzlük | import-linter `layers` sözleşmesi |
| Bir özellik yalnızca `FEATURE.depends_on`'daki özellikleri kullanır | `tests/contract/test_dependencies.py`: AST ile import'lar çıkarılır, bildirimle karşılaştırılır |
| Başka bir özellikten yalnızca `__all__`'daki adlar kullanılır | aynı test: içe aktarılan her ad, hedef özelliğin `__all__`'ında olmalı |

**Protocol ve bağımlılık enjeksiyonu nerede?** Yalnızca gerçek bir dış sınırda: `TokenStore` (saklama SaaS'ın işi) ve testlerdeki sahteler (`Session`'a HTTP istemcisi ve tarayıcı enjekte edilebilir). Özellikler arasında Protocol yoktur; genel işlemler ve modeller zaten sözleşmedir. Gerekçe: her yeni soyutlama, yeni bir şeyin bakımı demek. Burada çözülecek somut bir sorun yok.

## 6. Core tasarımı

### 6.1 Session yaşam döngüsü

```python
class Session:
    def __init__(self, config: Config | None = None, *, on_event=None, token_store=None,
                 http: HttpClient | None = None, browser: Browser | None = None) -> None: ...
    async def __aenter__(self) -> Session:
        self._stack = AsyncExitStack()
        self.http = await self._stack.enter_async_context(self._http or HttpClient(self.config))
        return self
    async def browser(self) -> Browser:          # ilk ihtiyaçta, kilit altında başlatılır
        async with self._browser_lock:
            if self._browser is None:
                self._browser = await self._stack.enter_async_context(Browser(self.config))
        return self._browser
    async def __aexit__(self, *exc) -> None:
        await self._stack.aclose()               # ters sırada kapatır; iptalde de çalışır
```

- **Ayrı servis sınıfları:** zaten var olan üç somut sınıf yeterli: `HttpClient`, `HostLimiter`, `Browser`. Ayrıca "Manager", "Registry" veya "Factory" sınıfı açılmaz.
- **Eşzamanlılık güvencesi:** tek bir olay döngüsü içinde birden çok görev aynı `Session`'ı kullanabilir. Bir `Session` iş parçacıkları veya olay döngüleri arasında paylaşılamaz; bu, aiohttp'nin `ClientSession` kuralıyla aynıdır.
- **SaaS kullanımı:** süreç başına tek, uzun ömürlü bir oturum önerilir. Bağlantı havuzu, nezaket kuralları ve tarayıcı böylece paylaşılır. Çağrı başına sınırlar (`max_pages`, zaman aşımı) parametredir.
- **Testler:** `http=` ve `browser=` ile sahte nesneler verilir. Bu, Session'ın tek enjeksiyon noktasıdır.

### 6.2 İşlem hattı

`@operation` dışarıdan tek bir dekoratördür. İçeride `core/feature.py`'de sıralı, küçük fonksiyonlardan oluşur. Middleware zinciri yoktur; sıra koddadır ve değişmez.

| Sıra | Aşama | Hata davranışı |
|---|---|---|
| 1 | Girdi doğrulama (`pydantic.validate_call`) | yanlış tip veya eksik parametre → `TypeError` / `ValueError` (programcı hatası, exception) |
| 2 | Oturum: dıştakini kullan ya da geçici aç | — |
| 3 | Gereksinim denetimi | eksik ekstra veya anahtar → `missing-extra` / `missing-credential`, işlem çağrılmaz |
| 4 | Bağlam: `trace_id`, son tarih, istek kaydı `contextvars`'a yazılır | — |
| 5 | Çalıştırma: `asyncio.timeout_at(min(kendi, dış))` | süre dolar → `timeout` |
| 6 | Exception eşleme | bilinen altyapı hataları → ilgili `ErrorCode`; `CancelledError` → yeniden fırlatılır; diğer her şey → `internal-error` |
| 7 | Gözlem: log, `on_event`'e `started` / `finished` (süre, istek sayısı, bayt) | olay kancası hatası işlemi bozmaz, `WARNING` loglanır |
| 8 | `Result` kurma (bulgu metinleri katalogdan) ve geçici oturumu kapatma | — |

- **Metrikler:** ayrı bir metrik kütüphanesi yok. `finished` olayı süreyi, istek sayısını ve baytı taşır; Prometheus veya OpenTelemetry'ye bağlamak kullanan projenin işi.
- **Bütçe:** istek ve bayt bütçesi `HostLimiter`'da uygulanır, dekoratörde değil. Dekoratör yalnızca bütçe aşımı exception'ını koda çevirir.

### 6.3 Result ve hata modeli

Plandaki model korunur; eklenenler:

- **HTTP durumu veridir, hata değildir** (P1-8).
- **`unwrap()` zincir kurar:** `raise SeoprobeError(code, result) from result._exception` (P2-2). Özel alan serileşmez.
- **`ErrorInfo.details` güvenlidir:** yalnızca `url` (maskelenmiş), `status`, `service`, `exception` (yalnızca tür adı) (P1-7).
- **Kısmi başarı:** `status=partial`; `not_checked` her eksik parçanın nedenini kodla taşır.
- **Tip güvenliği:** `Result[TechStack]` gibi tipli dönüş; Python kullanıcısı `res.data.technologies` ile çalışır, JSON gerekirse `to_dict()` / `to_json()` kullanılır.

### 6.4 Ortak modeller kuralı

Bir model `core/models.py`'ye ancak şu iki koşuldan biri sağlanırsa girer:
- `core`'un kendisi onu kullanır (`Finding`, `NotChecked`, `Requirements`, `Severity`, `Category`);
- iki veya daha fazla özellik onu girdi olarak alır.

Bir özelliğin çıktısı olan model, başka bir özellik onu tüketse bile üreten özelliğin `models.py`'sinde kalır (ör. `RobotsRules` → `site/models.py`). Tüketen özellik onu `__all__` üzerinden alır. Böylece `core/models.py` büyümez.

## 7. Feature tasarımı

| Paket | Sorumluluk | Değerlendirme | Öneri |
|---|---|---|---|
| `page` | sayfa içi kurallar, sınıflandırma | `fetch` / `normalize` core'a geçince sorumluluğu netleşir | P0-1 |
| `content` | içerik ve yapılandırılmış veri | tutarlı; `local_seo` ve `suggest_schema` aynı JSON-LD ayrıştırmasını paylaşıyor | değişiklik yok |
| `site` | site düzeyi ayarlar ve erişim | en büyük paket (8 işlem) ama tek bir konuyu kapsıyor; işlemler birbirinin sonucunu kullanıyor | değişiklik yok; `robots` çıktısı (`RobotsRules`) dışa açık sözleşme |
| `crawl` | sayfa kümesi ve bağlantı grafiği | tutarlı; `site`'a bağımlılığı gerçek (robots, sitemap) | değişiklik yok |
| `render` | tarayıcıda açılmış sayfa | tutarlı | P0-3 belgesi |
| `performance` | hız ölçümleri | tutarlı | P0-2 (anahtar başlığa), P1-5 (yeniden deneme) |
| `search` | arama motorlarının verisi | tutarlı; `indexnow` paketteki tek yazma işlemi olarak açıkça işaretli | P1-6 (durumsuz OAuth) |
| `offsite` | sitenin dış görünümü | `competitors` bir orkestrasyon işi, paketi `page` / `content` / `site`'a bağlıyor | P2-1: `competitors` → `audit` |
| `monitoring` | URL listeleri ve değişim | tutarlı | P1-9 (log analizi parçalı) |
| `audit` | orkestrasyon | tüm özellikleri genel işlemleri üzerinden çağırmalı; sayfayı bir kez çekip `Page` olarak dağıtmalı | ADR-4 |
| `report` | denetim sonucunun işlenmesi | yalnızca `audit` modellerini ve `core` modellerini tüketiyor; doğru | P2-7 (kalite kapısı JSON'a) |

**Bağımsız test:** `fetch` core'a geçtiği için her özellik, sahte bir `HttpClient` ile ve yalnızca kendi bağımlılıkları yüklenerek test edilebilir.

## 8. Performans ve güvenlik kararları

| Konu | Karar | Etiket |
|---|---|---|
| API biçimi | yalnızca async; senkron katman yok (kullanıcı kararı) | — |
| Bağlantı havuzu | oturum başına tek aiohttp `ClientSession`; süreç başına tek oturum önerilir | P1 |
| Genel eşzamanlılık | `TCPConnector(limit=Config.max_connections)` (varsayılan 100) | P1 |
| Host başına eşzamanlılık | mevcut `HostLimiter` (2 eşzamanlı, 0,4 s aralık, devre kesici) | korunur |
| Dış API hız sınırı | her API host'u da `HostLimiter`'dan geçer; kota hatasında `Retry-After` | P1 |
| Yeniden deneme | yalnızca dış API'ler, en fazla 3, üstel + tam jitter; hedef site için yok | P1 |
| Son tarih | `contextvars` + `asyncio.timeout_at`; iç işlemler dışın süresini aşamaz | P1 |
| İptal | `CancelledError` yutulmaz; `AsyncExitStack` ve tarayıcı context'lerinde `finally` ile kapanış | P1 |
| Geri basınç | `crawl` sınırı `max_pages`; `analyze_logs` akış; `audit_stream` için sınırlı kuyruk | P2 |
| Bellek | yanıt başına 10 MB (korunur) + işlem başına `max_total_bytes` | P1 |
| CPU işleri | `analyze_logs` parçalı `to_thread`; HTML ayrıştırma ölçümle | P1 / P2 |
| Önbellek | oturumlar arası önbellek yok (YAGNI). Denetim içinde paylaşım açık: `Page` bir kez çekilir. Bot doğrulamasında ters DNS sonuçları oturum içinde tutulur | — |
| SSRF | kaynak tasarım korunur: her adımda politika, bağlantı anında adres, IPv4-mapped / DNS64 / CGNAT / metadata | korunur |
| Tarayıcı DNS rebinding | ağ düzeyinde çıkış politikası bir dağıtım gereksinimi | P0 |
| Anahtar sızıntısı | anahtarlar başlıkta; `RequestLog` maskeleme | P0 |
| Hata ayrıntıları | iç adres ve exception metni yalnızca DEBUG logunda | P1 |
| OAuth | durumsuz `AuthRequest`; token saklama `TokenStore` ile SaaS'ta (şifreli depolama önerisi kaynak kodda da yazılı: `google_oauth.py:14`) | P1 |
| Güvenilmeyen HTML | yalnızca ayrıştırılır; tarayıcıda yalıtılmış context; çıktıdaki sayfa kaynaklı dizeler güvenilmeyen veri olarak belgelenir | P1 |
| XML | DOCTYPE reddi, `resolve_entities=False` | korunur |
| Kaynak tüketimi | gövde, yönlendirme (5), sayfa (200), toplu URL (1000), bayt bütçesi, tarayıcı eşzamanlılığı | P1 |

## 9. Test stratejisi

| Katman | Kapsam | Araç |
|---|---|---|
| Birim | `core` ve her özelliğin saf mantığı (kurallar, skor, ayrıştırma) | pytest |
| Özellik | her işlem sahte `HttpClient` / `Browser` ile; `tests/features/<ad>/` | pytest + `tests/fakes.py` |
| Sözleşme | JSON Schema anlık görüntüleri, `__all__` ve imzalar, bağımlılıklar, kataloglar, `ErrorCode` ↔ belge | pytest + import-linter |
| Güvenlik | SSRF derlemi (kaynak projedeki testler), yönlendirmeyle SSRF, DNS rebinding (sahte çözümleyici), anahtar maskeleme, güvenli hata ayrıntıları, XML entity | pytest |
| Eşzamanlılık | iptalde kaynak sızıntısı yok, son tarih aşağı akıyor, tarayıcı tek kez başlatılıyor | pytest-asyncio |
| Regresyon | kaynak projenin altın örnekleriyle davranış eşliği | pytest |
| Entegrasyon | yerel HTTP sunucusuna karşı uçtan uca işlemler | pytest + `aiohttp.web` (yeni bağımlılık yok) |
| Paket | temiz venv'de wheel kurulumu, veri dosyaları, ekstrasız import | CI |

Ek kurallar:
- **Ağ varsayılan olarak kapalı:** `conftest.py`'de `socket.socket.connect` yerel adresler dışında engellenir. Yeni bağımlılık gerekmez.
- **Zaman kontrol edilebilir:** saat ve uyku, enjekte edilen tek bir `clock` üzerinden değil, `monkeypatch` ile kontrol edilir. Kaynak kod OAuth'ta zaten `_now()` kullanıyor.
- **Rastgelelik tohumlu:** örnekleme zaten tohumlu (`crawl/sampling.py`).

CI sırası: `ruff check` → `ruff format --check` → `mypy --strict` → `lint-imports` → `pytest` → `python -m build` + temiz venv kurulum testi.

**Neden Pyright değil:** kaynak proje `mypy --strict` ile temiz; araç değiştirmek ek fayda sağlamaz.

## 10. Geçiş planı

Henüz kod olmadığı için "geçiş" iki şey demek: planın güncellenmesi ve uygulama aşamalarına yerleştirme. Yayınlanmış bir API olmadığından **hiçbir değişiklik kırıcı değildir.** Genel API'de değişen tek imza OAuth'tur (`gsc.exchange_code(request, code)`).

| # | Görev | Değişen | Gerekçe | Bağımlılık | Risk | Test / kabul kriteri | Etiket |
|---|---|---|---|---|---|---|---|
| G1 | `fetch`, `normalize`, `Page` → `core/page.py`; `features/page` = `onpage`, `classify` | plan §4, §7.1, Ek A | P0-1 | — | düşük | import-linter: `core` → `features` yasağı yeşil | P0 |
| G2 | Google API anahtarı `X-Goog-Api-Key` başlığında; `RequestLog` maskeleme | plan §6, aşama 6 | P0-2 | — | düşük | test: hiçbir `Result` JSON'unda ve log kaydında anahtar değeri geçmiyor | P0 |
| G3 | "Dağıtım gereksinimleri" bölümü: tarayıcı için ağ çıkış politikası | plan yeni §, README | P0-3 | — | yok (belge) | belge var; `render` gereksinimleri bunu anıyor | P0 |
| G4 | Özellikler arası sözleşme: yalnızca `__all__`; test | plan §4.2 | P1-1 | G1 | düşük | `test_dependencies.py` iç ad kullanımını yakalıyor | P1 |
| G5 | İç içe işlemler: son tarih, oturum, istek kaydı | plan §6.5 | P1-2 | — | orta | test: iç işlem dışın kalan süresini aşamıyor | P1 |
| G6 | Session: `AsyncExitStack`, tembel tarayıcı kilidi, eşzamanlılık güvencesi, SaaS kullanım notu | plan §6.6 | P1-3 | — | düşük | test: iptalde açık bağlantı / tarayıcı kalmıyor | P1 |
| G7 | `max_connections`, `max_total_bytes` | plan §6.6, §7.11 | P1-4 | G6 | düşük | test: bütçe aşımı `not_checked`'e giriyor | P1 |
| G8 | Dış API yeniden deneme politikası | plan §5 / §6, `core/http/api.py` | P1-5 | — | düşük | test: 429'da `Retry-After`'a uyuluyor, hedef site denenmiyor | P1 |
| G9 | Durumsuz OAuth (`AuthRequest`) | plan §7.8 | P1-6 | — | düşük | test: farklı süreçte `exchange_code` çalışıyor; tekrar kullanılan `state` reddediliyor | P1 |
| G10 | Güvenli hata ayrıntıları; HTTP durumu veridir | plan §6.3 | P1-7, P1-8 | — | düşük | test: engellenen adres `details`'te yok; 404 → `ok` + bulgu | P1 |
| G11 | `analyze_logs` parçalı `to_thread` | plan §7.11 | P1-9 | — | düşük | test: işlem sırasında olay döngüsü 100 ms'den uzun kilitlenmiyor | P1 |
| G12 | Genel API anlık görüntüsü ve kullanımdan kaldırma politikası | plan §8, §9 | P1-10 | — | düşük | test: `__all__` veya imza değişirse kırılıyor | P1 |
| G13 | Güvenilmeyen çıktı notu | plan §6.2, README | P1-11 | — | yok (belge) | belge var | P1 |
| G14 | `competitors` → `audit` | plan §4, §7 | P2-1 | G4 | düşük | `offsite` yalnızca `core`'a bağımlı | P2 |
| G15 | `_exception` ve zincirleme `unwrap` | plan §6.1 | P2-2 | — | düşük | test: `__cause__` dolu | P2 |
| G16 | HTML ayrıştırma ölçümü, gerekirse `to_thread` | uygulama aşaması 9 | P2-3 | — | düşük | ölçüm raporu | P2 |
| G17 | Kalite kapısını JSON rapora uyarlama | plan Ek A | P2-7 | — | düşük | kalan kontrollerin testleri | P2 |

**Öncelik sırası:** G1–G3 (P0) plana hemen işlenir. G4–G13 ilgili uygulama aşamalarına eklenir. P2'ler, ölçüm ya da ihtiyaç doğduğunda ele alınır.

## 11. Son değerlendirme

**Beklenen avantajlar:**
- `core` gerçekten bağımsız hale gelir; özellikler sahte bir HTTP istemcisiyle tek başına test edilir.
- Özellikler arası iletişimin tek yolu genel işlemler ve modellerdir; bu kural CI'da denetlenir.
- Uzun ömürlü tek bir oturumla SaaS'ta bağlantı havuzu, nezaket kuralları ve tarayıcı paylaşılır; bellek ve bağlantı sayısı sınırlıdır.
- Anahtar ve iç ağ bilgisi sızıntısı kapanır.
- Hiçbir yeni bağımlılık, katman veya soyutlama eklenmez.

**Dezavantajlar ve riskler:**

| Risk | Etki | Azaltma |
|---|---|---|
| Tarayıcı DNS rebinding kütüphane içinde tam kapanmıyor | yanlış dağıtımda iç ağa erişim | dağıtım gereksinimi olarak belgelenir; P2-6 proxy seçeneği açık kalır |
| Süreç başına tek oturum, tek bir sorunlu sitenin devre kesiciyi açmasını tüm kiracılar için paylaşır | bir kiracının taraması diğerini etkileyebilir | devre kesici host başınadır, kiracıları değil siteyi korur; bu, nezaketin amacı |
| İç içe işlem semantiği `@operation`'ı karmaşıklaştırabilir | dekoratör büyür | aşamalar ayrı fonksiyonlar; her aşamanın kendi testi |
| `max_total_bytes` büyük sitelerde denetimi erken kesebilir | eksik sonuç | kesinti `not_checked`'te görünür; değer `Config`'ten ayarlanır |

**Belirttiğim varsayımlar:**
- SaaS, kütüphaneyi tek bir olay döngüsü içinde ve süreç başına bir oturumla çalıştıracak.
- Tarayıcı işlemleri ağ politikası uygulanabilen bir ortamda (container / VM) çalışacak.
- Kaynak koddaki davranış (bulgular, skor) referans kabul edilecek.
