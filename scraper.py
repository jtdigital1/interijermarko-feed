#!/usr/bin/env python3
"""
Meta katalog feed za interijermarko.hr – samostalna skripta, bez ikakvog modula na shopu.

Skripta čita javni web kao posjetitelj i za Meta katalog izvlači:
  * svaki aktivni proizvod (popis iz kategorije "Naslovnica" + sve podkategorije),
  * SVAKU kombinaciju (dimenzija / boja) kao zasebnu varijantu s vlastitom cijenom,
    šifrom, slikom i dostupnošću (ista AJAX ruta koju koristi sama stranica proizvoda),
  * proizvode u rolama (PVC, tepisoni, umjetna trava) s cijenom po m²,
  * proizvode koji se ne mogu kupiti online nego samo "pošalji upit" (LVT, laminati, parketi).

Izlaz:
  feed/meta-katalog.csv  i  feed/meta-katalog.xml  (isti sadržaj, koristi jedan)
  feed/izvjestaj.txt     (koliko čega je izvučeno + upozorenja)

Pokretanje:
  pip install requests beautifulsoup4
  python scraper.py                 # cijeli shop
  python scraper.py --limit 10      # brzi test
  python scraper.py --probe         # dodatno provjeri SVE ID-eve 1..max (sporije, ~30 min)
"""
import argparse
import csv
import html as htmlmod
import itertools
import json
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from xml.sax.saxutils import escape

import requests
from bs4 import BeautifulSoup

# ------------------------------------------------------------------ postavke
BASE = "https://interijermarko.hr"
BRAND = "Interijer Marko"
CURRENCY = "EUR"
WORKERS = 3          # paralelnih zahtjeva (ne pretjerivati – dijeljeni hosting)
DELAY = 0.3          # pauza po zahtjevu, po radniku
OUT_DIR = Path(__file__).parent / "feed"

# Kako Meti prijaviti artikle koji se ne mogu kupiti online:
#  - proizvod koji se UVIJEK prodaje samo na upit (LVT, laminat, parket)
AVAIL_INQUIRY_ONLY = "available for order"   # Meta ga i dalje može oglašavati
#  - dimenzija koja trenutno nije dostupna ("Ova dimenzija trenutno nije dostupna" + POŠALJI UPIT)
AVAIL_SIZE_UNAVAILABLE = "out of stock"      # Meta ga NE oglašava
INCLUDE_UNAVAILABLE_SIZES = True             # False = takve dimenzije uopće ne idu u feed
#  - proizvod kojem je cijena na webu SKRIVENA (dio parketa/LVT-a: nema cijene, samo "Pošalji upit").
#    Meta traži cijenu koja odgovara stranici, pa ih po defaultu izostavljamo.
#    True = koristi internu cijenu iz shopa (rizik: Meta odbije zbog nepodudaranja cijene)
USE_HIDDEN_PRICES = False

# ------------------------------------------------------------------ HTTP
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; InterijerMarko-MetaFeed/2.0)",
    "Accept-Language": "hr-HR,hr;q=0.9",
})
warnings = []


def http(method, url, tries=4, **kw):
    for i in range(tries):
        try:
            r = session.request(method, url, timeout=40, **kw)
            time.sleep(DELAY)
            if r.status_code in (200, 404):
                return r
        except requests.RequestException as e:
            if i == tries - 1:
                warnings.append(f"{url}: {e}")
        time.sleep(3 * (i + 1))
    return None


def clean_url(href):
    p = urlsplit(urljoin(BASE, href))
    return urlunsplit(("https", p.netloc.replace("www.", ""), p.path, "", ""))


def to_float(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^\d,.\-]", "", str(v))
    if not s:
        return None
    if "," in s:                       # 1.234,50 -> 1234.50
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def clean_ref(v):
    v = (v or "").strip()
    return v if re.fullmatch(r"[\w\-./]{2,70}", v) else ""


def text_of(html_fragment):
    t = BeautifulSoup(html_fragment or "", "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", htmlmod.unescape(t)).strip()


