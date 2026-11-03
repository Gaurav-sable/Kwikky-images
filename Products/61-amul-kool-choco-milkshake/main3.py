"""
Product image downloader (Blinkit + Amazon.in).

Opens a browser once and stays open. For each product:
  1. Browse to the product page in the browser window
  2. Come back to the terminal, type a folder name (or 'q' to quit)
  3. Press ENTER -> images are saved to <out>/<folder name>/

The site is detected from the page you currently have open, so you can mix
Blinkit and Amazon products in the same session.

Usage:
    python main2.py                                   # starts on Amazon.in, saves under ./output
    python main2.py --site blinkit                    # starts on Blinkit
    python main2.py --url https://www.amazon.in/...   # start on any URL
    python main2.py --out ~/Kwikky-images/Products
"""
import argparse
import json
import os
import re

import requests
from playwright.sync_api import sync_playwright

DEFAULT_OUTPUT_DIR = "output"
QUIT_WORDS = {"q", "quit", "exit"}

START_URLS = {
    "amazon": "https://www.amazon.in/Gourmet-Specialty-Foods/b/ref=glow_cls?ie=UTF8&node=2454178031&ref_=nav_cs_grocery",
    "blinkit": "https://blinkit.com",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


# --------------------------------------------------------------------------- helpers

def download_image(session, url, filepath):
    try:
        r = session.get(url, timeout=60)
        if r.status_code == 200 and r.content:
            with open(filepath, "wb") as f:
                f.write(r.content)
            return True
        print(f"  HTTP {r.status_code} for {url}")
    except Exception as e:
        print(f"  {e}")
    return False


def safe_folder_name(name):
    """Turn user input into a safe relative path. 'Dairy/Amul Butter' makes a nested folder."""
    parts = []
    for part in name.replace("\\", "/").split("/"):
        part = re.sub(r'[<>:"|?*\x00-\x1f]', "", part).strip().strip(".")
        if part:
            parts.append(part)
    return os.path.join(*parts) if parts else ""


def detect_site(url):
    u = url.lower()
    if "amazon." in u:
        return "amazon"
    if "blinkit." in u:
        return "blinkit"
    return None


def is_product_url(url):
    u = url.lower()
    if "amazon." in u:
        return bool(re.search(r"/(dp|gp/product|gp/aw/d)/", u))
    if "blinkit." in u:
        return "/prn/" in u
    return False


def choose_page(context):
    """
    Playwright can't tell which tab is in front (every tab reports itself visible), so choose by
    URL instead: the newest tab that is a product page. Returns (page_or_None, all_open_pages).
    """
    pages = [pg for pg in context.pages if not pg.is_closed()]
    products = [pg for pg in pages if is_product_url(pg.url)]
    if products:
        return products[-1], pages
    return (pages[-1] if pages else None), pages


def dedupe(items):
    return list(dict.fromkeys(items))


# --------------------------------------------------------------------------- Blinkit

def blinkit_make_large(url):
    url = re.sub(r"w=\d+", "w=1200", url)
    url = re.sub(r"h=\d+", "h=1200", url)
    return url


def blinkit_images(page, img_srcs):
    out = []
    for url in img_srcs:
        if not url or "cms/product/" not in url or "/assets/" in url:
            continue
        out.append(url)
    return [blinkit_make_large(u) for u in dedupe(out)]


# --------------------------------------------------------------------------- Amazon

AMAZON_SIZE_MODIFIER = re.compile(r"\._[A-Za-z0-9_,%\-]+_(?=\.[A-Za-z]{3,4}$)")
AMAZON_IMAGE_ID = re.compile(r"/images/I/([^./]+)")


def amazon_full_size(url):
    """.../I/71abc._SX300_.jpg -> .../I/71abc.jpg (original resolution)."""
    return AMAZON_SIZE_MODIFIER.sub("", url.split("?")[0])


def _matching_bracket(text, start):
    """Index of the ']' matching the '[' at text[start]; strings are skipped."""
    depth, in_str, quote, i = 0, False, "", start
    while i < len(text):
        c = text[i]
        if in_str:
            if c == "\\":
                i += 1
            elif c == quote:
                in_str = False
        elif c in "\"'":
            in_str, quote = True, c
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def amazon_gallery_from_html(html):
    """Hi-res gallery URLs of the *selected* variant, from the page's embedded data."""
    m = re.search(r"['\"]colorImages['\"]\s*:\s*\{\s*['\"]initial['\"]\s*:\s*\[", html)
    if not m:
        return []
    start = m.end() - 1
    end = _matching_bracket(html, start)
    if end == -1:
        return []
    block = html[start:end + 1]

    entries = None
    try:
        entries = json.loads(block)
    except Exception:
        pass

    urls = []
    if entries is not None:
        for e in entries:
            if not isinstance(e, dict):
                continue
            url = e.get("hiRes") or e.get("large")
            if not url and isinstance(e.get("main"), dict) and e["main"]:
                url = max(e["main"], key=lambda k: len(k))  # keys are URLs
            if url:
                urls.append(url)
    else:  # embedded data wasn't valid JSON: fall back to regex
        urls = re.findall(r'"hiRes"\s*:\s*"([^"]+)"', block) or re.findall(r'"large"\s*:\s*"([^"]+)"', block)
    return urls


def amazon_images(page, img_srcs):
    url = page.url
    if not re.search(r"/(dp|gp/product|gp/aw/d)/", url):
        print("  This doesn't look like a single product page (no /dp/ in the URL).")
        print("  Click a product from the list so its own page opens, then try again.")
        return []

    urls = amazon_gallery_from_html(page.content())

    if not urls:  # fallback 1: thumbnail strip next to the main image
        urls = page.eval_on_selector_all(
            "#altImages img, #imageBlock img.a-dynamic-image, #imgTagWrapperId img",
            "imgs => imgs.map(i => i.currentSrc || i.src)",
        )
    if not urls:  # fallback 2: main image only
        urls = [u for u in img_srcs if "/images/I/" in u][:1]

    cleaned, seen_ids = [], set()
    for u in urls:
        if not u or "/images/I/" not in u:
            continue
        low = u.lower()
        if any(x in low for x in ("sprite", "transparent-pixel", "play-button", "play-icon", "pkplay", "video")):
            continue
        full = amazon_full_size(u)
        m = AMAZON_IMAGE_ID.search(full)
        key = m.group(1) if m else full
        if key in seen_ids:
            continue
        seen_ids.add(key)
        cleaned.append(full)
    return cleaned


# --------------------------------------------------------------------------- collection

def collect_product_images(page, attempts=5):
    """Read the page, retrying if it is mid-navigation (both sites are SPA-ish)."""
    img_srcs = []
    for attempt in range(1, attempts + 1):
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1500)  # let the gallery render
            img_srcs = page.eval_on_selector_all("img", "imgs => imgs.map(i => i.currentSrc || i.src)")
            break
        except Exception as e:
            if "context was destroyed" in str(e) or "navigation" in str(e).lower():
                print(f"  Page still navigating, retrying ({attempt}/{attempts})...")
                page.wait_for_timeout(2000)
                continue
            raise
    else:
        print("  Page kept navigating; wait for it to finish loading and try again.")
        return []

    site = detect_site(page.url)
    if site == "amazon":
        print("  Site: Amazon")
        return amazon_images(page, img_srcs)
    if site == "blinkit":
        print("  Site: Blinkit")
        return blinkit_images(page, img_srcs)

    print(f"  Unsupported site: {page.url}")
    return []


