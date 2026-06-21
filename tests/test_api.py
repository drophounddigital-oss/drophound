from drophound import db


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_landing_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "DropHound" in r.text
    assert "Never miss a drop" in r.text


def test_subscribe_creates_free_subscriber(client, conn):
    r = client.post("/subscribe", data={"email": "newbie@example.com"})
    assert r.status_code == 200  # followed redirect back to landing
    assert "list" in r.text.lower()
    row = db.one(conn, "SELECT * FROM subscribers WHERE email=?", ("newbie@example.com",))
    assert row is not None and row["tier"] == "free"


def test_subscribe_rejects_bad_email(client, conn):
    client.get("/")  # warm
    r = client.post("/subscribe", data={"email": "not-an-email"})
    assert r.status_code == 200
    assert db.one(conn, "SELECT * FROM subscribers WHERE email=?", ("not-an-email",)) is None


def test_drops_page_and_api(client):
    assert "Live drops" in client.get("/drops").text
    data = client.get("/api/drops").json()
    assert isinstance(data["drops"], list) and len(data["drops"]) > 0


def test_dashboard_and_collection(client):
    # Not logged in: both routes redirect to login
    assert client.get("/app", follow_redirects=False).status_code == 303
    assert client.get("/collection", follow_redirects=False).status_code == 303
    # After registering, dashboard loads and shows the user's email
    client.post("/register", data={"email": "dash@example.com", "password": "password123"})
    assert "dash@example.com" in client.get("/app").text
    assert "Cost basis" in client.get("/collection").text


def test_pricing_and_digest(client):
    assert "Premium" in client.get("/pricing").text
    assert client.get("/digest").status_code == 200


def test_go_redirect_logs_click(client, conn):
    pid = client.get("/api/products").json()["products"][0]["id"]
    r = client.get(f"/go/{pid}?to=ebay", follow_redirects=False)
    assert r.status_code == 302
    assert "ebay.com" in r.headers["location"]
    assert db.one(conn, "SELECT COUNT(*) c FROM affiliate_clicks WHERE product_id=?",
                  (pid,))["c"] >= 1


def test_hook_restock_known_sku(client, conn):
    before = db.one(conn, "SELECT COUNT(*) c FROM restock_events")["c"]
    r = client.post("/hook/restock", json={"sku": "PM-LAB-MAC-01", "price": 13.99})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert {c["channel"] for c in body["channels"]} == {"telegram", "discord", "email"}
    after = db.one(conn, "SELECT COUNT(*) c FROM restock_events")["c"]
    assert after == before + 1


def test_hook_restock_adhoc_name_only(client):
    r = client.post("/hook/restock", json={"name": "Mystery Drop", "price": 20})
    assert r.status_code == 200
    assert r.json()["product"] == "Mystery Drop"


def test_hook_restock_requires_name_or_sku(client):
    assert client.post("/hook/restock", json={}).status_code == 400


def test_hook_restock_secret_enforced(client, monkeypatch):
    monkeypatch.setenv("DROPHOUND_HOOK_SECRET", "s3cret")
    assert client.post("/hook/restock", json={"name": "X"}).status_code == 401
    ok = client.post("/hook/restock", json={"name": "X"},
                     headers={"X-DropHound-Secret": "s3cret"})
    assert ok.status_code == 200


def test_collection_value_api(client, conn):
    sub = db.one(conn, "SELECT * FROM subscribers WHERE email='demo@drophound.app'")
    r = client.get(f"/api/collection/{sub['id']}/value")
    assert r.status_code == 200
    body = r.json()
    assert body["cost_total"] > 0
    assert "value_total" in body and "gain" in body


def test_catalog_search(client):
    data = client.get("/api/catalog").json()
    assert data["total"] > 0 and len(data["products"]) > 0
    one = client.get("/api/catalog?q=Labubu").json()
    assert one["products"] and all(
        "labubu" in (p["name"] + p["character"] + p["brand"]).lower()
        for p in one["products"])


def test_watch_page_renders(client):
    assert "Pick what you watch" in client.get("/watch").text


def test_watch_add_list_remove(client, conn):
    pid = client.get("/api/products").json()["products"][0]["id"]
    # Register an account; the TestClient preserves the session cookie automatically.
    client.post("/register", data={"email": "picker@example.com", "password": "password123"})
    add = client.post("/watch/add", data={"product_id": pid}).json()
    assert add["watched"] is True and add["count"] == 1
    # idempotent: adding again stays at 1
    again = client.post("/watch/add", data={"product_id": pid}).json()
    assert again["count"] == 1
    assert client.get("/api/catalog").json()["watch_count"] == 1
    rem = client.post("/watch/remove", data={"product_id": pid}).json()
    assert rem["watched"] is False and rem["count"] == 0
    assert db.one(conn, "SELECT tier FROM subscribers WHERE email=?",
                  ("picker@example.com",))["tier"] == "free"


def test_watch_requires_login(client):
    # Not logged in: watch/add returns 401 with login_required flag
    r = client.post("/watch/add", data={"product_id": "1"})
    assert r.status_code == 401
    assert r.json().get("login_required") is True


def test_robots_txt(client):
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert "User-agent: *" in r.text and "Sitemap:" in r.text


def test_sitemap_xml(client):
    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    assert "<urlset" in r.text and "/watch" in r.text


def test_seo_meta_on_homepage(client):
    t = client.get("/").text
    assert 'property="og:image"' in t
    assert 'rel="canonical"' in t
    assert "application/ld+json" in t
    assert 'name="twitter:card"' in t
    assert "&amp;amp;" not in t  # title not double-escaped
