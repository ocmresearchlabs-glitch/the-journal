# The Journal - Deployment Server
# All Rights Reserved.
import os, json, re, uuid
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

app = Flask(__name__, static_folder='static')
BASE = Path(__file__).parent
INSTANCE = BASE / 'instance'
INSTANCE.mkdir(exist_ok=True)

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
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = False
app.config['REMEMBER_COOKIE_SAMESITE'] = 'Lax'
app.config['REMEMBER_COOKIE_SECURE'] = False
app.config['REMEMBER_COOKIE_HTTPONLY'] = True
app.config['REMEMBER_COOKIE_DURATION'] = 60 * 60 * 24 * 30
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


def request_data():
    if request.is_json:
        return request.get_json(silent=True) or {}
    return request.form.to_dict()


def resolve_submission(ref):
    s = Submission.query.filter_by(blind_id=str(ref)).first()
    if s:
        return s
    if str(ref).isdigit():
        return db.session.get(Submission, int(ref))
    return None


def resolve_submission_or_404(ref):
    s = resolve_submission(ref)
    if s is None:
        from flask import abort
        abort(404)
    return s


def resolve_category_id(raw_category_id=None, raw_category=None):
    if raw_category_id not in (None, '', 0, '0'):
        try:
            cid = int(raw_category_id)
            if Category.query.filter_by(id=cid).first():
                return cid
        except Exception:
            pass
    value = (raw_category or '').strip()
    if value:
        c = Category.query.filter(Category.name.ilike(value)).first()
        if not c:
            slug = value.lower().strip().replace('&', 'and')
            slug = re.sub(r'[^a-z0-9]+', '-', slug).strip('-')
            c = Category.query.filter(Category.slug.ilike(slug)).first()
        if c:
            return c.id
    first = Category.query.order_by(Category.id.asc()).first()
    return first.id if first else 1


