# server.py
# The Journal - Deployment Server
# All Rights Reserved.

import os
import io
import csv
import json
import math
import re
import uuid
import threading
from datetime import datetime
from pathlib import Path
from functools import wraps

from flask import Flask, jsonify, request, send_from_directory, send_file, make_response
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, current_user, login_user, logout_user
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

secret = os.getenv("SECRET_KEY") or "dev-change-before-deploy"
app.config["SECRET_KEY"] = secret
app.secret_key = secret
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "1") != "0"
app.config["REMEMBER_COOKIE_SAMESITE"] = "Lax"
app.config["REMEMBER_COOKIE_SECURE"] = app.config["SESSION_COOKIE_SECURE"]

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
    role = db.Column(db.String(20), default="member")
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
        p = self.display_name.strip().split()
        return (p[0][0] + p[-1][0]).upper() if len(p) >= 2 else self.display_name[:2].upper()

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
            "is_banned": self.is_banned,
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
    return db.session.get(User, int(uid))


class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(80), unique=True)
    name = db.Column(db.String(120))
    emoji = db.Column(db.String(10), default="x")


class Submission(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    blind_id = db.Column(db.String(20), unique=True)
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
        "desk_passed": "Under Review",
        "in_discovery": "In Discovery",
        "under_review": "Under Review",
        "published": "Published",
        "desk_returned": "Revision Suggested",
        "revision_requested": "Revision Requested",
        "declined": "Declined",
        "contested": "Contested",
        "desk_blocked": "Blocked",
        "revised": "Revised",
    }

    STATUS_COLORS = {
        "submitted": "#6b7db3",
        "in_discovery": "#5ea8ff",
        "under_review": "#f0a030",
        "published": "#4ade80",
        "desk_returned": "#f0a030",
        "revision_requested": "#f0a030",
        "declined": "#ef4444",
        "contested": "#ef4444",
        "desk_blocked": "#ef4444",
        "revised": "#5ea8ff",
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
            "status": self.status,
            "status_label": self.STATUS_LABELS.get(self.status, self.status),
            "status_color": self.STATUS_COLORS.get(self.status, "#6b7db3"),
            "category": {"id": self.category.id, "name": self.category.name, "emoji": self.category.emoji} if self.category else {},
            "author": self.author.to_dict() if self.author else {},
            "author_name": self.author.display_name if self.author else None,
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


def create_notification(user_id, title, body=""):
    if not user_id:
        return
    try:
        db.session.add(Notification(user_id=user_id, title=title[:255], body=body or ""))
    except Exception:
        pass


def run_desk_review(title, abstract, body):
    lower = (title + " " + abstract + " " + body).lower()
    wc = len(body.split())
    terms = [
        "energy", "force", "momentum", "field", "wave", "quantum", "gravity", "entropy",
        "experiment", "friction", "chaos", "nonlinear", "measurement", "hypothesis",
        "spacetime", "curvature", "symmetry", "oscillation", "dynamics", "dynamical",
        "operator", "model", "invariant", "selection", "persistence", "forcing",
        "dissipative", "contraction", "stability", "equilibrium", "perturbation",
        "phase", "coupling", "correlation", "variance", "stochastic", "deterministic",
        "ergodic", "bifurcation", "trajectory", "tensor", "manifold", "topology",
        "cosmological", "negentropic", "admissible", "convergence", "divergence",
        "differential", "equation", "lagrangian", "hamiltonian", "metric", "geodesic",
        "substrate", "attractor", "resonance", "damping", "spectrum", "frequency",
        "amplitude", "conservation", "particle", "electromagnetic", "radiation",
        "thermodynamic", "statistical", "mechanical", "relativistic", "singularity",
        "horizon", "black hole", "dark matter", "dark energy", "stress-energy",
        "wavefunction", "eigenvalue", "observable", "decoherence", "entanglement",
        "renormalization", "gauge", "action", "variational",
        "boundary", "constraint", "restoration", "restorative", "bounded",
        "coherence", "diffusion", "viscosity", "turbulence", "convection",
        "photon", "electron", "neutron", "proton", "boson", "fermion",
    ]
    pc = sum(1 for t in terms if t in lower)
    claims = [
        "we show", "we find", "we propose", "we introduce", "we investigate",
        "we establish", "we emphasize", "we conjecture", "we conclude", "we define",
        "we demonstrate", "we derive", "we analyze", "we present", "we report",
        "i show", "i find", "i propose", "i introduce", "i measured", "i observed",
        "this paper", "this work", "this manuscript", "this study",
        "result shows", "results show", "results demonstrate", "results establish",
        "data indicates", "data suggest", "these results", "this establishes",
        "is experimentally validated", "hypothesis", "our analysis",
        "the model predicts", "our results", "the findings",
    ]
    cc = sum(1 for p in claims if p in lower)
    sections = [
        "introduction", "method", "methods", "results", "discussion", "conclusion",
        "references", "theory", "formalism", "derivation", "appendix", "abstract",
        "overview", "implications", "background", "related work", "acknowledgment",
        "figure", "table", "proof", "theorem", "corollary", "lemma",
    ]
    sf = sum(1 for s in sections if s in lower)
    spam = ["buy now", "click here", "guaranteed", "act now", "free money", "bitcoin investment"]
    is_spam = any(s in lower for s in spam)
    scores = {
        "scope": min(5, max(1, (pc + 2) // 3)),
        "claim": min(5, max(1, cc + 1)),
        "structure": min(5, max(1, sf)),
        "clarity": min(5, 2 + min(3, wc // 200)),
        "quantitative": 4 if re.search(r"\d+\s*[=<>]", body) else (3 if re.search(r"\d", body) else 1),
        "citations": 4 if re.search(r"\[\d+\]", body) and lower.count("[") >= 3 else (3 if re.search(r"\[\d+\]|references", lower) else 1),
        "anonymity": 5,
        "good_faith": 0 if is_spam else min(5, max(1, 1 + wc // 150)),
    }
    overall = round(sum(scores.values()) / 40 * 100)
    rec = "block" if is_spam else ("pass" if overall >= 55 and scores["good_faith"] >= 2 else "return")
    strengths = []
    suggestions = []
    if scores["scope"] >= 4:
        strengths.append("Strong physics content with clear domain relevance.")
    if scores["claim"] >= 3:
        strengths.append("Clear identifiable claims and research contributions.")
    if scores["structure"] >= 4:
        strengths.append("Well-organized with recognizable sections.")
    if scores["quantitative"] >= 3:
        strengths.append("Includes quantitative analysis or numerical results.")
    if scores["citations"] >= 3:
        strengths.append("References prior work appropriately.")
    if scores["scope"] < 3:
        suggestions.append("Consider adding more explicit physics terminology and context.")
    if scores["claim"] < 3:
        suggestions.append("State your central claim more explicitly.")
    if scores["structure"] < 3:
        suggestions.append("Add clearer section headings: Introduction, Methods, Results, Conclusion.")
    if scores["quantitative"] < 3:
        suggestions.append("Include more quantitative results or numerical analysis.")
    if scores["citations"] < 3:
        suggestions.append("Add references to prior work using [N] notation.")
    summary_parts = []
    if rec == "pass":
        summary_parts.append("Ready for community review.")
    elif rec == "return":
        summary_parts.append("Needs more development before community review.")
    else:
        summary_parts.append("Not accepted.")
    if strengths:
        summary_parts.append("Strengths: " + " ".join(strengths))
    if suggestions:
        summary_parts.append("Suggestions: " + " ".join(suggestions))
    return {
        "overall_score": overall,
        "score": overall,
        "recommendation": rec,
        "scores": scores,
        "summary": " ".join(summary_parts),
        "encouragement": "Your curiosity is valued here." if rec != "block" else "We welcome genuine submissions.",
    }


def seed():
    cats = [
        ("foundations", "Foundations of Physics", "\xf0\x9f\x8c\x8c".encode().decode()),
        ("math-physics", "Mathematical Physics", "\xf0\x9f\x93\x90".encode().decode()),
        ("nonlinear", "Nonlinear Dynamics", "\xf0\x9f\x8c\x80".encode().decode()),
        ("stat-mech", "Statistical Mechanics", "\xe2\x9a\x9b\xef\xb8\x8f".encode().decode()),
        ("complex", "Complex Systems", "\xf0\x9f\x95\xb8\xef\xb8\x8f".encode().decode()),
        ("experimental", "Experimental & Observational", "\xf0\x9f\x94\xac".encode().decode()),
    ]
    for slug, name, emoji in cats:
        if not Category.query.filter_by(slug=slug).first():
            db.session.add(Category(slug=slug, name=name, emoji=emoji))
    if not User.query.filter_by(email="admin@journal.local").first():
        u = User(email="admin@journal.local", display_name="Founding Editor", role="admin")
        u.set_password("change-me-now")
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
            "user": u.to_dict()
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
            return jsonify({"error": "Account is banned"}), 403

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
    if display_name:
        current_user.display_name = display_name[:120]
    current_user.bio = bio[:2000]
    db.session.commit()
    return jsonify({"ok": True, "user": current_user.to_dict()})


@app.route("/api/categories")
def categories():
    return jsonify({
        "categories": [
            {"id": c.id, "name": c.name, "emoji": c.emoji, "slug": c.slug}
            for c in Category.query.all()
        ]
    })


@app.route("/api/suggest-category", methods=["POST"])
def suggest_category():
    d = request.get_json(silent=True) or {}
    text = ((d.get("title") or "") + " " + (d.get("abstract") or "") + " " + (d.get("body_text") or "")).lower()
    clusters = {
        "foundations": ["spacetime", "curvature", "relativity", "gravity", "metric", "geodesic",
            "singularity", "horizon", "black hole", "cosmolog", "dark matter", "dark energy",
            "stress-energy", "einstein", "general relativity", "persistence", "admissible",
            "negentropic", "restorative", "invariant", "phase-space", "manifold"],
        "math-physics": ["manifold", "topology", "symmetry", "group", "algebra", "proof",
            "theorem", "lemma", "corollary", "operator", "eigenvalue", "hilbert",
            "banach", "tensor", "differential geometry", "variational", "functional",
            "isomorphism", "homomorphism", "fiber bundle", "lagrangian", "hamiltonian"],
        "nonlinear": ["chaos", "attractor", "bifurcation", "nonlinear", "dynamical system",
            "lyapunov", "oscillation", "limit cycle", "strange attractor", "fractal",
            "forced", "dissipative", "contraction", "divergence", "trajectory",
            "cellular automata", "rule 110", "turing complete"],
        "stat-mech": ["entropy", "temperature", "partition", "boltzmann", "distribution",
            "statistical mechanics", "thermodynamic", "ensemble", "ergodic",
            "fluctuation", "free energy", "microstate", "macrostate", "canonical"],
        "complex": ["network", "emergence", "agent", "complex system", "feedback",
            "self-organization", "scale-free", "power law", "percolation",
            "information", "mutual information", "transfer entropy", "coupling"],
        "experimental": ["experiment", "measurement", "observation", "detector", "data",
            "apparatus", "calibration", "systematic error", "uncertainty",
            "signal-to-noise", "spectroscopy", "interferometer", "sensor"],
    }
    scores = {}
    for slug, keywords in clusters.items():
        score = sum(1 for k in keywords if k in text)
        if score > 0:
            scores[slug] = score
    if not scores:
        return jsonify({"suggested_slug": "foundations", "confidence": 0})
    best = max(scores, key=scores.get)
    total = sum(scores.values())
    confidence = round(scores[best] / max(1, total), 2)
    return jsonify({"suggested_slug": best, "confidence": confidence, "scores": scores})


@app.route("/api/feed/discovery")
def discovery():
    page = request.args.get("page", 1, type=int)
    q = Submission.query.filter(
        Submission.status.in_(["in_discovery", "under_review", "revision_requested", "revised", "contested", "desk_returned"])
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
    p = Submission.query.filter_by(status="published").order_by(Submission.published_at.desc()).paginate(page=1, per_page=20, error_out=False)
    uid = current_user.id if current_user.is_authenticated else None
    return jsonify({"papers": [s.to_card(uid) for s in p.items]})


def category_from_payload(d):
    category_id = d.get("category_id")
    if category_id:
        try:
            cat = db.session.get(Category, int(category_id))
            if cat:
                return cat.id
        except Exception:
            pass

    category_name = (d.get("category") or "").strip()
    if category_name:
        cat = Category.query.filter_by(name=category_name).first()
        if cat:
            return cat.id
        slug = category_name.lower().replace("&", "and").replace(" ", "-")
        cat = Category.query.filter_by(slug=slug).first()
        if cat:
            return cat.id

    return 1


def read_uploaded_text():
    parts = []
    for f in request.files.values():
        if not f:
            continue
        try:
            raw = f.read()
            if raw:
                parts.append(raw.decode("utf-8", errors="ignore"))
        except Exception:
            continue
    return "\n\n".join([p for p in parts if p])


@app.route("/api/submissions", methods=["POST"])
@api_login_required
def create_submission():
    if request.content_type and "multipart/form-data" in request.content_type:
        d = request.form
        uploaded_text = read_uploaded_text()
        title = (d.get("title") or "").strip()
        abstract = (d.get("abstract") or "").strip()
        body_text = (d.get("body_text") or "").strip()
        if not body_text and uploaded_text:
            body_text = uploaded_text[:120000]
        elif uploaded_text:
            body_text = (body_text + "\n\n" + uploaded_text[:60000]).strip()
        payload = {
            "title": title,
            "abstract": abstract,
            "body_text": body_text,
            "tags": (d.get("tags") or "").strip(),
            "category_id": category_from_payload(d),
        }
    else:
        d = request.get_json(silent=True) or {}
        payload = {
            "title": (d.get("title") or "").strip(),
            "abstract": (d.get("abstract") or "").strip(),
            "body_text": (d.get("body_text") or "").strip(),
            "tags": (d.get("tags") or "").strip(),
            "category_id": category_from_payload(d),
        }

    if not payload["title"] or not payload["abstract"]:
        return jsonify({"error": "Title and abstract required"}), 400
    if not payload["body_text"]:
        payload["body_text"] = payload["abstract"]

    sub = Submission(
        blind_id=uuid.uuid4().hex[:12].upper(),
        title=payload["title"],
        abstract=payload["abstract"],
        body_text=payload["body_text"],
        tags=payload["tags"],
        author_id=current_user.id,
        category_id=payload["category_id"],
    )
    desk = run_desk_review(sub.title, sub.abstract, sub.body_text)
    sub.status = {"pass": "in_discovery", "return": "desk_returned", "block": "desk_blocked"}[desk["recommendation"]]
    db.session.add(sub)
    db.session.flush()
    db.session.add(DeskDecision(
        submission_id=sub.id,
        decision=desk["recommendation"],
        overall_score=desk["overall_score"],
        summary=desk["summary"],
        encouragement=desk["encouragement"],
        scores_json=json.dumps(desk["scores"]),
    ))
    db.session.commit()
    return jsonify({"submission": sub.to_card(current_user.id), "desk_review": desk}), 201


@app.route("/api/submissions/<bid>")
def get_submission(bid):
    s = Submission.query.filter_by(blind_id=bid).first_or_404()
    d = s.to_card(current_user.id if current_user.is_authenticated else None)
    d["body_text"] = s.body_text
    d["comments"] = [
        {
            "id": c.id,
            "author": c.author.to_dict(),
            "comment_type": c.comment_type,
            "body": c.body,
            "created_at": c.created_at.isoformat(),
        }
        for c in Comment.query.filter_by(submission_id=s.id).order_by(Comment.created_at).all()
    ]
    return jsonify({"submission": d})


@app.route("/api/submissions/<bid>/like", methods=["POST"])
@api_login_required
def toggle_like(bid):
    s = Submission.query.filter_by(blind_id=bid).first_or_404()
    ex = Like.query.filter_by(user_id=current_user.id, submission_id=s.id).first()
    if ex:
        db.session.delete(ex)
        db.session.commit()
        like_count = Like.query.filter_by(submission_id=s.id).count()
        return jsonify({"liked": False, "like_count": like_count})
    db.session.add(Like(user_id=current_user.id, submission_id=s.id))
    if s.author_id != current_user.id:
        create_notification(s.author_id, f"{current_user.display_name} liked your paper", s.title)
    db.session.commit()
    like_count = Like.query.filter_by(submission_id=s.id).count()
    return jsonify({"liked": True, "like_count": like_count})


@app.route("/api/submissions/<bid>/bookmark", methods=["POST"])
@api_login_required
def toggle_bookmark(bid):
    s = Submission.query.filter_by(blind_id=bid).first_or_404()
    ex = Bookmark.query.filter_by(user_id=current_user.id, submission_id=s.id).first()
    if ex:
        db.session.delete(ex)
        db.session.commit()
        return jsonify({"bookmarked": False})
    db.session.add(Bookmark(user_id=current_user.id, submission_id=s.id))
    db.session.commit()
    return jsonify({"bookmarked": True})


@app.route("/api/submissions/<bid>/comments", methods=["POST"])
@api_login_required
def add_comment(bid):
    s = Submission.query.filter_by(blind_id=bid).first_or_404()
    d = request.get_json(silent=True) or {}
    body = (d.get("body") or "").strip()
    if not body:
        return jsonify({"error": "Comment required"}), 400
    c = Comment(
        submission_id=s.id,
        author_id=current_user.id,
        comment_type=d.get("comment_type", "note"),
        body=body,
    )
    db.session.add(c)
    if s.author_id != current_user.id:
        create_notification(s.author_id, f"{current_user.display_name} commented on your paper", body[:200])
    db.session.commit()
    return jsonify({
        "comment": {
            "id": c.id,
            "author": current_user.to_dict(),
            "comment_type": c.comment_type,
            "body": c.body,
            "created_at": c.created_at.isoformat(),
        }
    }), 201


@app.route("/api/users/<int:uid>")
def get_user(uid):
    u = User.query.get_or_404(uid)
    d = u.to_dict()
    d["papers"] = [
        s.to_card(current_user.id if current_user.is_authenticated else None)
        for s in Submission.query.filter_by(author_id=uid).filter(
            Submission.status.in_(["in_discovery", "under_review", "published", "desk_returned"])
        ).order_by(Submission.updated_at.desc()).limit(20).all()
    ]
    return jsonify({"user": d})


@app.route("/api/users/<int:uid>/follow", methods=["POST"])
@api_login_required
def toggle_follow(uid):
    if uid == current_user.id:
        return jsonify({"error": "Cannot follow yourself"}), 400
    other = User.query.get_or_404(uid)
    if current_user.following.filter(User.id == uid).first():
        current_user.following.remove(other)
        db.session.commit()
        return jsonify({"followed": False})
    current_user.following.append(other)
    create_notification(other.id, f"{current_user.display_name} followed you", "")
    db.session.commit()
    return jsonify({"followed": True})


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
                "message": n.title,
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
        "discovery_count": Submission.query.filter(Submission.status.in_(["in_discovery", "under_review", "desk_returned"])).count(),
        "user_count": User.query.count(),
    })


def extract_floats(text):
    if not text:
        return []
    vals = re.findall(r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?", text)
    out = []
    for v in vals:
        try:
            out.append(float(v))
        except Exception:
            pass
    return out


def get_tool_input():
    text_parts = []
    tool = ""

    if request.content_type and "multipart/form-data" in request.content_type:
        tool = (request.form.get("tool") or "").strip()
        text_parts.append(request.form.get("input") or "")
        text_parts.append(read_uploaded_text())
    else:
        d = request.get_json(silent=True) or {}
        tool = (d.get("tool") or "").strip()
        text_parts.append(d.get("input") or "")

    text = "\n".join([t for t in text_parts if t])
    series = extract_floats(text)
    return tool, text, series


def tool_ocm(series):
    N = len(series)
    WZ = 200
    WB = 500
    eps = 1e-8
    if N < WB + 10:
        return {"error": "Need at least 600 numeric values for OCM analysis."}

    z = [0.0] * N
    for t in range(WZ, N):
        w = series[t-WZ:t]
        mu = sum(w) / WZ
        var = sum((x - mu) ** 2 for x in w) / WZ
        z[t] = (series[t] - mu) / (math.sqrt(var) + eps)

    b2 = [0.0] * N
    for t in range(WB, N):
        b2[t] = sum(z[t-WB:t]) / WB

    s = max(WZ, WB)
    D = [abs(z[t] - b2[t]) for t in range(N)]
    ind = [1 if D[t+1] < D[t] else 0 for t in range(s, N-1)]
    CR = sum(ind) / max(1, len(ind))
    zS = (CR - 0.5) / math.sqrt(0.25 / max(1, len(ind)))
    cls = "DRIVEN-DISSIPATIVE" if CR > 0.50522 else "AUTONOMOUS CHAOTIC" if CR < 0.495 else "STOCHASTIC"
    return {
        "classification": cls,
        "summary": f"Contraction rate = {CR:.6f}. Threshold = 0.50522. This series is classified as {cls}.",
        "details": {
            "contraction_rate": round(CR, 6),
            "z_statistic": round(zS, 2),
            "points": N,
            "threshold": 0.50522
        }
    }


def tool_er(series):
    N = len(series)
    WZ = 100
    stride = 50
    threshold = 0.5
    eps = 1e-8
    if N < WZ * 3:
        return {"error": "Need at least 300 numeric values for topology mapping."}

    nodes = list(range(WZ, N - WZ, stride))
    segs = []
    for t in nodes:
        s = series[t - WZ // 2:t + WZ // 2]
        mu = sum(s) / len(s)
        var = sum((x - mu) ** 2 for x in s) / len(s)
        std = math.sqrt(var) + eps
        segs.append([(x - mu) / std for x in s])

    edges = 0
    bridges = 0
    max_gap = 0

    for i in range(len(segs)):
        for j in range(i + 1, len(segs)):
            n = min(len(segs[i]), len(segs[j]))
            dot = sum(segs[i][k] * segs[j][k] for k in range(n))
            if abs(dot / n) >= threshold:
                edges += 1
                gap = abs(nodes[j] - nodes[i])
                if gap > 500:
                    bridges += 1
                    if gap > max_gap:
                        max_gap = gap

    density = edges / max(1, len(nodes) * (len(nodes) - 1) / 2)
    topo = "TRIVIAL" if len(nodes) < 4 else "BRIDGED" if bridges > 0 else "CONNECTED" if edges > len(nodes) else "FRAGMENTED"

    adj = []
    for i in range(len(segs) - 1):
        n = min(len(segs[i]), len(segs[i + 1]))
        dot = sum(segs[i][k] * segs[i + 1][k] for k in range(n))
        adj.append(dot / n)

    min_coh = min(adj) if adj else 0.0
    mean_coh = sum(adj) / len(adj) if adj else 0.0

    return {
        "classification": topo,
        "summary": f"Topology = {topo}. Nodes = {len(nodes)}, edges = {edges}, bridges = {bridges}.",
        "details": {
            "nodes": len(nodes),
            "edges": edges,
            "bridges": bridges,
            "density": round(density, 4),
            "min_coherence": round(min_coh, 3),
            "mean_coherence": round(mean_coh, 3),
            "max_bridge_gap": max_gap
        }
    }


def tool_icm(series):
    N = len(series)
    WZ = 200
    eps = 1e-8
    if N < WZ * 3:
        return {"error": "Need at least 600 numeric values for invariant detection."}

    z = [0.0] * N
    for t in range(WZ, N):
        w = series[t-WZ:t]
        mu = sum(w) / WZ
        var = sum((x - mu) ** 2 for x in w) / WZ
        z[t] = (series[t] - mu) / (math.sqrt(var) + eps)

    events = []
    in_ev = False
    onset = 0
    peak = 0
    p_val = 0.0

    for t in range(WZ, N):
        az = abs(z[t])
        if not in_ev and az > 2:
            in_ev = True
            onset = t
            peak = t
            p_val = az
        elif in_ev:
            if az > p_val:
                peak = t
                p_val = az
            if az < 1.4:
                if t - onset >= 10:
                    rec_time = t - peak
                    asym = (peak - onset) / max(1, rec_time)
                    events.append({
                        "onset": onset,
                        "peak": peak,
                        "recovery": t,
                        "amp": p_val,
                        "rec_time": rec_time,
                        "asym": asym
                    })
                in_ev = False

    if not events:
        return {
            "classification": "NO_EVENTS",
            "summary": "No disruption events detected.",
            "details": {
                "events": 0,
                "admissible": False,
                "regime": "SUBCRITICAL"
            }
        }

    amps = [e["amp"] for e in events]
    recs = [e["rec_time"] for e in events]
    asyms = [e["asym"] for e in events]
    n_ev = len(events)
    m_amp = sum(amps) / n_ev
    m_rec = sum(recs) / n_ev
    m_asym = sum(asyms) / n_ev
    std_amp = math.sqrt(sum((a - m_amp) ** 2 for a in amps) / n_ev)
    amp_cv = std_amp / m_amp if m_amp > 0 else 0

    dx = [series[i] - series[i - 1] for i in range(1, N)]
    dmu = sum(dx) / len(dx)
    dxc = [v - dmu for v in dx]
    c0 = sum(v * v for v in dxc) / len(dxc)
    c1 = sum(dxc[i] * dxc[i + 1] for i in range(len(dxc) - 1)) / len(dxc)
    rho1 = c1 / c0 if c0 > 1e-12 else 0
    damping = -math.log(abs(rho1)) if abs(rho1) > 1e-12 and abs(rho1) < 1 else 0
    forcing = n_ev / N
    R = damping * forcing
    regime = "SUBCRITICAL" if R < 1e-10 else "WEAK" if R < 0.001 else "RESONANT" if R < 0.05 else "SATURATED"
    morph = "SHARP_ONSET" if m_asym > 3 else "GRADUAL_ONSET" if m_asym < 0.3 else "SYMMETRIC"

    A1 = n_ev > 0
    A2 = regime in ("WEAK", "RESONANT")
    A3 = m_rec > 0 and m_rec < N / 4
    A4 = amp_cv < 3
    admissible = A1 and A2 and A3 and A4

    return {
        "classification": "ADMISSIBLE" if admissible else "INADMISSIBLE",
        "summary": f"{n_ev} events found. Morphology = {morph}. Regime = {regime}. Admissible = {admissible}.",
        "details": {
            "events": n_ev,
            "morphology": morph,
            "regime": regime,
            "mean_amplitude": round(m_amp, 3),
            "mean_recovery": round(m_rec, 1),
            "mean_asymmetry": round(m_asym, 3),
            "resonance_R": round(R, 8),
            "amp_cv": round(amp_cv, 3),
            "gates": {"A1": A1, "A2": A2, "A3": A3, "A4": A4}
        }
    }


def _mk_ca_step(state, rule):
    N = len(state)
    out = [0] * N
    for i in range(N):
        a = state[(i - 1 + N) % N]
        b = state[i]
        c = state[(i + 1) % N]
        out[i] = (rule >> ((a << 2) | (b << 1) | c)) & 1
    return out


def _i_clm(K, N):
    psi = []
    for k in range(K):
        row = [0.0] * N
        row[(1337 + 13 * k) % N] = 0.01
        psi.append(row)
    return {"psi": psi, "G": [0.0] * N, "kw": 1.0, "Dp": 1e-6, "ri": 0.0}


def _s_clm(st, forcing, K, N, fg):
    psi = st["psi"]
    G = []
    for n in range(N):
        s = 0.0
        for k in range(K):
            s += psi[k][n]
        G.append(s / K)

    newP = []
    for k in range(K):
        row = []
        for n in range(N):
            x = psi[k][n]
            x2 = 0.88 * x + 0.35 * (G[n] - x) + 0.07 * (G[n] - x) + fg * forcing[k][n]
            ax = abs(x)
            if ax > 1.6:
                x2 += max(-0.25, min(0.25, -0.14 * (ax - 1.6) * (x / max(1e-9, ax))))
            row.append(2.25 * math.tanh(x2 / 2.25))
        newP.append(row)

    eS = 0.0
    flat = []
    for k in range(K):
        for n in range(N):
            eS += abs(newP[k][n] - G[n])
            flat.append(newP[k][n])

    ge = eS / (K * N)
    df = max(1e-4, 0.035)
    kw = max(0.0, min(10.0, 0.75 * st["kw"] + 0.25 * ((ge + df) / (max(st["Dp"], 0.0) + df))))
    stab = max(0.0, min(1.0, (1 - max(0.0, kw - 1)) * (1 - ge / 0.28)))
    prms = math.sqrt(sum(x * x for x in flat) / len(flat))
    ri = st["ri"] + max(0.0, st["Dp"] - ge)

    lbl = "SURGE" if kw > 1.25 else "ELIGIBLE" if stab >= 0.5 and ge < 0.14 else "COIL" if kw < 1 else "DRIFT"

    return {
        "st": {"psi": newP, "G": G, "kw": kw, "Dp": ge, "ri": ri},
        "m": {"ge": ge, "kw": kw, "stab": stab, "prms": prms, "lbl": lbl, "ri": ri},
        "G": G
    }


def tool_clm(series):
    K = 7
    N = 64
    diff = 3
    st = _i_clm(K, N)

    if series:
        mn = min(series)
        mx = max(series)
        rg = mx - mn if mx != mn else 1.0
        norm = [((v - mn) / rg) * 2 - 1 for v in series]
    else:
        norm = []

    ca_states = []
    for k in range(K):
        row = [0] * N
        row[(1337 + 101 * k) % N] = 1
        ca_states.append(row)

    tot = 136
    set_phase = 24
    storm = 40
    last_metrics = None

    for s in range(tot):
        if s < set_phase:
            sc = 0.004
        elif s < set_phase + storm:
            sc = diff * 0.02
        else:
            sc = 0.007

        if norm:
            forcing = []
            for k in range(K):
                row = []
                for n in range(N):
                    idx = (s * N + n + k * 137) % len(norm)
                    row.append(norm[idx])
                forcing.append(row)
        else:
            forcing = []
            new_states = []
            for k in range(K):
                ca_states[k] = _mk_ca_step(ca_states[k], 110)
                new_states.append(ca_states[k])
                forcing.append([1 if b else -1 for b in ca_states[k]])

        res = _s_clm(st, forcing, K, N, sc)
        st = res["st"]
        last_metrics = res["m"]

    return {
        "classification": last_metrics["lbl"],
        "summary": f"Final coherence label = {last_metrics['lbl']}. kappa_w = {last_metrics['kw']:.4f}, glue_error = {last_metrics['ge']:.4f}.",
        "details": {
            "kappa_w": round(last_metrics["kw"], 4),
            "glue_error": round(last_metrics["ge"], 4),
            "stability": round(last_metrics["stab"], 4),
            "prms": round(last_metrics["prms"], 4),
            "ri": round(last_metrics["ri"], 4),
            "points_used": len(series) if series else 0
        }
    }


@app.route("/api/tools/run", methods=["POST"])
@api_login_required
def tools_run():
    tool, text, series = get_tool_input()
    if not tool:
        return jsonify({"error": "Tool not provided"}), 400

    if tool == "desk_review":
        cleaned = (text or "").strip()
        if not cleaned:
            return jsonify({"error": "Paste manuscript text or upload a text file."}), 400
        lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
        title = lines[0][:500] if lines else "Untitled"
        abstract = lines[1][:2000] if len(lines) > 1 else cleaned[:500]
        body = cleaned
        desk = run_desk_review(title, abstract, body)
        return jsonify({
            "classification": desk["recommendation"].upper(),
            "summary": desk["summary"],
            "details": desk
        })

    if tool == "ocm":
        return jsonify(tool_ocm(series))

    if tool == "er":
        return jsonify(tool_er(series))

    if tool == "icm":
        return jsonify(tool_icm(series))

    if tool == "clm":
        return jsonify(tool_clm(series))

    return jsonify({"error": "Unknown tool"}), 400


@app.route("/api/admin/submissions")
@admin_required
def admin_submissions():
    items = Submission.query.order_by(Submission.updated_at.desc()).limit(200).all()
    return jsonify({
        "submissions": [
            {
                **s.to_card(current_user.id),
                "author_name": s.author.display_name if s.author else "Anonymous"
            }
            for s in items
        ]
    })


@app.route("/api/admin/submissions/<int:sid>/status", methods=["POST"])
@admin_required
def admin_submission_status(sid):
    s = Submission.query.get_or_404(sid)
    d = request.get_json(silent=True) or {}
    status = (d.get("status") or "").strip()
    allowed = {
        "published", "under_review", "desk_returned", "declined",
        "in_discovery", "submitted", "revision_requested", "contested"
    }
    if status not in allowed:
        return jsonify({"error": "Invalid status"}), 400
    s.status = status
    if status == "published":
        s.published_at = datetime.utcnow()
    db.session.commit()
    if s.author_id:
        create_notification(s.author_id, f"Submission status updated: {Submission.STATUS_LABELS.get(status, status)}", s.title)
        db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/submissions/<int:sid>", methods=["DELETE"])
@admin_required
def admin_delete_submission(sid):
    s = Submission.query.get_or_404(sid)
    Like.query.filter_by(submission_id=s.id).delete()
    Bookmark.query.filter_by(submission_id=s.id).delete()
    Comment.query.filter_by(submission_id=s.id).delete()
    Review.query.filter_by(submission_id=s.id).delete()
    DeskDecision.query.filter_by(submission_id=s.id).delete()
    db.session.delete(s)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/users")
@admin_required
def admin_users():
    items = User.query.order_by(User.created_at.desc()).limit(500).all()
    return jsonify({"users": [u.to_dict() for u in items]})


@app.route("/api/admin/users/<int:uid>/role", methods=["POST"])
@admin_required
def admin_user_role(uid):
    if current_user.id == uid:
        return jsonify({"error": "Cannot change your own role here"}), 400
    u = User.query.get_or_404(uid)
    d = request.get_json(silent=True) or {}
    role = (d.get("role") or "").strip()
    if role not in ("member", "admin"):
        return jsonify({"error": "Invalid role"}), 400
    u.role = role
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/users/<int:uid>/ban", methods=["POST"])
@admin_required
def admin_user_ban(uid):
    if current_user.id == uid:
        return jsonify({"error": "Cannot ban yourself"}), 400
    u = User.query.get_or_404(uid)
    u.is_banned = True
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/users/<int:uid>/unban", methods=["POST"])
@admin_required
def admin_user_unban(uid):
    u = User.query.get_or_404(uid)
    u.is_banned = False
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/users/<int:uid>", methods=["DELETE"])
@admin_required
def admin_user_delete(uid):
    if current_user.id == uid:
        return jsonify({"error": "Cannot delete yourself"}), 400

    u = User.query.get_or_404(uid)

    submission_ids = [s.id for s in Submission.query.filter_by(author_id=u.id).all()]
    if submission_ids:
        Like.query.filter(Like.submission_id.in_(submission_ids)).delete(synchronize_session=False)
        Bookmark.query.filter(Bookmark.submission_id.in_(submission_ids)).delete(synchronize_session=False)
        Comment.query.filter(Comment.submission_id.in_(submission_ids)).delete(synchronize_session=False)
        Review.query.filter(Review.submission_id.in_(submission_ids)).delete(synchronize_session=False)
        DeskDecision.query.filter(DeskDecision.submission_id.in_(submission_ids)).delete(synchronize_session=False)
        Submission.query.filter(Submission.id.in_(submission_ids)).delete(synchronize_session=False)

    Like.query.filter_by(user_id=u.id).delete()
    Bookmark.query.filter_by(user_id=u.id).delete()
    Comment.query.filter_by(author_id=u.id).delete()
    Review.query.filter_by(reviewer_id=u.id).delete()
    Notification.query.filter_by(user_id=u.id).delete()

    conn = db.session.connection()
    conn.execute(user_follows.delete().where(user_follows.c.follower_id == u.id))
    conn.execute(user_follows.delete().where(user_follows.c.followed_id == u.id))

    db.session.delete(u)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/admin/reset-db", methods=["POST"])
@admin_required
def admin_reset_db():
    admin_ids = [u.id for u in User.query.filter_by(role="admin").all()]

    Like.query.delete()
    Bookmark.query.delete()
    Comment.query.delete()
    Review.query.delete()
    DeskDecision.query.delete()
    Notification.query.delete()
    Submission.query.delete()

    conn = db.session.connection()
    conn.execute(user_follows.delete())

    for u in User.query.all():
        if u.id not in admin_ids:
            db.session.delete(u)

    db.session.commit()
    return jsonify({"ok": True})


if __name__ == "__main__":
    init_db()
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.getenv("FLASK_DEBUG", "0") == "1")