def download_current_page(page, session, out_dir, folder_name):
    product_images = collect_product_images(page)

    print(f"Gallery images found: {len(product_images)}")
    if not product_images:
        print("  No product images found. Make sure the product page and its gallery are fully loaded, then try again.")
        return

    folder = os.path.join(out_dir, folder_name)
    os.makedirs(folder, exist_ok=True)

    downloaded = 0
    for idx, url in enumerate(product_images, start=1):
        ext = ".png" if url.lower().split("?")[0].endswith(".png") else ".jpg"
        filepath = os.path.join(folder, f"{idx}{ext}")
        if download_image(session, url, filepath):
            downloaded += 1
            print(f"  Downloaded {idx}")

    print(f"Saved {downloaded}/{len(product_images)} images to {os.path.abspath(folder)}")


# --------------------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(description="Download product images from Blinkit / Amazon.in.")
    parser.add_argument("--out", default=DEFAULT_OUTPUT_DIR,
                        help=f"Base output directory; each product gets a subfolder (default: {DEFAULT_OUTPUT_DIR})")
    parser.add_argument("--site", choices=sorted(START_URLS), default="amazon",
                        help="Which site to open at startup (default: amazon)")
    parser.add_argument("--url", help="Start URL (overrides --site)")
    parser.add_argument("--profile", default=".browser_profile",
                        help="Folder where the browser keeps cookies/sign-ins between runs (default: .browser_profile)")
    args = parser.parse_args()

    out_dir = os.path.expanduser(args.out)
    os.makedirs(out_dir, exist_ok=True)
    start_url = args.url or START_URLS[args.site]

    session = requests.Session()
    session.headers.update(HEADERS)

    with sync_playwright() as p:
        # Persistent profile: tabs you open yourself (right-click > open in new tab,
        # ctrl+click, target=_blank links) belong to this context, so the script can see them.
        # It also keeps cookies/sign-ins between runs.
        context = p.chromium.launch_persistent_context(
            os.path.abspath(os.path.expanduser(args.profile)),
            headless=False,
            no_viewport=True,
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(start_url, wait_until="networkidle", timeout=45000)
        except Exception:
            # Some sites never go fully idle; the page is usually usable anyway
            print("Page is still loading in the background; continuing.")

        print()
        print("MANUAL STEPS (repeat for each product)")
        print("=" * 50)
        print("1. In the browser, search a product and open its own product page")
        print("2. Wait until the gallery is visible")
        print("3. Come back here and type a folder name")
        print("   (use 'Dairy/Amul Butter' for a nested folder, or 'q' to quit)")
        print(f"Saving under: {os.path.abspath(out_dir)}")
        print()

        while True:
            name = input("Folder name for product (q to quit): ").strip()

            if name.lower() in QUIT_WORDS:
                break

            folder_name = safe_folder_name(name)
            if not folder_name:
                print("  Please enter a folder name.")
                continue

            page, pages = choose_page(context)
            if page is None:
                print("  Browser window was closed. Exiting.")
                break

            print(f"  Tabs the script can see: {len(pages)}")
            for i, pg in enumerate(pages, start=1):
                mark = "  <-- using this" if pg is page else ""
                print(f"    {i}. {pg.url[:80]}{mark}")

            temp_page = None
            if not is_product_url(page.url):
                print("  None of the open tabs is a product page.")
                url = input("  Paste the product page URL (or just press ENTER to try again): ").strip()
                if not url:
                    continue
                temp_page = context.new_page()
                try:
                    temp_page.goto(url, wait_until="domcontentloaded", timeout=45000)
                except Exception as e:
                    print(f"  Could not open that URL: {e}")
                    temp_page.close()
                    continue
                page = temp_page

            try:
                download_current_page(page, session, out_dir, folder_name)
            except Exception as e:
                print(f"  Error: {e}")
            finally:
                if temp_page is not None and not temp_page.is_closed():
                    temp_page.close()

            print()
            print("Open the next product in the browser, then enter its folder name.")
            print()

        context.close()


if __name__ == "__main__":
    main()