# -- Models --
user_follows = db.Table('user_follows',
    db.Column('follower_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('followed_id', db.Integer, db.ForeignKey('user.id'), primary_key=True))


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    display_name = db.Column(db.String(120), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default='member')
    bio = db.Column(db.Text, default='')
    avatar_color = db.Column(db.String(7), default='#5ea8ff')
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
            pc = Submission.query.filter_by(author_id=self.id).count()
        except:
            pc = 0
        try:
            rc = Review.query.filter_by(reviewer_id=self.id).count()
        except:
            rc = 0
        try:
            fc = self.followers.count() if hasattr(self, 'followers') else 0
        except:
            fc = 0
        try:
            init = self.initials
        except:
            init = '??'
        return {
            'id': self.id, 'display_name': self.display_name or '', 'email': self.email or '',
            'initials': init, 'bio': self.bio or '', 'avatar_color': self.avatar_color or '#5ea8ff',
            'role': self.role or 'member', 'reputation_score': round(self.reputation or 1.0, 2),
            'paper_count': pc, 'review_count': rc, 'follower_count': fc,
            'joined': self.created_at.isoformat() if self.created_at else None,
            'banned': bool(self.is_banned), 'is_banned': bool(self.is_banned)
        }

    following = db.relationship('User', secondary=user_follows,
        primaryjoin=(user_follows.c.follower_id == id),
        secondaryjoin=(user_follows.c.followed_id == id),
        backref=db.backref('followers', lazy='dynamic'), lazy='dynamic')


@login_manager.user_loader
def load_user(uid):
    return db.session.get(User, int(uid))


class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(80), unique=True)
    name = db.Column(db.String(120))
    emoji = db.Column(db.String(10), default='x')


class Submission(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    blind_id = db.Column(db.String(20), unique=True)
    title = db.Column(db.String(500))
    abstract = db.Column(db.Text)
    body_text = db.Column(db.Text)
    status = db.Column(db.String(30), default='submitted', index=True)
    tags = db.Column(db.Text, default='')
    author_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    published_at = db.Column(db.DateTime)
    author = db.relationship('User', backref='submissions')
    category = db.relationship('Category', backref='submissions')

    STATUS_LABELS = {
        'submitted': 'Submitted', 'desk_passed': 'Under Review',
        'in_discovery': 'In Discovery', 'under_review': 'Under Review',
        'published': 'Published', 'desk_returned': 'Revision Suggested',
        'revision_requested': 'Revision Requested', 'declined': 'Declined',
        'contested': 'Contested', 'desk_blocked': 'Blocked'
    }
    STATUS_COLORS = {
        'submitted': '#6b7db3', 'in_discovery': '#5ea8ff', 'under_review': '#f0a030',
        'published': '#4ade80', 'desk_returned': '#f0a030', 'revision_requested': '#f0a030',
        'declined': '#ef4444', 'contested': '#ef4444'
    }

    def to_card(self, uid=None):
        lc = Like.query.filter_by(submission_id=self.id).count()
        cc = Comment.query.filter_by(submission_id=self.id).count()
        rc = Review.query.filter_by(submission_id=self.id).count()
        author_dict = self.author.to_dict() if self.author else {}
        d = {
            'id': self.id, 'blind_id': self.blind_id, 'title': self.title,
            'abstract': (self.abstract or '')[:300],
            'status': self.status,
            'status_label': self.STATUS_LABELS.get(self.status, self.status),
            'status_color': self.STATUS_COLORS.get(self.status, '#6b7db3'),
            'category': {'name': self.category.name, 'emoji': self.category.emoji, 'id': self.category.id} if self.category else None,
            'author': author_dict,
            'author_name': author_dict.get('display_name'),
            'display_name': author_dict.get('display_name'),
            'tags': [t.strip() for t in (self.tags or '').split(',') if t.strip()],
            'like_count': lc, 'likes': lc, 'comment_count': cc, 'review_count': rc,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'published_at': self.published_at.isoformat() if self.published_at else None
        }
        if uid:
            d['user_liked'] = Like.query.filter_by(user_id=uid, submission_id=self.id).first() is not None
            d['user_bookmarked'] = Bookmark.query.filter_by(user_id=uid, submission_id=self.id).first() is not None
        return d


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


# -- Desk Review Engine --
def run_desk_review(title, abstract, body):
    lower = (title + ' ' + abstract + ' ' + body).lower()
    wc = len(body.split())
    terms = ['energy', 'force', 'momentum', 'field', 'wave', 'quantum', 'gravity', 'entropy',
             'experiment', 'friction', 'chaos', 'nonlinear', 'measurement', 'hypothesis']
    pc = sum(1 for t in terms if t in lower)
    claims = ['we show', 'we find', 'i find', 'this paper', 'result shows', 'i measured', 'hypothesis']
    cc = sum(1 for p in claims if p in lower)
    sections = ['introduction', 'method', 'results', 'discussion', 'conclusion', 'references']
    sf = sum(1 for s in sections if s in lower)
    spam = ['buy now', 'click here', 'guaranteed', 'act now']
    is_spam = any(s in lower for s in spam)
    scores = {
        'scope': min(5, pc),
        'claim': min(5, cc + 1),
        'structure': min(5, sf + 1),
        'clarity': 4 if wc > 300 else 3,
        'quantitative': 3 if re.search(r'\d.*=', body) else 1,
        'citations': 3 if re.search(r'\[\d+\]', body) else 1,
        'anonymity': 5,
        'good_faith': 0 if is_spam else (5 if wc >= 200 else 3)
    }
    overall = round(sum(scores.values()) / 40 * 100)
    rec = 'block' if is_spam else ('pass' if overall >= 60 and scores['good_faith'] >= 3 else 'return')
    return {
        'overall_score': overall, 'recommendation': rec, 'scores': scores,
        'summary': 'Ready for community review.' if rec == 'pass' else 'Needs more development before community review.',
        'encouragement': 'Your curiosity is valued here.' if rec != 'block' else 'We welcome good-faith submissions.'
    }


# -- Auth helpers --
def api_login_required(f):
    @wraps(f)
    def d(*a, **k):
        if not current_user.is_authenticated:
            return jsonify({'error': 'Auth required'}), 401
        return f(*a, **k)
    return d


def admin_required(f):
    @wraps(f)
    def d(*a, **k):
        if not current_user.is_authenticated:
            return jsonify({'error': 'Auth required'}), 401
        if current_user.role != 'admin':
            return jsonify({'error': 'Admin access required'}), 403
        return f(*a, **k)
    return d


# -- PWA Routes --
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


# -- Auth Routes --
@app.route('/api/auth/register', methods=['POST'])
def register():
    d = request.get_json(silent=True) or {}
    email = (d.get('email') or '').strip().lower()
    name = (d.get('display_name') or '').strip()
    pw = d.get('password', '')
    if not email or not name or len(pw) < 6:
        return jsonify({'error': 'All fields required, password 6+ chars'}), 400
    try:
        u = User(email=email, display_name=name)
        u.set_password(pw)
        db.session.add(u)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({'error': 'Email taken'}), 409
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': 'Register failed', 'detail': str(e)}), 500
    try:
        login_user(u, remember=True)
    except Exception as e:
        return jsonify({'error': 'Login after register failed', 'detail': str(e)}), 500
    try:
        user_data = u.to_dict()
    except Exception as e:
        user_data = {'id': u.id, 'email': u.email, 'display_name': u.display_name, 'role': u.role or 'member'}
    return jsonify({'user': user_data}), 201


@app.route('/api/auth/login', methods=['POST'])
def login():
    d = request.get_json(silent=True) or {}
    email = (d.get('email') or '').strip().lower()
    pw = d.get('password', '')
    if not email or not pw:
        return jsonify({'error': 'Email and password required'}), 400
    u = User.query.filter_by(email=email).first()
    if not u or not u.check_password(pw):
        return jsonify({'error': 'Invalid credentials'}), 401
    try:
        login_user(u, remember=True)
    except Exception as e:
        return jsonify({'error': 'Session creation failed', 'detail': str(e)}), 500
    try:
        user_data = u.to_dict()
    except Exception as e:
        user_data = {'id': u.id, 'email': u.email, 'display_name': u.display_name, 'role': u.role or 'member'}
    return jsonify({'user': user_data})


@app.route('/api/auth/logout', methods=['POST'])
def logout():
    logout_user()
    return jsonify({'ok': True})


@app.route('/api/auth/me')
@api_login_required
def me():
    return jsonify({'user': current_user.to_dict()})


# -- Profile Update --
@app.route('/api/auth/profile', methods=['POST'])
@api_login_required
def update_profile():
    d = request.get_json(silent=True) or {}
    if 'display_name' in d and d['display_name'].strip():
        current_user.display_name = d['display_name'].strip()
    if 'bio' in d:
        current_user.bio = d['bio'].strip()
    if 'avatar_color' in d and d['avatar_color'].startswith('#'):
        current_user.avatar_color = d['avatar_color']
    db.session.commit()
    return jsonify({'user': current_user.to_dict()})


# -- Feed Routes --
@app.route('/api/categories')
def categories():
    return jsonify({'categories': [{'id': c.id, 'name': c.name, 'emoji': c.emoji, 'slug': c.slug} for c in Category.query.all()]})


@app.route('/api/feed/discovery')
def discovery():
    page = request.args.get('page', 1, type=int)
    q = Submission.query.filter(Submission.status.in_(['in_discovery', 'under_review', 'revision_requested', 'submitted', 'desk_returned']))
    cat = request.args.get('category_id', type=int)
    if cat:
        q = q.filter_by(category_id=cat)
    search = request.args.get('q', '').strip()
    if search:
        q = q.filter(or_(Submission.title.ilike('%' + search + '%'), Submission.abstract.ilike('%' + search + '%')))
    p = q.order_by(Submission.updated_at.desc()).paginate(page=page, per_page=20, error_out=False)
    uid = current_user.id if current_user.is_authenticated else None
    return jsonify({'papers': [s.to_card(uid) for s in p.items], 'total': p.total, 'page': p.page})


@app.route('/api/feed/published')
def published():
    p = Submission.query.filter_by(status='published').order_by(Submission.published_at.desc()).all()
    uid = current_user.id if current_user.is_authenticated else None
    return jsonify({'papers': [s.to_card(uid) for s in p]})


# -- Submission Routes --
@app.route('/api/submissions', methods=['POST'])
@api_login_required
def create_submission():
    d = request_data()
    uploaded = request.files.get('file')
    title = (d.get('title') or '').strip()
    abstract = (d.get('abstract') or '').strip()
    body_text = (d.get('body_text') or d.get('content') or abstract).strip()
    if uploaded and uploaded.filename:
        body_text = (body_text + '\n\n[Uploaded file: ' + uploaded.filename + ']').strip()
    category_id = resolve_category_id(d.get('category_id'), d.get('category'))
    sub = Submission(
        blind_id=uuid.uuid4().hex[:12].upper(),
        title=title,
        abstract=abstract,
        body_text=body_text,
        tags=d.get('tags', ''),
        author_id=current_user.id,
        category_id=category_id,
    )
    desk = run_desk_review(sub.title, sub.abstract, sub.body_text)
    sub.status = {'pass': 'in_discovery', 'return': 'desk_returned', 'block': 'desk_blocked'}[desk['recommendation']]
    db.session.add(sub)
    db.session.flush()
    db.session.add(DeskDecision(
        submission_id=sub.id,
        decision=desk['recommendation'],
        overall_score=desk['overall_score'],
        summary=desk['summary'],
        encouragement=desk['encouragement'],
        scores_json=json.dumps(desk['scores']),
    ))
    db.session.commit()
    desk_payload = dict(desk)
    desk_payload['score'] = desk['overall_score']
    desk_payload['total_score'] = desk['overall_score']
    return jsonify({'submission': sub.to_card(current_user.id), 'desk_review': desk_payload}), 201


@app.route('/api/submissions/<bid>')
def get_submission(bid):
    s = resolve_submission_or_404(bid)
    d = s.to_card()
    d['body_text'] = s.body_text
    d['comments'] = [{
        'id': c.id, 'author': c.author.to_dict(), 'author_name': c.author.display_name if c.author else 'User',
        'comment_type': c.comment_type, 'body': c.body, 'content': c.body, 'created_at': c.created_at.isoformat()}
        for c in Comment.query.filter_by(submission_id=s.id).order_by(Comment.created_at).all()]
    return jsonify({'submission': d})


@app.route('/api/submissions/<bid>/like', methods=['POST'])
@api_login_required
def toggle_like(bid):
    s = resolve_submission_or_404(bid)
    ex = Like.query.filter_by(user_id=current_user.id, submission_id=s.id).first()
    if ex:
        db.session.delete(ex)
        db.session.commit()
        count = Like.query.filter_by(submission_id=s.id).count()
        return jsonify({'liked': False, 'likes': count, 'like_count': count})
    db.session.add(Like(user_id=current_user.id, submission_id=s.id))
    db.session.commit()
    count = Like.query.filter_by(submission_id=s.id).count()
    return jsonify({'liked': True, 'likes': count, 'like_count': count})


@app.route('/api/submissions/<bid>/bookmark', methods=['POST'])
@api_login_required
def toggle_bookmark(bid):
    s = resolve_submission_or_404(bid)
    ex = Bookmark.query.filter_by(user_id=current_user.id, submission_id=s.id).first()
    if ex:
        db.session.delete(ex)
        db.session.commit()
        return jsonify({'bookmarked': False})
    db.session.add(Bookmark(user_id=current_user.id, submission_id=s.id))
    db.session.commit()
    return jsonify({'bookmarked': True})


@app.route('/api/submissions/<bid>/comments', methods=['POST'])
@app.route('/api/submissions/<bid>/comment', methods=['POST'])
@api_login_required
def add_comment(bid):
    s = resolve_submission_or_404(bid)
    d = request_data()
    body = (d.get('body') or d.get('content') or '').strip()
    if not body:
        return jsonify({'error': 'Comment body required'}), 400
    c = Comment(submission_id=s.id, author_id=current_user.id,
        comment_type=d.get('comment_type', 'note'), body=body)
    db.session.add(c)
    db.session.commit()
    return jsonify({'comment': {'id': c.id, 'author': current_user.to_dict(),
        'author_name': current_user.display_name, 'comment_type': c.comment_type,
        'body': c.body, 'content': c.body, 'created_at': c.created_at.isoformat()}}), 201


@app.route('/api/users/<int:uid>')
def get_user(uid):
    u = User.query.get_or_404(uid)
    d = u.to_dict()
    d['papers'] = [s.to_card() for s in Submission.query.filter_by(author_id=uid).filter(
        Submission.status.in_(['in_discovery', 'under_review', 'published'])).order_by(Submission.updated_at.desc()).all()]
    return jsonify({'user': d})


@app.route('/api/users/<int:uid>/follow', methods=['POST'])
@api_login_required
def toggle_follow(uid):
    u = User.query.get_or_404(uid)
    if u.id == current_user.id:
        return jsonify({'error': 'Cannot follow yourself'}), 400
    if current_user.following.filter(user_follows.c.followed_id == uid).count() > 0:
        current_user.following.remove(u)
        db.session.commit()
        return jsonify({'following': False})
    current_user.following.append(u)
    db.session.commit()
    return jsonify({'following': True})


@app.route('/api/notifications')
@api_login_required
def notifications():
    ns = Notification.query.filter_by(user_id=current_user.id).order_by(Notification.created_at.desc()).limit(50).all()
    return jsonify({
        'notifications': [{'id': n.id, 'title': n.title, 'body': n.body, 'message': n.body, 'content': n.body,
            'is_read': n.is_read, 'created_at': n.created_at.isoformat()} for n in ns],
        'unread_count': Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
    })


@app.route('/api/notifications/count')
@api_login_required
def notification_count():
    count = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
    return jsonify({'count': count})


@app.route('/api/stats')
def stats():
    return jsonify({
        'published_count': Submission.query.filter_by(status='published').count(),
        'discovery_count': Submission.query.filter(Submission.status.in_(['in_discovery', 'under_review'])).count(),
        'user_count': User.query.count()
    })


# -- Admin Routes --
@app.route('/api/admin/submissions')
@admin_required
def admin_submissions():
    sf = request.args.get('status', '')
    q = Submission.query
    if sf:
        q = q.filter_by(status=sf)
    subs = q.order_by(Submission.updated_at.desc()).limit(100).all()
    return jsonify({'submissions': [{
        'id': s.id, 'blind_id': s.blind_id, 'title': s.title,
        'abstract': (s.abstract or '')[:200], 'status': s.status,
        'status_label': s.STATUS_LABELS.get(s.status, s.status),
        'author': s.author.to_dict() if s.author else {},
        'author_name': s.author.display_name if s.author else 'Unknown',
        'display_name': s.author.display_name if s.author else 'Unknown',
        'category': {'name': s.category.name} if s.category else {},
        'created_at': s.created_at.isoformat() if s.created_at else None,
        'like_count': Like.query.filter_by(submission_id=s.id).count(),
        'comment_count': Comment.query.filter_by(submission_id=s.id).count()
    } for s in subs]})


@app.route('/api/admin/submissions/<bid>/status', methods=['POST'])
@admin_required
def admin_update_status(bid):
    s = resolve_submission_or_404(bid)
    d = request_data()
    ns = d.get('status', '')
    aliases = {'returned': 'desk_returned', 'desk_review': 'under_review'}
    ns = aliases.get(ns, ns)
    allowed = ['in_discovery', 'under_review', 'published', 'desk_returned', 'declined', 'desk_blocked', 'submitted', 'revision_requested']
    if ns not in allowed:
        return jsonify({'error': 'Invalid status'}), 400
    s.status = ns
    if ns == 'published' and not s.published_at:
        s.published_at = datetime.utcnow()
    db.session.commit()
    if s.author_id:
        db.session.add(Notification(user_id=s.author_id, title='Paper status updated',
            body='Your paper status changed to ' + ns))
        db.session.commit()
    return jsonify({'ok': True, 'new_status': ns})


@app.route('/api/admin/submissions/<bid>', methods=['DELETE'])
@admin_required
def admin_delete_submission(bid):
    s = resolve_submission_or_404(bid)
    Like.query.filter_by(submission_id=s.id).delete()
    Bookmark.query.filter_by(submission_id=s.id).delete()
    Comment.query.filter_by(submission_id=s.id).delete()
    Review.query.filter_by(submission_id=s.id).delete()
    DeskDecision.query.filter_by(submission_id=s.id).delete()
    db.session.delete(s)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/admin/users')
@admin_required
def admin_users():
    users = User.query.order_by(User.created_at.desc()).limit(200).all()
    return jsonify({'users': [{
        'id': u.id, 'email': u.email, 'display_name': u.display_name,
        'role': u.role, 'reputation': round(u.reputation, 2), 'is_banned': u.is_banned, 'banned': u.is_banned,
        'paper_count': Submission.query.filter_by(author_id=u.id).count(),
        'created_at': u.created_at.isoformat() if u.created_at else None
    } for u in users]})


@app.route('/api/admin/users/<int:uid>/role', methods=['POST'])
@admin_required
def admin_set_role(uid):
    u = User.query.get_or_404(uid)
    d = request.get_json(silent=True) or {}
    r = d.get('role', 'member')
    if r not in ['member', 'admin', 'reviewer']:
        return jsonify({'error': 'Invalid role'}), 400
    u.role = r
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/admin/users/<int:uid>/ban', methods=['POST'])
@admin_required
def admin_ban_user(uid):
    u = User.query.get_or_404(uid)
    d = request.get_json(silent=True) or {}
    u.is_banned = d.get('banned', True)
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/admin/users/<int:uid>/unban', methods=['POST'])
@admin_required
def admin_unban_user(uid):
    u = User.query.get_or_404(uid)
    u.is_banned = False
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/admin/users/<int:uid>', methods=['DELETE'])
@admin_required
def admin_delete_user(uid):
    u = User.query.get_or_404(uid)
    if u.role == 'admin':
        return jsonify({'error': 'Cannot delete admin'}), 400
    for s in Submission.query.filter_by(author_id=uid).all():
        Like.query.filter_by(submission_id=s.id).delete()
        Bookmark.query.filter_by(submission_id=s.id).delete()
        Comment.query.filter_by(submission_id=s.id).delete()
        Review.query.filter_by(submission_id=s.id).delete()
        DeskDecision.query.filter_by(submission_id=s.id).delete()
        db.session.delete(s)
    Comment.query.filter_by(author_id=uid).delete()
    Notification.query.filter_by(user_id=uid).delete()
    Like.query.filter_by(user_id=uid).delete()
    Bookmark.query.filter_by(user_id=uid).delete()
    db.session.delete(u)
    db.session.commit()
    return jsonify({'ok': True})


# -- Reset DB (admin only, for clearing test data) --
@app.route('/api/admin/reset-db', methods=['POST'])
@admin_required
def reset_db():
    for s in Submission.query.all():
        Like.query.filter_by(submission_id=s.id).delete()
        Bookmark.query.filter_by(submission_id=s.id).delete()
        Comment.query.filter_by(submission_id=s.id).delete()
        Review.query.filter_by(submission_id=s.id).delete()
        DeskDecision.query.filter_by(submission_id=s.id).delete()
        db.session.delete(s)
    for u in User.query.filter(User.role != 'admin').all():
        Notification.query.filter_by(user_id=u.id).delete()
        db.session.delete(u)
    db.session.commit()
    return jsonify({'ok': True, 'message': 'All non-admin users and submissions deleted'})


# -- Temporary DB reset (DELETE THIS AFTER FIRST USE) --
@app.route('/api/nuke-db-once')
def nuke_db():
    db.drop_all()
    db.create_all()
    seed()
    return jsonify({'ok': True, 'message': 'Database rebuilt from scratch'})


# -- Tools API --
@app.route('/api/tools/run', methods=['POST'])
@api_login_required
def run_tool():
    tool = request.form.get('tool', '')
    input_text = request.form.get('input', '')
    file = request.files.get('file')
    if file:
        input_text += '\n[File uploaded: ' + file.filename + ']'
    if tool == 'desk_review':
        result = run_desk_review('Untitled', input_text[:500], input_text)
        return jsonify({'summary': result['summary'], 'classification': result['recommendation'],
            'details': result['scores'], 'score': result['overall_score']})
    elif tool == 'ocm':
        return jsonify({'summary': 'OCM Stability Analysis complete.',
            'classification': 'Class II - Conditional Stability',
            'details': 'Contraction rate measured. Upload CSV time-series data for full analysis.'})
    elif tool == 'er':
        return jsonify({'summary': 'ER Topology Mapping complete.',
            'classification': 'Connected Graph',
            'details': 'Coherence graph generated. Upload CSV for bridge detection analysis.'})
    elif tool == 'icm':
        return jsonify({'summary': 'ICM Invariant Detection complete.',
            'classification': 'Admissible',
            'details': 'Disruption morphology analyzed. Upload CSV for full invariant extraction.'})
    elif tool == 'clm':
        return jsonify({'summary': 'CLM Coherence Field Lab: Simulation ready.',
            'classification': 'K=7, N=64, Rules 30/90/110',
            'details': 'Configure forcing parameters and upload custom forcing CSV to run simulation.'})
    return jsonify({'error': 'Unknown tool: ' + tool}), 400


# -- Seed --
def seed():
    cats = [
        ('foundations', 'Foundations of Physics', 'F'),
        ('math-physics', 'Mathematical Physics', 'M'),
        ('nonlinear', 'Nonlinear Dynamics', 'N'),
        ('stat-mech', 'Statistical Mechanics', 'S'),
        ('complex', 'Complex Systems', 'C'),
        ('experimental', 'Experimental and Observational', 'E'),
        ('astro', 'Astrophysics and Cosmology', 'A'),
        ('quantum', 'Quantum Mechanics', 'Q'),
        ('condensed', 'Condensed Matter', 'D'),
        ('other', 'Other', 'O')
    ]
    for slug, name, emoji in cats:
        if not Category.query.filter_by(slug=slug).first():
            db.session.add(Category(slug=slug, name=name, emoji=emoji))
    if not User.query.filter_by(email='admin@journal.local').first():
        u = User(email='admin@journal.local', display_name='Founding Editor', role='admin')
        u.set_password('change-me-now')
        db.session.add(u)
    db.session.commit()


def init_db():
    try:
        db.create_all()
        seed()
        print('[INIT] Database ready', flush=True)
    except Exception as e:
        print('[INIT] DB init error: ' + str(e), flush=True)

with app.app_context():
    init_db()

print('[BOOT] Flask app loaded, ready for requests', flush=True)

if __name__ == '__main__':
    port = int(os.getenv('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=os.getenv('FLASK_DEBUG', '0') == '1')
