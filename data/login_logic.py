import sqlite3
import hashlib


def check_password(login, password):
    orig_pass = get_password_from_db(login)
    h = hashlib.sha256()
    h.update(password.encode('utf-8'))
    curr_pass = bytes.fromhex(h.hexdigest())
    print(curr_pass)
    print(orig_pass)
    if orig_pass == None:
        return 0
    if orig_pass == curr_pass:
        return 1
    else: 
        return 0

def get_is_admin_from_db(login):
    connection = sqlite3.connect('logins.db')
    cursor = connection.cursor()
    cursor.execute(f'SELECT is_admin FROM logins WHERE login = "{login}"')
    is_admin = cursor.fetchone()
    connection.close()
    if not is_admin:
        return False
    else:
        return is_admin[0]

def get_password_from_db(login):
    connection = sqlite3.connect('logins.db')
    cursor = connection.cursor()
    cursor.execute(f'SELECT password FROM logins WHERE login = "{login}";')
    password = cursor.fetchone()
    connection.close()
    if not password:
        return None
    else:
        return password[0]

class user:
    is_admin = 0
    login = None

    def __init__(self, login):
        self.login = login
        self.is_admin = get_is_admin_from_db(login)

    def get_login(self):
        return self.login
    


""" #That's the test:
connection = sqlite3.connect('logins.db')
cursor = connection.cursor()
h = hashlib.sha256()
t = 'test_password'
h.update(t.encode('utf-8'))
hash_ = h.hexdigest()
#cursor.execute(f'INSERT INTO logins VALUES ("test_login", x\'{hash_}\', {False})')
connection.commit()
cursor.execute(f'SELECT * FROM logins;')
for i in cursor.fetchall():
    print(i)
txt = 'test_password'
print(hash_)
h = hashlib.sha256()
h.update(txt.encode('utf-8'))
print(h.hexdigest())
print(check_password('test_login', 'test_password'))
print()
connection.close()
"""
