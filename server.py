# server.py
# The Journal - Deployment Server
# All Rights Reserved.

import io
import os
import re
import csv
import json
import math
import uuid
import threading
from datetime import datetime
from pathlib import Path
from functools import wraps

from flask import (
    Flask,
    jsonify,
    request,
    send_from_directory,
    send_file,
    make_response,
    abort,
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_user,
    logout_user,
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.exceptions import HTTPException
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

app = Flask(__name__, static_folder="static")
BASE = Path(__file__).parent
INSTANCE = BASE / "instance"
INSTANCE.mkdir(exist_ok=True)

db_url = os.getenv("DATABASE_URL")
if db_url and db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

if not db_url:
    sqlite_path = Path(os.getenv("SQLITE_PATH", "/tmp/journal.db"))
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    db_url = "sqlite:///" + str(sqlite_path)

secret_key = os.getenv("SECRET_KEY") or "dev-change-before-deploy"

app.config["SECRET_KEY"] = secret_key
app.secret_key = secret_key
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True

if db_url.startswith("sqlite"):
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "connect_args": {"check_same_thread": False}
    }

db = SQLAlchemy(app)
login_manager = LoginManager(app)

_db_init_lock = threading.Lock()
_db_initialized = False


@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        response = make_response()
        response.headers["Access-Control-Allow-Origin"] = request.headers.get("Origin", "*")
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With"
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Max-Age"] = "3600"
        return response, 200


@app.after_request
def add_cors(response):
    origin = request.headers.get("Origin", "*")
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-Requested-With"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Credentials"] = "true"
    return response


@app.errorhandler(Exception)
def handle_exception(e):
    if isinstance(e, HTTPException):
        return e
    app.logger.exception("Unhandled server error")
    db.session.rollback()
    return jsonify({"error": "Internal server error", "detail": str(e)}), 500


user_follows = db.Table(
    "user_follows",
    db.Column("follower_id", db.Integer, db.ForeignKey("user.id"), primary_key=True),
    db.Column("followed_id", db.Integer, db.ForeignKey("user.id"), primary_key=True),
)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    display_name = db.Column(db.String(120), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default="member", index=True)
    bio = db.Column(db.Text, default="")
    avatar_color = db.Column(db.String(7), default="#5ea8ff")
    reputation = db.Column(db.Float, default=1.0)
    is_banned = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)

    @property
    def initials(self):
        name = (self.display_name or "").strip()
        if not name:
            return "??"
        parts = name.split()
        return (parts[0][0] + parts[-1][0]).upper() if len(parts) >= 2 else name[:2].upper()

    def to_dict(self):
        try:
            paper_count = Submission.query.filter_by(author_id=self.id).count()
        except Exception:
            paper_count = 0
        try:
            review_count = Review.query.filter_by(reviewer_id=self.id).count()
        except Exception:
            review_count = 0
        try:
            follower_count = self.followers.count() if hasattr(self, "followers") else 0
        except Exception:
            follower_count = 0

        return {
            "id": self.id,
            "email": self.email,
            "display_name": self.display_name,
            "initials": self.initials,
            "bio": self.bio,
            "avatar_color": self.avatar_color,
            "reputation_score": round(self.reputation, 2),
            "paper_count": paper_count,
            "review_count": review_count,
            "follower_count": follower_count,
            "joined": self.created_at.isoformat() if self.created_at else None,
            "role": self.role,
            "banned": self.is_banned,
        }

    following = db.relationship(
        "User",
        secondary=user_follows,
        primaryjoin=(user_follows.c.follower_id == id),
        secondaryjoin=(user_follows.c.followed_id == id),
        backref=db.backref("followers", lazy="dynamic"),
        lazy="dynamic",
    )


@login_manager.user_loader
def load_user(uid):
    try:
        return db.session.get(User, int(uid))
    except Exception:
        return None


class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(80), unique=True)
    name = db.Column(db.String(120))
    emoji = db.Column(db.String(10), default="x")