# ------------------------------------------------------------------ 1) popis proizvoda
def list_category(url):
    """svi proizvodi iz kategorije (resultsPerPage + straničenje kao rezerva)"""
    found = {}
    for page in range(1, 200):
        r = http("GET", f"{url}?resultsPerPage=1000&page={page}")
        if not r or r.status_code != 200:
            break
        soup = BeautifulSoup(r.text, "html.parser")
        new = 0
        for art in soup.select("article[data-id-product]"):
            pid = art.get("data-id-product")
            if pid and pid.isdigit() and pid not in found:
                found[pid] = True
                new += 1
        if not new or not soup.select_one("a[rel=next]"):
            break
    return found


def discover_products(probe=False):
    ids = {}
    home = http("GET", BASE + "/")
    cats = {BASE + "/2-naslovnica"}
    if home:
        for a in BeautifulSoup(home.text, "html.parser").find_all("a", href=True):
            path = urlsplit(urljoin(BASE, a["href"])).path
            if re.fullmatch(r"/\d+-[a-z0-9-]+", path):
                cats.add(clean_url(path))
    for c in sorted(cats):
        n0 = len(ids)
        ids.update(list_category(c))
        print(f"  {c}: +{len(ids) - n0}")

    if probe and ids:
        top = max(map(int, ids)) + 300
        print(f"  provjeravam sve ID-eve 1..{top} ...")
        def check(i):
            r = http("GET", f"{BASE}/index.php?controller=product&id_product={i}")
            return str(i) if r is not None and r.status_code == 200 and 'id="product-details"' in r.text else None
        with ThreadPoolExecutor(WORKERS) as ex:
            for pid in ex.map(check, range(1, top)):
                if pid and pid not in ids:
                    ids[pid] = True
                    warnings.append(f"proizvod {pid} je aktivan, ali nije ni u jednoj kategoriji")
    return sorted(ids, key=int)


# ------------------------------------------------------------------ 2) jedan proizvod
def variant_groups(soup):
    """{ 'group[1]': {'label':'Dimenzija (cm)', 'options':[('25','160 x 230'), ...]} }"""
    groups = {}
    for wrap in soup.select(".product-variants-item"):
        label_el = wrap.select_one(".control-label, label")
        label = re.sub(r"\s+", " ", label_el.get_text(" ", strip=True)) if label_el else ""
        sel = wrap.select_one("select[name^='group[']")
        if sel:
            groups[sel["name"]] = {"label": label, "options": [
                (o.get("value"), o.get_text(strip=True) or o.get("title", "")) for o in sel.find_all("option")]}
            continue
        for inp in wrap.select("input[name^='group[']"):
            g = groups.setdefault(inp["name"], {"label": label, "options": []})
            li = inp.find_parent(["li", "label"])
            name = inp.get("title") or (li.get_text(" ", strip=True) if li else "") or inp.get("value")
            g["options"].append((inp.get("value"), name))
    # rezerva ako tema nema .product-variants-item
    if not groups:
        for el in soup.select(".product-variants [name^='group[']"):
            g = groups.setdefault(el["name"], {"label": el["name"], "options": []})
            if el.name == "select":
                g["options"] += [(o.get("value"), o.get_text(strip=True)) for o in el.find_all("option")]
            else:
                g["options"].append((el.get("value"), el.get("title") or el.get("value")))
    return {k: v for k, v in groups.items() if v["options"]}


def parse_prices_block(fragment):
    """iz HTML-a .product-prices (stranica ili AJAX) vrati (cijena, redovna_cijena, jedinica)"""
    s = BeautifulSoup(fragment or "", "html.parser")
    price = None
    m = s.select_one("meta[itemprop=price]") or s.select_one("[itemprop=price][content]")
    if m and m.get("content"):
        price = to_float(m["content"])
    if price is None:
        cur = s.select_one(".current-price-value, .current-price")
        price = to_float(cur.get("content") or cur.get_text()) if cur else None
    regular = None
    reg = s.select_one(".hrc-c-old, .regular-price, .old-price")
    if reg:
        regular = to_float(reg.get_text())
    unit = ""
    txt = s.get_text("\n")
    um = re.search(r"/\s*za\s*(m\s*2|m²|kom|m)(?![a-zžćčšđ])", txt, re.I)
    if um:
        u = re.sub(r"\s+", "", um.group(1).lower())
        unit = "m²" if u in ("m2", "m²") else u
    return price, regular, unit


