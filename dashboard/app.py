"""
JENT Dashboard Server
---------------------
Serves the local web dashboard at http://localhost:8765
Reads seen_jobs.json and log/cycle_stats.json — read-only, never modifies agent state.

Usage:
    python dashboard/app.py

Then open http://localhost:8765 in your browser.
"""
import json
import sys
import os
import hmac
import hashlib
import uuid
import calendar
from pathlib import Path
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, send_from_directory, request, redirect

BASE_DIR = Path(os.path.dirname(os.path.abspath(__file__))).parent
SEEN_JOBS_FILE = BASE_DIR / "seen_jobs.json"
CYCLE_STATS_FILE = BASE_DIR / "log" / "cycle_stats.json"
LOG_FILE = BASE_DIR / "log" / "agent.log"
APPLIED_JOBS_FILE = BASE_DIR / "applied_jobs.json"
SUBSCRIPTIONS_FILE = BASE_DIR / "subscriptions.json"
DASHBOARD_DIR = Path(os.path.dirname(os.path.abspath(__file__)))

ALLOWED_RESUME_EXTS = {".pdf", ".docx", ".doc"}

# ── Cashfree / Subscription config ───────────────────────────────────────────
def _load_yaml_cfg() -> dict:
    cfg_file = BASE_DIR / "config.yaml"
    if not cfg_file.exists():
        return {}
    try:
        import yaml
        with open(cfg_file, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}

_cfg = _load_yaml_cfg()

def _gcfg(key, default):
    return os.environ.get(key.upper(), _cfg.get(key.lower(), default))

CASHFREE_APP_ID     = _gcfg("cashfree_app_id", "")
CASHFREE_SECRET     = _gcfg("cashfree_secret_key", "")
CASHFREE_ENV        = _gcfg("cashfree_env", "test")   # "test" or "prod"
SUBSCRIPTION_AMT    = int(_gcfg("subscription_amount", 75))
SUBSCRIPTION_REQ    = str(_gcfg("subscription_required", "true")).lower() == "true"

# Cashfree base URLs
_CF_BASE = (
    "https://sandbox.cashfree.com/pg"
    if CASHFREE_ENV == "test"
    else "https://api.cashfree.com/pg"
)
_CF_JS = (
    "https://sdk.cashfree.com/js/v3/cashfree.js"
    if CASHFREE_ENV == "test"
    else "https://sdk.cashfree.com/js/v3/cashfree.js"
)

app = Flask(__name__, static_folder=str(DASHBOARD_DIR), static_url_path="")

import requests as _requests   # for Cashfree API calls

import sys
sys.path.insert(0, str(BASE_DIR))
import db


# -- Helpers ------------------------------------------------------------------

def load_seen() -> dict:
    return db.load_seen()


def load_cycle_stats() -> list:
    return db.load_cycle_stats()


def tail_log(n: int = 200) -> list:
    if not LOG_FILE.exists():
        return []
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [l.rstrip() for l in lines[-n:]]
    except Exception:
        return []


def find_resume():
    """Return info about the current resume file, or None."""
    for ext in ALLOWED_RESUME_EXTS:
        path = BASE_DIR / f"resume{ext}"
        if path.exists():
            stat = path.stat()
            return {
                "filename": path.name,
                "size_bytes": stat.st_size,
                "size_kb": round(stat.st_size / 1024, 1),
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                "path": str(path),
            }
    return None


# -- Subscription helpers ------------------------------------------------------

def load_subscriptions() -> dict:
    if not SUBSCRIPTIONS_FILE.exists():
        return {"subscribers": []}
    try:
        with open(SUBSCRIPTIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"subscribers": []}


