"""The demo site used by the end-to-end and parity tests (copied from the source project)."""

from __future__ import annotations

BASE = "https://demo.test"
H = {"content-type": "text/html; charset=utf-8"}

ORG = '<script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization","name":"Demo A.S.","url":"https://demo.test/"}</script>'


def page(
    title: str,
    h1: str | None,
    body: str,
    *,
    desc: str | None = None,
    canonical: str | None = None,
    extra_head: str = "",
    lang: str = "tr",
) -> bytes:
    head = f'<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>'
    if desc:
        head += f'<meta name="description" content="{desc}">'
    if canonical:
        head += f'<link rel="canonical" href="{canonical}">'
    head += extra_head
    h = f"<h1>{h1}</h1>" if h1 else ""
    return f'<!doctype html><html lang="{lang}"><head>{head}</head><body><header><nav><a href="/">Ana sayfa</a> <a href="/hakkimizda">Hakkımızda</a> <a href="/iletisim">İletişim</a> <a href="/blog/yazi-1-seo-nedir">Blog</a></nav></header><main>{h}{body}</main><footer><a href="/gizlilik">Gizlilik Politikası</a> <a href="/iletisim">İletişim</a></footer></body></html>'.encode()


LOREM = " ".join(
    ["Bu paragraf, arama motoru optimizasyonu hakkında gerçek bir içerik örneğidir ve birkaç cümleden oluşur."] * 30
)


def build() -> dict[str, tuple[int, dict[str, str], bytes]]:
    pages: dict[str, tuple[int, dict[str, str], bytes]] = {}
    pages[f"{BASE}/"] = (
        200,
        H,
        page(
            "Demo A.S. | Kurumsal SEO ve dijital pazarlama hizmetleri",
            "Demo A.S.",
            f"<p>{LOREM}</p><a href='/blog/yazi-1-seo-nedir'>SEO nedir?</a> <a href='/blog/yazi-2-teknik-seo'>Teknik SEO</a> <a href='/eski-sayfa'>Eski sayfa</a>"
            "<img src='/a.jpg' width='100' height='50'><img src='/b.jpg'>",
            desc="Demo A.S. kurumsal SEO, içerik ve dijital pazarlama hizmetleri sunar. Ücretsiz ilk görüşme için bize ulaşın.",
            canonical=f"{BASE}/",
            extra_head=ORG,
        ),
    )
    pages[f"{BASE}/hakkimizda"] = (
        200,
        H,
        page("Hakkımızda | Demo A.S. kurumsal profil ve ekip", "Hakkımızda", "<p>Kısa bir sayfa.</p>"),
    )
    pages[f"{BASE}/iletisim"] = (
        200,
        H,
        page(
            "İletişim | Demo A.S. adres telefon ve form",
            None,
            f"<p>{LOREM}</p>",
            desc="Bize ulaşın: adres, telefon ve iletişim formu bilgileri burada yer alır, size en kısa sürede döneriz.",
        ),
    )
    pages[f"{BASE}/gizlilik"] = (
        200,
        H,
        page(
            "Gizlilik Politikası | Demo A.S. kişisel verilerin korunması",
            "Gizlilik",
            f"<p>{LOREM}</p>",
            desc="Demo A.S. gizlilik politikası ve kişisel verilerin korunmasına ilişkin aydınlatma metni burada.",
        ),
    )
    titles = [
        "SEO nedir? Başlangıç rehberi ve temel kavramlar",
        "Teknik SEO kontrol listesi ve en iyi uygulamalar",
        "Teknik SEO kontrol listesi ve en iyi uygulamalar",
        "İçerik pazarlaması stratejisi nasıl kurulur",
        "Bağlantı kurma yöntemleri ve dikkat edilecekler",
        "Yerel SEO ipuçları ve harita sonuçları",
    ]
    slugs = [
        "yazi-1-seo-nedir",
        "yazi-2-teknik-seo",
        "yazi-3-teknik-seo",
        "yazi-4-icerik",
        "yazi-5-baglanti",
        "yazi-6-yerel",
    ]
    for t, s in zip(titles, slugs, strict=True):
        pages[f"{BASE}/blog/{s}"] = (
            200,
            H,
            page(
                t,
                t.split("?")[0],
                f"<p>{LOREM}</p><p>{LOREM}</p>",
                desc=f"{t}. Bu yazıda konuyu adım adım ele alıyoruz ve örneklerle açıklıyoruz.",
                canonical=f"{BASE}/blog/{s}",
            ),
        )
    pages[f"{BASE}/eski-sayfa"] = (404, H, b"<html><title>404</title></html>")
    urls = [f"{BASE}/", f"{BASE}/hakkimizda", f"{BASE}/iletisim", f"{BASE}/gizlilik"] + [
        f"{BASE}/blog/{s}" for s in slugs
    ]
    sm = (
        '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(f"<url><loc>{u}</loc><lastmod>2026-10-01</lastmod></url>" for u in urls)
        + "</urlset>"
    )
    pages[f"{BASE}/sitemap.xml"] = (200, {"content-type": "application/xml"}, sm.encode())
    pages[f"{BASE}/robots.txt"] = (
        200,
        {"content-type": "text/plain"},
        f"User-agent: *\nDisallow: /yonetim/\nSitemap: {BASE}/sitemap.xml\n".encode(),
    )
    return pages
