"""Test della ricerca web gratuita (parsing DDG/Bing, senza rete)."""
import unittest

from ekko.connectors import websearch


DDG_HTML = """
<div class="results">
  <div class="result results_links results_links_deep web-result">
    <h2 class="result__title">
      <a rel="nofollow" class="result__a"
         href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.tripadvisor.it%2FRestaurant_Review-g187791-d123-Reviews-Da_Mario-Roma.html&amp;rut=abc">
        Da Mario, Roma - <b>Recensioni</b> | TripAdvisor</a>
    </h2>
  </div>
  <div class="result result--ad">
    <a class="result__a"
       href="//duckduckgo.com/y.js?ad_domain=example.com&amp;u3=xyz">Annuncio sponsorizzato</a>
  </div>
  <div class="result">
    <a rel="nofollow" class="result__a"
       href="https://www.tripadvisor.it/Restaurants-g187791-Roma.html">I migliori ristoranti a Roma</a>
  </div>
</div>
"""

BING_HTML = """
<ol id="b_results">
  <li class="b_algo">
    <h2><a href="https://www.tripadvisor.it/Restaurant_Review-g187791-d123-Reviews-Da_Mario-Roma.html"
           h="ID=SERP,1">Da Mario, Roma &#8211; Recensioni</a></h2>
    <p>Recensioni del ristorante.</p>
  </li>
  <li class="b_ad">
    <h2><a href="https://www.bing.com/aclick?ld=xyz">Annuncio</a></h2>
  </li>
  <li class="b_algo">
    <h2><a href="https://it.trustpilot.com/review/damario.it">Da Mario | Trustpilot</a></h2>
  </li>
</ol>
"""


class ParseDdgTest(unittest.TestCase):
    def test_decodes_uddg_redirect_and_skips_ads(self):
        hits = websearch._parse_ddg(DDG_HTML)
        self.assertEqual(len(hits), 2)          # l'annuncio y.js è escluso
        self.assertEqual(
            hits[0]["url"],
            "https://www.tripadvisor.it/Restaurant_Review-g187791-d123-Reviews-Da_Mario-Roma.html")
        # titolo pulito: niente tag interni, entità decodificate
        self.assertEqual(hits[0]["title"], "Da Mario, Roma - Recensioni | TripAdvisor")
        # link diretto (non redirect) passato tale e quale
        self.assertEqual(hits[1]["url"],
                         "https://www.tripadvisor.it/Restaurants-g187791-Roma.html")

    def test_empty_html(self):
        self.assertEqual(websearch._parse_ddg("<html></html>"), [])


class ParseBingTest(unittest.TestCase):
    def test_parses_b_algo_and_skips_ads(self):
        hits = websearch._parse_bing(BING_HTML)
        self.assertEqual(len(hits), 2)          # il blocco b_ad è escluso
        self.assertIn("Restaurant_Review", hits[0]["url"])
        self.assertEqual(hits[0]["title"], "Da Mario, Roma – Recensioni")
        self.assertEqual(hits[1]["url"], "https://it.trustpilot.com/review/damario.it")

    def test_empty_html(self):
        self.assertEqual(websearch._parse_bing("<html></html>"), [])

    def test_decodes_ck_redirect(self):
        # Bing spesso avvolge gli URL organici in bing.com/ck/a?…&u=a1<base64url>
        import base64
        real = "https://www.autoscout24.it/concessionari/pasquale-auto-srl"
        u = "a1" + base64.urlsafe_b64encode(real.encode()).decode().rstrip("=")
        html = (f'<li class="b_algo"><h2><a href='
                f'"https://www.bing.com/ck/a?!&amp;&amp;p=xx&amp;u={u}&amp;ntb=1">'
                f'Pasquale Auto Srl</a></h2></li>')
        hits = websearch._parse_bing(html)
        self.assertEqual(hits, [{"url": real, "title": "Pasquale Auto Srl"}])


class SearchFallbackTest(unittest.TestCase):
    """search(): DDG primario, Bing di riserva, [] se tutto fallisce."""

    def setUp(self):
        self._orig = (websearch._fetch_ddg, websearch._fetch_bing)

    def tearDown(self):
        websearch._fetch_ddg, websearch._fetch_bing = self._orig

    def test_ddg_first(self):
        websearch._fetch_ddg = lambda q: DDG_HTML
        websearch._fetch_bing = lambda q, num: self.fail("Bing non va chiamato")
        hits = websearch.search("site:tripadvisor.it da mario roma", num=1)
        self.assertEqual(len(hits), 1)          # rispetta num

    def test_bing_when_ddg_fails(self):
        def boom(q):
            raise RuntimeError("DDG bloccato")
        websearch._fetch_ddg = boom
        websearch._fetch_bing = lambda q, num: BING_HTML
        hits = websearch.search("da mario")
        self.assertEqual(len(hits), 2)
        self.assertIn("tripadvisor", hits[0]["url"])

    def test_all_fail_returns_empty(self):
        def boom(*a, **k):
            raise RuntimeError("rete giù")
        websearch._fetch_ddg = boom
        websearch._fetch_bing = boom
        self.assertEqual(websearch.search("qualsiasi cosa"), [])


if __name__ == "__main__":
    unittest.main()
