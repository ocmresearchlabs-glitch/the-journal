import os
import io
import csv
import json
import math
import random
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from functools import wraps

from flask import Flask, jsonify, request, send_file, send_from_directory, make_response
from flask_login import LoginManager, UserMixin, current_user, login_user, logout_user
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_, inspect, text
from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import HTTPException
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__, static_folder='static')
BASE = Path(__file__).parent
INSTANCE = BASE / 'instance'
INSTANCE.mkdir(exist_ok=True)

# --- Database config ---
db_url = os.getenv('DATABASE_URL')
if db_url and db_url.startswith('postgres://'):
    db_url = db_url.replace('postgres://', 'postgresql://', 1)
if not db_url:
    sqlite_path = Path(os.getenv('SQLITE_PATH', '/tmp/journal.db'))
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    db_url = 'sqlite:///' + str(sqlite_path)

app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-change-before-deploy')
app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
secure_cookies = bool(os.getenv('RENDER'))
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = secure_cookies
app.config['REMEMBER_COOKIE_SAMESITE'] = 'Lax'
app.config['REMEMBER_COOKIE_SECURE'] = secure_cookies
app.config['REMEMBER_COOKIE_HTTPONLY'] = True
app.config['REMEMBER_COOKIE_DURATION'] = timedelta(days=30)
if db_url.startswith('sqlite'):
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'connect_args': {'check_same_thread': False}}


db = SQLAlchemy(app)
login_manager = LoginManager(app)


@app.before_request
def handle_preflight():
    if request.method == 'OPTIONS':
        response = make_response()
        response.headers['Access-Control-Allow-Origin'] = request.headers.get('Origin', '*')
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Requested-With'
        response.headers['Access-Control-Allow-Credentials'] = 'true'
        response.headers['Access-Control-Max-Age'] = '3600'
        return response, 200


@app.after_request
def add_cors(response):
    origin = request.headers.get('Origin', '*')
    response.headers['Access-Control-Allow-Origin'] = origin
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Requested-With'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Credentials'] = 'true'
    return response


@app.errorhandler(Exception)
def handle_exception(e):
    if isinstance(e, HTTPException):
        return e
    app.logger.exception('Unhandled server error')
    db.session.rollback()
    return jsonify({'error': 'Internal server error', 'detail': str(e)}), 500


# --- Models ---
user_follows = db.Table(
    'user_follows',
    db.Column('follower_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('followed_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    display_name = db.Column(db.String(120), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default='member')
    bio = db.Column(db.Text, default='')
    avatar_color = db.Column(db.String(7), default='#5ea8ff')
    orcid = db.Column(db.String(32), default='')
    reputation = db.Column(db.Float, default=1.0)
    is_banned = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    following = db.relationship(
        'User',
        secondary=user_follows,
        primaryjoin=(user_follows.c.follower_id == id),
        secondaryjoin=(user_follows.c.followed_id == id),
        backref=db.backref('followers', lazy='dynamic'),
        lazy='dynamic',
    )

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)

    @property
    def initials(self):
        pieces = (self.display_name or '').strip().split()
        if len(pieces) >= 2:
            return (pieces[0][0] + pieces[-1][0]).upper()
        return (self.display_name or '??')[:2].upper()

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
            follower_count = self.followers.count()
        except Exception:
            follower_count = 0
        return {
            'id': self.id,
            'email': self.email,
            'display_name': self.display_name,
            'initials': self.initials,
            'bio': self.bio,
            'avatar_color': self.avatar_color,
            'orcid': self.orcid or '',
            'role': self.role,
            'reputation_score': round(self.reputation or 0, 2),
            'paper_count': paper_count,
            'review_count': review_count,
            'follower_count': follower_count,
            'joined': self.created_at.isoformat() if self.created_at else None,
        }


@login_manager.user_loader
def load_user(uid):
    return db.session.get(User, int(uid))


class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(80), unique=True)
    name = db.Column(db.String(120))
    emoji = db.Column(db.String(16), default='📐')


