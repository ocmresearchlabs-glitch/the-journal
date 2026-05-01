# server.py
# OpenField - Deployment Server
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
    pdf_data = db.Column(db.LargeBinary)
    pdf_filename = db.Column(db.String(255))
    status = db.Column(db.String(30), default="draft", index=True)
    is_draft = db.Column(db.Boolean, default=True, index=True)
    tags = db.Column(db.Text, default="")
    author_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    category_id = db.Column(db.Integer, db.ForeignKey("category.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    published_at = db.Column(db.DateTime)
    openfield_id = db.Column(db.String(20), unique=True)
    package_meta = db.Column(db.Text)  # JSON: figures, data_files, code_files from zip uploads

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
            "has_pdf": self.pdf_data is not None and len(self.pdf_data or b"") > 0,
            "openfield_id": self.openfield_id or "",
            "package_meta": json.loads(self.package_meta) if self.package_meta else None,
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
    try:
        ensure_column("submission", "openfield_id", "ALTER TABLE submission ADD COLUMN openfield_id VARCHAR(20)")
    except Exception:
        pass
    try:
        ensure_column("submission", "pdf_data", "ALTER TABLE submission ADD COLUMN pdf_data BLOB")
    except Exception:
        pass
    try:
        ensure_column("submission", "pdf_filename", "ALTER TABLE submission ADD COLUMN pdf_filename VARCHAR(255)")
    except Exception:
        pass
    try:
        ensure_column("submission", "package_meta", "ALTER TABLE submission ADD COLUMN package_meta TEXT")
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


def generate_openfield_id():
    """Generate a unique OpenField ID: OF-YYYY-MMDD-NNN"""
    now = datetime.utcnow()
    date_str = now.strftime("%Y-%m%d")
    # Count how many papers were published today
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    count = Submission.query.filter(
        Submission.published_at >= today_start,
        Submission.openfield_id.isnot(None),
    ).count()
    seq = count + 1
    return f"OF-{date_str}-{seq:03d}"


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

def strip_latex(text):
    """Strip LaTeX markup to produce cleaner text for AI review."""
    if not text or '\\' not in text:
        return text
    import re as _re
    t = text
    # Remove comments
    t = _re.sub(r'%.*$', '', t, flags=_re.MULTILINE)
    # Remove document class, usepackage, etc.
    t = _re.sub(r'\\(documentclass|usepackage|geometry|setlength|pagestyle|newcommand|renewcommand|bibliographystyle|graphicspath|hypersetup)\{[^}]*\}(\{[^}]*\})*(\[[^\]]*\])*', '', t)
    t = _re.sub(r'\\(documentclass|usepackage)\[[^\]]*\]\{[^}]*\}', '', t)
    # Remove begin/end document
    t = _re.sub(r'\\(begin|end)\{document\}', '', t)
    # Convert sections to plain headers
    t = _re.sub(r'\\section\*?\{([^}]*)\}', r'\n\n\1\n', t)
    t = _re.sub(r'\\subsection\*?\{([^}]*)\}', r'\n\n\1\n', t)
    t = _re.sub(r'\\subsubsection\*?\{([^}]*)\}', r'\n\1\n', t)
    # Convert text formatting
    t = _re.sub(r'\\textbf\{([^}]*)\}', r'\1', t)
    t = _re.sub(r'\\textit\{([^}]*)\}', r'\1', t)
    t = _re.sub(r'\\emph\{([^}]*)\}', r'\1', t)
    t = _re.sub(r'\\text\{([^}]*)\}', r'\1', t)
    t = _re.sub(r'\\textrm\{([^}]*)\}', r'\1', t)
    t = _re.sub(r'\\texttt\{([^}]*)\}', r'\1', t)
    # Convert math delimiters
    t = _re.sub(r'\\\[', ' ', t)
    t = _re.sub(r'\\\]', ' ', t)
    # Remove label, ref, cite formatting but keep content
    t = _re.sub(r'\\label\{[^}]*\}', '', t)
    t = _re.sub(r'\\ref\{([^}]*)\}', r'\1', t)
    t = _re.sub(r'\\cite\{([^}]*)\}', r'[\1]', t)
    t = _re.sub(r'\\eqref\{([^}]*)\}', r'Eq.(\1)', t)
    # Convert lists
    t = _re.sub(r'\\begin\{(enumerate|itemize)\}(\[[^\]]*\])*', '', t)
    t = _re.sub(r'\\end\{(enumerate|itemize)\}', '', t)
    t = _re.sub(r'\\item\s*', '- ', t)
    # Convert environments to labeled blocks
    for env in ['theorem', 'proposition', 'lemma', 'corollary', 'definition', 'proof', 'remark', 'equation', 'align', 'figure', 'table', 'abstract']:
        t = _re.sub(r'\\begin\{' + env + r'\*?\}(\[[^\]]*\])?', env.capitalize() + ':', t)
        t = _re.sub(r'\\end\{' + env + r'\*?\}', '', t)
    # Remove remaining begin/end
    t = _re.sub(r'\\begin\{[^}]*\}(\[[^\]]*\])?', '', t)
    t = _re.sub(r'\\end\{[^}]*\}', '', t)
    # Clean up common commands
    t = _re.sub(r'\\(hspace|vspace|noindent|medskip|bigskip|smallskip|newpage|clearpage|maketitle)\*?(\{[^}]*\})?', '', t)
    t = _re.sub(r'\\(centering|raggedright|raggedleft)', '', t)
    # Keep math content but remove \frac, \sqrt etc wrappers in a readable way
    t = _re.sub(r'\\frac\{([^}]*)\}\{([^}]*)\}', r'(\1)/(\2)', t)
    t = _re.sub(r'\\sqrt\{([^}]*)\}', r'sqrt(\1)', t)
    # Remove remaining backslash commands that aren't math symbols
    t = _re.sub(r'\\(left|right|big|Big|bigg|Bigg)[.|(|)|\\{|\\}|\[|\]]?', '', t)
    # Clean up whitespace
    t = _re.sub(r'\n{3,}', '\n\n', t)
    t = _re.sub(r'[ \t]+', ' ', t)
    return t.strip()


_claude_call_times = []
CLAUDE_RATE_LIMIT = 20  # max calls per hour


def run_claude_review(title, abstract, body):
    """Call Claude API for intelligent paper review. Returns None on failure."""
    global _claude_call_times
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("[CLAUDE] No ANTHROPIC_API_KEY found in environment")
        return None
    # Rate limiting: max N calls per hour
    now = datetime.utcnow()
    _claude_call_times = [t for t in _claude_call_times if (now - t).total_seconds() < 3600]
    if len(_claude_call_times) >= CLAUDE_RATE_LIMIT:
        print(f"[CLAUDE] Rate limited: {len(_claude_call_times)} calls in last hour (max {CLAUDE_RATE_LIMIT})")
        return None
    _claude_call_times.append(now)
    print(f"[CLAUDE] API key found, length={len(api_key)}, starts with: {api_key[:12]}...")
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        # Smart truncation: send up to 80K chars, always include references
        MAX_BODY = 80000
        # Strip LaTeX markup for cleaner review
        clean_body = strip_latex(body)
        if len(clean_body) <= MAX_BODY:
            body_to_send = clean_body
        else:
            # Find references section
            ref_idx = -1
            for marker in ["References", "REFERENCES", "Bibliography", "BIBLIOGRAPHY"]:
                idx = clean_body.rfind(marker)
                if idx > 0:
                    ref_idx = idx
                    break
            if ref_idx > 0:
                refs_section = clean_body[ref_idx:][:8000]
                remaining = MAX_BODY - len(refs_section) - 200
                body_to_send = clean_body[:remaining] + "\n\n[...truncated...]\n\n" + refs_section
            else:
                body_to_send = clean_body[:MAX_BODY]
        text_to_review = f"TITLE: {title}\n\nABSTRACT: {abstract}\n\nPAPER:\n{body_to_send}"
        print(f"[CLAUDE] Sending {len(text_to_review)} chars for review (body was {len(body)} chars)")
        print(f"[CLAUDE] Calling API with {len(text_to_review)} chars of text...")
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            system="""You are a rigorous but fair desk reviewer for OpenField, an independent open-access science journal. You evaluate manuscripts for scientific readiness. This journal welcomes independent researchers -- evaluate the science, not credentials or institutional affiliation. Standards are high but not exclusionary: roughly 35% of submissions should be returned for revision.

IMPORTANT CONTEXT:
- OpenField is NOT a blind review journal. Author names, affiliations, and ORCIDs on the title page are EXPECTED and should not be penalized.
- Papers may include LaTeX markup, code files, data files, and supplementary material. Evaluate the scientific content, not the formatting.
- If the submission includes code, data tables, or supplementary files, credit these as evidence of reproducibility and rigor.
- Evaluate the FULL paper including appendices, data tables, and references at the end.

CALIBRATION RULES -- follow these strictly:
- Score 5/5: Exceptional. Publication-ready in this criterion.
- Score 4/5: Strong. Minor issues only. Meets the standard of a good journal.
- Score 3/5: Adequate. Competent but has clear gaps. This is the baseline for acceptable work.
- Score 2/5: Weak. Significant issues that must be addressed.
- Score 1/5: Missing or fundamentally inadequate.

Score on these 8 criteria (1-5 each):
- scope: Does it address a genuine scientific question with appropriate depth? A paper that spans multiple scientific domains with empirical validation across them demonstrates EXCEPTIONAL scope (4-5), not narrow scope. Interdisciplinary work connecting formal mathematics to multiple empirical domains is ambitious and should be scored generously. Only score 1-2 if the paper lacks a coherent scientific question or addresses a trivial topic.
- claim: Are there clear, testable claims supported by the analysis? A paper with formal propositions, proofs, and explicit claim hierarchies scores high. Vague gesturing scores low.
- structure: Is it organized with logical flow? Standard sections (intro, methods, results, discussion, conclusion, references) score high. Papers with formal definitions, propositions, and proofs have excellent structure.
- clarity: Is the writing precise and readable? Technical density is expected in physics -- do not penalize specialized terminology if it is defined. Score based on whether a domain expert could follow the argument.
- quantitative: Does it include rigorous math, data analysis, error bounds, statistical testing, or numerical validation? Papers with confidence intervals, surrogate tests, calibration against controls, and formal proofs score 4-5.
- citations: Does it engage with prior literature? Count the actual references listed. STRICT RULE: 5 or more substantive references that engage with relevant literature MUST score 3 or higher. 10+ substantive references MUST score 4 or higher. Do NOT score 2 for a paper with 5+ real references -- that violates this calibration rule. Score 1-2 only if fewer than 5 references or references are superficial/irrelevant.
- reproducibility: Does the submission include or reference reproducible methods? Code, data, parameter specifications, or detailed methodology that allows verification scores high. This replaces the anonymity criterion.
- good_faith: Is this genuine, effortful research? (0=spam, 3=minimal effort, 5=substantial investigation with months of work evident)

BE SPECIFIC in suggestions -- name exactly what is missing or weak. Do not just say "add more detail." Say what detail and where.

Recommendation guidelines:
- "pass": Overall >= 65% AND no criterion below 2. Ready for community discovery.
- "return": Overall 40-64% OR any criterion at 1. Encourage revision with specific guidance.
- "block": Spam or not research.

Respond ONLY with valid JSON in this exact format, no other text:
{"scores":{"scope":N,"claim":N,"structure":N,"clarity":N,"quantitative":N,"citations":N,"reproducibility":N,"good_faith":N},"strengths":["...","..."],"suggestions":["...","..."],"recommendation":"pass or return or block","summary":"One paragraph assessment"}""",
            messages=[{"role": "user", "content": text_to_review}],
        )
        raw = message.content[0].text.strip()
        print(f"[CLAUDE] Got response, {len(raw)} chars")
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
        # ETCE meta-layer: treat score vector as a trajectory point
        score_vec = [scores.get(k, 3) for k in ["scope", "claim", "structure", "clarity", "quantitative", "citations", "reproducibility", "good_faith"]]
        etce_meta = None
        try:
            # Create a simple trajectory from the scores: intro-like, methods-like, results-like, conclusion-like
            # by simulating how scores "develop" across the paper
            vecs = [
                [score_vec[0], score_vec[2], score_vec[7], score_vec[3]],  # intro: scope, structure, good_faith, clarity
                [score_vec[4], score_vec[0], score_vec[5], score_vec[2]],  # methods: quantitative, scope, citations, structure
                [score_vec[4], score_vec[1], score_vec[3], score_vec[5]],  # results: quantitative, claim, clarity, citations
                [score_vec[1], score_vec[3], score_vec[0], score_vec[7]],  # discussion: claim, clarity, scope, good_faith
            ]
            # Compute simple trajectory metrics
            exp_vol = sum(math.sqrt(sum((a - b) ** 2 for a, b in zip(vecs[i], vecs[i + 1]))) for i in range(len(vecs) - 1))
            con_d = math.sqrt(sum((a - b) ** 2 for a, b in zip(vecs[-2], vecs[-1])))
            R = exp_vol / (con_d + 0.01)
            D = math.sqrt(sum((a - b) ** 2 for a, b in zip(vecs[0], vecs[-1])))
            D_norm = D / 10.0
            if R >= 8.0:
                trajectory = "BREAKTHROUGH" if D_norm >= 0.15 else "CIRCULAR"
            elif R >= 2.0:
                trajectory = "INTEGRATING" if D_norm >= 0.15 else "DEVELOPING"
            else:
                trajectory = "WANDERING" if D_norm >= 0.15 else "STATIC"
            etce_meta = {"trajectory": trajectory, "collapse_ratio": round(R, 2), "displacement": round(D_norm, 3)}
        except Exception:
            pass
        # SERVER-SIDE OVERRIDE: force PASS when scores qualify
        # Rule: overall >= 65% AND no criterion below 2 = PASS regardless of Claude's recommendation
        recommendation = result["recommendation"]
        min_score = min(scores.values())
        override_applied = False
        if overall >= 65 and min_score >= 2 and recommendation != "pass":
            print(f"[CLAUDE] OVERRIDE: {recommendation.upper()} -> PASS (overall={overall}%, min_score={min_score})")
            recommendation = "pass"
            override_applied = True
        elif overall < 40 or min_score < 1:
            # Also enforce block floor
            if recommendation != "block":
                recommendation = "block"
                override_applied = True
        review_result = {
            "overall_score": overall,
            "recommendation": recommendation,
            "recommendation_override": override_applied,
            "scores": scores,
            "summary": result.get("summary", ""),
            "strengths": result.get("strengths", []),
            "suggestions": result.get("suggestions", []),
            "encouragement": "Your curiosity is valued here.",
            "review_engine": "claude",
        }
        if etce_meta:
            review_result["trajectory"] = etce_meta
        return review_result
    except Exception as e:
        print(f"[CLAUDE] FAILED: {type(e).__name__}: {e}")
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
        "reproducibility": 3,
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

    # Reproducibility scoring
    repro = 3  # baseline
    if re.search(r'github|gitlab|zenodo|code.*available|data.*available|reproducib', lower):
        repro += 1
    if re.search(r'\.py\b|\.r\b|\.m\b|script|algorithm|pseudocode|implementation', lower):
        repro += 1
    scores['reproducibility'] = min(5, repro)

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


def nda_analysis(series):
    """Natural Dynamics Analyzer - ecological time series analysis."""
    n = len(series)
    if n < 50:
        return {"error": "Need at least 50 data points for natural dynamics analysis."}

    # Trend detection via linear regression
    x_mean = (n - 1) / 2.0
    y_mean = sum(series) / n
    num = sum((i - x_mean) * (series[i] - y_mean) for i in range(n))
    den = sum((i - x_mean) ** 2 for i in range(n))
    slope = num / den if den > 0 else 0
    trend = "INCREASING" if slope > 0.01 else "DECREASING" if slope < -0.01 else "STABLE"

    # Seasonality detection via autocorrelation peaks
    max_lag = min(n // 2, 200)
    acf = []
    var = sum((v - y_mean) ** 2 for v in series) / n
    for lag in range(1, max_lag):
        cov = sum((series[i] - y_mean) * (series[i - lag] - y_mean) for i in range(lag, n)) / n
        acf.append(cov / var if var > 1e-12 else 0)

    # Find first significant peak after lag 2
    season_period = 0
    for i in range(2, len(acf) - 1):
        if acf[i] > acf[i-1] and acf[i] > acf[i+1] and acf[i] > 0.2:
            season_period = i + 1
            break
    seasonal = season_period > 0

    # Anomaly detection: points beyond 2.5 std from rolling mean
    window = max(10, n // 20)
    anomalies = []
    for i in range(window, n):
        w = series[i-window:i]
        mu = sum(w) / window
        std = math.sqrt(sum((x - mu) ** 2 for x in w) / window) + 1e-8
        z = abs(series[i] - mu) / std
        if z > 2.5:
            anomalies.append({"index": i, "value": round(series[i], 4), "z_score": round(z, 2)})

    # Regime classification
    # Coefficient of variation
    std_all = math.sqrt(sum((v - y_mean) ** 2 for v in series) / n)
    cv = std_all / abs(y_mean) if abs(y_mean) > 1e-8 else 0

    # Consecutive direction changes (roughness)
    changes = sum(1 for i in range(2, n) if (series[i] - series[i-1]) * (series[i-1] - series[i-2]) < 0)
    roughness = changes / max(1, n - 2)

    if cv < 0.1 and roughness < 0.4:
        regime = "STABLE"
    elif seasonal:
        regime = "CYCLIC"
    elif cv > 0.8 or roughness > 0.7:
        regime = "CHAOTIC"
    elif abs(slope) > 0.05:
        regime = "TRANSITIONAL"
    else:
        regime = "FLUCTUATING"

    # Stability score (0-100): higher = more stable
    stability = max(0, min(100, round(100 - cv * 40 - roughness * 30 - len(anomalies) * 2)))

    # Recovery metric: after anomalies, how quickly does the series return to mean?
    recovery_times = []
    for a in anomalies[:20]:
        idx = a["index"]
        for j in range(idx + 1, min(idx + window * 2, n)):
            w = series[max(0, j-window):j]
            mu = sum(w) / len(w)
            if abs(series[j] - mu) / (std_all + 1e-8) < 1.0:
                recovery_times.append(j - idx)
                break
    mean_recovery = sum(recovery_times) / len(recovery_times) if recovery_times else 0

    # Sample for sparkline
    step = max(1, n // 200)
    preview = [round(series[i], 4) for i in range(0, n, step)][:200]

    cls = "RESILIENT" if stability >= 60 and mean_recovery > 0 else "VULNERABLE" if stability < 40 else "MONITORING"

    return {
        "classification": cls,
        "summary": f"Regime: {regime}. Trend: {trend}. {len(anomalies)} anomalies detected. Stability score: {stability}/100. {'Seasonal period ~' + str(season_period) + ' points.' if seasonal else 'No clear seasonality.'}",
        "details": {
            "regime": regime,
            "trend": trend,
            "trend_slope": round(slope, 6),
            "seasonal": seasonal,
            "season_period": season_period,
            "anomaly_count": len(anomalies),
            "anomalies": anomalies[:10],
            "stability_score": stability,
            "coefficient_of_variation": round(cv, 4),
            "roughness": round(roughness, 4),
            "mean_recovery_time": round(mean_recovery, 1),
            "data_points": n,
            "series_preview": preview,
        },
    }


def rcf_analysis(series):
    """RCF - Relational Coherence Framework. Unification engine."""
    n = len(series)
    if n < 600:
        return {"error": "Need at least 600 data points for RCF analysis."}
    window = 200
    eps = 1e-12
    # Phase 1: Coherence curvature K_C
    G = [0.0] * n
    for t in range(window, n):
        G[t] = sum(series[t - window:t]) / window
    K_series = []
    for t in range(window + 1, n):
        grad = G[t] - G[t - 1]
        K_series.append(grad * grad)
    K_C = sum(K_series) / max(1, len(K_series))
    K_max = max(K_series) if K_series else 0.0
    K_std = math.sqrt(sum((k - K_C) ** 2 for k in K_series) / max(1, len(K_series)))
    # Phase 2: Stability field S(t) and Born-like probabilities
    S = [0.0] * n
    for t in range(window, n):
        seg = series[t - window:t]
        mu = sum(seg) / window
        std = math.sqrt(sum((v - mu) ** 2 for v in seg) / window) + eps
        D_norm = abs(series[t] - mu) / std
        S[t] = 1.0 / (1.0 + D_norm)
    valid_S = [s for s in S[window:] if s > 0]
    stab_mean = sum(valid_S) / len(valid_S) if valid_S else 0.0
    stab_std = math.sqrt(sum((s - stab_mean) ** 2 for s in valid_S) / len(valid_S)) if valid_S else 0.0
    dispersion = stab_std / stab_mean if stab_mean > eps else 0.0
    # Born-like probabilities P ~ S^2
    n_bins = 10
    s_min = min(valid_S) if valid_S else 0.0
    s_max = max(valid_S) if valid_S else 1.0
    bin_w = (s_max - s_min) / n_bins if s_max > s_min + eps else 0.1
    bin_stab = [0.0] * n_bins
    bin_cnt = [0] * n_bins
    for s in valid_S:
        idx = min(int((s - s_min) / bin_w), n_bins - 1)
        bin_stab[idx] += s
        bin_cnt[idx] += 1
    bin_means = [bin_stab[i] / bin_cnt[i] if bin_cnt[i] > 0 else 0.0 for i in range(n_bins)]
    s2 = [m * m for m in bin_means]
    total_s2 = sum(s2)
    probs = [x / total_s2 if total_s2 > eps else 1.0 / n_bins for x in s2]
    entropy = -sum(p * math.log(p + 1e-12) for p in probs if p > 0)
    # Phase 3: Regime classification
    high_frac = sum(1 for s in valid_S if s > 0.7) / len(valid_S) if valid_S else 0.0
    if stab_mean > 0.7 and dispersion < 0.3:
        regime = "GR_GEOMETRIC"
    elif stab_mean < 0.4 or dispersion > 0.6:
        regime = "QT_STATISTICAL"
    else:
        regime = "MIXED"
    # Phase 4: Recurrence closure
    disruptions = []
    in_d = False
    vS = S[window:]
    for t in range(1, len(vS)):
        if not in_d and vS[t] < 0.3 and vS[t - 1] >= 0.3:
            in_d = True
            disruptions.append(t)
        elif in_d and vS[t] >= 0.5:
            in_d = False
    rec_times = []
    for ds in disruptions[:20]:
        for dt in range(1, min(window * 5, len(vS) - ds)):
            if vS[ds + dt] >= 0.5:
                rec_times.append(dt)
                break
    closure = len(rec_times) == len(disruptions[:20]) and len(disruptions) > 0
    # Phase 5: Unification
    geo_ok = math.isfinite(K_C) and K_C >= 0 and K_C < 1e6
    stat_ok = len(probs) > 0 and abs(sum(probs) - 1.0) < 0.01
    unified = geo_ok and stat_ok
    verdict = "UNIFIED" if unified else "INCONSISTENT"
    # Sparklines
    step = max(1, len(K_series) // 200)
    k_preview = [round(K_series[i], 6) for i in range(0, len(K_series), step)][:200]
    s_step = max(1, len(valid_S) // 200)
    s_preview = [round(valid_S[i], 4) for i in range(0, len(valid_S), s_step)][:200]
    return {
        "classification": verdict,
        "summary": f"Regime: {regime}. K_C={K_C:.6f}. Born entropy={entropy:.3f}. Recurrence {'holds' if closure else 'open'}. Verdict: {verdict}.",
        "details": {
            "K_C": round(K_C, 6), "K_C_max": round(K_max, 6), "K_C_std": round(K_std, 6),
            "regime": regime, "stability_mean": round(stab_mean, 4), "dispersion": round(dispersion, 4),
            "high_stability_fraction": round(high_frac, 4),
            "born_entropy": round(entropy, 4), "born_probabilities": [round(p, 4) for p in probs],
            "recurrence_holds": closure, "n_disruptions": len(disruptions),
            "mean_recovery_time": round(sum(rec_times) / len(rec_times), 1) if rec_times else 0,
            "geometric_consistent": geo_ok, "statistical_consistent": stat_ok,
            "verdict": verdict, "data_points": n,
            "k_series": k_preview, "stability_series": s_preview,
        },
    }


def etce_analysis(series):
    """ETCE - Embedding Trajectory Collapse Engine. Analyzes trajectory geometry."""
    n = len(series)
    if n < 20:
        return {"error": "Need at least 20 data points for ETCE analysis."}
    # Build feature vectors from rolling windows
    w = min(10, n // 4)
    step = max(1, w // 2)
    vectors = []
    for i in range(0, n - w + 1, step):
        seg = series[i:i + w]
        mu = sum(seg) / w
        var = sum((x - mu) ** 2 for x in seg) / w
        vol = math.sqrt(var)
        skew = (sum((x - mu) ** 3 for x in seg) / w) / (vol ** 3) if vol > 1e-10 else 0.0
        mn = min(seg)
        mx = max(seg)
        vectors.append([mu, vol, skew, mn, mx])
    if len(vectors) < 4:
        return {"error": "Not enough data windows for ETCE analysis."}
    # Run ETCE logic
    explore_window = min(8, len(vectors) - 2)
    conclude_window = min(3, len(vectors) // 3)
    readings = []
    for vi in range(len(vectors)):
        if vi < 4:
            readings.append({"classification": "WARMING", "collapse_ratio": 0.0, "displacement": 0.0})
            continue
        exp_s = max(0, vi - explore_window - conclude_window)
        exp_e = max(0, vi - conclude_window)
        explore_vecs = vectors[exp_s:exp_e]
        conclude_vecs = vectors[vi - conclude_window:vi + 1]
        # Exploration volume
        exp_vol = 0.0
        for j in range(len(explore_vecs) - 1):
            d = math.sqrt(sum((a - b) ** 2 for a, b in zip(explore_vecs[j], explore_vecs[j + 1])))
            exp_vol += d
        # Conclusion density
        con_steps = []
        for j in range(len(conclude_vecs) - 1):
            d = math.sqrt(sum((a - b) ** 2 for a, b in zip(conclude_vecs[j], conclude_vecs[j + 1])))
            con_steps.append(d)
        con_den = sum(con_steps) / len(con_steps) if con_steps else 0.0
        R = exp_vol / con_den if con_den > 1e-8 else exp_vol * 10.0
        # Displacement
        e_start = explore_vecs[0] if explore_vecs else vectors[0]
        e_final = [sum(v[d] for v in conclude_vecs) / len(conclude_vecs) for d in range(len(conclude_vecs[0]))]
        D = math.sqrt(sum((a - b) ** 2 for a, b in zip(e_start, e_final)))
        # Normalize D by scale
        all_vals = [v for vec in vectors for v in vec]
        scale = max(all_vals) - min(all_vals) if all_vals else 1.0
        D_norm = D / (scale + 1e-8)
        d_thresh = 0.15
        if R >= 8.0:
            cls = "BREAKTHROUGH" if D_norm >= d_thresh else "CIRCULAR"
        elif R >= 2.0:
            cls = "INTEGRATING" if D_norm >= d_thresh else "DEVELOPING"
        else:
            cls = "WANDERING" if D_norm >= d_thresh else "STATIC"
        readings.append({"classification": cls, "collapse_ratio": round(R, 4), "displacement": round(D_norm, 4)})
    # Summary
    monitored = [r for r in readings if r["classification"] != "WARMING"]
    counts = {}
    for r in readings:
        counts[r["classification"]] = counts.get(r["classification"], 0) + 1
    dominant = max(counts, key=counts.get) if counts else "WARMING"
    peak_R = max((r["collapse_ratio"] for r in monitored), default=0.0)
    peak_D = max((r["displacement"] for r in monitored), default=0.0)
    final = readings[-1] if readings else {"classification": "WARMING", "collapse_ratio": 0.0, "displacement": 0.0}
    r_series = [r["collapse_ratio"] for r in readings]
    d_series = [r["displacement"] for r in readings]
    return {
        "classification": final["classification"],
        "summary": f"Trajectory: {final['classification']}. Dominant: {dominant}. Peak R={peak_R:.2f}, Peak D={peak_D:.3f}. {len(vectors)} windows analyzed.",
        "details": {
            "current": final["classification"], "dominant": dominant,
            "collapse_ratio": final["collapse_ratio"], "displacement": final["displacement"],
            "peak_collapse_ratio": round(peak_R, 4), "peak_displacement": round(peak_D, 4),
            "classification_counts": counts, "n_windows": len(vectors), "data_points": n,
            "r_series": r_series, "d_series": d_series,
        },
    }


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
        "natural": ["ecology", "population", "species", "habitat", "migration", "predator",
            "prey", "biodiversity", "ecosystem", "conservation", "wildlife", "vegetation",
            "climate", "environmental", "soil", "water quality", "pollutant", "organism",
            "breeding", "phenology", "biomass", "trophic", "food web", "coral",
            "deforestation", "extinction", "invasive", "wetland", "fisheries",
            "bird", "mammal", "insect", "marine", "freshwater", "forest"],
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


def extract_pdf_text(pdf_bytes):
    """Extract text from PDF bytes using PyMuPDF."""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        pages = []
        for page in doc:
            pages.append(page.get_text())
        doc.close()
        text = "\n\n".join(pages)
        print(f"[PDF] Extracted {len(text)} chars from {len(pages)} pages")
        return text
    except ImportError:
        print("[PDF] PyMuPDF not installed, falling back to raw decode")
        return pdf_bytes.decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"[PDF] Extraction failed: {e}")
        return pdf_bytes.decode("utf-8", errors="ignore")


def process_zip_package(zip_bytes):
    """Process a zip file containing a paper package. Returns dict with manuscript text, figures, data, code."""
    import zipfile
    result = {"text": "", "pdf_bytes": None, "pdf_filename": None, "figures": [], "data_files": [], "code_files": [], "supplementary": [], "all_text": ""}
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        names = zf.namelist()
        print(f"[ZIP] Processing package with {len(names)} files")
        tex_text = ""
        pdf_text = ""
        for name in names:
            if name.endswith("/") or name.startswith("__MACOSX") or name.startswith("."):
                continue
            low = name.lower()
            base = os.path.basename(name)
            parent = os.path.dirname(name).lower()
            data = zf.read(name)
            # Figure PDFs: files in figs/, figures/, images/, plots/ directories
            is_fig_dir = any(d in parent for d in ["fig", "image", "plot", "graphic", "diagram"])
            if low.endswith(".pdf"):
                if is_fig_dir:
                    result["figures"].append(base)
                    print(f"[ZIP] Figure PDF: {name}")
                elif not result["pdf_bytes"] or "manuscript" in low or "paper" in low or "main" in low:
                    result["pdf_bytes"] = data
                    result["pdf_filename"] = base
                    pdf_text = extract_pdf_text(data)
                    print(f"[ZIP] Manuscript PDF: {name} -> {len(pdf_text)} chars")
                else:
                    result["supplementary"].append(base)
            elif low.endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".eps", ".tiff", ".bmp")):
                result["figures"].append(base)
            elif low.endswith((".csv", ".tsv", ".dat")):
                result["data_files"].append(base)
                try:
                    content = data.decode("utf-8", errors="ignore")
                    # Include full data for smaller files, truncate larger ones
                    limit = 5000 if len(content) < 8000 else 2000
                    result["all_text"] += f"\n\n--- DATA FILE: {base} ---\n{content[:limit]}\n"
                except Exception:
                    pass
            elif low.endswith((".xls", ".xlsx")):
                result["data_files"].append(base)
            elif low.endswith((".py", ".r", ".m", ".jl", ".nb", ".ipynb")):
                result["code_files"].append(base)
                try:
                    content = data.decode("utf-8", errors="ignore")
                    result["all_text"] += f"\n\n--- CODE FILE: {base} ---\n{content[:4000]}\n"
                except Exception:
                    pass
            elif low.endswith(".tex"):
                try:
                    content = data.decode("utf-8", errors="ignore")
                    if len(content) > len(tex_text):
                        tex_text = content
                    print(f"[ZIP] LaTeX source: {name} ({len(content)} chars)")
                except Exception:
                    pass
            elif low.endswith(".bib"):
                try:
                    content = data.decode("utf-8", errors="ignore")
                    result["all_text"] += f"\n\n--- BIBLIOGRAPHY: {base} ---\n{content[:6000]}\n"
                except Exception:
                    pass
            elif low.endswith((".txt", ".md", ".cff", ".rst")):
                try:
                    content = data.decode("utf-8", errors="ignore")
                    result["supplementary"].append(base)
                    result["all_text"] += f"\n\n--- {base} ---\n{content[:3000]}\n"
                except Exception:
                    pass
            else:
                result["supplementary"].append(base)
        zf.close()
        # Prefer TEX source over PDF extraction (TEX has equations and references intact)
        if tex_text and len(tex_text) > 1000:
            result["text"] = tex_text
            print(f"[ZIP] Using TEX source: {len(tex_text)} chars (preferred over PDF)")
        elif pdf_text:
            result["text"] = pdf_text
            print(f"[ZIP] Using PDF text: {len(pdf_text)} chars")
    except Exception as e:
        print(f"[ZIP] Processing failed: {e}")
    return result


def read_uploaded_text():
    """Read text from uploaded files, skipping binary formats (PDF, ZIP) which are handled separately."""
    parts = []
    for key in request.files:
        f = request.files[key]
        if not f or not f.filename:
            continue
        fname = (f.filename or "").lower()
        # Skip binary formats - these are handled by the zip/pdf processing pipeline
        if fname.endswith((".pdf", ".zip", ".gz", ".tar", ".rar")):
            continue
        try:
            raw = f.read()
            if not raw:
                continue
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
            title = (d.get("title") or "").strip()
            abstract = (d.get("abstract") or "").strip()
            body_text = (d.get("body_text") or "").strip()

            # Process zip/PDF FIRST (before read_uploaded_text consumes file streams)
            zip_text = ""
            pdf_bytes_store = None
            pdf_fname_store = None
            package_meta_store = None
            uploaded = request.files.get("pdf") or request.files.get("file")
            if uploaded and uploaded.filename:
                fname = uploaded.filename.lower()
                file_bytes = uploaded.read()
                print(f"[UPLOAD] File received: {uploaded.filename} ({len(file_bytes)} bytes)")
                if len(file_bytes) <= 10 * 1024 * 1024:
                    if fname.endswith(".zip"):
                        pkg = process_zip_package(file_bytes)
                        if pkg["pdf_bytes"]:
                            pdf_bytes_store = pkg["pdf_bytes"]
                            pdf_fname_store = pkg["pdf_filename"]
                        zip_text = (pkg["text"] + pkg["all_text"]).strip()
                        package_meta_store = json.dumps({"figures": pkg["figures"], "data_files": pkg["data_files"], "code_files": pkg["code_files"], "supplementary": pkg["supplementary"]})
                        print(f"[UPLOAD] ZIP processed: {len(zip_text)} chars text, {len(pkg['figures'])} figs, {len(pkg['data_files'])} data, {len(pkg['code_files'])} code")
                    elif fname.endswith(".pdf"):
                        pdf_bytes_store = file_bytes
                        pdf_fname_store = uploaded.filename
                        extracted = extract_pdf_text(file_bytes)
                        if extracted:
                            zip_text = extracted
                        print(f"[UPLOAD] PDF processed: {len(zip_text)} chars extracted")

            # Now read any additional text files (zip/pdf already consumed above)
            uploaded_text = read_uploaded_text()
            # Combine all text sources
            if zip_text and len(zip_text) > len(body_text):
                body_text = zip_text
            elif uploaded_text:
                if not body_text:
                    body_text = uploaded_text[:200000]
                else:
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
            pdf_bytes_store = None
            pdf_fname_store = None
            package_meta_store = None
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
        # Store pre-processed file data
        if pdf_bytes_store:
            sub.pdf_data = pdf_bytes_store
            sub.pdf_filename = pdf_fname_store
        if package_meta_store:
            sub.package_meta = package_meta_store
        db.session.add(sub)
        desk = None
        if not payload["is_draft"]:
            # Use full body text (possibly extracted from PDF) for review
            review_text = sub.body_text or sub.abstract or ""
            desk = run_desk_review(sub.title, sub.abstract, review_text)
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
    # Handle file uploads on PUT (for FormData submissions with zip/pdf)
    if request.content_type and "multipart/form-data" in request.content_type:
        uploaded = request.files.get("pdf") or request.files.get("file")
        if uploaded and uploaded.filename:
            fname = uploaded.filename.lower()
            file_bytes = uploaded.read()
            print(f"[PUT] File uploaded: {uploaded.filename} ({len(file_bytes)} bytes)")
            if len(file_bytes) <= 10 * 1024 * 1024:
                if fname.endswith(".zip"):
                    pkg = process_zip_package(file_bytes)
                    if pkg["pdf_bytes"]:
                        sub.pdf_data = pkg["pdf_bytes"]
                        sub.pdf_filename = pkg["pdf_filename"]
                    if pkg["text"] and len(pkg["text"]) > len(sub.body_text or ""):
                        sub.body_text = (pkg["text"] + pkg["all_text"])[:200000]
                        print(f"[PUT] Extracted {len(sub.body_text)} chars from zip")
                    meta = {"figures": pkg["figures"], "data_files": pkg["data_files"], "code_files": pkg["code_files"], "supplementary": pkg["supplementary"]}
                    sub.package_meta = json.dumps(meta)
                elif fname.endswith(".pdf"):
                    sub.pdf_data = file_bytes
                    sub.pdf_filename = uploaded.filename
                    if not sub.body_text or len(sub.body_text) < 500:
                        extracted = extract_pdf_text(file_bytes)
                        if extracted and len(extracted) > len(sub.body_text or ""):
                            sub.body_text = extracted[:200000]
                            print(f"[PUT] Extracted {len(sub.body_text)} chars from PDF")
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
@app.route("/api/submissions/<bid>/pdf")
def serve_pdf(bid):
    s = Submission.query.filter_by(blind_id=bid).first_or_404()
    if not s.pdf_data:
        return jsonify({"error": "No PDF available"}), 404
    return send_file(
        io.BytesIO(s.pdf_data),
        mimetype="application/pdf",
        as_attachment=False,
        download_name=(s.pdf_filename or "paper.pdf"),
    )


@app.route("/api/submissions/<bid>/related")
def related_papers(bid):
    s = Submission.query.filter_by(blind_id=bid).first_or_404()
    words = set((s.title or "").lower().split() + (s.abstract or "").lower().split()[:50])
    stop = {"the", "a", "an", "of", "in", "to", "for", "and", "is", "on", "with", "that", "this", "by", "from", "are", "as", "at", "it", "or", "be", "we", "not", "but", "its"}
    keywords = [w for w in words if len(w) > 3 and w not in stop][:20]
    if not keywords:
        return jsonify({"related": []})
    candidates = Submission.query.filter(
        Submission.id != s.id,
        Submission.is_draft.is_(False),
        Submission.status.in_(["in_discovery", "under_review", "published"]),
    ).limit(100).all()
    scored = []
    for c in candidates:
        ctxt = ((c.title or "") + " " + (c.abstract or "")).lower()
        score = sum(1 for k in keywords if k in ctxt)
        if score > 0:
            scored.append((score, c))
    scored.sort(key=lambda x: -x[0])
    uid = current_user.id if current_user.is_authenticated else None
    return jsonify({"related": [c.to_card(uid) for _, c in scored[:5]]})


@app.route("/api/users/<int:uid>/timeline")
def user_timeline(uid):
    subs = Submission.query.filter_by(author_id=uid).order_by(Submission.updated_at.desc()).limit(50).all()
    decisions = []
    for s in subs:
        dd = DeskDecision.query.filter_by(submission_id=s.id).order_by(DeskDecision.created_at.desc()).first()
        decisions.append({
            "paper_id": s.id,
            "blind_id": s.blind_id,
            "title": s.title,
            "status": s.status,
            "status_label": Submission.STATUS_LABELS.get(s.status, s.status),
            "status_color": Submission.STATUS_COLORS.get(s.status, "#6b7db3"),
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "updated_at": s.updated_at.isoformat() if s.updated_at else None,
            "desk_score": dd.overall_score if dd else None,
            "desk_decision": dd.decision if dd else None,
            "is_draft": s.is_draft,
        })
    return jsonify({"timeline": decisions})


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


# --- AI Writing Helper ---
@app.route("/api/ai/assist", methods=["POST"])
@api_login_required
def ai_assist():
    """AI writing assistant for paper construction."""
    global _claude_call_times
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return jsonify({"error": "AI assistance not available (no API key configured)"}), 503

    now = datetime.utcnow()
    _claude_call_times = [t for t in _claude_call_times if (now - t).total_seconds() < 3600]
    if len(_claude_call_times) >= CLAUDE_RATE_LIMIT:
        return jsonify({"error": "Rate limit reached. Try again in a few minutes."}), 429
    _claude_call_times.append(now)

    d = request_data()
    action = (d.get("action") or "").strip()
    title = (d.get("title") or "").strip()
    abstract = (d.get("abstract") or "").strip()
    section = (d.get("section") or "").strip()
    section_text = (d.get("section_text") or "").strip()
    full_text = (d.get("full_text") or "").strip()
    user_prompt = (d.get("prompt") or "").strip()

    actions = {
        "improve_section": f"Improve this section of a scientific paper. Keep the author's voice but strengthen the scientific writing, clarity, and rigor. Section: {section}\n\nCurrent text:\n{section_text[:6000]}",
        "suggest_structure": f"Given this paper title and abstract, suggest a detailed section-by-section outline with what each section should contain. Be specific about what arguments, data, and references each section needs.\n\nTitle: {title}\nAbstract: {abstract}",
        "strengthen_claims": f"Review this section and identify claims that need stronger support. For each weak claim, suggest what evidence, analysis, or citation would strengthen it.\n\nSection: {section}\nText:\n{section_text[:6000]}",
        "write_introduction": f"Help draft an introduction section for this paper. Include motivation, context, gap in literature, and a clear statement of contribution.\n\nTitle: {title}\nAbstract: {abstract}\nFull paper context:\n{full_text[:4000]}",
        "write_conclusion": f"Help draft a conclusion section for this paper. Summarize key findings, state limitations, and suggest future work.\n\nTitle: {title}\nAbstract: {abstract}\nFull paper:\n{full_text[:6000]}",
        "check_consistency": f"Check this paper for internal consistency. Look for contradictions between claims and results, undefined terms, logical gaps, and missing connections between sections.\n\nTitle: {title}\nAbstract: {abstract}\nPaper:\n{full_text[:8000]}",
        "format_references": f"Help format the references section. Identify any citations mentioned in the text that need full references, and suggest proper formatting.\n\nPaper text:\n{full_text[:6000]}",
        "general": f"You are a scientific writing assistant for an independent physics journal called OpenField. Help the researcher with their request.\n\nPaper title: {title}\nAbstract: {abstract}\nCurrent section ({section}):\n{section_text[:4000]}\n\nResearcher's request: {user_prompt}",
    }

    if action not in actions:
        return jsonify({"error": f"Unknown action: {action}. Available: {', '.join(actions.keys())}"}), 400

    prompt = actions[action]
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2000,
            system="You are a scientific writing assistant for OpenField, an independent science journal. You help researchers improve their papers. Be constructive, specific, and maintain the author's voice. Focus on scientific rigor, clarity, and structure. Do not rewrite everything -- suggest improvements and explain why.",
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        return jsonify({"response": raw, "action": action})
    except Exception as e:
        print(f"[AI_ASSIST] Failed: {type(e).__name__}: {e}")
        return jsonify({"error": "AI assistance failed", "detail": str(e)}), 500


@app.route("/api/submissions/<bid>/review", methods=["POST"])
@api_login_required
def submission_review(bid):
    """Run AI desk review on a stored submission, using full text including PDF extraction."""
    sub = resolve_submission(bid)
    # Gather all available text
    title = sub.title or "Untitled"
    abstract = sub.abstract or ""
    body = sub.body_text or ""
    # If body is short but we have a PDF, extract from PDF
    if len(body) < 500 and sub.pdf_data:
        print(f"[REVIEW] body_text is short ({len(body)} chars), extracting from PDF...")
        extracted = extract_pdf_text(sub.pdf_data)
        if extracted and len(extracted) > len(body):
            body = extracted
            # Also update the stored body_text so future reviews don't need to re-extract
            sub.body_text = body[:200000]
            db.session.commit()
            print(f"[REVIEW] Extracted {len(body)} chars from PDF, updated body_text")
    # Catalog attachments for Claude's context
    attachments_summary = ""
    if sub.pdf_data:
        attachments_summary += f"[PDF attached: {sub.pdf_filename or 'paper.pdf'}]\n"
    # Check for package_meta (zip contents)
    pkg = getattr(sub, '_package_meta', None)
    if hasattr(sub, 'package_meta') and sub.package_meta:
        try:
            meta = json.loads(sub.package_meta)
            if meta.get("figures"):
                attachments_summary += f"[{len(meta['figures'])} figure(s): {', '.join(meta['figures'])}]\n"
            if meta.get("data_files"):
                attachments_summary += f"[{len(meta['data_files'])} data file(s): {', '.join(meta['data_files'])}]\n"
            if meta.get("code_files"):
                attachments_summary += f"[{len(meta['code_files'])} code file(s): {', '.join(meta['code_files'])}]\n"
            if meta.get("supplementary"):
                attachments_summary += f"[{len(meta['supplementary'])} supplementary file(s)]\n"
        except Exception:
            pass
    if attachments_summary:
        body = body + "\n\n--- SUBMISSION PACKAGE ---\n" + attachments_summary
    full_text = f"TITLE: {title}\nABSTRACT: {abstract}\nBODY:\n{body}"
    print(f"[REVIEW] Reviewing submission {bid}: {len(full_text)} chars total (title={len(title)}, abstract={len(abstract)}, body={len(body)})")
    desk = run_desk_review(title, abstract, body)
    # Save the review result
    try:
        db.session.add(DeskDecision(
            submission_id=sub.id,
            decision=desk["recommendation"],
            overall_score=desk["overall_score"],
            summary=desk["summary"],
            encouragement=desk.get("encouragement", ""),
            scores_json=json.dumps(desk["scores"]),
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()
    return jsonify({
        "classification": desk["recommendation"].upper(),
        "summary": desk["summary"],
        "details": desk,
    })


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
            extra_text = ""
            if file_uploaded and file_uploaded.filename:
                fname = file_uploaded.filename.lower()
                file_bytes = file_uploaded.read()
                if fname.endswith(".pdf"):
                    extra_text = extract_pdf_text(file_bytes)
                elif fname.endswith(".zip"):
                    pkg = process_zip_package(file_bytes)
                    extra_text = (pkg["text"] + pkg["all_text"]) if pkg["text"] else ""
                elif is_plain_text_file(file_uploaded):
                    file_uploaded.seek(0)
                    extra_text = file_uploaded.read().decode("utf-8", errors="ignore")
                else:
                    return jsonify({"error": "Unsupported file type. Upload PDF, ZIP, TXT, TEX, or paste text."}), 400

            cleaned = ((text or "") + "\n\n" + extra_text).strip()
            if not cleaned:
                return jsonify({"error": "Paste manuscript text or upload a file."}), 400
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

        if tool == "nda":
            if len(series) < 50:
                return jsonify({"error": "Need at least 50 numeric values for natural dynamics analysis."}), 400
            return jsonify(nda_analysis(series))

        if tool == "rcf":
            if len(series) < 600:
                return jsonify({"error": "Need at least 600 numeric values for RCF analysis."}), 400
            return jsonify(rcf_analysis(series))

        if tool == "etce":
            if len(series) < 20:
                return jsonify({"error": "Need at least 20 numeric values for ETCE analysis."}), 400
            return jsonify(etce_analysis(series))

        return jsonify({"error": "Unknown tool"}), 400
    except Exception as e:
        app.logger.exception("Tool execution failed")
        return jsonify({"error": "Tool execution failed", "detail": str(e)}), 500


@app.route("/api/tools/sample/<tool_id>")
@api_login_required
def tool_sample_data(tool_id):
    """Generate sample data for a tool so users can try it."""
    samples = {
        "ocm": lambda: ",".join(str(round(x, 6)) for x in generate_duffing(2000)),
        "er": lambda: ",".join(str(round(x, 6)) for x in generate_duffing(1000)),
        "icm": lambda: ",".join(str(round(x, 6)) for x in generate_lorenz(2000)),
        "clm": lambda: ",".join(str(round(x, 6)) for x in generate_duffing(200)),
        "nda": lambda: ",".join(str(round(x, 6)) for x in [50 + 10 * math.sin(i * 0.1) + random.gauss(0, 2) for i in range(500)]),
        "rcf": lambda: ",".join(str(round(x, 6)) for x in generate_duffing(2000)),
        "etce": lambda: ",".join(str(round(x, 6)) for x in generate_lorenz(200)),
    }
    if tool_id not in samples:
        return jsonify({"error": "No sample for this tool"}), 404
    return jsonify({"data": samples[tool_id](), "tool": tool_id})


@app.route("/api/tools/export", methods=["POST"])
@api_login_required
def tools_export():
    d = request_data()
    result = d.get("result", {})
    if not result:
        return jsonify({"error": "No result to export"}), 400
    details = result.get("details", {})
    cls = result.get("classification", "")
    summary = result.get("summary", "")
    scores_html = ""
    if details.get("scores"):
        rows = "".join(f"<tr><td style='padding:6px 12px;border:1px solid #ddd;font-weight:600'>{k.title()}</td><td style='padding:6px 12px;border:1px solid #ddd;text-align:center'>{v}/5</td></tr>" for k, v in details["scores"].items())
        scores_html = f"<table style='border-collapse:collapse;width:100%;margin:16px 0'><thead><tr><th style='padding:8px 12px;border:1px solid #ddd;background:#f5f5f5;text-align:left'>Criterion</th><th style='padding:8px 12px;border:1px solid #ddd;background:#f5f5f5'>Score</th></tr></thead><tbody>{rows}</tbody></table>"
    metrics_html = ""
    metric_keys = ["contraction_rate", "z_statistic", "threshold", "points", "nodes", "edges", "bridges", "density", "min_coherence", "mean_coherence", "events", "morphology", "regime", "mean_amplitude", "mean_recovery", "resonance_R", "kappa_w", "glue_error", "stability", "prms", "ri", "trend", "trend_slope", "seasonal", "season_period", "anomaly_count", "stability_score", "coefficient_of_variation", "roughness", "mean_recovery_time", "data_points", "K_C", "K_C_max", "born_entropy", "dispersion", "stability_mean", "geometric_consistent", "statistical_consistent", "verdict", "recurrence_holds", "collapse_ratio", "displacement", "peak_collapse_ratio", "peak_displacement", "dominant", "n_windows"]
    found = [(k, details[k]) for k in metric_keys if k in details]
    if found:
        rows = "".join(f"<tr><td style='padding:6px 12px;border:1px solid #ddd'>{k}</td><td style='padding:6px 12px;border:1px solid #ddd'>{v}</td></tr>" for k, v in found)
        metrics_html = f"<table style='border-collapse:collapse;width:100%;margin:16px 0'><thead><tr><th style='padding:8px 12px;border:1px solid #ddd;background:#f5f5f5;text-align:left'>Metric</th><th style='padding:8px 12px;border:1px solid #ddd;background:#f5f5f5'>Value</th></tr></thead><tbody>{rows}</tbody></table>"
    overall_html = ""
    if details.get("overall_score") is not None:
        overall_html = f"<div style='padding:12px;background:#f0f7ff;border-radius:8px;margin:16px 0;font-size:16px'><strong>Overall Score: {details['overall_score']}%</strong> | Recommendation: <strong>{(details.get('recommendation') or '').upper()}</strong></div>"
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OpenField - Analysis Report</title>
<style>body{{font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',sans-serif;max-width:700px;margin:0 auto;padding:20px;color:#1a1a2e;line-height:1.6}}h1{{font-size:20px;border-bottom:2px solid #5ea8ff;padding-bottom:8px}}h2{{font-size:16px;color:#3d70b8;margin-top:24px}}.badge{{display:inline-block;padding:4px 12px;border-radius:20px;font-weight:700;font-size:13px;background:#e8f4ff;color:#3d70b8}}.footer{{margin-top:32px;padding-top:16px;border-top:1px solid #ddd;font-size:11px;color:#888}}</style>
</head><body>
<h1>OpenField &mdash; Analysis Report</h1>
<p class="badge">{cls}</p>
<h2>Summary</h2><p>{summary}</p>
{scores_html}{metrics_html}{overall_html}
{('<h2>Strengths</h2><ul>' + ''.join(f'<li>{s}</li>' for s in details.get('strengths', [])) + '</ul>') if details.get('strengths') else ''}
{('<h2>Suggestions</h2><ul>' + ''.join(f'<li>{s}</li>' for s in details.get('suggestions', [])) + '</ul>') if details.get('suggestions') else ''}
<div class="footer">Generated by OpenField &mdash; Independent Science Platform<br>All Rights Reserved &mdash; OCM Research Labs</div>
</body></html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


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
            if not s.openfield_id:
                s.openfield_id = generate_openfield_id()
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
        ("foundations", "Foundations of Physics", "\xf0\x9f\x8c\x8c".encode().decode()),
        ("math-physics", "Mathematical Physics", "\xf0\x9f\x93\x90".encode().decode()),
        ("nonlinear", "Nonlinear Dynamics", "\xf0\x9f\x8c\x80".encode().decode()),
        ("stat-mech", "Statistical Mechanics", "\xe2\x9a\x9b\xef\xb8\x8f".encode().decode()),
        ("complex", "Complex Systems", "\xf0\x9f\x95\xb8\xef\xb8\x8f".encode().decode()),
        ("experimental", "Experimental & Observational", "\xf0\x9f\x94\xac".encode().decode()),
        ("natural", "Natural & Environmental Sciences", "\xf0\x9f\x8c\xbf".encode().decode()),
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
