"""audit_pipeline.score(): pinned rubric thresholds, categories, grade."""
import pytest

import audit_pipeline as ap


def ev(**checks):
    return {"checks": {k.replace("_", ".").replace(".linked", "-linked"): {"value": v, "method": "m"}
                       for k, v in checks.items()}}


def s(cid, value):
    return ap.score({"checks": {cid: {"value": value, "method": "m"}}})["check_scores"][cid]


@pytest.mark.parametrize("ms,want", [(0, 1), (599, 1), (600, 0.5), (1499, 0.5), (1500, 0), (4000, 0)])
def test_ttfb_thresholds(ms, want):
    assert s("1.1", ms)["score"] == want


MB = 1024 * 1024


@pytest.mark.parametrize("b,want", [(2 * MB - 1, 1), (2 * MB, 0.5), (4 * MB - 1, 0.5), (4 * MB, 0)])
def test_page_weight_thresholds(b, want):
    v = {"est_total_bytes": b, "html_bytes": 1000, "n_resources": 3}
    assert s("1.5", v)["score"] == want


def test_images():
    assert s("1.6", {"n_images": 0, "images_over_500k": []})["score"] is None
    assert s("1.6", {"n_images": 4, "images_over_500k": []})["score"] == 1
    assert s("1.6", {"n_images": 4, "images_over_500k": [{"url": "u"}]})["score"] == 0


def test_blocking_resources():
    assert s("1.7", {"sync_scripts": 0, "blocking_stylesheets": 0})["score"] == 1
    assert s("1.7", {"sync_scripts": 0, "blocking_stylesheets": 1})["score"] == 0


def test_boolean_checks():
    for cid in ("2.1", "3.5-linked", "4.5", "5.2", "6.1", "6.2", "6.5"):
        assert s(cid, True)["score"] == 1, cid
        assert s(cid, False)["score"] == 0, cid
    assert s("2.4", ["tel:1"])["score"] == 1 and s("2.4", [])["score"] == 0
    assert s("6.4", {"facebook.com": "x"})["score"] == 1 and s("6.4", {})["score"] == 0


@pytest.mark.parametrize("title,want", [
    ("", 0), ("Home", 0.5), ("Short title", 0.5), ("x" * 30, 1), ("x" * 60, 1), ("x" * 61, 0.5)])
def test_title(title, want):
    assert s("3.1", {"title": title, "len": len(title)})["score"] == want


@pytest.mark.parametrize("n,want", [(0, 0), (50, 0.5), (119, 0.5), (120, 1), (160, 1), (161, 0.5)])
def test_meta_description(n, want):
    assert s("3.2", {"description": "d" * n, "len": n})["score"] == want


@pytest.mark.parametrize("n,want", [(0, 0), (1, 1), (2, 0)])
def test_h1(n, want):
    assert s("3.3", {"h1_count": n, "h1s": []})["score"] == want


def test_schema():
    assert s("3.4", ["LocalBusiness/Plumber"])["score"] == 1
    assert s("3.4", ["WebSite"])["score"] == 0.5
    assert s("3.4", [])["score"] == 0


def test_https():
    assert s("3.7", {"https": True, "cert": {"valid": True}})["score"] == 1
    assert s("3.7", {"https": True, "cert": {"valid": False}})["score"] == 0.5
    assert s("3.7", {"https": False, "cert": None})["score"] == 0


def test_robots_sitemap():
    assert s("3.8", {"robots": 200, "sitemap_xml": 200})["score"] == 1
    assert s("3.8", {"robots": 200, "sitemap_xml": 404})["score"] == 0.5
    assert s("3.8", {"robots": None, "sitemap_xml": 404})["score"] == 0


def test_forms():
    assert s("4.2", {"n_forms": 0, "forms": []})["score"] == 0
    good = {"fields": ["name", "phone", "message"]}
    weak = {"fields": ["email", "submit"]}
    assert s("4.2", {"n_forms": 2, "forms": [weak, good]})["score"] == 1
    assert s("4.2", {"n_forms": 1, "forms": [weak]})["score"] == 0.5


def test_cta_coverage():
    assert s("4.3", {"pages_checked": [], "pages_with_cta": []})["score"] is None
    assert s("4.3", {"pages_checked": list("abcde"), "pages_with_cta": list("abcd")})["score"] == 1
    assert s("4.3", {"pages_checked": list("abcde"), "pages_with_cta": list("abc")})["score"] == 0.5
    assert s("4.3", {"pages_checked": list("abcde"), "pages_with_cta": []})["score"] == 0


def test_quote_path():
    assert s("4.4", {"contact_links": [], "form_or_tel_on_target": False})["score"] == 0
    assert s("4.4", {"contact_links": ["u"], "form_or_tel_on_target": True})["score"] == 1
    assert s("4.4", {"contact_links": ["u"], "form_or_tel_on_target": False})["score"] == 0


