"""
User database - manuel kontrol ediliyor şuan

kullanıcı ekleme:
1. python make_password.py
2. copy the hash
3. aşağıya bir entry daha ekle

ilkel ama bu daha başlangıç

Roller:
- admin: create flowlara kendi ekranında onay verebilir e.g. Datacenter ekibi üyeleri
- user: create yaptığı zaman bizim ekibe mail gelir (inş)
"""


USERS = {
    "user": {
        "password_hash": "$2a$10$t/iSsgqWnLTJcZyIF29pxenix.J4M6uQl2OXoLstGBCRO2S1lPsfu",
        "role": "user",
        "email": "emates@ziraatteknoloji.com",
        "team": "MAY1"
    },
    "admin": {
        "password_hash": "$2b$12$4SQ1owck1zpAHmHXAXer5OET.fWFJ7pNP1QzZEt8dUeMemcwmLNqC",
        "role": "admin",
        "email": "ZTMAY1DatacenterNetwork@ziraatteknoloji.com",
        "team": "MAY1"
    },
    "sevil": {
        "password_hash": "$2b$12$D4ych.yTnKHlyPaFf76uRe2bk/V6aKL8FTyw1KnOgw3gCZxq/XCQ6",
        "role": "user",
        "email": "ssendere@ziraatteknoloji.com",
        "team": "MSY"
    }
 
}

def verify_user(username: str, password: str):
    #eğer cred doğruysa dict dön, yanlışsa nevermind
    
    import bcrypt
    user = USERS.get(username)
    if not user:
        return None
    stored = user["password_hash"].encode("utf-8")
    if bcrypt.checkpw(password.encode("utf-8"), stored):
        return user
    return None


def get_admin_emails():
    #adminlerin maillerini returnler
    return [u["email"] for u in USERS.values() if u.get("role") == "admin" and u.get("email")]