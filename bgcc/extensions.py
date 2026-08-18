from flask_caching import Cache
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address)
cache = Cache()


@login_manager.user_loader
def load_user(user_id):
    from bgcc.models.users import User

    try:
        numeric_id = int(str(user_id).split(":")[0])
    except (TypeError, ValueError):
        return None

    return db.session.get(User, numeric_id)