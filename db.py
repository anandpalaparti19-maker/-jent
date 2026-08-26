
import os, json, logging
from pathlib import Path
from datetime import datetime, timezone

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
SEEN_JOBS_FILE = BASE_DIR / 'seen_jobs.json'
APPLIED_JOBS_FILE = BASE_DIR / 'applied_jobs.json'
LOG_DIR = BASE_DIR / 'log'
CYCLE_STATS_FILE = LOG_DIR / 'cycle_stats.json'

DEFAULT_USER_ID = os.environ.get('DEFAULT_USER_ID', 'default_user')
MONGODB_URI = os.environ.get('MONGODB_URI', 'mongodb://localhost:27017/jent')

_client = None
_db = None
_connected = False

def init_db(uri: str = None) -> bool:
    global _client, _db, _connected
    target_uri = uri or os.environ.get('MONGODB_URI') or MONGODB_URI
    if not target_uri or target_uri.strip() == '':
        _connected = False
        return False
    try:
        import pymongo
        _client = pymongo.MongoClient(target_uri, serverSelectionTimeoutMS=2000)
        _client.admin.command('ping')
        _db = _client.get_database(default='jent')
        _connected = True
        log.info(f'[DB] Connected to MongoDB ({_db.name})')
        _setup_indexes()
        return True
    except Exception as e:
        log.info(f'[DB] MongoDB not connected ({e}). Using JSON file fallback.')
        _client = None
        _db = None
        _connected = False
        return False

def is_mongodb_connected() -> bool:
    return _connected and _db is not None

def _setup_indexes():
    if not _connected or _db is None:
        return
    try:
        _db.jobs.create_index('id', unique=True)
        _db.seen_jobs.create_index([('user_id', 1), ('job_id', 1)], unique=True)
        _db.applied_jobs.create_index([('user_id', 1), ('job_id', 1)], unique=True)
        _db.cycle_stats.create_index('timestamp')
        _db.user_profiles.create_index('user_id', unique=True)
    except Exception as e:
        log.warning(f'[DB] Error creating index: {e}')

init_db()

def load_seen(user_id: str = None) -> dict:
    uid = user_id or DEFAULT_USER_ID
    if is_mongodb_connected():
        try:
            seen_dict = {}
            for doc in _db.seen_jobs.find({'user_id': uid}):
                jid = doc.get('job_id')
                if jid:
                    seen_dict[jid] = {
                        'score': doc.get('score', 0),
                        'found_at': doc.get('found_at', ''),
                        'title': doc.get('title', ''),
                        'company': doc.get('company', ''),
                        'url': doc.get('url', ''),
                        'source': doc.get('source', ''),
                        'location': doc.get('location', ''),
                    }
            return seen_dict
        except Exception as e:
            log.warning(f'[DB] MongoDB load_seen failed: {e}')
    if SEEN_JOBS_FILE.exists():
        try:
            with open(SEEN_JOBS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, list):
                return {jid: {'score': 0, 'found_at': ''} for jid in data}
            return data
        except Exception:
            return {}
    return {}

def save_seen(seen_dict: dict, user_id: str = None):
    uid = user_id or DEFAULT_USER_ID
    if is_mongodb_connected():
        try:
            import pymongo
            ops = []
            for jid, info in seen_dict.items():
                if isinstance(info, dict):
                    doc = {
                        'user_id': uid,
                        'job_id': jid,
                        'score': info.get('score', 0),
                        'found_at': info.get('found_at', ''),
                        'title': info.get('title', ''),
                        'company': info.get('company', ''),
                        'url': info.get('url', ''),
                        'source': info.get('source', ''),
                        'location': info.get('location', ''),
                        'updated_at': datetime.now(timezone.utc).isoformat(),
                    }
                else:
                    doc = {
                        'user_id': uid,
                        'job_id': jid,
                        'score': 0,
                        'found_at': '',
                        'updated_at': datetime.now(timezone.utc).isoformat(),
                    }
                ops.append(pymongo.UpdateOne({'user_id': uid, 'job_id': jid}, {'$set': doc}, upsert=True))
            if ops:
                _db.seen_jobs.bulk_write(ops)
        except Exception as e:
            log.warning(f'[DB] MongoDB save_seen error: {e}')
    try:
        with open(SEEN_JOBS_FILE, 'w', encoding='utf-8') as f:
            json.dump(seen_dict, f, indent=2)
    except Exception as e:
        log.warning(f'[DB] Save seen_jobs.json error: {e}')

