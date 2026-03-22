"""
The Journal — Deployment Server
Single-file Flask app serving PWA front-end + JSON API.
Deploy to Railway, Render, or Fly.io.
All Rights Reserved.
"""
import os, json, re, math, hashlib
from datetime import datetime
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, current_user, login_user, logout_user, login_required
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps

# ─── APP SETUP ────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder='static')
BASE = Path(__file__).parent
INSTANCE = BASE / 'instance'
INSTANCE.mkdir(exist_ok=True)

app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-change-before-deploy')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', f"sqlite:///{INSTANCE / 'journal.db'}")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)

# ─── MODELS ───────────────────────────────────────────────────────────────────
user_follows = db.Table('user_follows',
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
    reputation = db.Column(db.Float, default=1.0)
    is_banned = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, pw): self.password_hash = generate_password_hash(pw)
    def check_password(self, pw): return check_password_hash(self.password_hash, pw)
    @property
    def initials(self):
        p = self.display_name.strip().split()
        return (p[0][0]+p[-1][0]).upper() if len(p)>=2 else self.display_name[:2].upper()
    def to_dict(self):
        return {'id':self.id,'display_name':self.display_name,'initials':self.initials,
                'bio':self.bio,'avatar_color':self.avatar_color,'reputation_score':round(self.reputation,2),
                'paper_count':Submission.query.filter_by(author_id=self.id).count(),
                'review_count':Review.query.filter_by(reviewer_id=self.id).count(),
                'follower_count':self.followers.count() if hasattr(self,'followers') else 0,
                'joined':self.created_at.isoformat() if self.created_at else None}

    following = db.relationship('User', secondary=user_follows,
        primaryjoin=(user_follows.c.follower_id == id),
        secondaryjoin=(user_follows.c.followed_id == id),
        backref=db.backref('followers', lazy='dynamic'), lazy='dynamic')

@login_manager.user_loader
def load_user(uid): return User.query.get(int(uid))

class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(80), unique=True)
    name = db.Column(db.String(120))
    emoji = db.Column(db.String(10), default='📄')

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

    STATUS_LABELS = {'submitted':'Submitted','desk_passed':'Under Review','in_discovery':'In Discovery',
        'under_review':'Under Review','published':'Published','desk_returned':'Revision Suggested',
        'revision_requested':'Revision Requested','declined':'Declined','contested':'Contested'}
    STATUS_COLORS = {'submitted':'#6b7db3','in_discovery':'#5ea8ff','under_review':'#f0a030',
        'published':'#4ade80','desk_returned':'#f0a030','revision_requested':'#f0a030',
        'declined':'#ef4444','contested':'#ef4444'}

    def to_card(self, uid=None):
        lc = Like.query.filter_by(submission_id=self.id).count()
        cc = Comment.query.filter_by(submission_id=self.id).count()
        rc = Review.query.filter_by(submission_id=self.id).count()
        d = {'id':self.id,'blind_id':self.blind_id,'title':self.title,'abstract':(self.abstract or '')[:300],
             'status':self.status,'status_label':self.STATUS_LABELS.get(self.status,self.status),
             'status_color':self.STATUS_COLORS.get(self.status,'#6b7db3'),
             'category':{'name':self.category.name,'emoji':self.category.emoji} if self.category else {},
             'author':self.author.to_dict() if self.author else {},
             'tags':[t.strip() for t in (self.tags or '').split(',') if t.strip()],
             'like_count':lc,'comment_count':cc,'review_count':rc,
             'created_at':self.created_at.isoformat() if self.created_at else None,
             'published_at':self.published_at.isoformat() if self.published_at else None}
        if uid:
            d['user_liked'] = Like.query.filter_by(user_id=uid,submission_id=self.id).first() is not None
            d['user_bookmarked'] = Bookmark.query.filter_by(user_id=uid,submission_id=self.id).first() is not None
        return d

