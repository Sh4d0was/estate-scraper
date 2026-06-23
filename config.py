"""
Scraper configuration — edit this file to target a different listings site.
"""

BASE_URL = "http://books.toscrape.com"
START_PAGE = "http://books.toscrape.com/catalogue/page-{page}.html"

# CSS selectors for the listing card grid.
# Swap these (and BASE_URL / START_PAGE) to point the engine at another site.
SELECTORS = {
    "card": "article.product_pod",
    "title": "h3 > a",            # read the `title` attribute, not inner text
    "price": "p.price_color",
    "rating": "p.star-rating",    # second CSS class word encodes the rating
    "availability": "p.instock.availability",
    "image": "img",               # first <img> inside the card
    "next_page": "li.next > a",
}

# Maps the word-form star-rating class to an integer.
RATING_MAP = {"One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5}

USER_AGENT = (
    "EstateScraper/1.0 (+https://github.com/yourname/estate-scraper; "
    "educational portfolio project; not for commercial use)"
)

# CLI defaults
DEFAULT_MAX_PAGES = 5
DEFAULT_OUTPUT = "output/listings.csv"
DEFAULT_FORMAT = "csv"
DEFAULT_DELAY = 1.0
DEFAULT_WORKERS = 4
