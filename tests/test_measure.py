"""f1_measure.measure(): HTML parsing against static fixtures, no network."""
import json
import os

import pytest

import f1_measure
from conftest import ACME, EXAMPLE, acme_routes, example_routes


@pytest.fixture
def acme(fake_web, tmp_path):
    web = fake_web(acme_routes())
    ev = f1_measure.measure("Acme Plumbing", ACME + "/", str(tmp_path))
    return ev, ev["checks"], web, tmp_path


def test_lxml_parser_is_installed():
    # f1_measure parses with BeautifulSoup(..., "lxml"); without the lxml
    # package a fresh install fails with bs4.FeatureNotFound.
    from bs4 import BeautifulSoup
    assert BeautifulSoup("<p>x</p>", "lxml").p.text == "x"


def test_writes_slug_named_evidence_file(acme):
    ev, C, web, tmp = acme
    path = tmp / "acme-plumbing-evidence.json"
    assert path.exists()
    assert json.loads(path.read_text())["checks"].keys() == C.keys()
    assert ev["business"] == "Acme Plumbing" and ev["url"] == ACME + "/"


def test_every_check_records_method_and_timestamp(acme):
    _, C, _, _ = acme
    expected = {"1.1", "1.5", "1.6", "1.7", "2.1", "2.4", "3.1", "3.2", "3.3", "3.4",
                "3.5-linked", "3.7", "3.8", "4.1", "4.2", "4.3", "4.4", "4.5", "4.6",
                "5.2", "6.1", "6.2", "6.3", "6.4", "6.5", "6.6"}
    assert set(C) == expected
    for cid, c in C.items():
        assert c["method"] and c["ts"], cid


def test_speed_checks(acme):
    _, C, _, _ = acme
    assert C["1.1"]["value"] == 420 and C["1.1"]["note"] == "pass<600ms"
    v = C["1.5"]["value"]
    assert v["n_resources"] == 6  # 2 img + 2 same-origin scripts + 2 stylesheets
    assert v["resource_bytes_known"] == 120000 + 700000 + 50000 + 10000 + 20000
    assert v["est_total_bytes"] == v["html_bytes"] + v["resource_bytes_known"]
    six = C["1.6"]["value"]
    assert six["n_images"] == 2  # third-party shutterstock image is not HEAD-checked
    assert [i["url"] for i in six["images_over_500k"]] == [ACME + "/img/hero.jpg"]
    assert six["nonmodern"] == [ACME + "/img/hero.jpg"]
    # app.js + third-party widget.js lack async/defer; print.css has media=print
    assert C["1.7"]["value"] == {"sync_scripts": 2, "blocking_stylesheets": 1}


def test_mobile_and_seo_checks(acme):
    _, C, _, _ = acme
    assert C["2.1"]["value"] is True
    assert C["2.4"]["value"] == ["tel:+15550102030"]
    assert C["3.1"]["value"] == {"title": "Acme Plumbing | 24/7 Emergency Plumber in Springfield",
                                 "len": 53}
    assert C["3.2"]["value"]["len"] == 143
    assert C["3.3"]["value"] == {"h1_count": 1, "h1s": ["Springfield's 24/7 Emergency Plumber"]}
    assert C["3.4"]["value"] == ["LocalBusiness/Plumber", "WebSite"]
    assert C["3.5-linked"]["value"] is True
    assert C["3.7"]["value"]["https"] is True and C["3.7"]["value"]["cert"]["valid"] is True
    assert C["3.8"]["value"] == {"robots": 200, "sitemap_xml": 404, "sitemap_index_guess": 200}


