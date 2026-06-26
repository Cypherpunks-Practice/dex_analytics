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
    
    def admin_add_user(self, login, password, is_admin):
        '''
        returns None if user is not admin, 
        True if user has been added
        False if user with this login already exists
        '''
        if self.is_admin == 1:

            h = hashlib.sha256()
            p = password
            h.update(p.encode('utf-8'))
            password_hash = h.hexdigest()

            connection = sqlite3.connect('logins.db')
            cursor = connection.cursor()
            cursor.execute(f'SELECT * FROM logins WHERE login = "{login}"')
            user_exists = cursor.fetchone()
            
            if not user_exists:
                cursor.execute(f'INSERT INTO logins VALUES ("{login}", x\'{password_hash}\', {is_admin})')
                connection.commit()
                connection.close()
                return True
            else:
                connection.close()
                return False
            
        else: return None

    def admin_delete_user(self, login):
        '''
        returns None if user is not admin, 
        else returns number of deleted users
        '''
        if self.is_admin == 1:
            connection = sqlite3.connect('logins.db')
            cursor = connection.cursor()
            cursor.execute(f'DELETE FROM logins WHERE login = "{login}"')
            deletions_counter = cursor.rowcount
            connection.commit()
            connection.close()
            return deletions_counter
        else: return None

    def admin_get_users_list(self):
        '''
        returns None if user is not admin, 
        else returns users list in format [["name", is_admin],...]
        '''
        if self.is_admin == 1:
            connection = sqlite3.connect('logins.db')
            cursor = connection.cursor()
            cursor.execute(f'SELECT login, is_admin FROM logins')
            users_list = cursor.fetchall()
            connection.close()
            return users_list
        else: return None
    


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