class Submission(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    blind_id = db.Column(db.String(20), unique=True, index=True)
    title = db.Column(db.String(500))
    abstract = db.Column(db.Text)
    body_text = db.Column(db.Text)
    status = db.Column(db.String(30), default="submitted", index=True)
    tags = db.Column(db.Text, default="")
    author_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    category_id = db.Column(db.Integer, db.ForeignKey("category.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    published_at = db.Column(db.DateTime)

    author = db.relationship("User", backref="submissions")
    category = db.relationship("Category", backref="submissions")

    STATUS_LABELS = {
        "submitted": "Submitted",
        "desk_passed": "Desk Passed",
        "in_discovery": "In Discovery",
        "under_review": "Under Review",
        "published": "Published",
        "desk_returned": "Revision Suggested",
        "revision_requested": "Revision Requested",
        "declined": "Declined",
        "contested": "Contested",
        "desk_blocked": "Blocked",
    }

    STATUS_COLORS = {
        "submitted": "#6b7db3",
        "desk_passed": "#5ea8ff",
        "in_discovery": "#5ea8ff",
        "under_review": "#f0a030",
        "published": "#4ade80",
        "desk_returned": "#f0a030",
        "revision_requested": "#f0a030",
        "declined": "#ef4444",
        "contested": "#ef4444",
        "desk_blocked": "#ef4444",
    }

    def to_card(self, uid=None):
        lc = Like.query.filter_by(submission_id=self.id).count()
        cc = Comment.query.filter_by(submission_id=self.id).count()
        rc = Review.query.filter_by(submission_id=self.id).count()
        d = {
            "id": self.id,
            "blind_id": self.blind_id,
            "title": self.title,
            "abstract": (self.abstract or "")[:300],
            "body_text": self.body_text or "",
            "status": self.status,
            "status_label": self.STATUS_LABELS.get(self.status, self.status),
            "status_color": self.STATUS_COLORS.get(self.status, "#6b7db3"),
            "category": (
                {"id": self.category.id, "name": self.category.name, "emoji": self.category.emoji}
                if self.category
                else {}
            ),
            "author": self.author.to_dict() if self.author else {},
            "author_name": self.author.display_name if self.author else "Unknown",
            "tags": [t.strip() for t in (self.tags or "").split(",") if t.strip()],
            "like_count": lc,
            "comment_count": cc,
            "review_count": rc,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "published_at": self.published_at.isoformat() if self.published_at else None,
        }
        if uid:
            d["user_liked"] = Like.query.filter_by(user_id=uid, submission_id=self.id).first() is not None
            d["user_bookmarked"] = Bookmark.query.filter_by(user_id=uid, submission_id=self.id).first() is not None
        return d


class Like(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    submission_id = db.Column(db.Integer, db.ForeignKey("submission.id"))
    __table_args__ = (db.UniqueConstraint("user_id", "submission_id"),)


class Bookmark(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    submission_id = db.Column(db.Integer, db.ForeignKey("submission.id"))
    __table_args__ = (db.UniqueConstraint("user_id", "submission_id"),)


class Comment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    submission_id = db.Column(db.Integer, db.ForeignKey("submission.id"))
    author_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    comment_type = db.Column(db.String(30), default="note")
    body = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    author = db.relationship("User")


class Review(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    submission_id = db.Column(db.Integer, db.ForeignKey("submission.id"))
    reviewer_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    signal = db.Column(db.String(40))
    rationale = db.Column(db.Text)
    clarity_score = db.Column(db.Integer)
    rigor_score = db.Column(db.Integer)
    novelty_score = db.Column(db.Integer)
    literature_score = db.Column(db.Integer)
    fatal_flaw = db.Column(db.Boolean, default=False)
    weight = db.Column(db.Float, default=1.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    title = db.Column(db.String(255))
    body = db.Column(db.Text)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class DeskDecision(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    submission_id = db.Column(db.Integer, db.ForeignKey("submission.id"))
    decision = db.Column(db.String(20))
    overall_score = db.Column(db.Float)
    summary = db.Column(db.Text)
    encouragement = db.Column(db.Text)
    scores_json = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


def run_desk_review(title, abstract, body):
    lower = (title + " " + abstract + " " + body).lower()
    wc = len((body or "").split())
    terms = [
        "energy", "force", "momentum", "field", "wave", "quantum", "gravity", "entropy",
        "experiment", "friction", "chaos", "nonlinear", "measurement", "hypothesis",
    ]
    pc = sum(1 for t in terms if t in lower)
    claims = ["we show", "we find", "i find", "this paper", "result shows", "i measured", "hypothesis"]
    cc = sum(1 for p in claims if p in lower)
    sections = ["introduction", "method", "results", "discussion", "conclusion", "references"]
    sf = sum(1 for s in sections if s in lower)
    spam = ["buy now", "click here", "guaranteed", "act now"]
    is_spam = any(s in lower for s in spam)

    scores = {
        "scope": min(5, pc),
        "claim": min(5, cc + 1),
        "structure": min(5, sf + 1),
        "clarity": 4 if wc > 300 else 3,
        "quantitative": 3 if re.search(r"\d.*=", body or "") else 1,
        "citations": 3 if re.search(r"\[\d+\]", body or "") else 1,
        "anonymity": 5,
        "good_faith": 0 if is_spam else (5 if wc >= 200 else 3),
    }
    overall = round(sum(scores.values()) / 40 * 100)
    rec = "block" if is_spam else ("pass" if overall >= 60 and scores["good_faith"] >= 3 else "return")
    return {
        "overall_score": overall,
        "recommendation": rec,
        "scores": scores,
        "summary": (
            "Ready for community review."
            if rec == "pass"
            else "Needs more development."
            if rec == "return"
            else "Not accepted."
        ),
        "encouragement": "Your curiosity is valued here." if rec != "block" else "We welcome genuine submissions.",
    }


def seed():
    cats = [
        ("foundations", "Foundations of Physics", "🌌"),
        ("math-physics", "Mathematical Physics", "📐"),
        ("nonlinear", "Nonlinear Dynamics", "🌀"),
        ("stat-mech", "Statistical Mechanics", "⚛️"),
        ("complex", "Complex Systems", "🕸️"),
        ("experimental", "Experimental & Observational", "🔬"),
    ]
    for slug, name, emoji in cats:
        if not Category.query.filter_by(slug=slug).first():
            db.session.add(Category(slug=slug, name=name, emoji=emoji))

    admin_email = os.getenv("ADMIN_EMAIL", "admin@journal.local")
    admin_password = os.getenv("ADMIN_PASSWORD", "change-me-now")

    if not User.query.filter_by(email=admin_email).first():
        u = User(email=admin_email, display_name="Founding Editor", role="admin")
        u.set_password(admin_password)
        db.session.add(u)

    db.session.commit()


def init_db():
    global _db_initialized
    if _db_initialized:
        return
    with _db_init_lock:
        if _db_initialized:
            return
        with app.app_context():
            db.create_all()
            seed()
        _db_initialized = True


@app.before_request
def initialize_database():
    path = request.path or ""
    if path in ("/", "/healthz", "/manifest.json", "/sw.js"):
        return
    if path.startswith("/static/"):
        return
    if path.startswith("/favicon"):
        return
    try:
        init_db()
    except Exception as e:
        app.logger.exception("Database initialization failed")
        return jsonify({"error": "Database initialization failed", "detail": str(e)}), 500


def api_login_required(f):
    @wraps(f)
    def d(*a, **k):
        if not current_user.is_authenticated:
            return jsonify({"error": "Auth required"}), 401
        if getattr(current_user, "is_banned", False):
            logout_user()
            return jsonify({"error": "Account banned"}), 403
        return f(*a, **k)
    return d


def admin_required(f):
    @wraps(f)
    def d(*a, **k):
        if not current_user.is_authenticated:
            return jsonify({"error": "Auth required"}), 401
        if current_user.role != "admin":
            return jsonify({"error": "Admin required"}), 403
        return f(*a, **k)
    return d


def create_notification(user_id, title, body=""):
    try:
        db.session.add(Notification(user_id=user_id, title=title, body=body))
        db.session.flush()
    except Exception:
        pass


def get_submission_by_identifier(identifier):
    s = None
    if str(identifier).isdigit():
        s = db.session.get(Submission, int(identifier))
    if not s:
        s = Submission.query.filter_by(blind_id=str(identifier)).first()
    if not s:
        abort(404)
    return s


def parse_numeric_series(text):
    if not text:
        return []

    rows = []
    try:
        dialect = csv.Sniffer().sniff(text[:2048], delimiters=",\t;")
        reader = csv.reader(io.StringIO(text), dialect)
        rows = list(reader)
    except Exception:
        rows = []

    values = []
    if rows:
        for row in rows:
            for cell in row:
                cell = (cell or "").strip()
                if not cell:
                    continue
                try:
                    values.append(float(cell))
                except Exception:
                    pass
        if values:
            return values

    matches = re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", text)
    for m in matches:
        try:
            values.append(float(m))
        except Exception:
            pass
    return values


def ocm_analysis(data):
    n = len(data)
    wz = 200
    wb = 500
    eps = 1e-8
    if n < wb + 10:
        return {"error": "Need at least 600 data points."}

    z = [0.0] * n
    for t in range(wz, n):
        w = data[t - wz:t]
        mu = sum(w) / wz
        var = sum((x - mu) ** 2 for x in w) / wz
        z[t] = (data[t] - mu) / (math.sqrt(var) + eps)

    b2 = [0.0] * n
    for t in range(wb, n):
        w = z[t - wb:t]
        b2[t] = sum(w) / wb

    s = max(wz, wb)
    diffs = [abs(z[i] - b2[i]) for i in range(n)]
    ind = [1 if diffs[t + 1] < diffs[t] else 0 for t in range(s, n - 1)]
    cr = sum(ind) / max(1, len(ind))
    z_stat = (cr - 0.5) / math.sqrt(0.25 / max(1, len(ind)))

    if cr > 0.50522:
        cls = "DRIVEN-DISSIPATIVE"
    elif cr < 0.495:
        cls = "AUTONOMOUS CHAOTIC"
    else:
        cls = "STOCHASTIC"

    return {
        "summary": f"Contraction rate analysis complete. Classified as {cls}.",
        "classification": cls,
        "details": {
            "Contraction Rate": round(cr, 6),
            "Z-statistic": round(z_stat, 2),
            "Threshold": 0.50522,
            "Window Z": wz,
            "Window B": wb,
        },
    }


def er_analysis(data):
    n = len(data)
    wz = 100
    stride = 50
    threshold = 0.5
    eps = 1e-8
    if n < wz * 3:
        return {"error": "Need at least 300 data points."}

    nodes = list(range(wz, n - wz, stride))
    segs = []
    for t in nodes:
        s = data[t - wz // 2:t + wz // 2]
        mu = sum(s) / len(s)
        var = sum((x - mu) ** 2 for x in s) / len(s)
        std = math.sqrt(var) + eps
        segs.append([(x - mu) / std for x in s])

    edges = 0
    bridges = 0
    max_gap = 0
    for i in range(len(segs)):
        for j in range(i + 1, len(segs)):
            m = min(len(segs[i]), len(segs[j]))
            dot = sum(segs[i][k] * segs[j][k] for k in range(m))
            corr = abs(dot / m)
            if corr >= threshold:
                edges += 1
                gap = abs(nodes[j] - nodes[i])
                if gap > 500:
                    bridges += 1
                    if gap > max_gap:
                        max_gap = gap

    density = edges / max(1, (len(nodes) * (len(nodes) - 1) / 2))
    if len(nodes) < 4:
        topo = "TRIVIAL"
    elif bridges > 0:
        topo = "BRIDGED"
    elif edges > len(nodes):
        topo = "CONNECTED"
    else:
        topo = "FRAGMENTED"

    return {
        "summary": f"Topology mapping complete. Structure classified as {topo}.",
        "classification": topo,
        "details": {
            "Nodes": len(nodes),
            "Edges": edges,
            "Bridges": bridges,
            "Density": round(density, 4),
            "Max Bridge Gap": max_gap,
        },
    }


def icm_analysis(data):
    n = len(data)
    wz = 200
    eps = 1e-8
    if n < wz * 3:
        return {"error": "Need at least 600 data points."}

    z = [0.0] * n
    for t in range(wz, n):
        w = data[t - wz:t]
        mu = sum(w) / wz
        var = sum((x - mu) ** 2 for x in w) / wz
        z[t] = (data[t] - mu) / (math.sqrt(var) + eps)

    events = []
    in_ev = False
    onset = peak = 0
    p_val = 0.0

    for t in range(wz, n):
        az = abs(z[t])
        if not in_ev and az > 2:
            in_ev = True
            onset = peak = t
            p_val = az
        elif in_ev:
            if az > p_val:
                peak = t
                p_val = az
            if az < 1.4:
                if t - onset >= 10:
                    rec_time = t - peak
                    asym = (peak - onset) / max(1, rec_time)
                    events.append({"amp": p_val, "recTime": rec_time, "asym": asym})
                in_ev = False

    if not events:
        return {
            "summary": "No admissible disruption events detected.",
            "classification": "NO_EVENTS",
            "details": {"events": 0, "admissible": False},
        }

    amps = [e["amp"] for e in events]
    recs = [e["recTime"] for e in events]
    asyms = [e["asym"] for e in events]

    mean_amp = sum(amps) / len(amps)
    mean_rec = sum(recs) / len(recs)
    mean_asym = sum(asyms) / len(asyms)

    if mean_asym > 3:
        morph = "SHARP_ONSET"
    elif mean_asym < 0.3:
        morph = "GRADUAL_ONSET"
    else:
        morph = "SYMMETRIC"

    admissible = len(events) > 0 and mean_rec < (n / 4)

    return {
        "summary": f"Invariant detection complete. Morphology: {morph}.",
        "classification": "ADMISSIBLE" if admissible else "INADMISSIBLE",
        "details": {
            "Events": len(events),
            "Mean Amp": round(mean_amp, 3),
            "Mean Recovery": round(mean_rec, 1),
            "Mean Asymmetry": round(mean_asym, 3),
            "Morphology": morph,
        },
    }


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True}), 200


@app.route("/")
def index():
    return send_file(BASE / "index.html")


@app.route("/manifest.json")
def manifest():
    return send_file(BASE / "manifest.json")


@app.route("/sw.js")
def service_worker():
    return send_file(BASE / "sw.js")


@app.route("/static/<path:p>")
def static_files(p):
    return send_from_directory(BASE / "static", p)


@app.route("/api/auth/register", methods=["POST"])
def register():
    d = request.get_json(silent=True) or {}
    email = (d.get("email") or "").strip().lower()
    name = (d.get("display_name") or "").strip()
    pw = d.get("password", "")

    if not email or not name or len(pw) < 6:
        return jsonify({"error": "All fields required, password 6+ chars"}), 400

    try:
        u = User(email=email, display_name=name)
        u.set_password(pw)
        db.session.add(u)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "Email taken"}), 409
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Register failed")
        return jsonify({"error": "Register failed", "detail": str(e)}), 500

    try:
        login_user(u, remember=True)
    except Exception as e:
        app.logger.exception("Post-register login failed")
        return jsonify({
            "error": "Account created but session login failed",
            "detail": str(e),
            "user": u.to_dict(),
        }), 200

    return jsonify({"user": u.to_dict()}), 201


@app.route("/api/auth/login", methods=["POST"])
def login():
    try:
        d = request.get_json(silent=True) or {}
        email = (d.get("email") or "").strip().lower()
        pw = d.get("password", "")

        if not email or not pw:
            return jsonify({"error": "Email and password required"}), 400

        u = User.query.filter_by(email=email).first()
        if not u or not u.check_password(pw):
            return jsonify({"error": "Invalid credentials"}), 401
        if u.is_banned:
            return jsonify({"error": "Account banned"}), 403

        login_user(u, remember=True)
        return jsonify({"user": u.to_dict()})
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Login failed")
        return jsonify({"error": "Login failed", "detail": str(e)}), 500


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    logout_user()
    return jsonify({"ok": True})


@app.route("/api/auth/me")
@api_login_required
def me():
    return jsonify({"user": current_user.to_dict()})


@app.route("/api/auth/profile", methods=["POST"])
@api_login_required
def update_profile():
    d = request.get_json(silent=True) or {}
    display_name = (d.get("display_name") or "").strip()
    bio = (d.get("bio") or "").strip()
    avatar_color = (d.get("avatar_color") or "").strip()

    if display_name:
        current_user.display_name = display_name
    current_user.bio = bio
    if re.fullmatch(r"#[0-9a-fA-F]{6}", avatar_color or ""):
        current_user.avatar_color = avatar_color

    db.session.commit()
    return jsonify({"user": current_user.to_dict()})


@app.route("/api/categories")
def categories():
    return jsonify({
        "categories": [
            {"id": c.id, "name": c.name, "emoji": c.emoji, "slug": c.slug}
            for c in Category.query.order_by(Category.id.asc()).all()
        ]
    })


@app.route("/api/feed/discovery")
def discovery():
    page = request.args.get("page", 1, type=int)
    q = Submission.query.filter(
        Submission.status.in_(["in_discovery", "under_review", "revision_requested", "revised", "contested"])
    )
    cat = request.args.get("category_id", type=int)
    if cat:
        q = q.filter_by(category_id=cat)
    search = request.args.get("q", "").strip()
    if search:
        q = q.filter(or_(Submission.title.ilike("%" + search + "%"), Submission.abstract.ilike("%" + search + "%")))
    p = q.order_by(Submission.updated_at.desc()).paginate(page=page, per_page=20, error_out=False)
    uid = current_user.id if current_user.is_authenticated else None
    return jsonify({"papers": [s.to_card(uid) for s in p.items], "total": p.total, "page": p.page})


@app.route("/api/feed/published")
def published():
    p = Submission.query.filter_by(status="published").order_by(Submission.published_at.desc(), Submission.updated_at.desc()).paginate(
        page=1, per_page=20, error_out=False
    )
    uid = current_user.id if current_user.is_authenticated else None
    return jsonify({"papers": [s.to_card(uid) for s in p.items]})


@app.route("/api/submissions", methods=["POST"])
@api_login_required
def create_submission():
    d = request.get_json(silent=True)
    if not d:
        d = request.form.to_dict() if request.form else {}

    sub = Submission(
        blind_id=uuid.uuid4().hex[:12].upper(),
        title=d.get("title", ""),
        abstract=d.get("abstract", ""),
        body_text=d.get("body_text", d.get("body", "")),
        tags=d.get("tags", ""),
        author_id=current_user.id,
        category_id=d.get("category_id", 1),
    )

    desk = run_desk_review(sub.title or "", sub.abstract or "", sub.body_text or "")
    sub.status = {"pass": "in_discovery", "return": "desk_returned", "block": "desk_blocked"}[desk["recommendation"]]

    db.session.add(sub)
    db.session.flush()
    db.session.add(
        DeskDecision(
            submission_id=sub.id,
            decision=desk["recommendation"],
            overall_score=desk["overall_score"],
            summary=desk["summary"],
            encouragement=desk["encouragement"],
            scores_json=json.dumps(desk["scores"]),
        )
    )
    db.session.commit()

    return jsonify({"submission": sub.to_card(current_user.id), "desk_review": desk}), 201


@app.route("/api/submissions/<identifier>")
def get_submission(identifier):
    s = get_submission_by_identifier(identifier)
    d = s.to_card(current_user.id if current_user.is_authenticated else None)
    d["body_text"] = s.body_text
    d["comments"] = [
        {
            "id": c.id,
            "author": c.author.to_dict(),
            "author_name": c.author.display_name if c.author else "Unknown",
            "comment_type": c.comment_type,
            "body": c.body,
            "content": c.body,
            "created_at": c.created_at.isoformat(),
        }
        for c in Comment.query.filter_by(submission_id=s.id).order_by(Comment.created_at).all()
    ]
    return jsonify({"submission": d})


@app.route("/api/submissions/<identifier>/like", methods=["POST"])
@api_login_required
def toggle_like(identifier):
    s = get_submission_by_identifier(identifier)
    ex = Like.query.filter_by(user_id=current_user.id, submission_id=s.id).first()
    if ex:
        db.session.delete(ex)
        db.session.commit()
        return jsonify({"liked": False, "likes": Like.query.filter_by(submission_id=s.id).count()})
    db.session.add(Like(user_id=current_user.id, submission_id=s.id))
    db.session.commit()
    return jsonify({"liked": True, "likes": Like.query.filter_by(submission_id=s.id).count()})


@app.route("/api/submissions/<identifier>/bookmark", methods=["POST"])
@api_login_required
def toggle_bookmark(identifier):
    s = get_submission_by_identifier(identifier)
    ex = Bookmark.query.filter_by(user_id=current_user.id, submission_id=s.id).first()
    if ex:
        db.session.delete(ex)
        db.session.commit()
        return jsonify({"bookmarked": False})
    db.session.add(Bookmark(user_id=current_user.id, submission_id=s.id))
    db.session.commit()
    return jsonify({"bookmarked": True})


@app.route("/api/submissions/<identifier>/comments", methods=["POST"])
@api_login_required
def add_comment(identifier):
    s = get_submission_by_identifier(identifier)
    d = request.get_json(silent=True) or {}
    body = d.get("body", d.get("content", ""))
    c = Comment(
        submission_id=s.id,
        author_id=current_user.id,
        comment_type=d.get("comment_type", "note"),
        body=body,
    )
    db.session.add(c)

    if s.author_id and s.author_id != current_user.id:
        create_notification(
            s.author_id,
            f"{current_user.display_name} commented on your paper",
            body[:140],
        )

    db.session.commit()
    return jsonify({
        "comment": {
            "id": c.id,
            "author": current_user.to_dict(),
            "author_name": current_user.display_name,
            "comment_type": c.comment_type,
            "body": c.body,
            "content": c.body,
            "created_at": c.created_at.isoformat(),
        }
    }), 201


@app.route("/api/submissions/<identifier>/comment", methods=["POST"])
@api_login_required
def add_comment_alias(identifier):
    return add_comment(identifier)


@app.route("/api/users/<int:uid>")
def get_user(uid):
    u = db.session.get(User, uid)
    if not u:
        abort(404)
    d = u.to_dict()
    d["papers"] = [
        s.to_card(current_user.id if current_user.is_authenticated else None)
        for s in Submission.query.filter_by(author_id=uid).filter(
            Submission.status.in_(["in_discovery", "under_review", "published"])
        ).order_by(Submission.updated_at.desc()).limit(20).all()
    ]
    if current_user.is_authenticated:
        d["is_following"] = current_user.following.filter_by(id=uid).first() is not None
    return jsonify({"user": d})


@app.route("/api/users/<int:uid>/follow", methods=["POST"])
@api_login_required
def toggle_follow(uid):
    if current_user.id == uid:
        return jsonify({"error": "Cannot follow yourself"}), 400

    target = db.session.get(User, uid)
    if not target:
        abort(404)

    existing = current_user.following.filter_by(id=uid).first()
    if existing:
        current_user.following.remove(target)
        db.session.commit()
        return jsonify({"following": False})

    current_user.following.append(target)
    if target.id != current_user.id:
        create_notification(target.id, f"{current_user.display_name} followed you", "")
    db.session.commit()
    return jsonify({"following": True})


@app.route("/api/notifications")
@api_login_required
def notifications():
    ns = Notification.query.filter_by(user_id=current_user.id).order_by(Notification.created_at.desc()).limit(50).all()
    return jsonify({
        "notifications": [
            {
                "id": n.id,
                "title": n.title,
                "body": n.body,
                "message": n.body or n.title,
                "content": n.body or n.title,
                "is_read": n.is_read,
                "created_at": n.created_at.isoformat(),
            }
            for n in ns
        ],
        "unread_count": Notification.query.filter_by(user_id=current_user.id, is_read=False).count(),
    })


@app.route("/api/notifications/count")
@api_login_required
def notifications_count():
    count = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
    return jsonify({"count": count})


@app.route("/api/stats")
def stats():
    return jsonify({
        "published_count": Submission.query.filter_by(status="published").count(),
        "discovery_count": Submission.query.filter(Submission.status.in_(["in_discovery", "under_review"])).count(),
        "user_count": User.query.count(),
    })


@app.route("/api/tools/run", methods=["POST"])
@api_login_required
def run_tool():
    tool = (request.form.get("tool") or request.json.get("tool") if request.is_json else request.form.get("tool") or "").strip()
    input_text = ""

    if request.is_json:
        d = request.get_json(silent=True) or {}
        tool = (d.get("tool") or tool).strip()
        input_text = d.get("input", "") or ""
    else:
        input_text = request.form.get("input", "") or ""

    if "file" in request.files and request.files["file"]:
        try:
            uploaded = request.files["file"].read().decode("utf-8", "ignore")
            if uploaded:
                input_text = uploaded
        except Exception:
            pass

    if tool == "desk_review":
        r = run_desk_review(input_text[:120], "", input_text)
        return jsonify({
            "summary": r["summary"],
            "classification": r["recommendation"].upper(),
            "details": r,
        })

    if tool == "ocm":
        return jsonify(ocm_analysis(parse_numeric_series(input_text)))

    if tool == "er":
        return jsonify(er_analysis(parse_numeric_series(input_text)))

    if tool == "icm":
        return jsonify(icm_analysis(parse_numeric_series(input_text)))

    if tool == "clm":
        data = parse_numeric_series(input_text)
        return jsonify({
            "summary": "Coherence Field Lab ready. Use the client-side visualization for the live field graphics.",
            "classification": "READY",
            "details": {
                "points_received": len(data),
                "message": "Backend accepted the forcing signal.",
            },
        })

    return jsonify({"error": "Unknown tool"}), 400


def _admin_submission_payload(s):
    d = s.to_card(current_user.id if current_user.is_authenticated else None)
    d["body_text"] = s.body_text or ""
    d["author_name"] = s.author.display_name if s.author else "Unknown"
    return d


def _set_submission_status(submission, raw_status):
    status = (raw_status or "").strip().lower()

    aliases = {
        "review": "in_discovery",
        "discover": "in_discovery",
        "discovery": "in_discovery",
        "in_discovery": "in_discovery",
        "publish": "published",
        "published": "published",
        "under_review": "under_review",
        "return": "desk_returned",
        "returned": "desk_returned",
        "desk_returned": "desk_returned",
        "decline": "declined",
        "declined": "declined",
    }

    normalized = aliases.get(status, status)
    valid = {"in_discovery", "published", "under_review", "desk_returned", "declined"}
    if normalized not in valid:
        raise ValueError("Invalid status")

    submission.status = normalized
    if normalized == "published":
        submission.published_at = datetime.utcnow()
    else:
        if submission.status != "published":
            submission.published_at = submission.published_at

    if submission.author_id:
        if normalized == "in_discovery":
            create_notification(submission.author_id, "Your paper moved into Discovery", submission.title or "")
        elif normalized == "published":
            create_notification(submission.author_id, "Your paper was published", submission.title or "")
        elif normalized == "desk_returned":
            create_notification(submission.author_id, "Revision suggested", submission.title or "")
        elif normalized == "declined":
            create_notification(submission.author_id, "Your paper was declined", submission.title or "")
        elif normalized == "under_review":
            create_notification(submission.author_id, "Your paper is under review", submission.title or "")


@app.route("/api/admin/submissions")
@api_login_required
@admin_required
def admin_submissions():
    items = Submission.query.order_by(Submission.updated_at.desc(), Submission.created_at.desc()).all()
    return jsonify({"submissions": [_admin_submission_payload(s) for s in items]})


@app.route("/api/admin/submissions/<int:sid>/status", methods=["POST"])
@api_login_required
@admin_required
def admin_submission_status(sid):
    s = db.session.get(Submission, sid)
    if not s:
        abort(404)

    d = request.get_json(silent=True) or {}
    status = d.get("status", "")
    try:
        _set_submission_status(s, status)
        db.session.commit()
        return jsonify({"ok": True, "submission": _admin_submission_payload(s)})
    except ValueError as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400


@app.route("/api/admin/submissions/<int:sid>/review", methods=["POST"])
@api_login_required
@admin_required
def admin_submission_review(sid):
    s = db.session.get(Submission, sid)
    if not s:
        abort(404)
    _set_submission_status(s, "in_discovery")
    db.session.commit()
    return jsonify({"ok": True, "submission": _admin_submission_payload(s)})


@app.route("/api/admin/submissions/<int:sid>/publish", methods=["POST"])
@api_login_required
@admin_required
def admin_submission_publish(sid):
    s = db.session.get(Submission, sid)
    if not s:
        abort(404)
    _set_submission_status(s, "published")
    db.session.commit()
    return jsonify({"ok": True, "submission": _admin_submission_payload(s)})


@app.route("/api/admin/submissions/<int:sid>/return", methods=["POST"])
@api_login_required
@admin_required
def admin_submission_return(sid):
    s = db.session.get(Submission, sid)
    if not s:
        abort(404)
    _set_submission_status(s, "desk_returned")
    db.session.commit()
    return jsonify({"ok": True, "submission": _admin_submission_payload(s)})


@app.route("/api/admin/submissions/<int:sid>/decline", methods=["POST"])
@api_login_required
@admin_required
def admin_submission_decline(sid):
    s = db.session.get(Submission, sid)
    if not s:
        abort(404)
    _set_submission_status(s, "declined")
    db.session.commit()
    return jsonify({"ok": True, "submission": _admin_submission_payload(s)})


@app.route("/api/admin/submissions/<int:sid>", methods=["DELETE"])
@api_login_required
@admin_required
def admin_delete_submission(sid):
    s = db.session.get(Submission, sid)
    if not s:
        abort(404)

    DeskDecision.query.filter_by(submission_id=s.id).delete()
    Review.query.filter_by(submission_id=s.id).delete()
    Comment.query.filter_by(submission_id=s.id).delete()
    Like.query.filter_by(submission_id=s.id).delete()
    Bookmark.query.filter_by(submission_id=s.id).delete()
    db.session.delete(s)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/users")
@api_login_required
@admin_required
def admin_users():
    items = User.query.order_by(User.created_at.desc()).all()
    return jsonify({"users": [u.to_dict() for u in items]})


@app.route("/api/admin/users/<int:uid>/role", methods=["POST"])
@api_login_required
@admin_required
def admin_user_role(uid):
    u = db.session.get(User, uid)
    if not u:
        abort(404)

    d = request.get_json(silent=True) or {}
    role = (d.get("role") or "").strip()
    if role not in {"member", "admin"}:
        return jsonify({"error": "Invalid role"}), 400
    u.role = role
    db.session.commit()
    return jsonify({"ok": True, "user": u.to_dict()})


@app.route("/api/admin/users/<int:uid>/ban", methods=["POST"])
@api_login_required
@admin_required
def admin_user_ban(uid):
    u = db.session.get(User, uid)
    if not u:
        abort(404)
    if u.id == current_user.id:
        return jsonify({"error": "Cannot ban yourself"}), 400
    u.is_banned = True
    db.session.commit()
    return jsonify({"ok": True, "user": u.to_dict()})


@app.route("/api/admin/users/<int:uid>/unban", methods=["POST"])
@api_login_required
@admin_required
def admin_user_unban(uid):
    u = db.session.get(User, uid)
    if not u:
        abort(404)
    u.is_banned = False
    db.session.commit()
    return jsonify({"ok": True, "user": u.to_dict()})


@app.route("/api/admin/users/<int:uid>", methods=["DELETE"])
@api_login_required
@admin_required
def admin_delete_user(uid):
    u = db.session.get(User, uid)
    if not u:
        abort(404)
    if u.id == current_user.id:
        return jsonify({"error": "Cannot delete yourself"}), 400
    if u.role == "admin":
        return jsonify({"error": "Cannot delete another admin from this endpoint"}), 400

    DeskDecision.query.filter(DeskDecision.submission_id.in_(
        [s.id for s in Submission.query.filter_by(author_id=u.id).all()]
    )).delete(synchronize_session=False)
    Review.query.filter_by(reviewer_id=u.id).delete()
    Comment.query.filter_by(author_id=u.id).delete()
    Notification.query.filter_by(user_id=u.id).delete()
    Like.query.filter_by(user_id=u.id).delete()
    Bookmark.query.filter_by(user_id=u.id).delete()
    Submission.query.filter_by(author_id=u.id).delete()
    db.session.execute(
        user_follows.delete().where(
            (user_follows.c.follower_id == u.id) | (user_follows.c.followed_id == u.id)
        )
    )
    db.session.delete(u)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/reset-db", methods=["POST"])
@api_login_required
@admin_required
def admin_reset_db():
    admin_ids = [u.id for u in User.query.filter_by(role="admin").all()]

    DeskDecision.query.delete()
    Review.query.delete()
    Comment.query.delete()
    Like.query.delete()
    Bookmark.query.delete()
    Notification.query.delete()
    Submission.query.delete()
    db.session.execute(user_follows.delete())

    if admin_ids:
        User.query.filter(~User.id.in_(admin_ids)).delete(synchronize_session=False)

    db.session.commit()
    seed()
    return jsonify({"ok": True})


if __name__ == "__main__":
    init_db()
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.getenv("FLASK_DEBUG", "0") == "1")