def load_applied(user_id: str = None) -> dict:
    uid = user_id or DEFAULT_USER_ID
    if is_mongodb_connected():
        try:
            applied = {}
            for doc in _db.applied_jobs.find({'user_id': uid}):
                jid = doc.get('job_id')
                if jid:
                    applied[jid] = {
                        'title': doc.get('title', ''),
                        'company': doc.get('company', ''),
                        'url': doc.get('url', ''),
                        'source': doc.get('source', ''),
                        'score': doc.get('score', 0),
                        'applied_at': doc.get('applied_at', ''),
                        'status': doc.get('status', 'submitted'),
                        'note': doc.get('note', ''),
                        'progress_stage': doc.get('progress_stage', ''),
                        'platform': doc.get('platform', ''),
                        'started_at': doc.get('started_at', ''),
                        'completed_at': doc.get('completed_at', ''),
                        'error': doc.get('error', ''),
                    }
            return applied
        except Exception as e:
            log.warning(f'[DB] MongoDB load_applied failed: {e}')
    if APPLIED_JOBS_FILE.exists():
        try:
            with open(APPLIED_JOBS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_applied(data: dict, user_id: str = None):
    uid = user_id or DEFAULT_USER_ID
    if is_mongodb_connected():
        try:
            import pymongo
            ops = []
            for jid, info in data.items():
                doc = {
                    'user_id': uid,
                    'job_id': jid,
                    'title': info.get('title', ''),
                    'company': info.get('company', ''),
                    'url': info.get('url', ''),
                    'source': info.get('source', ''),
                    'score': info.get('score', 0),
                    'applied_at': info.get('applied_at', datetime.now(timezone.utc).isoformat()),
                    'status': info.get('status', 'submitted'),
                    'note': info.get('note', ''),
                    'progress_stage': info.get('progress_stage', ''),
                    'platform': info.get('platform', ''),
                    'started_at': info.get('started_at', ''),
                    'completed_at': info.get('completed_at', ''),
                    'error': info.get('error', ''),
                }
                ops.append(pymongo.UpdateOne({'user_id': uid, 'job_id': jid}, {'$set': doc}, upsert=True))
            if ops:
                _db.applied_jobs.bulk_write(ops)
        except Exception as e:
            log.warning(f'[DB] MongoDB save_applied error: {e}')
    try:
        with open(APPLIED_JOBS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.warning(f'[DB] Save applied_jobs.json error: {e}')

def update_application_progress(job: dict, stage: str, user_id: str = None, platform: str = ''):
    """Update in-progress application stage for dashboard live tracking."""
    applied = load_applied(user_id)
    jid = job['id']
    now = datetime.now(timezone.utc).isoformat()
    if jid not in applied:
        applied[jid] = {
            'title': job.get('title', ''),
            'company': job.get('company', ''),
            'url': job.get('url', ''),
            'source': job.get('source', ''),
            'score': round(job.get('score', 0), 4),
            'applied_at': now,
            'status': 'in_progress',
            'note': '',
            'progress_stage': stage,
            'platform': platform,
            'started_at': now,
            'completed_at': '',
            'error': '',
        }
    else:
        applied[jid]['progress_stage'] = stage
        applied[jid]['status'] = 'in_progress'
        if platform:
            applied[jid]['platform'] = platform
    save_applied(applied, user_id)


def record_application(job: dict, status: str = 'submitted', note: str = '',
                       user_id: str = None, platform: str = ''):
    applied = load_applied(user_id)
    jid = job['id']
    now = datetime.now(timezone.utc).isoformat()
    existing = applied.get(jid, {})
    applied[jid] = {
        'title': job.get('title', ''),
        'company': job.get('company', ''),
        'url': job.get('url', ''),
        'source': job.get('source', ''),
        'score': round(job.get('score', 0), 4),
        'applied_at': existing.get('applied_at', now),
        'status': status,
        'note': note,
        'progress_stage': 'done' if status == 'submitted' else stage_label(status),
        'platform': platform or existing.get('platform', ''),
        'started_at': existing.get('started_at', now),
        'completed_at': now,
        'error': note if status == 'failed' else '',
    }
    save_applied(applied, user_id)


def stage_label(status: str) -> str:
    return {'skipped': 'skipped', 'failed': 'error'}.get(status, status)

def load_cycle_stats() -> list:
    if is_mongodb_connected():
        try:
            stats = []
            for doc in _db.cycle_stats.find({}, {'_id': 0}).sort('timestamp', 1):
                stats.append(doc)
            if stats:
                return stats
        except Exception as e:
            log.warning(f'[DB] MongoDB load_cycle_stats failed: {e}')
    if CYCLE_STATS_FILE.exists():
        try:
            with open(CYCLE_STATS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_cycle_stats(stats: list):
    if is_mongodb_connected():
        try:
            if stats:
                latest = stats[-1]
                _db.cycle_stats.update_one({'timestamp': latest.get('timestamp')}, {'': latest}, upsert=True)
        except Exception as e:
            log.warning(f'[DB] MongoDB save_cycle_stats error: {e}')
    LOG_DIR.mkdir(exist_ok=True)
    try:
        with open(CYCLE_STATS_FILE, 'w', encoding='utf-8') as f:
            json.dump(stats[-200:], f, indent=2)
    except Exception as e:
        log.warning(f'[DB] Save cycle_stats.json error: {e}')

def save_raw_jobs(jobs_list: list):
    if not is_mongodb_connected() or not jobs_list:
        return
    try:
        import pymongo
        ops = []
        now_iso = datetime.now(timezone.utc).isoformat()
        for j in jobs_list:
            if not j.get('id'):
                continue
            doc = {
                'id': j['id'],
                'title': j.get('title', ''),
                'company': j.get('company', ''),
                'url': j.get('url', ''),
                'location': j.get('location', ''),
                'description': j.get('description', ''),
                'source': j.get('source', ''),
                'posted_at': j.get('posted_at', ''),
                'last_scraped_at': now_iso,
            }
            ops.append(pymongo.UpdateOne({'id': j['id']}, {'': doc}, upsert=True))
        if ops:
            _db.jobs.bulk_write(ops)
    except Exception as e:
        log.warning(f'[DB] MongoDB save_raw_jobs error: {e}')

def get_user_profile(user_id: str = None) -> dict:
    uid = user_id or DEFAULT_USER_ID
    if is_mongodb_connected():
        try:
            doc = _db.user_profiles.find_one({'user_id': uid}, {'_id': 0})
            if doc:
                return doc
        except Exception as e:
            log.warning(f'[DB] MongoDB get_user_profile failed: {e}')
    return {'user_id': uid}

def save_user_profile(profile_data: dict, user_id: str = None):
    uid = user_id or DEFAULT_USER_ID
    if is_mongodb_connected():
        try:
            profile_data['user_id'] = uid
            profile_data['updated_at'] = datetime.now(timezone.utc).isoformat()
            _db.user_profiles.update_one({'user_id': uid}, {'$set': profile_data}, upsert=True)
        except Exception as e:
            log.warning(f'[DB] MongoDB save_user_profile error: {e}')

# ── Multi-Tenant SaaS User Authentication & Subscriber Helpers ───────────────

import hashlib, secrets

USERS_FILE = BASE_DIR / 'users.json'

def _hash_password(password: str, salt: str = None) -> tuple:
    if not salt:
        salt = secrets.token_hex(16)
    hashed = hashlib.sha256((password + salt).encode('utf-8')).hexdigest()
    return hashed, salt

def load_users() -> dict:
    if is_mongodb_connected():
        try:
            users = {}
            for doc in _db.users.find({}, {'_id': 0}):
                users[doc['email']] = doc
            return users
        except Exception as e:
            log.warning(f'[DB] MongoDB load_users failed: {e}')
    if USERS_FILE.exists():
        try:
            with open(USERS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_user(user_doc: dict):
    email = user_doc.get('email')
    if not email:
        return
    if is_mongodb_connected():
        try:
            _db.users.update_one({'email': email}, {'$set': user_doc}, upsert=True)
        except Exception as e:
            log.warning(f'[DB] MongoDB save_user error: {e}')
    users = load_users()
    users[email] = user_doc
    try:
        with open(USERS_FILE, 'w', encoding='utf-8') as f:
            json.dump(users, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.warning(f'[DB] Save users.json error: {e}')

def create_user(email: str, password: str, name: str = '') -> dict | None:
    email = email.strip().lower()
    users = load_users()
    if email in users:
        return None  # Email already registered
    hashed_pwd, salt = _hash_password(password)
    user_id = f"usr_{hashlib.md5(email.encode()).hexdigest()[:12]}"
    user_doc = {
        'user_id': user_id,
        'email': email,
        'name': name or email.split('@')[0],
        'password_hash': hashed_pwd,
        'salt': salt,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'subscription_status': 'active',  # Free tier / active trial default
        'plan': 'trial',
    }
    save_user(user_doc)
    return user_doc

def authenticate_user(email: str, password: str) -> dict | None:
    email = email.strip().lower()
    users = load_users()
    user = users.get(email)
    if not user:
        return None
    hashed, _ = _hash_password(password, user.get('salt', ''))
    if hashed == user.get('password_hash'):
        return user
    return None

def get_all_subscribers() -> list:
    """Return all users with active subscriptions or active trial profiles."""
    users = load_users()
    active_users = []
    for email, u in users.items():
        if u.get('subscription_status') in ('active', 'trial', 'dev'):
            active_users.append(u)
    if not active_users:
        # Fallback to default user if no users registered yet
        return [{
            'user_id': DEFAULT_USER_ID,
            'email': 'default@jent.ai',
            'name': 'Default User',
            'subscription_status': 'active',
            'plan': 'default'
        }]
    return active_users