def test_hours_area():
    assert s("4.6", {"hours_stated": True, "service_area_stated": True})["score"] == 1
    assert s("4.6", {"hours_stated": False, "service_area_stated": True})["score"] == 0.5
    assert s("4.6", {"hours_stated": False, "service_area_stated": False})["score"] == 0


def test_stock_photos_and_broken_links():
    assert s("6.3", {"n_images": 2, "with_alt": 1, "known_stock_urls": []})["score"] == 1
    assert s("6.3", {"n_images": 2, "with_alt": 1, "known_stock_urls": ["x"]})["score"] == 0
    assert s("6.6", {"checked": 3, "broken": []})["score"] == 1
    assert s("6.6", {"checked": 3, "broken": [{"url": "u", "status": 404}]})["score"] == 0


def test_unknown_and_malformed_checks():
    res = ap.score({"checks": {"9.9": {"value": 1}, "2.1": {"method": "no value"}}})
    assert "9.9" not in res["check_scores"]
    assert res["check_scores"]["2.1"]["score"] is None


def test_categories_weights_sum_to_100():
    assert sum(w for _, w, _ in ap.CATEGORIES) == 100
    ids = [i for _, _, ids in ap.CATEGORIES for i in ids]
    assert sorted(ids) == sorted(ap.FIXES)


def test_category_average_excludes_unscored():
    res = ap.score({"checks": {
        "1.1": {"value": 100}, "1.6": {"value": {"n_images": 0}},
        "1.7": {"value": {"sync_scripts": 1, "blocking_stylesheets": 0}}}})
    speed = res["categories"]["Speed"]
    assert speed["score"] == 50  # (1 + 0) / 2; 1.6 excluded, 1.5 missing
    assert set(speed["excluded"]) == {"1.5", "1.6"}
    assert res["categories"]["Reviews"]["score"] == 0  # nothing measured


def all_pass_evidence():
    return {"checks": {k: {"value": v, "method": "m"} for k, v in {
        "1.1": 100, "1.5": {"est_total_bytes": 1000, "html_bytes": 500, "n_resources": 1},
        "1.6": {"n_images": 1, "images_over_500k": []},
        "1.7": {"sync_scripts": 0, "blocking_stylesheets": 0},
        "2.1": True, "2.4": ["tel:1"],
        "3.1": {"title": "x" * 40, "len": 40}, "3.2": {"description": "d" * 140, "len": 140},
        "3.3": {"h1_count": 1, "h1s": ["h"]}, "3.4": ["LocalBusiness"], "3.5-linked": True,
        "3.7": {"https": True, "cert": {"valid": True}}, "3.8": {"robots": 200, "sitemap_xml": 200},
        "4.1": {"phone_in_header_or_hero": True, "sample": []},
        "4.2": {"n_forms": 1, "forms": [{"fields": ["name", "phone", "message"]}]},
        "4.3": {"pages_checked": ["a"], "pages_with_cta": ["a"]},
        "4.4": {"contact_links": ["c"], "form_or_tel_on_target": True},
        "4.5": True, "4.6": {"hours_stated": True, "service_area_stated": True},
        "5.2": True, "6.1": True, "6.2": True,
        "6.3": {"n_images": 1, "with_alt": 1, "known_stock_urls": []},
        "6.4": {"facebook.com": "f"}, "6.5": True, "6.6": {"checked": 1, "broken": []},
    }.items()}}


def test_perfect_site_scores_100_grade_a():
    res = ap.score(all_pass_evidence())
    assert res["total"] == 100 and res["grade"] == "A"
    assert all(c["score"] == 100 for c in res["categories"].values())


def test_weighted_total_and_grade():
    e = all_pass_evidence()
    e["checks"]["5.2"]["value"] = False          # Reviews 0  -> -10
    e["checks"]["2.1"]["value"] = False          # Mobile 50  -> -10
    res = ap.score(e)
    assert res["categories"]["Reviews"]["score"] == 0
    assert res["categories"]["Mobile"]["score"] == 50
    assert res["total"] == 80 and res["grade"] == "B"


def test_report_from_fixture(fake_web, tmp_path):
    import f1_measure
    from conftest import ACME, acme_routes
    fake_web(acme_routes())
    evd = f1_measure.measure("Acme Plumbing", ACME + "/", str(tmp_path))
    sc = ap.score(evd)
    md = ap.build_report("Acme Plumbing", ACME + "/", evd, sc, "2026-11-05", "acme-plumbing.test")
    assert f"## YOUR SCORE: {sc['total']}/100 -- Grade {sc['grade']}" in md
    assert "**Prepared for: Acme Plumbing | acme-plumbing.test | Audited 2026-11-05**" in md
    # failing checks appear as findings, ranked CRITICAL before MAJOR before MINOR
    assert "Broken links found" in md and "Oversized or legacy-format images" in md
    assert md.index("-- MAJOR") < md.index("-- MINOR")
    for cid, label, _ in ap.NOT_MEASURED:
        assert f"- {cid} {label}" in md