class Like(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    submission_id = db.Column(db.Integer, db.ForeignKey('submission.id'))
    __table_args__ = (db.UniqueConstraint('user_id','submission_id'),)

class Bookmark(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    submission_id = db.Column(db.Integer, db.ForeignKey('submission.id'))
    __table_args__ = (db.UniqueConstraint('user_id','submission_id'),)

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

# ─── DESK REVIEW ENGINE ──────────────────────────────────────────────────────
def run_desk_review(title, abstract, body):
    lower = f"{title}\n{abstract}\n{body}".lower()
    wc = len(body.split())
    terms = ["energy","force","momentum","field","wave","quantum","gravity","entropy",
             "experiment","friction","chaos","nonlinear","measurement","hypothesis"]
    pc = sum(1 for t in terms if t in lower)
    claims = ["we show","we find","i find","this paper","result shows","i measured","hypothesis"]
    cc = sum(1 for p in claims if p in lower)
    sections = ["introduction","method","results","discussion","conclusion","references"]
    sf = sum(1 for s in sections if s in lower)
    spam = ["buy now","click here","guaranteed","act now"]
    is_spam = any(s in lower for s in spam)

    scores = {'scope':min(5,pc),'claim':min(5,cc+1),'structure':min(5,sf+1),
              'clarity':4 if wc>300 else 3,'quantitative':3 if re.search(r'\d.*=',body) else 1,
              'citations':3 if re.search(r'\[\d+\]',body) else 1,'anonymity':5,
              'good_faith':0 if is_spam else (5 if wc>=200 else 3)}
    overall = round(sum(scores.values())/40*100)
    rec = 'block' if is_spam else ('pass' if overall>=60 and scores['good_faith']>=3 else 'return')
    return {'overall_score':overall,'recommendation':rec,'scores':scores,
            'summary':'Ready for community review.' if rec=='pass' else 'Needs more development.' if rec=='return' else 'Not accepted.',
            'encouragement':'Your curiosity is valued here.' if rec!='block' else 'We welcome genuine submissions.'}

# ─── CORS ─────────────────────────────────────────────────────────────────────
@app.after_request
def cors(r):
    r.headers['Access-Control-Allow-Origin'] = '*'
    r.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    r.headers['Access-Control-Allow-Methods'] = 'GET,POST,PUT,DELETE,OPTIONS'
    return r

def api_login_required(f):
    @wraps(f)
    def d(*a,**k):
        if not current_user.is_authenticated: return jsonify({'error':'Auth required'}),401
        return f(*a,**k)
    return d

# ─── PWA ROUTES ───────────────────────────────────────────────────────────────
@app.route('/')
def index(): return send_file('index.html')

@app.route('/manifest.json')
def manifest(): return send_file('manifest.json')

@app.route('/sw.js')
def service_worker(): return send_file('sw.js')

@app.route('/static/<path:p>')
def static_files(p): return send_from_directory('static', p)

# ─── API ROUTES ───────────────────────────────────────────────────────────────
@app.route('/api/auth/register', methods=['POST'])
def register():
    d = request.get_json(silent=True) or {}
    email = (d.get('email') or '').strip().lower()
    name = (d.get('display_name') or '').strip()
    pw = d.get('password','')
    if not email or not name or len(pw)<6: return jsonify({'error':'All fields required, password 6+ chars'}),400
    if User.query.filter_by(email=email).first(): return jsonify({'error':'Email taken'}),409
    u = User(email=email, display_name=name)
    u.set_password(pw)
    db.session.add(u); db.session.commit(); login_user(u)
    return jsonify({'user':u.to_dict()}),201

@app.route('/api/auth/login', methods=['POST'])
def login():
    d = request.get_json(silent=True) or {}
    u = User.query.filter_by(email=(d.get('email') or '').strip().lower()).first()
    if not u or not u.check_password(d.get('password','')): return jsonify({'error':'Invalid credentials'}),401
    login_user(u)
    return jsonify({'user':u.to_dict()})

@app.route('/api/auth/logout', methods=['POST'])
def logout(): logout_user(); return jsonify({'ok':True})

@app.route('/api/auth/me')
@api_login_required
def me(): return jsonify({'user':current_user.to_dict()})

@app.route('/api/categories')
def categories():
    return jsonify({'categories':[{'id':c.id,'name':c.name,'emoji':c.emoji,'slug':c.slug}
        for c in Category.query.all()]})

@app.route('/api/feed/discovery')
def discovery():
    page = request.args.get('page',1,type=int)
    q = Submission.query.filter(Submission.status.in_(['in_discovery','under_review','revision_requested','revised','contested']))
    cat = request.args.get('category_id',type=int)
    if cat: q = q.filter_by(category_id=cat)
    search = request.args.get('q','').strip()
    if search: q = q.filter(db.or_(Submission.title.ilike(f'%{search}%'),Submission.abstract.ilike(f'%{search}%')))
    p = q.order_by(Submission.updated_at.desc()).paginate(page=page,per_page=20,error_out=False)
    uid = current_user.id if current_user.is_authenticated else None
    return jsonify({'papers':[s.to_card(uid) for s in p.items],'total':p.total,'page':p.page})

@app.route('/api/feed/published')
def published():
    p = Submission.query.filter_by(status='published').order_by(Submission.published_at.desc()).paginate(page=1,per_page=20,error_out=False)
    uid = current_user.id if current_user.is_authenticated else None
    return jsonify({'papers':[s.to_card(uid) for s in p.items]})

@app.route('/api/submissions', methods=['POST'])
@api_login_required
def create_submission():
    d = request.get_json(silent=True) or {}
    import uuid
    sub = Submission(blind_id=uuid.uuid4().hex[:12].upper(), title=d.get('title',''),
        abstract=d.get('abstract',''), body_text=d.get('body_text',''),
        tags=d.get('tags',''), author_id=current_user.id,
        category_id=d.get('category_id',1))
    desk = run_desk_review(sub.title, sub.abstract, sub.body_text)
    sub.status = {'pass':'in_discovery','return':'desk_returned','block':'desk_blocked'}[desk['recommendation']]
    db.session.add(sub); db.session.flush()
    db.session.add(DeskDecision(submission_id=sub.id, decision=desk['recommendation'],
        overall_score=desk['overall_score'], summary=desk['summary'],
        encouragement=desk['encouragement'], scores_json=json.dumps(desk['scores'])))
    db.session.commit()
    return jsonify({'submission':sub.to_card(current_user.id),'desk_review':desk}),201

@app.route('/api/submissions/<bid>')
def get_submission(bid):
    s = Submission.query.filter_by(blind_id=bid).first_or_404()
    d = s.to_card()
    d['body_text'] = s.body_text
    d['comments'] = [{'id':c.id,'author':c.author.to_dict(),'comment_type':c.comment_type,
        'body':c.body,'created_at':c.created_at.isoformat()} for c in Comment.query.filter_by(submission_id=s.id).order_by(Comment.created_at).all()]
    return jsonify({'submission':d})

@app.route('/api/submissions/<bid>/like', methods=['POST'])
@api_login_required
def toggle_like(bid):
    s = Submission.query.filter_by(blind_id=bid).first_or_404()
    ex = Like.query.filter_by(user_id=current_user.id,submission_id=s.id).first()
    if ex: db.session.delete(ex); db.session.commit(); return jsonify({'liked':False})
    db.session.add(Like(user_id=current_user.id,submission_id=s.id)); db.session.commit()
    return jsonify({'liked':True})

@app.route('/api/submissions/<bid>/bookmark', methods=['POST'])
@api_login_required
def toggle_bookmark(bid):
    s = Submission.query.filter_by(blind_id=bid).first_or_404()
    ex = Bookmark.query.filter_by(user_id=current_user.id,submission_id=s.id).first()
    if ex: db.session.delete(ex); db.session.commit(); return jsonify({'bookmarked':False})
    db.session.add(Bookmark(user_id=current_user.id,submission_id=s.id)); db.session.commit()
    return jsonify({'bookmarked':True})

@app.route('/api/submissions/<bid>/comments', methods=['POST'])
@api_login_required
def add_comment(bid):
    s = Submission.query.filter_by(blind_id=bid).first_or_404()
    d = request.get_json(silent=True) or {}
    c = Comment(submission_id=s.id, author_id=current_user.id,
        comment_type=d.get('comment_type','note'), body=d.get('body',''))
    db.session.add(c); db.session.commit()
    return jsonify({'comment':{'id':c.id,'author':current_user.to_dict(),
        'comment_type':c.comment_type,'body':c.body,'created_at':c.created_at.isoformat()}}),201

@app.route('/api/users/<int:uid>')
def get_user(uid):
    u = User.query.get_or_404(uid)
    d = u.to_dict()
    d['papers'] = [s.to_card() for s in Submission.query.filter_by(author_id=uid).filter(
        Submission.status.in_(['in_discovery','under_review','published'])).order_by(Submission.updated_at.desc()).limit(20).all()]
    return jsonify({'user':d})

@app.route('/api/notifications')
@api_login_required
def notifications():
    ns = Notification.query.filter_by(user_id=current_user.id).order_by(Notification.created_at.desc()).limit(50).all()
    return jsonify({'notifications':[{'id':n.id,'title':n.title,'body':n.body,
        'is_read':n.is_read,'created_at':n.created_at.isoformat()} for n in ns],
        'unread_count':Notification.query.filter_by(user_id=current_user.id,is_read=False).count()})

@app.route('/api/stats')
def stats():
    return jsonify({'published_count':Submission.query.filter_by(status='published').count(),
        'discovery_count':Submission.query.filter(Submission.status.in_(['in_discovery','under_review'])).count(),
        'user_count':User.query.count()})

# ─── DB INIT ──────────────────────────────────────────────────────────────────
def seed():
    cats = [('foundations','Foundations of Physics','🌌'),('math-physics','Mathematical Physics','📐'),
            ('nonlinear','Nonlinear Dynamics','🌀'),('stat-mech','Statistical Mechanics','⚛️'),
            ('complex','Complex Systems','🕸️'),('experimental','Experimental & Observational','🔬')]
    for slug,name,emoji in cats:
        if not Category.query.filter_by(slug=slug).first():
            db.session.add(Category(slug=slug,name=name,emoji=emoji))
    if not User.query.filter_by(email='admin@journal.local').first():
        u = User(email='admin@journal.local',display_name='Founding Editor',role='admin')
        u.set_password('change-me-now')
        db.session.add(u)
    db.session.commit()

with app.app_context():
    db.create_all()
    seed()

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=os.getenv('FLASK_DEBUG','0')=='1')