class Submission(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    blind_id = db.Column(db.String(20), unique=True, index=True)
    title = db.Column(db.String(500))
    abstract = db.Column(db.Text)
    body_text = db.Column(db.Text)
    status = db.Column(db.String(30), default='draft', index=True)
    is_draft = db.Column(db.Boolean, default=False, index=True)
    tags = db.Column(db.Text, default='')
    author_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    published_at = db.Column(db.DateTime)

    author = db.relationship('User', backref='submissions')
    category = db.relationship('Category', backref='submissions')

    STATUS_LABELS = {
        'draft': 'Private Draft',
        'submitted': 'Submitted',
        'in_discovery': 'In Discovery',
        'under_review': 'In Review',
        'published': 'Published',
        'desk_returned': 'Returned',
        'revision_requested': 'Revision Requested',
        'declined': 'Declined',
        'desk_blocked': 'Blocked',
    }
    STATUS_COLORS = {
        'draft': '#6b7db3',
        'submitted': '#8b9cc8',
        'in_discovery': '#5ea8ff',
        'under_review': '#f0a030',
        'published': '#4ade80',
        'desk_returned': '#f0a030',
        'revision_requested': '#f0a030',
        'declined': '#ef4444',
        'desk_blocked': '#ef4444',
    }

    def to_card(self, uid=None, full_abstract=False):
        like_count = Like.query.filter_by(submission_id=self.id).count()
        comment_count = Comment.query.filter_by(submission_id=self.id).count()
        review_count = Review.query.filter_by(submission_id=self.id).count()
        out = {
            'id': self.id,
            'blind_id': self.blind_id,
            'title': self.title,
            'abstract': self.abstract if full_abstract else (self.abstract or '')[:300],
            'body_text': self.body_text or '',
            'status': self.status,
            'status_label': self.STATUS_LABELS.get(self.status, self.status),
            'status_color': self.STATUS_COLORS.get(self.status, '#6b7db3'),
            'is_draft': bool(self.is_draft),
            'category': {
                'id': self.category.id,
                'slug': self.category.slug,
                'name': self.category.name,
                'emoji': self.category.emoji,
            } if self.category else None,
            'author': self.author.to_dict() if self.author else None,
            'tags': [t.strip() for t in (self.tags or '').split(',') if t.strip()],
            'like_count': like_count,
            'comment_count': comment_count,
            'review_count': review_count,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'published_at': self.published_at.isoformat() if self.published_at else None,
        }
        if uid:
            out['user_liked'] = Like.query.filter_by(user_id=uid, submission_id=self.id).first() is not None
            out['user_bookmarked'] = Bookmark.query.filter_by(user_id=uid, submission_id=self.id).first() is not None
        return out


class Like(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    submission_id = db.Column(db.Integer, db.ForeignKey('submission.id'))
    __table_args__ = (db.UniqueConstraint('user_id', 'submission_id'),)


class Bookmark(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    submission_id = db.Column(db.Integer, db.ForeignKey('submission.id'))
    __table_args__ = (db.UniqueConstraint('user_id', 'submission_id'),)


class Comment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    submission_id = db.Column(db.Integer, db.ForeignKey('submission.id'))
    author_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    comment_type = db.Column(db.String(30), default='note')
    body = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    author = db.relationship('User')


class Review(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    submission_id = db.Column(db.Integer, db.ForeignKey('submission.id'))
    reviewer_id = db.Column(db.Integer, db.ForeignKey('user.id'))
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
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    title = db.Column(db.String(255))
    body = db.Column(db.Text)
    link = db.Column(db.String(255), default='')
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class DeskDecision(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    submission_id = db.Column(db.Integer, db.ForeignKey('submission.id'))
    decision = db.Column(db.String(20))
    overall_score = db.Column(db.Float)
    summary = db.Column(db.Text)
    encouragement = db.Column(db.Text)
    scores_json = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# --- Schema migration helpers ---
def ensure_column(table_name, column_name, ddl_sql):
    inspector = inspect(db.engine)
    cols = {c['name'] for c in inspector.get_columns(table_name)}
    if column_name in cols:
        return
    with db.engine.begin() as conn:
        conn.execute(text(ddl_sql))


def migrate_schema():
    try:
        ensure_column('user', 'orcid', "ALTER TABLE \"user\" ADD COLUMN orcid VARCHAR(32) DEFAULT ''")
    except Exception:
        pass
    try:
        ensure_column('submission', 'is_draft', "ALTER TABLE submission ADD COLUMN is_draft BOOLEAN DEFAULT 0")
    except Exception:
        pass
    try:
        ensure_column('notification', 'link', "ALTER TABLE notification ADD COLUMN link VARCHAR(255) DEFAULT ''")
    except Exception:
        pass


# --- Helpers ---
def parse_bool(value):
    if isinstance(value, bool):
        return value
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'on'}


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
            return jsonify({'error': 'Auth required'}), 401
        if getattr(current_user, 'is_banned', False):
            return jsonify({'error': 'Account restricted'}), 403
        return f(*args, **kwargs)
    return wrapped


def admin_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return jsonify({'error': 'Auth required'}), 401
        if current_user.role != 'admin':
            return jsonify({'error': 'Admin access required'}), 403
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


def category_payload_list():
    return [
        {'id': c.id, 'slug': c.slug, 'name': c.name, 'emoji': c.emoji}
        for c in Category.query.order_by(Category.id).all()
    ]


def tool_contract_payload():
    return {
        'desk_review': {
            'mode': 'text_or_document',
            'minimum_points': 0,
            'sample_fallback': False,
            'summary': 'Manuscript rubric and desk-read support.',
        },
        'ocm': {
            'mode': 'numeric_series',
            'minimum_points': 600,
            'sample_fallback': True,
            'summary': 'Contraction-rate analysis for time-series data.',
        },
        'er': {
            'mode': 'numeric_series',
            'minimum_points': 300,
            'sample_fallback': True,
            'summary': 'Topology and bridge detection for sequential data.',
        },
        'icm': {
            'mode': 'numeric_series',
            'minimum_points': 600,
            'sample_fallback': True,
            'summary': 'Invariant detection and admissibility gating.',
        },
        'clm': {
            'mode': 'numeric_series',
            'minimum_points': 100,
            'sample_fallback': True,
            'summary': 'Coherence-field simulation under structured forcing.',
        },
    }


def app_contract_payload():
    return {
        'app': {
            'name': 'The Journal',
            'phase': 'phase_1_contract_stabilization',
            'schema': 1,
        },
        'navigation': {
            'default_view': 'discover',
            'public_views': ['discover', 'published'],
            'private_views': ['builder', 'tools', 'profile'],
            'admin_view': 'admin',
        },
        'feed': {
            'tabs': [
                {'id': 'discover', 'label': 'Discover', 'endpoint': '/api/feed/discovery'},
                {'id': 'published', 'label': 'Published', 'endpoint': '/api/feed/published'},
            ]
        },
        'builder': {
            'draft_statuses': ['draft', 'submitted', 'desk_returned', 'revision_requested', 'declined', 'desk_blocked'],
            'submit_target': 'admin_queue',
        },
        'admin': {
            'queue_statuses': ['submitted', 'desk_returned', 'revision_requested'],
            'public_statuses': ['in_discovery', 'under_review', 'published'],
        },
        'submission_statuses': {
            key: {'label': Submission.STATUS_LABELS.get(key, key), 'color': Submission.STATUS_COLORS.get(key, '#6b7db3')}
            for key in Submission.STATUS_LABELS.keys()
        },
        'tools': tool_contract_payload(),
        'profile': {'orcid_supported': True},
    }


def bootstrap_payload():
    payload = {
        'contract': app_contract_payload(),
        'categories': category_payload_list(),
        'stats': {
            'published_count': Submission.query.filter_by(status='published', is_draft=False).count(),
            'discovery_count': Submission.query.filter(Submission.status.in_(['in_discovery', 'under_review']), Submission.is_draft.is_(False)).count(),
            'user_count': User.query.count(),
        },
    }
    if current_user.is_authenticated:
        payload['user'] = current_user.to_dict()
    else:
        payload['user'] = None
    return payload


# --- Research tool implementations ---
def run_desk_review(title, abstract, body):
    lower = (title + ' ' + abstract + ' ' + body).lower()
    wc = len(body.split())
    scores = {
        'scope': 0,
        'claim': 0,
        'structure': 0,
        'clarity': 0,
        'quantitative': 0,
        'citations': 0,
        'anonymity': 5,
        'good_faith': 0,
    }
    strengths = []
    suggestions = []
    physics_terms = ['energy','force','momentum','field','wave','quantum','gravity','entropy','experiment','friction','chaos','nonlinear','measurement','hypothesis','oscillat','conservation','symmetry','spacetime','curvature','manifold','topology','invariant']
    scope_hits = len([t for t in physics_terms if t in lower])
    scores['scope'] = min(5, scope_hits // 2 + 1)
    if scores['scope'] >= 4:
        strengths.append('Strong physics content with clear domain relevance.')
    else:
        suggestions.append('Add more domain-specific physics language and framing.')

    claim_terms = ['we show','we find','i find','this paper','result shows','data indicate','we conclude','we derive','we measure','we demonstrate']
    claim_hits = len([t for t in claim_terms if t in lower])
    scores['claim'] = min(5, claim_hits + 1)
    if scores['claim'] >= 4:
        strengths.append('Clear identifiable claims and research contributions.')
    else:
        suggestions.append('State the central claim and contribution more explicitly.')

    structure_terms = ['introduction','methods','method','results','discussion','conclusion','references','appendix','abstract']
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
    recommendation = 'block' if spam else 'pass' if overall >= 60 and scores['good_faith'] >= 3 else 'return'
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

    # CSV/TSV-aware parsing with best numeric column selection
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
        return {'error': 'Need at least 600 data points for OCM analysis.'}
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
        },
    }


def er_analysis(series):
    n = len(series)
    wz = 100
    stride = 50
    threshold = 0.5
    eps = 1e-8
    if n < wz * 3:
        return {'error': 'Need at least 300 data points for topology mapping.'}
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
        },
    }


def icm_analysis(series):
    n = len(series)
    wz = 200
    eps = 1e-8
    if n < wz * 3:
        return {'error': 'Need at least 600 data points for invariant detection.'}
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
    # Lightweight structural simulation preserving the spirit of the earlier CLM tool
    k = 7
    n = 64
    rule = 110
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
    if not series:
        series = generate_duffing(1200)
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
@app.get('/healthz')
def healthz():
    return jsonify({'ok': True}), 200


@app.route('/')
def index():
    return send_file(BASE / 'index.html')


@app.route('/manifest.json')
def manifest():
    return send_file(BASE / 'manifest.json')


@app.route('/sw.js')
def service_worker():
    return send_file(BASE / 'sw.js')


@app.route('/static/<path:p>')
def static_files(p):
    return send_from_directory(BASE / 'static', p)


# --- Auth ---
@app.route('/api/auth/register', methods=['POST'])
def register():
    data = request_data()
    email = (data.get('email') or '').strip().lower()
    display_name = (data.get('display_name') or '').strip()
    pw = data.get('password', '')
    if not email or not display_name or len(pw) < 6:
        return jsonify({'error': 'All fields required, password 6+ chars'}), 400
    try:
        user = User(email=email, display_name=display_name)
        user.set_password(pw)
        db.session.add(user)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({'error': 'Email taken'}), 409
    login_user(user, remember=True)
    return jsonify({'user': user.to_dict()}), 201


@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request_data()
    email = (data.get('email') or '').strip().lower()
    pw = data.get('password', '')
    if not email or not pw:
        return jsonify({'error': 'Email and password required'}), 400
    user = User.query.filter_by(email=email).first()
    if not user or not user.check_password(pw):
        return jsonify({'error': 'Invalid credentials'}), 401
    if user.is_banned:
        return jsonify({'error': 'Account restricted'}), 403
    login_user(user, remember=True)
    return jsonify({'user': user.to_dict()})


@app.route('/api/auth/logout', methods=['POST'])
def logout():
    logout_user()
    return jsonify({'ok': True})


@app.route('/api/auth/me')
@api_login_required
def me():
    return jsonify({'user': current_user.to_dict()})


@app.route('/api/auth/profile', methods=['POST'])
@api_login_required
def update_profile():
    data = request_data()
    if (data.get('display_name') or '').strip():
        current_user.display_name = data.get('display_name').strip()
    if 'bio' in data:
        current_user.bio = (data.get('bio') or '').strip()
    if 'avatar_color' in data and str(data.get('avatar_color')).startswith('#'):
        current_user.avatar_color = str(data.get('avatar_color'))[:7]
    if 'orcid' in data:
        current_user.orcid = re.sub(r'[^0-9Xx-]', '', str(data.get('orcid') or ''))[:32]
    db.session.commit()
    return jsonify({'user': current_user.to_dict()})


# --- Categories + feed ---
@app.route('/api/categories')
def categories():
    return jsonify({'categories': category_payload_list()})


@app.route('/api/app/bootstrap')
def app_bootstrap():
    return jsonify(bootstrap_payload())


@app.route('/api/feed/discovery')
def discovery():
    page = request.args.get('page', 1, type=int)
    q = Submission.query.filter(Submission.is_draft.is_(False), Submission.status.in_(['in_discovery', 'under_review']))
    cat = request.args.get('category_id', type=int)
    if cat:
        q = q.filter_by(category_id=cat)
    search = (request.args.get('q') or '').strip()
    if search:
        q = q.join(User, Submission.author_id == User.id).filter(or_(
            Submission.title.ilike(f'%{search}%'),
            Submission.abstract.ilike(f'%{search}%'),
            User.display_name.ilike(f'%{search}%'),
        ))
    p = q.order_by(Submission.updated_at.desc()).paginate(page=page, per_page=20, error_out=False)
    uid = current_user.id if current_user.is_authenticated else None
    return jsonify({'papers': [s.to_card(uid) for s in p.items], 'page': p.page, 'total': p.total})


@app.route('/api/feed/published')
def published():
    q = Submission.query.filter_by(status='published', is_draft=False).order_by(Submission.published_at.desc(), Submission.updated_at.desc())
    uid = current_user.id if current_user.is_authenticated else None
    return jsonify({'papers': [s.to_card(uid) for s in q.all()]})


@app.route('/api/search/suggest')
def search_suggest():
    q = (request.args.get('q') or '').strip()
    if len(q) < 2:
        return jsonify({'suggestions': []})
    base = Submission.query.filter(Submission.is_draft.is_(False), Submission.status.in_(['in_discovery', 'under_review', 'published']))
    matches = base.filter(or_(Submission.title.ilike(f'%{q}%'), Submission.abstract.ilike(f'%{q}%'))).order_by(Submission.updated_at.desc()).limit(6).all()
    suggestions = []
    for sub in matches:
        suggestions.append({
            'blind_id': sub.blind_id,
            'title': sub.title,
            'author_name': sub.author.display_name if sub.author else 'Anonymous',
            'status': sub.status,
        })
    return jsonify({'suggestions': suggestions})


# --- Builder ---
@app.route('/api/builder/drafts')
@api_login_required
def builder_drafts():
    drafts = Submission.query.filter_by(author_id=current_user.id).filter(Submission.status.in_(['draft', 'submitted', 'desk_returned', 'revision_requested', 'declined', 'desk_blocked'])).order_by(Submission.updated_at.desc()).all()
    return jsonify({'papers': [s.to_card(current_user.id, full_abstract=True) for s in drafts]})


# --- Submission CRUD ---
@app.route('/api/submissions', methods=['POST'])
@api_login_required
def create_submission():
    data = request_data()
    title = (data.get('title') or '').strip() or 'Untitled Draft'
    abstract = (data.get('abstract') or '').strip()
    body_text = (data.get('body_text') or data.get('body') or '').strip()
    tags = (data.get('tags') or '').strip()
    category_id = data.get('category_id') or data.get('category') or 1
    try:
        category_id = int(category_id)
    except Exception:
        category_id = 1
    is_draft = parse_bool(data.get('is_draft'))
    if not is_draft and (not title or not abstract or not body_text):
        return jsonify({'error': 'Title, abstract, and full paper text are required for review.'}), 400
    sub = Submission(
        blind_id=uuid.uuid4().hex[:12].upper(),
        title=title,
        abstract=abstract,
        body_text=body_text,
        tags=tags,
        author_id=current_user.id,
        category_id=category_id,
        is_draft=is_draft,
        status='draft' if is_draft else 'submitted',
    )
    db.session.add(sub)
    db.session.flush()
    desk = None
    if not is_draft:
        desk = run_desk_review(sub.title or '', sub.abstract or '', sub.body_text or '')
        db.session.add(DeskDecision(
            submission_id=sub.id,
            decision=desk['recommendation'],
            overall_score=desk['overall_score'],
            summary=desk['summary'],
            encouragement=desk['encouragement'],
            scores_json=json.dumps(desk['scores']),
        ))
    db.session.commit()
    return jsonify({'submission': sub.to_card(current_user.id, full_abstract=True), 'desk_review': desk}), 201


@app.route('/api/submissions/<bid>', methods=['GET', 'PUT', 'DELETE'])
@api_login_required
def submission_detail_edit_delete(bid):
    sub = resolve_submission(bid)
    if request.method == 'GET':
        data = sub.to_card(current_user.id, full_abstract=True)
        data['comments'] = [{
            'id': c.id,
            'author': c.author.to_dict() if c.author else None,
            'comment_type': c.comment_type,
            'body': c.body,
            'created_at': c.created_at.isoformat() if c.created_at else None,
        } for c in Comment.query.filter_by(submission_id=sub.id).order_by(Comment.created_at.asc()).all()]
        return jsonify({'submission': data})

    owner_or_admin = current_user.role == 'admin' or current_user.id == sub.author_id
    if not owner_or_admin:
        return jsonify({'error': 'Not allowed'}), 403

    if request.method == 'DELETE':
        Like.query.filter_by(submission_id=sub.id).delete()
        Bookmark.query.filter_by(submission_id=sub.id).delete()
        Comment.query.filter_by(submission_id=sub.id).delete()
        Review.query.filter_by(submission_id=sub.id).delete()
        DeskDecision.query.filter_by(submission_id=sub.id).delete()
        db.session.delete(sub)
        db.session.commit()
        return jsonify({'ok': True})

    data = request_data()
    if 'title' in data:
        sub.title = (data.get('title') or '').strip() or sub.title
    if 'abstract' in data:
        sub.abstract = (data.get('abstract') or '').strip()
    if 'body_text' in data or 'body' in data:
        sub.body_text = (data.get('body_text') or data.get('body') or '').strip()
    if 'tags' in data:
        sub.tags = (data.get('tags') or '').strip()
    if 'category_id' in data or 'category' in data:
        try:
            sub.category_id = int(data.get('category_id') or data.get('category') or sub.category_id)
        except Exception:
            pass
    if 'is_draft' in data:
        sub.is_draft = parse_bool(data.get('is_draft'))
        sub.status = 'draft' if sub.is_draft else sub.status
    if 'submit_for_review' in data and parse_bool(data.get('submit_for_review')):
        if not sub.title or not (sub.abstract or '').strip() or not (sub.body_text or '').strip():
            return jsonify({'error': 'Title, abstract, and full paper text are required for review.'}), 400
        sub.is_draft = False
        sub.status = 'submitted'
        desk = run_desk_review(sub.title or '', sub.abstract or '', sub.body_text or '')
        db.session.add(DeskDecision(
            submission_id=sub.id,
            decision=desk['recommendation'],
            overall_score=desk['overall_score'],
            summary=desk['summary'],
            encouragement=desk['encouragement'],
            scores_json=json.dumps(desk['scores']),
        ))
    db.session.commit()
    return jsonify({'submission': sub.to_card(current_user.id, full_abstract=True), 'ok': True})


@app.route('/api/submissions/<bid>/public')
def get_public_submission(bid):
    sub = resolve_submission(bid)
    if sub.is_draft or sub.status not in {'in_discovery', 'under_review', 'published'}:
        return jsonify({'error': 'Not publicly available'}), 404
    data = sub.to_card(current_user.id if current_user.is_authenticated else None, full_abstract=True)
    data['comments'] = [{
        'id': c.id,
        'author': c.author.to_dict() if c.author else None,
        'comment_type': c.comment_type,
        'body': c.body,
        'created_at': c.created_at.isoformat() if c.created_at else None,
    } for c in Comment.query.filter_by(submission_id=sub.id).order_by(Comment.created_at.asc()).all()]
    return jsonify({'submission': data})


@app.route('/api/submissions/<bid>/like', methods=['POST'])
@api_login_required
def toggle_like(bid):
    sub = resolve_submission(bid)
    existing = Like.query.filter_by(user_id=current_user.id, submission_id=sub.id).first()
    if existing:
        db.session.delete(existing)
        db.session.commit()
        count = Like.query.filter_by(submission_id=sub.id).count()
        return jsonify({'liked': False, 'like_count': count})
    db.session.add(Like(user_id=current_user.id, submission_id=sub.id))
    if sub.author_id and sub.author_id != current_user.id:
        create_notification(sub.author_id, 'New like', f'{current_user.display_name} liked your paper “{sub.title}”.', f'/paper/{sub.blind_id}')
    db.session.commit()
    count = Like.query.filter_by(submission_id=sub.id).count()
    return jsonify({'liked': True, 'like_count': count})


@app.route('/api/submissions/<bid>/bookmark', methods=['POST'])
@api_login_required
def toggle_bookmark(bid):
    sub = resolve_submission(bid)
    existing = Bookmark.query.filter_by(user_id=current_user.id, submission_id=sub.id).first()
    if existing:
        db.session.delete(existing)
        db.session.commit()
        return jsonify({'bookmarked': False})
    db.session.add(Bookmark(user_id=current_user.id, submission_id=sub.id))
    db.session.commit()
    return jsonify({'bookmarked': True})


@app.route('/api/submissions/<bid>/comments', methods=['POST'])
@api_login_required
def add_comment(bid):
    sub = resolve_submission(bid)
    if sub.is_draft and current_user.role != 'admin' and current_user.id != sub.author_id:
        return jsonify({'error': 'Not allowed'}), 403
    data = request_data()
    body = (data.get('body') or '').strip()
    if not body:
        return jsonify({'error': 'Comment body required'}), 400
    comment = Comment(
        submission_id=sub.id,
        author_id=current_user.id,
        comment_type=(data.get('comment_type') or 'note').strip()[:30],
        body=body,
    )
    db.session.add(comment)
    if sub.author_id and sub.author_id != current_user.id:
        create_notification(sub.author_id, 'New comment', f'{current_user.display_name} commented on “{sub.title}”.', f'/paper/{sub.blind_id}')
    db.session.commit()
    return jsonify({'comment': {
        'id': comment.id,
        'author': current_user.to_dict(),
        'comment_type': comment.comment_type,
        'body': comment.body,
        'created_at': comment.created_at.isoformat() if comment.created_at else None,
    }}), 201


# --- Users ---
@app.route('/api/users/<int:uid>')
def get_user(uid):
    user = User.query.get_or_404(uid)
    data = user.to_dict()
    visible_statuses = ['in_discovery', 'under_review', 'published']
    data['papers'] = [s.to_card() for s in Submission.query.filter_by(author_id=uid).filter(Submission.status.in_(visible_statuses)).order_by(Submission.updated_at.desc()).limit(20).all()]
    return jsonify({'user': data})


@app.route('/api/users/<int:uid>/follow', methods=['POST'])
@api_login_required
def follow_toggle(uid):
    user = User.query.get_or_404(uid)
    if user.id == current_user.id:
        return jsonify({'error': 'Cannot follow yourself'}), 400
    if current_user.following.filter_by(id=user.id).first():
        current_user.following.remove(user)
        db.session.commit()
        return jsonify({'following': False})
    current_user.following.append(user)
    create_notification(user.id, 'New follower', f'{current_user.display_name} followed you.', f'/profile/{current_user.id}')
    db.session.commit()
    return jsonify({'following': True})


# --- Notifications ---
@app.route('/api/notifications')
@api_login_required
def notifications():
    notes = Notification.query.filter_by(user_id=current_user.id).order_by(Notification.created_at.desc()).limit(50).all()
    return jsonify({'notifications': [{
        'id': n.id,
        'title': n.title,
        'body': n.body,
        'link': n.link,
        'is_read': n.is_read,
        'created_at': n.created_at.isoformat() if n.created_at else None,
    } for n in notes], 'unread_count': Notification.query.filter_by(user_id=current_user.id, is_read=False).count()})


@app.route('/api/notifications/count')
@api_login_required
def notification_count():
    count = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
    return jsonify({'count': count})


@app.route('/api/notifications/read', methods=['POST'])
@api_login_required
def notification_read():
    data = request_data()
    nid = data.get('id')
    if nid:
        note = Notification.query.filter_by(id=nid, user_id=current_user.id).first_or_404()
        note.is_read = True
    else:
        Notification.query.filter_by(user_id=current_user.id, is_read=False).update({'is_read': True})
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/stats')
def stats():
    return jsonify({
        'published_count': Submission.query.filter_by(status='published', is_draft=False).count(),
        'discovery_count': Submission.query.filter(Submission.status.in_(['in_discovery', 'under_review']), Submission.is_draft.is_(False)).count(),
        'user_count': User.query.count(),
    })


# --- Admin ---
@app.route('/api/admin/submissions')
@admin_required
def admin_submissions():
    scope = (request.args.get('scope') or 'queue').strip().lower()
    q = Submission.query
    if scope == 'queue':
        q = q.filter(Submission.status.in_(['submitted', 'desk_returned', 'revision_requested']))
    elif scope == 'public':
        q = q.filter(Submission.status.in_(['in_discovery', 'under_review', 'published']))
    subs = q.order_by(Submission.updated_at.desc()).limit(200).all()
    return jsonify({'submissions': [s.to_card(full_abstract=True) for s in subs]})


@app.route('/api/admin/submissions/<ref>/status', methods=['POST'])
@admin_required
def admin_update_status(ref):
    sub = resolve_submission(ref)
    data = request_data()
    status = (data.get('status') or '').strip()
    allowed = {'submitted', 'in_discovery', 'under_review', 'published', 'desk_returned', 'declined', 'revision_requested', 'desk_blocked', 'draft'}
    if status not in allowed:
        return jsonify({'error': 'Invalid status'}), 400
    sub.status = status
    sub.is_draft = (status == 'draft')
    if status == 'published' and not sub.published_at:
        sub.published_at = datetime.utcnow()
    if status != 'published':
        sub.published_at = sub.published_at if sub.status == 'published' else sub.published_at
    create_notification(sub.author_id, 'Paper status updated', f'Your paper “{sub.title}” was moved to {Submission.STATUS_LABELS.get(status, status)}.', f'/paper/{sub.blind_id}')
    db.session.commit()
    return jsonify({'ok': True, 'submission': sub.to_card(sub.author_id, full_abstract=True)})


@app.route('/api/admin/submissions/<ref>', methods=['DELETE'])
@admin_required
def admin_delete_submission(ref):
    sub = resolve_submission(ref)
    Like.query.filter_by(submission_id=sub.id).delete()
    Bookmark.query.filter_by(submission_id=sub.id).delete()
    Comment.query.filter_by(submission_id=sub.id).delete()
    Review.query.filter_by(submission_id=sub.id).delete()
    DeskDecision.query.filter_by(submission_id=sub.id).delete()
    db.session.delete(sub)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/admin/users')
@admin_required
def admin_users():
    users = User.query.order_by(User.created_at.desc()).limit(200).all()
    return jsonify({'users': [{
        'id': u.id,
        'email': u.email,
        'display_name': u.display_name,
        'role': u.role,
        'orcid': u.orcid or '',
        'avatar_color': u.avatar_color,
        'reputation_score': round(u.reputation or 0, 2),
        'is_banned': u.is_banned,
        'paper_count': Submission.query.filter_by(author_id=u.id).count(),
        'created_at': u.created_at.isoformat() if u.created_at else None,
    } for u in users]})


@app.route('/api/admin/users/<int:uid>/role', methods=['POST'])
@admin_required
def admin_set_role(uid):
    user = User.query.get_or_404(uid)
    data = request_data()
    role = (data.get('role') or 'member').strip()
    if role not in {'member', 'reviewer', 'admin'}:
        return jsonify({'error': 'Invalid role'}), 400
    user.role = role
    db.session.commit()
    return jsonify({'ok': True, 'user': user.to_dict()})


@app.route('/api/admin/users/<int:uid>/ban', methods=['POST'])
@admin_required
def admin_ban_user(uid):
    user = User.query.get_or_404(uid)
    user.is_banned = True
    db.session.commit()
    return jsonify({'ok': True, 'user': user.to_dict()})


@app.route('/api/admin/users/<int:uid>/unban', methods=['POST'])
@admin_required
def admin_unban_user(uid):
    user = User.query.get_or_404(uid)
    user.is_banned = False
    db.session.commit()
    return jsonify({'ok': True, 'user': user.to_dict()})


@app.route('/api/admin/users/<int:uid>', methods=['DELETE'])
@admin_required
def admin_delete_user(uid):
    user = User.query.get_or_404(uid)
    if user.role == 'admin':
        return jsonify({'error': 'Cannot delete admin'}), 400
    authored = Submission.query.filter_by(author_id=uid).all()
    for sub in authored:
        Like.query.filter_by(submission_id=sub.id).delete()
        Bookmark.query.filter_by(submission_id=sub.id).delete()
        Comment.query.filter_by(submission_id=sub.id).delete()
        Review.query.filter_by(submission_id=sub.id).delete()
        DeskDecision.query.filter_by(submission_id=sub.id).delete()
        db.session.delete(sub)
    Comment.query.filter_by(author_id=uid).delete()
    Notification.query.filter_by(user_id=uid).delete()
    Like.query.filter_by(user_id=uid).delete()
    Bookmark.query.filter_by(user_id=uid).delete()
    db.session.delete(user)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/admin/reset-db', methods=['POST'])
@admin_required
def admin_reset_db():
    for sub in Submission.query.all():
        Like.query.filter_by(submission_id=sub.id).delete()
        Bookmark.query.filter_by(submission_id=sub.id).delete()
        Comment.query.filter_by(submission_id=sub.id).delete()
        Review.query.filter_by(submission_id=sub.id).delete()
        DeskDecision.query.filter_by(submission_id=sub.id).delete()
        db.session.delete(sub)
    for user in User.query.filter(User.role != 'admin').all():
        Notification.query.filter_by(user_id=user.id).delete()
        db.session.delete(user)
    db.session.commit()
    return jsonify({'ok': True})


# --- Tools API ---
@app.route('/api/tools/run', methods=['POST'])
@api_login_required
def run_tool():
    tool = (request.form.get('tool') or request.json.get('tool') if request.is_json else '') or ''
    tool = str(tool).strip()
    input_text = (request.form.get('input') if request.form else '') or ((request.get_json(silent=True) or {}).get('input') or '')
    file = request.files.get('file')

    if tool == 'desk_review':
        body = input_text or ''
        result = run_desk_review('Untitled', body[:500], body)
        return jsonify({
            'summary': result['summary'],
            'classification': result['recommendation'].upper(),
            'details': {
                'scores': result['scores'],
                'overall_score': result['overall_score'],
                'recommendation': result['recommendation'],
                'encouragement': result['encouragement'],
                'strengths': result['strengths'],
                'suggestions': result['suggestions'],
            },
        })

    series = parse_series(input_text, file, min_points=0)
    used_sample = False
    if tool in {'ocm', 'er', 'icm', 'clm'} and len(series) < (600 if tool in {'ocm', 'icm'} else 300 if tool == 'er' else 100):
        used_sample = True
        series = generate_duffing(5000) if tool in {'ocm', 'clm'} else generate_lorenz(5000) if tool == 'er' else generate_noise(5000)

    if tool == 'ocm':
        result = ocm_analysis(series)
    elif tool == 'er':
        result = er_analysis(series)
    elif tool == 'icm':
        result = icm_analysis(series)
    elif tool == 'clm':
        result = clm_analysis(series)
    else:
        return jsonify({'error': 'Unknown tool'}), 400

    if used_sample and 'error' not in result:
        result['summary'] = (result.get('summary') or '') + ' No numeric dataset was supplied, so a built-in sample series was used.'
    return jsonify(result)


# --- Seed + boot ---
def seed():
    cats = [
        ('foundations', 'Foundations of Physics', '🌌'),
        ('math-physics', 'Mathematical Physics', '📐'),
        ('nonlinear', 'Nonlinear Dynamics', '🌀'),
        ('stat-mech', 'Statistical Mechanics', '⚛️'),
        ('complex', 'Complex Systems', '🕸️'),
        ('experimental', 'Experimental & Observational', '🔬'),
    ]
    for slug, name, emoji in cats:
        if not Category.query.filter_by(slug=slug).first():
            db.session.add(Category(slug=slug, name=name, emoji=emoji))
    if not User.query.filter_by(email='admin@journal.local').first():
        admin = User(email='admin@journal.local', display_name='Founding Editor', role='admin')
        admin.set_password('change-me-now')
        db.session.add(admin)
    db.session.commit()


with app.app_context():
    db.create_all()
    migrate_schema()
    seed()


if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=os.getenv('FLASK_DEBUG', '0') == '1')