def save_subscriptions(data: dict):
    with open(SUBSCRIPTIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_active_subscriber() -> dict | None:
    """Return the most recent active subscriber record, or None."""
    if not SUBSCRIPTION_REQ:
        return {"email": "dev-bypass", "status": "active", "plan": "dev"}
    data = load_subscriptions()
    now = datetime.now(timezone.utc)
    for sub in reversed(data.get("subscribers", [])):
        if sub.get("status") != "active":
            continue
        # Check monthly expiry: subscribed_at + 31 days
        try:
            sub_date = datetime.fromisoformat(sub["subscribed_at"])
            if now - sub_date <= timedelta(days=31):
                return sub
        except Exception:
            return sub  # if date parsing fails, treat as valid
    return None


def _cf_headers() -> dict:
    return {
        "x-client-id": CASHFREE_APP_ID,
        "x-client-secret": CASHFREE_SECRET,
        "x-api-version": "2023-08-01",
        "Content-Type": "application/json",
    }



# -- API Routes ---------------------------------------------------------------

@app.route("/api/jobs")
def api_jobs():
    seen = load_seen()
    jobs = []
    for jid, info in seen.items():
        if not isinstance(info, dict) or not info.get("title"):
            continue
        jobs.append({
            "id": jid,
            "title": info.get("title", ""),
            "company": info.get("company", ""),
            "url": info.get("url", ""),
            "source": info.get("source", ""),
            "location": info.get("location", ""),
            "score": info.get("score", 0),
            "found_at": info.get("found_at", ""),
        })
    jobs.sort(key=lambda j: (j["score"], j["found_at"]), reverse=True)

    min_score = float(request.args.get("min_score", 0))
    source_filter = request.args.get("source", "").strip().lower()
    if min_score > 0:
        jobs = [j for j in jobs if j["score"] >= min_score]
    if source_filter:
        jobs = [j for j in jobs if source_filter in j["source"].lower()]

    return jsonify({"jobs": jobs, "total": len(jobs)})


@app.route("/api/stats")
def api_stats():
    seen = load_seen()
    cycles = load_cycle_stats()

    total_seen = len(seen)
    total_matches = sum(
        1 for info in seen.values()
        if isinstance(info, dict) and info.get("score", 0) > 0 and info.get("title")
    )
    source_counts = {}
    for info in seen.values():
        if not isinstance(info, dict) or not info.get("source"):
            continue
        src = info["source"]
        source_counts[src] = source_counts.get(src, 0) + 1

    applied_count = 0
    if APPLIED_JOBS_FILE.exists():
        try:
            with open(APPLIED_JOBS_FILE, "r", encoding="utf-8") as f:
                applied_count = len(json.load(f))
        except Exception:
            pass

    return jsonify({
        "total_seen": total_seen,
        "total_matches": total_matches,
        "total_applied": applied_count,
        "last_cycle": cycles[-1] if cycles else None,
        "source_breakdown": source_counts,
        "recent_cycles": cycles[-20:] if cycles else [],
    })


@app.route("/api/log")
def api_log():
    n = int(request.args.get("n", 150))
    return jsonify({"lines": tail_log(n)})


@app.route("/api/health")
def api_health():
    return jsonify({
        "status": "ok",
        "dashboard_version": "4.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "seen_jobs_exists": SEEN_JOBS_FILE.exists(),
        "log_exists": LOG_FILE.exists(),
        "mongodb_connected": db.is_mongodb_connected(),
    })


# -- Subscription API Routes --------------------------------------------------

@app.route("/api/subscription-status")
def api_subscription_status():
    sub = get_active_subscriber()
    if sub:
        # Calculate days remaining
        days_left = None
        try:
            sub_date = datetime.fromisoformat(sub["subscribed_at"])
            expires = sub_date + timedelta(days=31)
            days_left = max(0, (expires - datetime.now(timezone.utc)).days)
        except Exception:
            days_left = 31
        return jsonify({
            "active": True,
            "email": sub.get("email", ""),
            "name": sub.get("name", ""),
            "subscribed_at": sub.get("subscribed_at", ""),
            "days_left": days_left,
            "amount": SUBSCRIPTION_AMT,
            "subscription_required": SUBSCRIPTION_REQ,
        })
    return jsonify({
        "active": False,
        "amount": SUBSCRIPTION_AMT,
        "subscription_required": SUBSCRIPTION_REQ,
    })


@app.route("/api/create-order", methods=["POST"])
def api_create_order():
    """Create a Cashfree order for ₹75 and return payment_session_id."""
    if not CASHFREE_APP_ID or not CASHFREE_SECRET:
        return jsonify({"error": "Cashfree credentials not configured in config.yaml"}), 500

    body = request.get_json(force=True) or {}
    customer_name  = (body.get("name") or "").strip() or "JENT User"
    customer_email = (body.get("email") or "").strip()
    customer_phone = (body.get("phone") or "").strip() or "9999999999"

    if not customer_email:
        return jsonify({"error": "Email is required"}), 400

    order_id = f"jent_{uuid.uuid4().hex[:12]}"
    return_url = request.host_url.rstrip("/") + f"/api/payment-success?order_id={order_id}&email={customer_email}"

    payload = {
        "order_id": order_id,
        "order_amount": SUBSCRIPTION_AMT,
        "order_currency": "INR",
        "order_note": "JENT Job Agent — Monthly Subscription",
        "customer_details": {
            "customer_id": f"jent_{customer_email.replace('@','_at_').replace('.','_')}",
            "customer_name": customer_name,
            "customer_email": customer_email,
            "customer_phone": customer_phone,
        },
        "order_meta": {
            "return_url": return_url,
            "notify_url": request.host_url.rstrip("/") + "/api/payment-webhook",
        },
    }

    try:
        resp = _requests.post(
            f"{_CF_BASE}/orders",
            json=payload,
            headers=_cf_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return jsonify({
            "order_id": order_id,
            "payment_session_id": data.get("payment_session_id", ""),
            "cf_js": _CF_JS,
            "env": CASHFREE_ENV,
        })
    except Exception as e:
        return jsonify({"error": f"Cashfree order creation failed: {e}"}), 500


@app.route("/api/payment-success")
def api_payment_success():
    """Cashfree redirects here after payment. Verify and activate subscription."""
    order_id = request.args.get("order_id", "")
    email    = request.args.get("email", "")

    activated = False
    if order_id and CASHFREE_APP_ID:
        try:
            resp = _requests.get(
                f"{_CF_BASE}/orders/{order_id}",
                headers=_cf_headers(),
                timeout=15,
            )
            resp.raise_for_status()
            order_data = resp.json()
            status = order_data.get("order_status", "")
            if status == "PAID":
                payments = order_data.get("order_payment_details", {})
                payment_id = str(payments.get("payment_id", order_id))
                _activate_subscription(
                    email=email,
                    name=order_data.get("customer_details", {}).get("customer_name", ""),
                    order_id=order_id,
                    payment_id=payment_id,
                    amount=SUBSCRIPTION_AMT,
                )
                activated = True
        except Exception:
            pass

    # Redirect back to dashboard with status param
    return redirect(f"/?sub={'success' if activated else 'pending'}")


@app.route("/api/verify-payment", methods=["POST"])
def api_verify_payment():
    """Frontend calls this to verify an order and activate subscription."""
    body = request.get_json(force=True) or {}
    order_id = body.get("order_id", "")
    email    = body.get("email", "")

    if not order_id:
        return jsonify({"error": "order_id required"}), 400

    try:
        resp = _requests.get(
            f"{_CF_BASE}/orders/{order_id}",
            headers=_cf_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        order_data = resp.json()
        status = order_data.get("order_status", "")

        if status == "PAID":
            payments = order_data.get("order_payment_details", {})
            payment_id = str(payments.get("payment_id", order_id))
            customer  = order_data.get("customer_details", {})
            _activate_subscription(
                email=email or customer.get("customer_email", ""),
                name=customer.get("customer_name", ""),
                order_id=order_id,
                payment_id=payment_id,
                amount=SUBSCRIPTION_AMT,
            )
            return jsonify({"success": True, "status": status})
        return jsonify({"success": False, "status": status})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/payment-webhook", methods=["POST"])
def api_payment_webhook():
    """Cashfree server-to-server payment notification."""
    try:
        data = request.get_json(force=True) or {}
        order = data.get("data", {}).get("order", {})
        payment = data.get("data", {}).get("payment", {})
        if payment.get("payment_status") == "SUCCESS":
            order_id   = order.get("order_id", "")
            payment_id = payment.get("cf_payment_id", order_id)
            customer   = data.get("data", {}).get("customer_details", {})
            _activate_subscription(
                email=customer.get("customer_email", ""),
                name=customer.get("customer_name", ""),
                order_id=order_id,
                payment_id=str(payment_id),
                amount=SUBSCRIPTION_AMT,
            )
    except Exception:
        pass
    return jsonify({"status": "ok"})


def _activate_subscription(email, name, order_id, payment_id, amount):
    """Write subscriber record to subscriptions.json."""
    subs = load_subscriptions()
    # Mark any previous records as expired
    for s in subs["subscribers"]:
        if s.get("email") == email and s.get("status") == "active":
            s["status"] = "expired"
    subs["subscribers"].append({
        "email": email,
        "name": name,
        "order_id": order_id,
        "payment_id": payment_id,
        "amount": amount,
        "subscribed_at": datetime.now(timezone.utc).isoformat(),
        "status": "active",
        "plan": "monthly",
    })
    save_subscriptions(subs)



@app.route("/api/resume-status")
def api_resume_status():
    info = find_resume()
    return jsonify({"resume": info, "has_resume": info is not None})


@app.route("/api/upload-resume", methods=["POST"])
def api_upload_resume():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    ext = Path(f.filename).suffix.lower()
    if ext not in ALLOWED_RESUME_EXTS:
        return jsonify({"error": f"Unsupported file type '{ext}'. Use PDF or DOCX."}), 400

    # Remove old resume files before saving new one
    for old_ext in ALLOWED_RESUME_EXTS:
        old = BASE_DIR / f"resume{old_ext}"
        if old.exists():
            try:
                old.unlink()
            except Exception:
                pass

    save_path = BASE_DIR / f"resume{ext}"
    f.save(str(save_path))
    stat = save_path.stat()

    return jsonify({
        "success": True,
        "filename": save_path.name,
        "size_kb": round(stat.st_size / 1024, 1),
        "message": f"Resume saved. It will be used in the next agent cycle.",
    })


@app.route("/api/applied-jobs")
def api_applied_jobs():
    try:
        data = db.load_applied()
        jobs = [{"id": jid, **info} for jid, info in data.items()]
        jobs.sort(key=lambda j: j.get("applied_at", ""), reverse=True)
        return jsonify({"jobs": jobs, "total": len(jobs)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/")
def index():
    return send_from_directory(str(DASHBOARD_DIR), "index.html")


# -- Main ---------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("DASHBOARD_PORT", 8765))
    print(f"\n  JENT Dashboard running at http://localhost:{port}")
    print(f"  Reading data from: {BASE_DIR}\n")
    app.run(host="127.0.0.1", port=port, debug=False)
