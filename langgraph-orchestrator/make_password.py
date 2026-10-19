"""
Helper for generating password hash for a newly added user.
Usage python make_password.py
Then paste the hash into users.py
"""

import bcrypt
import getpass

password = getpass.getpass("Yeni kullanıcı şifresi: ")
hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())
print("\nHash (users.py'ye yapıştır):")
print(hashed.decode("utf-8"))