def is_buyable(add_to_cart_fragment):
    s = BeautifulSoup(add_to_cart_fragment or "", "html.parser")
    btn = s.select_one("[data-button-action=add-to-cart]")
    if not btn or btn.has_attr("disabled"):
        return False
    return "nije dostupna" not in s.get_text(" ").lower()


def images_from(dp, soup):
    out = []
    for img in dp.get("images") or []:
        url = (((img.get("bySize") or {}).get("large_default") or {}).get("url")) or img.get("large", {}).get("url")
        if url:
            out.append(url)
    cover = (((dp.get("cover") or {}).get("bySize") or {}).get("large_default") or {}).get("url")
    if cover:
        out.insert(0, cover)
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        out.append(og["content"])
    seen, res = set(), []
    for u in out:
        u = urljoin(BASE, u)
        if u not in seen:
            seen.add(u)
            res.append(u)
    return res


def scrape_product(pid):
    r = http("GET", f"{BASE}/index.php?controller=product&id_product={pid}")
    if not r or r.status_code != 200:
        warnings.append(f"{pid}: stranica nije dostupna")
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    det = soup.select_one("#product-details[data-product]")
    if not det:
        warnings.append(f"{pid}: nema data-product")
        return []
    dp = json.loads(det["data-product"])
    url = clean_url(r.url)

    name = htmlmod.unescape(dp.get("name") or "").strip()
    desc = text_of(dp.get("description_short")) or text_of(dp.get("description")) or name
    category = dp.get("category_name") or ""
    if category.lower() in ("naslovnica", "home"):
        category = ""
    brand = dp.get("manufacturer_name") or BRAND
    imgs = images_from(dp, soup)
    actions_txt = re.sub(r"\s+", " ", (soup.select_one(".product-actions") or soup).get_text(" "))
    orderable = str(dp.get("available_for_order", "1")) == "1"
    length_limit = re.search(r"ograni[čc]ene su do (dužine )?([\d,\.]+ ?m)", actions_txt, re.I)

    if str(dp.get("show_price", "1")) != "1" and not USE_HIDDEN_PRICES:
        warnings.append(f"{pid} {name}: cijena skrivena na webu – izostavljeno (USE_HIDDEN_PRICES)")
        return []

    page_price, page_regular, unit = parse_prices_block(str(soup.select_one(".product-prices") or ""))
    unity = dp.get("unity") or ""
    if not unit and ("m<sup>2" in unity or "m2" in unity):
        unit = "m²"

    base = {
        "title": name[:150],
        "brand": brand,
        "product_type": category,
        "condition": "new",
        "google_product_category": "",
    }

    def note(price):
        parts = []
        if unit == "m²" and price:
            parts.append(f"Cijena {price:.2f} € po m².")
        if length_limit:
            parts.append(f"Online narudžba do {length_limit.group(2)} dužine, veće dužine na upit.")
        if not orderable:
            parts.append("Prodaja na upit – pošaljite upit putem stranice proizvoda.")
        return " ".join(parts)

    def avail(buyable):
        if not orderable:
            return AVAIL_INQUIRY_ONLY
        return "in stock" if buyable else AVAIL_SIZE_UNAVAILABLE

    def price_fields(price, regular):
        if regular and price and regular > price + 0.009:
            return f"{regular:.2f} {CURRENCY}", f"{price:.2f} {CURRENCY}"
        return f"{price:.2f} {CURRENCY}", ""

    rows = []
    groups = variant_groups(soup)

    # ---------------- bez kombinacija
    if not groups:
        price = to_float(dp.get("price_amount")) or page_price
        if not price:
            warnings.append(f"{pid} {name}: nema cijene – preskočeno")
            return []
        buyable = is_buyable(str(soup.select_one(".product-add-to-cart") or ""))
        p, sp = price_fields(price, (to_float(dp.get("regular_price_amount")) if dp.get("has_discount") else None) or page_regular)
        rows.append({**base, "id": str(pid), "item_group_id": "", "price": p, "sale_price": sp,
                     "availability": avail(buyable), "link": url,
                     "image_link": imgs[0] if imgs else "", "additional_image_link": ",".join(imgs[1:11]),
                     "description": (note(price) + " " + desc).strip()[:5000],
                     "size": "", "color": "", "mpn": clean_ref(dp.get("reference")),
                     "custom_label_0": "po m2" if unit == "m²" else "",
                     "custom_label_1": "na upit" if not orderable else ("rola" if length_limit else "")})
        return rows

    # ---------------- sve kombinacije (dimenzija × boja ...)
    keys = list(groups)
    seen_ipa = set()
    for combo in itertools.product(*[groups[k]["options"] for k in keys]):
        data = {"ajax": "1", "action": "refresh", "quantity_wanted": "1"}
        for k, (val, _lbl) in zip(keys, combo):
            data[k] = val
        rr = http("POST", f"{BASE}/index.php?controller=product&id_product={pid}",
                  data=data, headers={"X-Requested-With": "XMLHttpRequest"})
        if not rr or rr.status_code != 200:
            warnings.append(f"{pid}: kombinacija {combo} nije učitana")
            continue
        try:
            j = rr.json()
        except ValueError:
            warnings.append(f"{pid}: neispravan AJAX odgovor")
            continue
        ipa = str(j.get("id_product_attribute") or "0")
        if ipa in seen_ipa or ipa == "0":
            continue  # nepostojeća kombinacija -> shop vrati najbližu, već obrađenu
        seen_ipa.add(ipa)

        price, regular, u2 = parse_prices_block(j.get("product_prices"))
        if not price:
            warnings.append(f"{pid}-{ipa}: nema cijene")
            continue
        buyable = is_buyable(j.get("product_add_to_cart"))
        if not buyable and orderable and not INCLUDE_UNAVAILABLE_SIZES:
            continue

        # stvarni nazivi atributa za ovu kombinaciju (iz odgovora, ne iz našeg zahtjeva)
        vsoup = BeautifulSoup(j.get("product_variants") or "", "html.parser")
        chosen = {}
        for item in vsoup.select(".product-variants-item"):
            lab = (item.select_one(".control-label, label") or item).get_text(" ", strip=True).lower()
            opt = item.select_one("option[selected]") or item.select_one("input[checked]")
            if opt:
                val = opt.get_text(strip=True) or opt.get("title") or ""
                if not val:
                    li = opt.find_parent(["li", "label"])
                    val = li.get_text(" ", strip=True) if li else ""
                chosen[lab] = val
        size = next((v for k, v in chosen.items() if "dimenz" in k or "velič" in k or "širin" in k), "")
        color = next((v for k, v in chosen.items() if "boj" in k), "")
        if not size and not color:  # rezerva
            for k, (_v, lbl) in zip(keys, combo):
                if "dimenz" in groups[k]["label"].lower():
                    size = lbl
                elif "boj" in groups[k]["label"].lower():
                    color = lbl

        csoup = BeautifulSoup(j.get("product_cover_thumbnails") or "", "html.parser")
        cimg = csoup.select_one(".js-qv-product-cover, img[data-image-large-src]")
        vimg = urljoin(BASE, cimg.get("src") or cimg.get("data-image-large-src")) if cimg else (imgs[0] if imgs else "")
        others = [i for i in imgs if i != vimg][:10]

        ref = ""
        dsoup = BeautifulSoup(j.get("product_details") or "", "html.parser")
        refel = dsoup.select_one(".product-reference span, [itemprop=sku]")
        if refel:
            ref = refel.get_text(strip=True)
        if not ref:
            m = re.search(r"Šifra:\s*([\w\-]+)", text_of(j.get("product_prices")))
            ref = m.group(1) if m else ""

        title = name
        if size and size not in title:
            title = f"{name} {size} cm" if re.fullmatch(r"[\d\s,x×]+", size) else f"{name} {size}"

        p, sp = price_fields(price, regular)
        rows.append({**base, "id": f"{pid}-{ipa}", "item_group_id": str(pid), "title": title[:150],
                     "price": p, "sale_price": sp, "availability": avail(buyable),
                     "link": urljoin(BASE, j.get("product_url") or url),
                     "image_link": vimg, "additional_image_link": ",".join(others),
                     "description": (note(price) + " " + desc).strip()[:5000],
                     "size": size, "color": color, "mpn": clean_ref(ref),
                     "custom_label_0": "po m2" if (u2 or unit) == "m²" else "",
                     "custom_label_1": "na upit" if not orderable else ("dostupno" if buyable else "dimenzija na upit")})
    return rows


