from flask import Flask, render_template, request, redirect, jsonify, flash
import sqlite3
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv, set_key
import os

import anthropic
from anthropic import APIError

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "smartpim-secret-key-2024")

# Always use the correct model name
DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-20250514"


# ─────────────────────────────────────────────
# AI Description
# ─────────────────────────────────────────────

def claude_product_description(name: str, price: str) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key or api_key.startswith("sk-ant-api03-xxxx"):
        raise ValueError("ANTHROPIC_API_KEY is not set. Add your real key to the .env file.")

    client = anthropic.Anthropic(api_key=api_key)
    user_msg = (
        f"Write a concise, SEO-friendly ecommerce product description (2-4 short paragraphs, "
        f"bullet list of 3-5 key features if appropriate). Product name: {name}. "
        f"Price: Rs.{price}. Use a professional, persuasive tone. Do not include a title line or markdown headings."
    )
    msg = client.messages.create(
        model=DEFAULT_CLAUDE_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": user_msg}],
    )
    parts = [block.text for block in msg.content if block.type == "text"]
    text = "\n\n".join(parts).strip()
    if not text:
        raise ValueError("The model returned an empty description.")
    return text


# ─────────────────────────────────────────────
# WooCommerce Helpers
# ─────────────────────────────────────────────

def get_wc_creds():
    return {
        "url": os.getenv("WC_URL", "").rstrip("/"),
        "key": os.getenv("WC_CONSUMER_KEY", ""),
        "secret": os.getenv("WC_CONSUMER_SECRET", ""),
    }

