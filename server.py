# server.py
# The Journal - Deployment Server
# All Rights Reserved.

import os
import io
import csv
import json
import math
import random
import re
import uuid
import threading
from datetime import datetime, timedelta
from pathlib import Path
from functools import wraps

from flask import Flask, jsonify, request, send_from_directory, send_file, make_response
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, current_user, login_user, logout_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.exceptions import HTTPException
from sqlalchemy import or_, inspect, text
from sqlalchemy.exc import IntegrityError

app = Flask(__name__, static_folder="static")
BASE = Path(__file__).parent
INSTANCE = BASE / "instance"
INSTANCE.mkdir(exist_ok=True)

# Ensure avatar folder exists
AVATAR_FOLDER = BASE / "static" / "avatars"
AVATAR_FOLDER.mkdir(parents=True, exist_ok=True)

# --- Database config ---
db_url = os.getenv("DATABASE_URL")
if db_url and db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
if not db_url:
    sqlite_path = Path(os.getenv("SQLITE_PATH", "/tmp/journal.db"))
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    db_url = "sqlite:///" + str(sqlite_path)

# --- Secret key with fallback ---
secret = os.getenv("SECRET_KEY")
if not secret or secret.strip() == "":
    secret = "dev-change-before-deploy"
    print("WARNING: SECRET_KEY not set or empty, using fallback")
app.config["SECRET_KEY"] = secret
app.secret_key = secret

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
secure_cookies = bool(os.getenv("RENDER"))
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = secure_cookies
app.config["REMEMBER_COOKIE_SAMESITE"] = "Lax"
app.config["REMEMBER_COOKIE_SECURE"] = secure_cookies
app.config["REMEMBER_COOKIE_HTTPONLY"] = True
app.config["REMEMBER_COOKIE_DURATION"] = timedelta(days=30)
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB max for avatar

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