# ------------------------------------------------------------------ 3) zapis
FIELDS = ["id", "item_group_id", "title", "description", "availability", "condition", "price", "sale_price",
          "link", "image_link", "additional_image_link", "brand", "mpn", "size", "color", "product_type",
          "google_product_category", "custom_label_0", "custom_label_1"]


def validate(rows):
    good = []
    for r in rows:
        miss = [f for f in ("id", "title", "price", "link", "image_link", "availability") if not r.get(f)]
        if miss:
            warnings.append(f"{r.get('id')}: nedostaje {', '.join(miss)} – izbačeno iz feeda")
            continue
        good.append(r)
    return good


def write_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_xml(rows, path):
    with open(path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<rss xmlns:g="http://base.google.com/ns/1.0" version="2.0">'
                f"<channel><title>{escape(BRAND)}</title><link>{BASE}</link><description>Meta katalog</description>\n")
        for r in rows:
            f.write("<item>" + "".join(f"<g:{k}>{escape(str(r[k]))}</g:{k}>" for k in FIELDS if r.get(k)) + "</item>\n")
        f.write("</channel></rss>\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="samo prvih N proizvoda (test)")
    ap.add_argument("--probe", action="store_true", help="provjeri i sve ID-eve (nađe proizvode izvan kategorija)")
    ap.add_argument("--ids", help="samo ovi ID-evi, npr. 1623,1866")
    args = ap.parse_args()

    t0 = time.time()
    if args.ids:
        ids = [i.strip() for i in args.ids.split(",") if i.strip()]
    else:
        print("Tražim proizvode ...")
        ids = discover_products(args.probe)
    if args.limit:
        ids = ids[: args.limit]
    print(f"Proizvoda: {len(ids)}")

    rows = []
    with ThreadPoolExecutor(WORKERS) as ex:
        for i, res in enumerate(ex.map(scrape_product, ids), 1):
            rows.extend(res)
            if i % 20 == 0:
                print(f"  {i}/{len(ids)} proizvoda, {len(rows)} redaka")
    rows = validate(rows)

    if not rows or (not args.limit and not args.ids and len(rows) < 100):
        sys.exit(f"Premalo redaka ({len(rows)}) – web je vjerojatno pao, feed NIJE prepisan.")

    OUT_DIR.mkdir(exist_ok=True)
    write_csv(rows, OUT_DIR / "meta-katalog.csv")
    write_xml(rows, OUT_DIR / "meta-katalog.xml")

    av = Counter(r["availability"] for r in rows)
    lab = Counter(r["custom_label_1"] or "obično" for r in rows)
    rep = [f"Vrijeme: {time.strftime('%Y-%m-%d %H:%M')}  ({time.time() - t0:.0f} s)",
           f"Proizvoda: {len(ids)}   Redaka u feedu (s varijantama): {len(rows)}",
           "Dostupnost: " + ", ".join(f"{k}={v}" for k, v in av.items()),
           "Vrste: " + ", ".join(f"{k}={v}" for k, v in lab.items()),
           f"Upozorenja ({len(warnings)}):"] + [f"  - {w}" for w in warnings[:300]]
    (OUT_DIR / "izvjestaj.txt").write_text("\n".join(rep) + "\n", encoding="utf-8")
    print("\n".join(rep[:4]))


if __name__ == "__main__":
    main()