def test_contact_path_checks(acme):
    _, C, _, _ = acme
    assert C["4.1"]["value"]["phone_in_header_or_hero"] is True
    assert "(555) 010-2030" in C["4.1"]["value"]["sample"]
    forms = C["4.2"]["value"]
    assert forms["n_forms"] == 1
    assert forms["forms"][0] == {"action": "/quote",
                                 "fields": ["name", "phone", "email", "message", "submit"],
                                 "n_fields": 5}
    cta = C["4.3"]["value"]
    assert cta["pages_checked"] == [ACME + "/", ACME + "/services", ACME + "/about",
                                    ACME + "/contact", ACME + "/old-page"]
    assert cta["pages_with_cta"] == [ACME + "/", ACME + "/services", ACME + "/contact"]
    flow = C["4.4"]["value"]
    # Quirk (documented, not fixed here): the 4.4 contact-link regex matches
    # "book" inside "facebook", so Facebook profile/share links are counted as
    # contact links. Only the first link is walked, so steps_to_send is unaffected.
    assert flow == {"steps_to_send": 2,
                    "contact_links": [ACME + "/contact",
                                      "https://www.facebook.com/acmeplumbingtest",
                                      "https://www.facebook.com/sharer/sharer.php?u=acme"],
                    "form_or_tel_on_target": True}
    assert C["4.5"]["value"] is True
    assert C["4.6"]["value"] == {"hours_stated": True, "service_area_stated": True}


def test_trust_checks(acme):
    _, C, _, _ = acme
    assert C["5.2"]["value"] is True
    assert C["6.1"]["value"] is True
    assert C["6.2"]["value"] is True
    assert C["6.3"]["value"] == {"n_images": 3, "with_alt": 2,
                                 "known_stock_urls": ["https://images.shutterstock.com/stock-plumber.jpg"]}
    assert C["6.4"]["value"] == {"facebook.com": "https://www.facebook.com/acmeplumbingtest",
                                 "instagram.com": "https://www.instagram.com/acmeplumbingtest"}
    assert C["6.5"]["value"] is True
    assert C["6.6"]["value"] == {"checked": 5, "broken": [{"url": ACME + "/old-page", "status": 404}]}


def test_only_fixture_urls_are_requested(acme):
    _, _, web, _ = acme
    routes = acme_routes()
    assert web.calls, "transport was not used"
    assert all(url in routes for _, url in web.calls)


def test_recorded_example_com(fake_web, tmp_path):
    fake_web(example_routes())
    C = f1_measure.measure("Example Domain", EXAMPLE + "/", str(tmp_path))["checks"]
    assert C["1.1"]["value"] == 95
    assert C["2.1"]["value"] is True
    assert C["2.4"]["value"] == []
    assert C["3.1"]["value"] == {"title": "Example Domain", "len": 14}
    assert C["3.2"]["value"] == {"description": "", "len": 0}
    assert C["3.3"]["value"] == {"h1_count": 0, "h1s": []}
    assert C["3.4"]["value"] == []
    assert C["1.5"]["value"]["n_resources"] == 1  # /s.js; data: icon skipped
    assert C["1.7"]["value"] == {"sync_scripts": 1, "blocking_stylesheets": 0}
    assert C["4.2"]["value"] == {"n_forms": 0, "forms": []}
    assert C["4.4"]["value"]["steps_to_send"] == "unverifiable"
    assert C["6.4"]["value"] == {}
    assert C["3.8"]["value"] == {"robots": 404, "sitemap_xml": 404, "sitemap_index_guess": 404}


def test_unreachable_homepage_is_fatal(fake_web, tmp_path):
    fake_web({})
    ev = f1_measure.measure("Gone Co", "https://gone.test/", str(tmp_path))
    assert ev["fatal"] == "homepage unreachable"
    assert ev["checks"] == {}
    # NOTE: the fatal path writes "evidence.json", not "<slug>-evidence.json".
    assert (tmp_path / "evidence.json").exists()
    assert not (tmp_path / "gone-co-evidence.json").exists()


def test_cert_info_fails_closed_without_network():
    info = f1_measure.cert_info("example.com")
    assert info["valid"] is False and "network disabled" in info["error"]