# --- Models ---
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
    avatar_filename = db.Column(db.String(255), default="")  # new column
    orcid = db.Column(db.String(32), default="")
    reputation = db.Column(db.Float, default=1.0)
    is_banned = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    following = db.relationship(
        "User",
        secondary=user_follows,
        primaryjoin=(user_follows.c.follower_id == id),
        secondaryjoin=(user_follows.c.followed_id == id),
        backref=db.backref("followers", lazy="dynamic"),
        lazy="dynamic",
    )

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
            "avatar_filename": self.avatar_filename or "",
            "orcid": self.orcid or "",
            "reputation_score": round(self.reputation or 0, 2),
            "paper_count": paper_count,
            "review_count": review_count,
            "follower_count": follower_count,
            "joined": self.created_at.isoformat() if self.created_at else None,
            "role": self.role,
            "is_banned": self.is_banned,
        }


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
    status = db.Column(db.String(30), default="draft", index=True)
    is_draft = db.Column(db.Boolean, default=True, index=True)
    tags = db.Column(db.Text, default="")
    author_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    category_id = db.Column(db.Integer, db.ForeignKey("category.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    published_at = db.Column(db.DateTime)

    author = db.relationship("User", backref="submissions")
    category = db.relationship("Category", backref="submissions")

    STATUS_LABELS = {
        "draft": "Private Draft",
        "submitted": "Submitted (Admin Queue)",
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
        "draft": "#6b7db3",
        "submitted": "#8b9cc8",
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

    def to_card(self, uid=None, full_abstract=False):
        lc = Like.query.filter_by(submission_id=self.id).count()
        cc = Comment.query.filter_by(submission_id=self.id).count()
        rc = Review.query.filter_by(submission_id=self.id).count()
        d = {
            "id": self.id,
            "blind_id": self.blind_id,
            "title": self.title,
            "abstract": self.abstract if full_abstract else (self.abstract or "")[:300],
            "body_text": self.body_text or "",
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
            "is_draft": self.is_draft,
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


# --- Schema migration helpers ---
def ensure_column(table_name, column_name, ddl_sql):
    inspector = inspect(db.engine)
    cols = {c["name"] for c in inspector.get_columns(table_name)}
    if column_name in cols:
        return
    with db.engine.begin() as conn:
        conn.execute(text(ddl_sql))


def migrate_schema():
    try:
        ensure_column("user", "orcid", "ALTER TABLE \"user\" ADD COLUMN orcid VARCHAR(32) DEFAULT ''")
    except Exception:
        pass
    try:
        ensure_column("user", "avatar_filename", "ALTER TABLE \"user\" ADD COLUMN avatar_filename VARCHAR(255) DEFAULT ''")
    except Exception:
        pass
    try:
        ensure_column("submission", "is_draft", "ALTER TABLE submission ADD COLUMN is_draft BOOLEAN DEFAULT 1")
    except Exception:
        pass
    try:
        ensure_column("notification", "link", "ALTER TABLE notification ADD COLUMN link VARCHAR(255) DEFAULT ''")
    except Exception:
        pass


# --- Helpers ---
PUBLIC_FEED_STATUSES = ("in_discovery", "under_review", "published")
DISCOVERY_STATUSES = ("in_discovery", "under_review")
BUILDER_STATUSES = ("draft", "submitted", "desk_returned", "revision_requested", "declined", "desk_blocked")
ADMIN_QUEUE_STATUSES = ("submitted", "desk_returned", "revision_requested")
EDITORIAL_STATUSES = ("submitted", "desk_returned", "revision_requested", "in_discovery", "under_review", "published", "declined", "desk_blocked")
PEER_REVIEW_VISIBLE_STATUSES = ("in_discovery", "under_review", "published")


def journal_capabilities_payload():
    return {
        "phase": "phase_2_builder_and_queue",
        "build_preserves_baseline": True,
        "feed_contract": {
            "bottom_nav_public_target": "Discover",
            "future_public_target": "Feed",
            "tabs_ready_for_future_phase": ["Discovery", "Published"],
            "current_discovery_statuses": list(DISCOVERY_STATUSES),
            "current_published_statuses": ["published"],
        },
        "builder_contract": {
            "private_workspace_enabled": True,
            "builder_statuses": list(BUILDER_STATUSES),
            "submission_enters_admin_queue_first": True,
        },
        "review_contract": {
            "peer_review_model_present": True,
            "editorial_decision_model_present": True,
            "peer_review_visible_statuses": list(PEER_REVIEW_VISIBLE_STATUSES),
            "separation_ready": True,
        },
        "tool_contract": {
            "supports_export_future_phase": True,
            "strict_invalid_numeric_handling_planned": True,
            "silent_sample_fallback_currently_present": False,
        },
        "status_groups": {
            "public_feed": list(PUBLIC_FEED_STATUSES),
            "discovery": list(DISCOVERY_STATUSES),
            "builder": list(BUILDER_STATUSES),
            "admin_queue": list(ADMIN_QUEUE_STATUSES),
            "editorial": list(EDITORIAL_STATUSES),
        },
    }


def parse_bool(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def request_data():
    if request.is_json:
        return request.get_json(silent=True) or {}
    if request.form:
        return request.form.to_dict()
    return {}


def api_login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return jsonify({"error": "Auth required"}), 401
        if getattr(current_user, "is_banned", False):
            return jsonify({"error": "Account restricted"}), 403
        return f(*args, **kwargs)
    return wrapped


def admin_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return jsonify({"error": "Auth required"}), 401
        if current_user.role != "admin":
            return jsonify({"error": "Admin access required"}), 403
        return f(*args, **kwargs)
    return wrapped


def resolve_submission(ref):
    sub = None
    ref_str = str(ref)
    if ref_str.isdigit():
        sub = Submission.query.get(int(ref_str))
        if sub:
            return sub
    return Submission.query.filter_by(blind_id=ref_str).first_or_404()


def create_notification(user_id, title, body, link=""):
    if not user_id:
        return
    db.session.add(Notification(user_id=user_id, title=title[:255], body=body or "", link=link or ""))


def add_editorial_comment(submission_id, admin_user, status):
    status_label = Submission.STATUS_LABELS.get(status, status)
    body = f"**Editorial decision:** Paper status changed to **{status_label}**."
    comment = Comment(
        submission_id=submission_id,
        author_id=admin_user.id,
        comment_type="editorial",
        body=body,
    )
    db.session.add(comment)


# --- Research tool implementations (without silent fallback) ---

def run_claude_review(title, abstract, body):
    """Call Claude API for intelligent paper review. Returns None on failure."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        text_to_review = f"TITLE: {title}\n\nABSTRACT: {abstract}\n\nPAPER:\n{body[:12000]}"
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            system="""You are a rigorous desk reviewer for The Journal, an independent physics journal. You evaluate manuscripts for scientific readiness. This journal welcomes independent researchers -- evaluate the science, not credentials. But standards are high: roughly 35% of submissions should be returned for revision.

CALIBRATION RULES -- follow these strictly:
- Score 5/5: Exceptional. Publication-ready in this criterion. Reserve for genuinely outstanding work.
- Score 4/5: Strong. Minor issues only. Meets the standard of a good physics journal.
- Score 3/5: Adequate. Competent but has clear gaps. This is the baseline for acceptable work.
- Score 2/5: Weak. Significant issues that must be addressed before community review.
- Score 1/5: Missing or fundamentally inadequate.

Score on these 8 criteria (1-5 each):
- scope: Does it address a genuine physics question with appropriate depth? Look for physical reasoning, not just terminology.
- claim: Are there clear, falsifiable claims supported by the analysis? Vague gesturing at ideas scores low.
- structure: Is it organized with logical flow? Look for introduction, methods/formalism, results, discussion, conclusion.
- clarity: Is the writing precise and readable? Jargon without explanation, run-on arguments, or unclear logical steps score low.
- quantitative: Does it include rigorous math, data analysis, error bounds, or numerical validation? Hand-waving scores low.
- citations: Does it engage with prior literature? Isolated work with no references scores low.
- anonymity: Is it properly formatted? (5 unless personal contact info is exposed in body)
- good_faith: Is this genuine, effortful research? (0=spam, 3=minimal effort, 5=substantial investigation)

BE SPECIFIC in suggestions -- name exactly what is missing or weak. Do not just say "add more detail." Say what detail and where.

Recommendation guidelines:
- "pass": Overall >= 70% AND no criterion below 2. Ready for community discovery.
- "return": Overall 40-69% OR any criterion at 1. Encourage revision with specific guidance.
- "block": Spam or not research.

Respond ONLY with valid JSON in this exact format, no other text:
{"scores":{"scope":N,"claim":N,"structure":N,"clarity":N,"quantitative":N,"citations":N,"anonymity":N,"good_faith":N},"strengths":["...","..."],"suggestions":["...","..."],"recommendation":"pass or return or block","summary":"One paragraph assessment"}""",
            messages=[{"role": "user", "content": text_to_review}],
        )
        raw = message.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
        result = json.loads(raw)
        # Validate structure
        if "scores" not in result or "recommendation" not in result:
            return None
        scores = result["scores"]
        overall = round(sum(scores.values()) / 40 * 100)
        return {
            "overall_score": overall,
            "recommendation": result["recommendation"],
            "scores": scores,
            "summary": result.get("summary", ""),
            "strengths": result.get("strengths", []),
            "suggestions": result.get("suggestions", []),
            "encouragement": "Your curiosity is valued here.",
            "review_engine": "claude",
        }
    except Exception as e:
        app.logger.warning(f"Claude review failed, falling back to heuristic: {e}")
        return None


def run_desk_review(title, abstract, body):
    # Try Claude first if API key is available
    claude_result = run_claude_review(title, abstract, body)
    if claude_result is not None:
        return claude_result

    # Fallback: expanded heuristic review
    lower = (title + " " + abstract + " " + body).lower()
    wc = len(body.split())
    scores = {
        "scope": 0,
        "claim": 0,
        "structure": 0,
        "clarity": 0,
        "quantitative": 0,
        "citations": 0,
        "anonymity": 5,
        "good_faith": 0,
    }
    strengths = []
    suggestions = []
    physics_terms = [
        'energy','force','momentum','field','wave','quantum','gravity','entropy',
        'experiment','friction','chaos','nonlinear','measurement','hypothesis',
        'oscillat','conservation','symmetry','spacetime','curvature','manifold',
        'topology','invariant','dynamics','dynamical','operator','model',
        'selection','persistence','forcing','dissipative','contraction','stability',
        'equilibrium','perturbation','phase','coupling','correlation','variance',
        'stochastic','deterministic','ergodic','bifurcation','trajectory','tensor',
        'cosmological','negentropic','admissible','convergence','divergence',
        'differential','equation','lagrangian','hamiltonian','metric','geodesic',
        'substrate','attractor','resonance','damping','spectrum','frequency',
        'amplitude','particle','electromagnetic','radiation','thermodynamic',
        'statistical','mechanical','relativistic','singularity','horizon',
        'black hole','dark matter','dark energy','stress-energy','wavefunction',
        'eigenvalue','observable','decoherence','entanglement','renormalization',
        'gauge','action','variational','boundary','constraint','restoration',
        'restorative','bounded','coherence','diffusion','viscosity','turbulence',
        'photon','electron','neutron','proton','boson','fermion',
    ]
    scope_hits = len([t for t in physics_terms if t in lower])
    scores['scope'] = min(5, max(1, (scope_hits + 2) // 3))
    if scores['scope'] >= 4:
        strengths.append('Strong physics content with clear domain relevance.')
    else:
        suggestions.append('Add more domain-specific physics language and framing.')

    claim_terms = [
        'we show','we find','we propose','we introduce','we investigate',
        'we establish','we emphasize','we conjecture','we conclude','we define',
        'we demonstrate','we derive','we analyze','we present','we report',
        'i show','i find','i propose','i introduce','i measured','i observed',
        'this paper','this work','this manuscript','this study',
        'result shows','results show','results demonstrate','results establish',
        'data indicate','data suggest','these results','this establishes',
        'is experimentally validated','hypothesis','our analysis',
        'the model predicts','our results','the findings',
    ]
    claim_hits = len([t for t in claim_terms if t in lower])
    scores['claim'] = min(5, claim_hits + 1)
    if scores['claim'] >= 4:
        strengths.append('Clear identifiable claims and research contributions.')
    else:
        suggestions.append('State the central claim and contribution more explicitly.')

    structure_terms = [
        'introduction','methods','method','results','discussion','conclusion',
        'references','appendix','abstract','theory','formalism','derivation',
        'overview','implications','background','related work','acknowledgment',
        'figure','table','proof','theorem','corollary','lemma',
    ]
    structure_hits = len([t for t in structure_terms if t in lower])
    scores['structure'] = min(5, structure_hits + 1)
    if scores['structure'] >= 4:
        strengths.append('Well-organized with recognizable sections.')
    else:
        suggestions.append('Add clearer structure using sections such as methods, results, and discussion.')

    sentence_count = max(1, len(re.findall(r'[.!?]+', body or '')))
    avg_sentence = wc / sentence_count if sentence_count else wc
    scores['clarity'] = 5 if wc >= 500 and avg_sentence < 35 else 4 if avg_sentence < 38 else 3
    if scores['clarity'] < 4:
        suggestions.append('Shorten dense sentences and tighten exposition.')

    q = 0
    if re.search(r'=', body or '') and re.search(r'\d', body or ''):
        q += 1
    if len(re.findall(r'\d+\.?\d*', body or '')) > 10:
        q += 1
    if re.search(r'\b(cm|m|kg|s|Hz|eV|N|J|W|%|R\^2|std|mean|variance|p-value)\b', body or '', re.I):
        q += 1
    scores['quantitative'] = min(5, max(1, q + 1))
    if scores['quantitative'] >= 3:
        strengths.append('Includes quantitative analysis or numerical results.')
    else:
        suggestions.append('Add equations, numerical values, or quantitative comparisons.')

    citations = 0
    if re.search(r'\[\d+\]', body or ''):
        citations += 2
    if 'references' in lower or 'bibliography' in lower:
        citations += 1
    scores['citations'] = min(5, max(1, citations + 1))
    if scores['citations'] >= 3:
        strengths.append('References prior work appropriately.')
    else:
        suggestions.append('Add a references section or cite prior work more explicitly.')

    if re.search(r'[\w.+-]+@[\w-]+\.[\w.-]+', body or ''):
        scores['anonymity'] = 3
        suggestions.append('Remove personal contact information from the manuscript body.')

    spam_terms = ['buy now', 'click here', 'guaranteed', 'act now']
    spam = any(term in lower for term in spam_terms)
    scores['good_faith'] = 0 if spam else 5 if wc >= 200 else 4 if wc >= 100 else 2 if wc >= 40 else 1
    if scores['good_faith'] >= 4:
        strengths.append('Good-faith research intent is clear.')
    else:
        suggestions.append('Expand the work so the intent and seriousness are clearer.')

    overall = round(sum(scores.values()) / 40 * 100)
    recommendation = 'block' if spam else 'pass' if overall >= 55 and scores['good_faith'] >= 2 else 'return'
    summary = (
        'Ready for community review. Strengths: ' + ' '.join(strengths)
        if recommendation == 'pass'
        else 'Needs development before community review. Suggested improvements: ' + ' '.join(suggestions or ['Expand the manuscript and strengthen the argument.'])
        if recommendation == 'return'
        else 'Not accepted. The submission does not appear to be good-faith research.'
    )
    return {
        'overall_score': overall,
        'recommendation': recommendation,
        'scores': scores,
        'summary': summary,
        'strengths': strengths,
        'suggestions': suggestions,
        'encouragement': 'Your curiosity is valued here.' if recommendation != 'block' else 'We welcome good-faith submissions.',
        'review_engine': 'heuristic',
    }


def generate_duffing(n=5000):
    x = 0.5
    v = 0.0
    t = 0.0
    for _ in range(10000):
        a = -0.25 * v - x * x * x + x + 0.3 * math.cos(t)
        v += a * 0.005
        x += v * 0.005
        t += 0.005
    out = []
    for i in range(n * 20):
        a = -0.25 * v - x * x * x + x + 0.3 * math.cos(t)
        v += a * 0.005
        x += v * 0.005
        t += 0.005
        if i % 20 == 0:
            out.append(x)
    return out[:n]


def generate_lorenz(n=5000):
    x, y, z = 1.0, 1.0, 1.0
    for _ in range(5000):
        dx = 10 * (y - x) * 0.01
        dy = (x * (28 - z) - y) * 0.01
        dz = (x * y - 8/3 * z) * 0.01
        x += dx
        y += dy
        z += dz
    out = []
    for i in range(n * 10):
        dx = 10 * (y - x) * 0.01
        dy = (x * (28 - z) - y) * 0.01
        dz = (x * y - 8/3 * z) * 0.01
        x += dx
        y += dy
        z += dz
        if i % 10 == 0:
            out.append(x)
    return out[:n]


def generate_noise(n=5000):
    return [random.gauss(0, 1) for _ in range(n)]


def parse_series(input_text, file_storage=None, min_points=0):
    chunks = []
    if input_text:
        chunks.append(input_text)
    if file_storage is not None:
        try:
            data = file_storage.read()
            if isinstance(data, bytes):
                chunks.append(data.decode('utf-8', errors='ignore'))
            else:
                chunks.append(str(data))
        except Exception:
            pass
    raw = '\n'.join(chunks).strip()
    if not raw:
        return []

    lines = [line for line in raw.splitlines() if line.strip()]
    if len(lines) >= 2:
        delimiter = '\t' if '\t' in lines[0] else ',' if ',' in lines[0] else None
        if delimiter:
            rows = [next(csv.reader([line], delimiter=delimiter)) for line in lines]
            width = max(len(row) for row in rows)
            columns = []
            for ci in range(width):
                vals = []
                for row in rows:
                    if ci < len(row):
                        try:
                            vals.append(float(str(row[ci]).strip()))
                        except Exception:
                            pass
                columns.append(vals)
            best = max(columns, key=len) if columns else []
            if len(best) >= min_points:
                return best

    nums = []
    for token in re.split(r'[\s,;]+', raw):
        token = token.strip()
        if not token:
            continue
        try:
            nums.append(float(token))
        except Exception:
            continue
    return nums


def ocm_analysis(series):
    n = len(series)
    wz = 200
    wb = 500
    eps = 1e-8
    if n < wb + 10:
        return {'error': 'Need at least 600 numeric values for OCM analysis.'}
    z = [0.0] * n
    for t in range(wz, n):
        window = series[t-wz:t]
        mu = sum(window) / wz
        var = sum((x - mu) ** 2 for x in window) / wz
        z[t] = (series[t] - mu) / (math.sqrt(var) + eps)
    b2 = [0.0] * n
    for t in range(wb, n):
        win = z[t-wb:t]
        b2[t] = sum(win) / wb
    s = max(wz, wb)
    diffs = [abs(z[i] - b2[i]) for i in range(n)]
    ind = [1 if diffs[t+1] < diffs[t] else 0 for t in range(s, n-1)]
    cr = sum(ind) / max(1, len(ind))
    zstat = (cr - 0.5) / math.sqrt(0.25 / max(1, len(ind)))
    cls = 'DRIVEN-DISSIPATIVE' if cr > 0.50522 else 'AUTONOMOUS CHAOTIC' if cr < 0.495 else 'STOCHASTIC'
    # Sample series for frontend SparkLine (every Nth point, max 200)
    step = max(1, n // 200)
    z_sample = [round(z[i], 4) for i in range(s, n, step)][:200]
    d_sample = [round(diffs[i], 4) for i in range(s, n, step)][:200]
    return {
        'summary': 'Contraction-rate analysis completed successfully.',
        'classification': cls,
        'details': {
            'contraction_rate': round(cr, 6),
            'z_statistic': round(zstat, 2),
            'threshold': 0.50522,
            'points': n,
            'series_preview': series[:300],
            'baseline_preview': b2[:300],
            'z_series': z_sample,
            'distance_series': d_sample,
        },
    }


def er_analysis(series):
    n = len(series)
    wz = 100
    stride = 50
    threshold = 0.5
    eps = 1e-8
    if n < wz * 3:
        return {'error': 'Need at least 300 numeric values for topology mapping.'}
    nodes = list(range(wz, n - wz, stride))
    segs = []
    for t in nodes:
        segment = series[t - wz // 2:t + wz // 2]
        mu = sum(segment) / len(segment)
        var = sum((x - mu) ** 2 for x in segment) / len(segment)
        std = math.sqrt(var) + eps
        segs.append([(x - mu) / std for x in segment])
    edges = 0
    bridges = 0
    max_gap = 0
    for i in range(len(segs)):
        for j in range(i + 1, len(segs)):
            m = min(len(segs[i]), len(segs[j]))
            dot = sum(segs[i][k] * segs[j][k] for k in range(m)) / max(1, m)
            if abs(dot) >= threshold:
                edges += 1
                gap = abs(nodes[j] - nodes[i])
                if gap > 500:
                    bridges += 1
                    max_gap = max(max_gap, gap)
    density = edges / max(1, len(nodes) * (len(nodes) - 1) / 2)
    topology = 'TRIVIAL' if len(nodes) < 4 else 'BRIDGED' if bridges > 0 else 'CONNECTED' if edges > len(nodes) else 'FRAGMENTED'
    adjacent = []
    for i in range(len(segs) - 1):
        m = min(len(segs[i]), len(segs[i+1]))
        adjacent.append(sum(segs[i][k] * segs[i+1][k] for k in range(m)) / max(1, m))
    min_coh = min(adjacent) if adjacent else 0.0
    mean_coh = sum(adjacent) / len(adjacent) if adjacent else 0.0
    return {
        'summary': 'Topology mapping completed.',
        'classification': topology,
        'details': {
            'nodes': len(nodes),
            'edges': edges,
            'bridges': bridges,
            'density': round(density, 4),
            'min_coherence': round(min_coh, 3),
            'mean_coherence': round(mean_coh, 3),
            'max_bridge_gap': max_gap,
            'series_preview': series[:300],
            'coherence_series': [round(c, 4) for c in adj][:200] if adj else [],
        },
    }


def icm_analysis(series):
    n = len(series)
    wz = 200
    eps = 1e-8
    if n < wz * 3:
        return {'error': 'Need at least 600 numeric values for invariant detection.'}
    z = [0.0] * n
    for t in range(wz, n):
        window = series[t-wz:t]
        mu = sum(window) / wz
        var = sum((x - mu) ** 2 for x in window) / wz
        z[t] = (series[t] - mu) / (math.sqrt(var) + eps)
    events = []
    in_event = False
    onset = peak = 0
    pval = 0.0
    for t in range(wz, n):
        az = abs(z[t])
        if not in_event and az > 2:
            in_event = True
            onset = peak = t
            pval = az
        elif in_event:
            if az > pval:
                peak = t
                pval = az
            if az < 1.4:
                if t - onset >= 10:
                    rec_time = t - peak
                    asym = (peak - onset) / max(1, t - peak)
                    events.append({'amp': pval, 'recTime': rec_time, 'asym': asym})
                in_event = False
    if not events:
        return {
            'summary': 'No significant disruption events were detected.',
            'classification': 'INADMISSIBLE',
            'details': {
                'events': 0,
                'morphology': 'NO_EVENTS',
                'regime': 'SUBCRITICAL',
                'mean_amplitude': 0.0,
                'mean_recovery': 0.0,
                'resonance_R': 0.0,
                'gates': {'A1': False, 'A2': False, 'A3': False, 'A4': False},
                'series_preview': series[:300],
            },
        }
    amps = [e['amp'] for e in events]
    recs = [e['recTime'] for e in events]
    asyms = [e['asym'] for e in events]
    mean_amp = sum(amps) / len(amps)
    mean_rec = sum(recs) / len(recs)
    mean_asym = sum(asyms) / len(asyms)
    std_amp = math.sqrt(sum((x - mean_amp) ** 2 for x in amps) / len(amps))
    amp_cv = std_amp / mean_amp if mean_amp > 0 else 0
    dx = [series[i] - series[i-1] for i in range(1, n)]
    mu = sum(dx) / len(dx)
    centered = [x - mu for x in dx]
    c0 = sum(x * x for x in centered) / max(1, len(centered))
    c1 = sum(centered[i] * centered[i+1] for i in range(len(centered)-1)) / max(1, len(centered))
    rho1 = c1 / c0 if c0 > 1e-12 else 0.0
    damping = -math.log(abs(rho1)) if 1e-12 < abs(rho1) < 1 else 0.0
    forcing = len(events) / n
    resonance_r = damping * forcing
    regime = 'SUBCRITICAL' if resonance_r < 1e-10 else 'WEAK' if resonance_r < 0.001 else 'RESONANT' if resonance_r < 0.05 else 'SATURATED'
    morphology = 'SHARP_ONSET' if mean_asym > 3 else 'GRADUAL_ONSET' if mean_asym < 0.3 else 'SYMMETRIC'
    gates = {
        'A1': len(events) > 0,
        'A2': regime in {'WEAK', 'RESONANT'},
        'A3': mean_rec > 0 and mean_rec < n / 4,
        'A4': amp_cv < 3,
    }
    cls = 'ADMISSIBLE' if all(gates.values()) else 'INADMISSIBLE'
    return {
        'summary': 'Disruption morphology and admissibility analysis completed.',
        'classification': cls,
        'details': {
            'events': len(events),
            'morphology': morphology,
            'regime': regime,
            'mean_amplitude': round(mean_amp, 3),
            'mean_recovery': round(mean_rec, 1),
            'mean_asymmetry': round(mean_asym, 3),
            'resonance_R': round(resonance_r, 8),
            'amp_cv': round(amp_cv, 3),
            'gates': gates,
            'series_preview': series[:300],
        },
    }


def clm_analysis(series):
    k = 7
    n = 64
    diff = 3
    def mk_ca(seed):
        state = [0] * n
        state[abs(seed) % n] = 1
        return state

    ca_states = [mk_ca(1337 + 101 * i) for i in range(k)]
    psi = [[0.0] * n for _ in range(k)]
    for i in range(k):
        psi[i][(1337 + 13 * i) % n] = 0.01
    kw = 1.0
    dp = 1e-6
    ri = 0.0
    hist_kw = []
    hist_ge = []
    if not series or len(series) < 10:
        return {'error': 'Need at least 10 numeric values for CLM simulation. Please provide a time series or a CSV with numbers.'}
    mn = min(series)
    mx = max(series)
    rg = (mx - mn) or 1.0
    norm = [((v - mn) / rg) * 2 - 1 for v in series]
    steps = 96
    last_g = [0.0] * n
    for step in range(steps):
        forcing = []
        for i in range(k):
            row = []
            for j in range(n):
                idx = (step * n + j + i * 137) % len(norm)
                row.append(norm[idx])
            forcing.append(row)
        g = []
        for j in range(n):
            g.append(sum(psi[i][j] for i in range(k)) / k)
        next_psi = [[0.0] * n for _ in range(k)]
        fg = 0.02 * diff if step < 48 else 0.007
        for i in range(k):
            for j in range(n):
                x = psi[i][j]
                x2 = 0.88 * x + 0.35 * (g[j] - x) + 0.07 * (g[j] - x) + fg * forcing[i][j]
                ax = abs(x)
                if ax > 1.6:
                    x2 += max(-0.25, min(0.25, -0.14 * (ax - 1.6) * (x / max(1e-9, ax))))
                next_psi[i][j] = 2.25 * math.tanh(x2 / 2.25)
        es = 0.0
        for i in range(k):
            for j in range(n):
                es += abs(next_psi[i][j] - g[j])
        ge = es / (k * n)
        df = max(1e-4, 0.035)
        kw = max(0.0, min(10.0, 0.75 * kw + 0.25 * ((ge + df) / (max(dp, 0.0) + df))))
        stab = max(0.0, min(1.0, (1 - max(0.0, kw - 1)) * (1 - ge / 0.28)))
        ri = ri + max(0.0, dp - ge)
        dp = ge
        psi = next_psi
        last_g = g
        hist_kw.append(kw)
        hist_ge.append(ge)
    flat = [x for row in psi for x in row]
    prms = math.sqrt(sum(x * x for x in flat) / max(1, len(flat)))
    cls = 'ELIGIBLE' if stab >= 0.5 and ge < 0.14 else 'SURGE' if kw > 1.25 else 'DRIFT'
    return {
        'summary': 'Coherence field simulation completed.',
        'classification': cls,
        'details': {
            'kappa_w': round(kw, 4),
            'glue_error': round(ge, 4),
            'stability': round(stab, 4),
            'prms': round(prms, 4),
            'ri': round(ri, 4),
            'g_preview': last_g,
            'hist_kw': [round(x, 4) for x in hist_kw],
            'hist_ge': [round(x, 4) for x in hist_ge],
        },
    }


# --- Routes ---
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


# --- Auth ---
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
    orcid = (d.get("orcid") or "").strip()
    if display_name:
        current_user.display_name = display_name[:120]
    current_user.bio = bio[:2000]
    if orcid:
        current_user.orcid = re.sub(r"[^0-9Xx-]", "", orcid)[:32]
    else:
        current_user.orcid = ""
    db.session.commit()
    return jsonify({"ok": True, "user": current_user.to_dict()})


@app.route("/api/auth/avatar", methods=["POST"])
@api_login_required
def upload_avatar():
    if "avatar" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    file = request.files["avatar"]
    if file.filename == "":
        return jsonify({"error": "Empty filename"}), 400
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in {".png", ".jpg", ".jpeg", ".gif"}:
        return jsonify({"error": "Only PNG, JPG, JPEG, GIF allowed"}), 400
    new_name = f"{uuid.uuid4().hex}{ext}"
    file_path = AVATAR_FOLDER / new_name
    try:
        file.save(str(file_path))
    except Exception as e:
        return jsonify({"error": f"Failed to save: {e}"}), 500
    if current_user.avatar_filename:
        old_path = AVATAR_FOLDER / current_user.avatar_filename
        if old_path.exists():
            old_path.unlink()
    current_user.avatar_filename = new_name
    db.session.commit()
    return jsonify({"ok": True, "avatar_filename": new_name})


# --- Categories + feed ---
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
    d = request_data()
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
        Submission.status.in_(DISCOVERY_STATUSES),
        Submission.is_draft.is_(False)
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
    p = Submission.query.filter_by(status="published", is_draft=False).order_by(Submission.published_at.desc()).paginate(page=1, per_page=20, error_out=False)
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


# --- Builder endpoints ---
@app.route("/api/builder/drafts")
@api_login_required
def builder_drafts():
    drafts = Submission.query.filter_by(author_id=current_user.id).filter(Submission.status.in_(BUILDER_STATUSES)).order_by(Submission.updated_at.desc()).all()
    return jsonify({"papers": [s.to_card(current_user.id, full_abstract=True) for s in drafts]})


@app.route("/api/submissions", methods=["POST"])
@api_login_required
def create_submission():
    try:
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
                "is_draft": parse_bool(d.get("is_draft", "true")),
            }
        else:
            d = request.get_json(silent=True) or {}
            payload = {
                "title": (d.get("title") or "").strip(),
                "abstract": (d.get("abstract") or "").strip(),
                "body_text": (d.get("body_text") or "").strip(),
                "tags": (d.get("tags") or "").strip(),
                "category_id": category_from_payload(d),
                "is_draft": parse_bool(d.get("is_draft", "true")),
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
            is_draft=payload["is_draft"],
            status="draft" if payload["is_draft"] else "submitted",
        )
        db.session.add(sub)
        desk = None
        if not payload["is_draft"]:
            desk = run_desk_review(sub.title, sub.abstract, sub.body_text)
            db.session.add(DeskDecision(
                submission_id=sub.id,
                decision=desk["recommendation"],
                overall_score=desk["overall_score"],
                summary=desk["summary"],
                encouragement=desk["encouragement"],
                scores_json=json.dumps(desk["scores"]),
            ))
        db.session.commit()
        return jsonify({"submission": sub.to_card(current_user.id, full_abstract=True), "desk_review": desk}), 201
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Submission creation failed")
        return jsonify({"error": "Submission failed", "detail": str(e)}), 500


@app.route("/api/submissions/<bid>", methods=["GET", "PUT", "DELETE"])
@api_login_required
def submission_detail_edit_delete(bid):
    sub = resolve_submission(bid)
    if request.method == "GET":
        data = sub.to_card(current_user.id, full_abstract=True)
        data["comments"] = [
            {
                "id": c.id,
                "author": c.author.to_dict() if c.author else None,
                "comment_type": c.comment_type,
                "body": c.body,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in Comment.query.filter_by(submission_id=sub.id).order_by(Comment.created_at.asc()).all()
        ]
        return jsonify({"submission": data})

    owner_or_admin = current_user.role == "admin" or current_user.id == sub.author_id
    if not owner_or_admin:
        return jsonify({"error": "Not allowed"}), 403

    if request.method == "DELETE":
        try:
            Like.query.filter_by(submission_id=sub.id).delete()
            Bookmark.query.filter_by(submission_id=sub.id).delete()
            Comment.query.filter_by(submission_id=sub.id).delete()
            Review.query.filter_by(submission_id=sub.id).delete()
            DeskDecision.query.filter_by(submission_id=sub.id).delete()
            db.session.delete(sub)
            db.session.commit()
            return jsonify({"ok": True})
        except Exception as e:
            db.session.rollback()
            return jsonify({"error": "Delete failed", "detail": str(e)}), 500

    data = request_data()
    # --- DEBUG: Print the received data to server logs ---
    print(f"[DEBUG] PUT data for {bid}: {json.dumps(data, default=str)[:500]}")
    try:
        if "title" in data:
            sub.title = (data.get("title") or "").strip() or sub.title
        if "abstract" in data:
            sub.abstract = (data.get("abstract") or "").strip()
        if "body_text" in data or "body" in data:
            sub.body_text = (data.get("body_text") or data.get("body") or "").strip()
            print(f"[DEBUG] body_text set to: {sub.body_text[:200] if sub.body_text else 'EMPTY'}")
        if "tags" in data:
            sub.tags = (data.get("tags") or "").strip()
        if "category_id" in data or "category" in data:
            try:
                sub.category_id = int(data.get("category_id") or data.get("category") or sub.category_id)
            except Exception:
                pass
        if "is_draft" in data:
            sub.is_draft = parse_bool(data.get("is_draft"))
            if sub.is_draft:
                sub.status = "draft"
        if "submit_for_review" in data and parse_bool(data.get("submit_for_review")):
            # --- FIX: Ensure body_text is not empty before validation ---
            # If the body_text is empty (or only whitespace), copy the abstract.
            if not sub.body_text or not sub.body_text.strip():
                sub.body_text = sub.abstract
                print(f"[DEBUG] body_text was empty, copied abstract")
            # Now validate
            if not sub.title or not (sub.abstract or "").strip() or not (sub.body_text or "").strip():
                return jsonify({"error": "Title, abstract, and full paper text are required for review."}), 400
            sub.is_draft = False
            sub.status = "submitted"
            desk = run_desk_review(sub.title or "", sub.abstract or "", sub.body_text or "")
            db.session.add(DeskDecision(
                submission_id=sub.id,
                decision=desk["recommendation"],
                overall_score=desk["overall_score"],
                summary=desk["summary"],
                encouragement=desk["encouragement"],
                scores_json=json.dumps(desk["scores"]),
            ))
        db.session.commit()
        return jsonify({"submission": sub.to_card(current_user.id, full_abstract=True), "ok": True})
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Update failed")
        return jsonify({"error": "Update failed", "detail": str(e)}), 500


@app.route("/api/submissions/<bid>/public")
def get_public_submission(bid):
    sub = resolve_submission(bid)
    if sub.is_draft or sub.status not in {"in_discovery", "under_review", "published"}:
        return jsonify({"error": "Not publicly available"}), 404
    data = sub.to_card(current_user.id if current_user.is_authenticated else None, full_abstract=True)
    data["comments"] = [
        {
            "id": c.id,
            "author": c.author.to_dict() if c.author else None,
            "comment_type": c.comment_type,
            "body": c.body,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in Comment.query.filter_by(submission_id=sub.id).order_by(Comment.created_at.asc()).all()
    ]
    return jsonify({"submission": data})


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
    comment_type = d.get("comment_type", "note").strip()[:30]
    c = Comment(
        submission_id=s.id,
        author_id=current_user.id,
        comment_type=comment_type,
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


# --- Users ---
@app.route("/api/users/<int:uid>")
def get_user(uid):
    u = User.query.get_or_404(uid)
    d = u.to_dict()
    d["papers"] = [
        s.to_card(current_user.id if current_user.is_authenticated else None)
        for s in Submission.query.filter_by(author_id=uid).filter(
            Submission.status.in_(["in_discovery", "under_review", "published", "desk_returned"]),
            Submission.is_draft.is_(False)
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


# --- Notifications ---
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


@app.route("/api/notifications/read", methods=["POST"])
@api_login_required
def notification_read():
    data = request_data()
    nid = data.get("id")
    if nid:
        note = Notification.query.filter_by(id=nid, user_id=current_user.id).first_or_404()
        note.is_read = True
    else:
        Notification.query.filter_by(user_id=current_user.id, is_read=False).update({"is_read": True})
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/stats")
def stats():
    return jsonify({
        "published_count": Submission.query.filter_by(status="published", is_draft=False).count(),
        "discovery_count": Submission.query.filter(Submission.status.in_(DISCOVERY_STATUSES), Submission.is_draft.is_(False)).count(),
        "user_count": User.query.count(),
    })


@app.route("/api/system/capabilities")
def system_capabilities():
    return jsonify(journal_capabilities_payload())


# --- Tools API ---
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


def is_plain_text_file(file_storage):
    if not file_storage:
        return True
    filename = file_storage.filename or ""
    allowed_extensions = (".txt", ".tex", ".csv", ".md", ".py", ".js", ".html", ".css", ".json")
    if filename.lower().endswith(allowed_extensions):
        return True
    try:
        file_storage.seek(0)
        sample = file_storage.read(1024)
        file_storage.seek(0)
        text_chars = bytes(range(9, 14)) + bytes(range(32, 127))
        if isinstance(sample, bytes):
            if any(b not in text_chars for b in sample[:256]):
                return False
        else:
            pass
    except Exception:
        return False
    return True


@app.route("/api/tools/run", methods=["POST"])
@api_login_required
def tools_run():
    try:
        tool, text, series = get_tool_input()
        if not tool:
            return jsonify({"error": "Tool not provided"}), 400

        if tool == "desk_review":
            file_uploaded = request.files.get("file")
            if file_uploaded and not is_plain_text_file(file_uploaded):
                return jsonify({"error": "File type not supported. Please upload plain text files (TXT, TEX, CSV, MD) or paste the text directly."}), 400

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
            if len(series) < 600:
                return jsonify({"error": "Need at least 600 numeric values for OCM analysis."}), 400
            return jsonify(ocm_analysis(series))

        if tool == "er":
            if len(series) < 300:
                return jsonify({"error": "Need at least 300 numeric values for topology mapping."}), 400
            return jsonify(er_analysis(series))

        if tool == "icm":
            if len(series) < 600:
                return jsonify({"error": "Need at least 600 numeric values for invariant detection."}), 400
            return jsonify(icm_analysis(series))

        if tool == "clm":
            if len(series) < 10:
                return jsonify({"error": "Need at least 10 numeric values for CLM simulation."}), 400
            return jsonify(clm_analysis(series))

        return jsonify({"error": "Unknown tool"}), 400
    except Exception as e:
        app.logger.exception("Tool execution failed")
        return jsonify({"error": "Tool execution failed", "detail": str(e)}), 500


# --- Admin ---
@app.route("/api/admin/submissions")
@admin_required
def admin_submissions():
    scope = (request.args.get("scope") or "queue").strip().lower()
    q = Submission.query
    if scope == "queue":
        q = q.filter(Submission.status.in_(ADMIN_QUEUE_STATUSES))
    elif scope == "in_review":
        q = q.filter(Submission.status.in_(["under_review", "in_discovery"]))
    elif scope == "published":
        q = q.filter(Submission.status.in_(["published"]))
    elif scope == "all":
        q = q.filter(Submission.is_draft.is_(False))
    else:
        q = q.filter(Submission.status.in_(ADMIN_QUEUE_STATUSES))
    items = q.order_by(Submission.updated_at.desc()).limit(200).all()
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
    try:
        s.status = status
        if status == "published" and not s.published_at:
            s.published_at = datetime.utcnow()
        if status in ("submitted", "in_discovery", "under_review", "published"):
            s.is_draft = False
        add_editorial_comment(s.id, current_user, status)
        db.session.commit()
        if s.author_id:
            create_notification(s.author_id, f"Paper status updated: {Submission.STATUS_LABELS.get(status, status)}", s.title)
            db.session.commit()
        return jsonify({"ok": True})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "Status update failed", "detail": str(e)}), 500


@app.route("/api/admin/submissions/<int:sid>", methods=["DELETE"])
@admin_required
def admin_delete_submission(sid):
    s = Submission.query.get_or_404(sid)
    try:
        Like.query.filter_by(submission_id=s.id).delete()
        Bookmark.query.filter_by(submission_id=s.id).delete()
        Comment.query.filter_by(submission_id=s.id).delete()
        Review.query.filter_by(submission_id=s.id).delete()
        DeskDecision.query.filter_by(submission_id=s.id).delete()
        db.session.delete(s)
        db.session.commit()
        return jsonify({"ok": True})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "Delete failed", "detail": str(e)}), 500


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
    try:
        u.role = role
        db.session.commit()
        return jsonify({"ok": True})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "Role update failed", "detail": str(e)}), 500


@app.route("/api/admin/users/<int:uid>/ban", methods=["POST"])
@admin_required
def admin_user_ban(uid):
    if current_user.id == uid:
        return jsonify({"error": "Cannot ban yourself"}), 400
    u = User.query.get_or_404(uid)
    try:
        u.is_banned = True
        db.session.commit()
        return jsonify({"ok": True})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "Ban failed", "detail": str(e)}), 500


@app.route("/api/admin/users/<int:uid>/unban", methods=["POST"])
@admin_required
def admin_user_unban(uid):
    u = User.query.get_or_404(uid)
    try:
        u.is_banned = False
        db.session.commit()
        return jsonify({"ok": True})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "Unban failed", "detail": str(e)}), 500


@app.route("/api/admin/users/<int:uid>", methods=["DELETE"])
@admin_required
def admin_user_delete(uid):
    if current_user.id == uid:
        return jsonify({"error": "Cannot delete yourself"}), 400

    u = User.query.get_or_404(uid)
    try:
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
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "User deletion failed", "detail": str(e)}), 500


@app.route("/api/admin/reset-db", methods=["POST"])
@admin_required
def admin_reset_db():
    admin_ids = [u.id for u in User.query.filter_by(role="admin").all()]

    try:
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
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "Reset failed", "detail": str(e)}), 500


# --- Seed + boot ---
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
    if not User.query.filter_by(email="admin@journal.local").first():
        admin = User(email="admin@journal.local", display_name="Founding Editor", role="admin")
        admin.set_password("change-me-now")
        db.session.add(admin)
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
            migrate_schema()
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


if __name__ == "__main__":
    init_db()
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.getenv("FLASK_DEBUG", "0") == "1")
