# Test fixtures

Every fixture here is static HTML served to `f1_measure.measure()` through a
fake `requests` transport in `tests/conftest.py`. No test touches the network.

| File | What it is |
|---|---|
| `example-com-home.html` | Recorded response body of `GET https://example.com/` (IANA's reserved documentation domain), captured 2026-11-05 with `curl -s https://example.com`. Byte-for-byte, unmodified. |
| `acme-home.html`, `acme-contact.html`, `acme-services.html`, `acme-about.html` | Hand-written pages for a fictional plumbing business (`acme-plumbing.test`, 555 phone numbers). Written for these tests so each audit check has a known, deliberate pass or fail. Not a real site. |