def wc_push_product(product: dict) -> dict:
    creds = get_wc_creds()
    if not creds["url"] or not creds["key"] or not creds["secret"]:
        raise ValueError("WooCommerce credentials not configured. Go to Settings.")

    endpoint = f"{creds['url']}/wp-json/wc/v3/products"
    payload = {
        "name": product["name"],
        "sku": product["sku"],
        "regular_price": str(product["price"]),
        "description": product["description"] or "",
        "status": "publish",
        "type": "simple",
    }
    resp = requests.post(
        endpoint,
        json=payload,
        auth=HTTPBasicAuth(creds["key"], creds["secret"]),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()

def wc_test_connection():
    creds = get_wc_creds()
    if not creds["url"] or not creds["key"] or not creds["secret"]:
        return False, "Credentials missing."
    try:
        resp = requests.get(
            f"{creds['url']}/wp-json/wc/v3/products?per_page=1",
            auth=HTTPBasicAuth(creds["key"], creds["secret"]),
            timeout=10,
        )
        if resp.status_code == 200:
            return True, "Connected! WooCommerce is reachable at " + creds["url"]
        elif resp.status_code == 401:
            return False, "Auth failed - check your Consumer Key and Secret."
        else:
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except requests.exceptions.ConnectionError:
        return False, f"Cannot reach {creds['url']} - is WordPress/Local running?"
    except Exception as e:
        return False, str(e)


# ─────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect("smartpim.db")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    db = get_db()
    db.execute("""CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        sku TEXT,
        price TEXT,
        description TEXT,
        synced INTEGER DEFAULT 0,
        wc_id INTEGER DEFAULT NULL
    )""")
    db.commit()


# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────

@app.route("/")
def index():
    db = get_db()
    products = db.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
    return render_template("index.html", products=products)


@app.route("/add", methods=["GET", "POST"])
def add_product():
    if request.method == "POST":
        name = request.form["name"].strip()
        sku = request.form["sku"].strip()
        price = request.form["price"].strip()
        description = (request.form.get("description") or "").strip()

        if not description:
            try:
                description = claude_product_description(name, price)
            except ValueError as e:
                flash(f"AI description skipped: {e}", "warning")
                description = ""
            except APIError as e:
                flash(f"Claude API error: {e.message}", "warning")
                description = ""

        db = get_db()
        db.execute(
            "INSERT INTO products (name, sku, price, description) VALUES (?,?,?,?)",
            [name, sku, price, description],
        )
        db.commit()
        flash(f"Product '{name}' added successfully!", "success")
        return redirect("/")
    return render_template("add.html")


@app.route("/generate-description", methods=["POST"])
def generate_description():
    if not request.is_json:
        return jsonify({"error": "Expected JSON body"}), 400
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    price = (data.get("price") or "").strip()
    if not name:
        return jsonify({"error": "Product name is required"}), 400
    try:
        description = claude_product_description(name, price or "0")
        return jsonify({"description": description})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except APIError as e:
        return jsonify({"error": e.message}), 502


@app.route("/sync/<int:id>")
def sync(id):
    db = get_db()
    p = db.execute("SELECT * FROM products WHERE id=?", [id]).fetchone()
    if not p:
        flash("Product not found.", "danger")
        return redirect("/")
    try:
        wc_product = wc_push_product(dict(p))
        wc_id = wc_product.get("id")
        db.execute("UPDATE products SET synced=1, wc_id=? WHERE id=?", [wc_id, id])
        db.commit()
        flash(f"'{p['name']}' synced to WooCommerce! (WC ID: {wc_id})", "success")
    except ValueError as e:
        flash(str(e), "warning")
    except requests.exceptions.HTTPError as e:
        flash(f"WooCommerce error: {e.response.text[:300]}", "danger")
    except Exception as e:
        flash(f"Sync failed: {e}", "danger")
    return redirect("/")


@app.route("/sync-all", methods=["POST"])
def sync_all():
    db = get_db()
    pending = db.execute("SELECT * FROM products WHERE synced=0").fetchall()
    success_count = 0
    errors = []
    for p in pending:
        try:
            wc_product = wc_push_product(dict(p))
            wc_id = wc_product.get("id")
            db.execute("UPDATE products SET synced=1, wc_id=? WHERE id=?", [wc_id, p["id"]])
            db.commit()
            success_count += 1
        except Exception as e:
            errors.append(f"{p['name']}: {e}")
    if success_count:
        flash(f"{success_count} product(s) synced to WooCommerce!", "success")
    if errors:
        flash(f"Errors: {'; '.join(errors)}", "danger")
    return redirect("/")


@app.route("/settings", methods=["GET", "POST"])
def settings():
    test_result = None
    if request.method == "POST":
        action = request.form.get("action")
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

        if action == "save":
            wc_url = request.form.get("wc_url", "").strip().rstrip("/")
            wc_key = request.form.get("wc_key", "").strip()
            wc_secret = request.form.get("wc_secret", "").strip()
            anthropic_key = request.form.get("anthropic_key", "").strip()

            if wc_url:
                os.environ["WC_URL"] = wc_url
                set_key(env_path, "WC_URL", wc_url)
            if wc_key:
                os.environ["WC_CONSUMER_KEY"] = wc_key
                set_key(env_path, "WC_CONSUMER_KEY", wc_key)
            if wc_secret:
                os.environ["WC_CONSUMER_SECRET"] = wc_secret
                set_key(env_path, "WC_CONSUMER_SECRET", wc_secret)
            if anthropic_key:
                os.environ["ANTHROPIC_API_KEY"] = anthropic_key
                set_key(env_path, "ANTHROPIC_API_KEY", anthropic_key)

            flash("Settings saved!", "success")

        elif action == "test_wc":
            ok, msg = wc_test_connection()
            test_result = {"ok": ok, "msg": msg}

        elif action == "test_ai":
            try:
                desc = claude_product_description("Test Wireless Earbuds", "1999")
                test_result = {"ok": True, "msg": f"Claude AI is working! Preview: {desc[:100]}..."}
            except Exception as e:
                test_result = {"ok": False, "msg": str(e)}

    creds = get_wc_creds()
    return render_template("settings.html",
        wc_url=creds["url"],
        wc_key=creds["key"],
        wc_secret=creds["secret"],
        anthropic_key=os.getenv("ANTHROPIC_API_KEY", ""),
        test_result=test_result,
        model=DEFAULT_CLAUDE_MODEL,
    )


@app.route("/delete/<int:id>", methods=["POST"])
def delete_product(id):
    db = get_db()
    db.execute("DELETE FROM products WHERE id=?", [id])
    db.commit()
    flash("Product deleted.", "success")
    return redirect("/")


if __name__ == "__main__":
    init_db()
    app.run(debug=True)