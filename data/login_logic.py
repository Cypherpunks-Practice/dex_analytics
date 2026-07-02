import sqlite3
import hashlib


def check_password(login, password):
    orig_pass = get_password_from_db(login)
    h = hashlib.sha256()
    h.update(password.encode('utf-8'))
    curr_pass = bytes.fromhex(h.hexdigest())
    if orig_pass == None:
        return 0
    if orig_pass == curr_pass:
        return 1
    else: 
        return 0


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
# add user - (login) check_is_admin -> func
class User:
    login = None

    def __init__(self, login):
        self.login = login

    def get_login(self):
        return self.login
    
    def admin_add_user(self, login, password, is_admin):
        '''
        returns None if user is not admin, 
        True if user has been added
        False if user with this login already exists
        '''
        return Admin_panel.add_user(login, password, is_admin, self.login)

    def admin_delete_user(self, login):
        '''
        returns None if user is not admin, 
        else returns number of deleted users
        '''
        return Admin_panel.delete_user(login, self.login)

    def admin_get_users_list(self):
        '''
        returns None if user is not admin, 
        else returns users list in format [["name", is_admin],...]
        '''
        return Admin_panel.get_users_list(self.login)
    

class Admin_panel:

    def check_is_admin(requester):
        connection = sqlite3.connect('logins.db')
        cursor = connection.cursor()
        cursor.execute(f'SELECT is_admin FROM logins WHERE login = "{requester}"')
        is_admin = cursor.fetchone()
        connection.close()
        if not is_admin:
            return False
        else:
            return is_admin[0]
        
    def add_user(login, password, is_admin, requester):
        '''
        returns None if user is not admin, 
        True if user has been added
        False if user with this login already exists
        '''
        if Admin_panel.check_is_admin(requester):

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

    def delete_user(login, requester):
        '''
        returns None if user is not admin, 
        else returns number of deleted users
        '''
        if Admin_panel.check_is_admin(requester):
            connection = sqlite3.connect('logins.db')
            cursor = connection.cursor()
            cursor.execute(f'DELETE FROM logins WHERE login = "{login}"')
            deletions_counter = cursor.rowcount
            connection.commit()
            connection.close()
            return deletions_counter
        else: return None

    def get_users_list(requester):
        '''
        returns None if user is not admin, 
        else returns users list in format [["name", is_admin],...]
        '''
        if Admin_panel.check_is_admin(requester):
            connection = sqlite3.connect('logins.db')
            cursor = connection.cursor()
            cursor.execute(f'SELECT login, is_admin FROM logins')
            users_list = cursor.fetchall()
            connection.close()
            return users_list
        else: return None



'''#That's the log-in test:
connection = sqlite3.connect('logins.db')
cursor = connection.cursor()
h = hashlib.sha256()
t = 'test_password'
h.update(t.encode('utf-8'))
hash_ = h.hexdigest()
cursor.execute(f'INSERT INTO logins VALUES ("test_login", x\'{hash_}\', {False})')
cursor.execute(f'SELECT * FROM logins;')
for i in cursor.fetchall():
    print(i)
txt = 'test_password'
print(hash_)
h = hashlib.sha256()
h.update(txt.encode('utf-8'))
print(h.hexdigest())
print(check_password('test_login', 'test_password'))
print("(if 1 -> login succesfull)")
connection.close()
'''

'''#that's the admin test
connection = sqlite3.connect('logins.db')
cursor = connection.cursor()
h = hashlib.sha256()
t = 'admin_password'
h.update(t.encode('utf-8'))
hash_ = h.hexdigest()
cursor.execute(f'INSERT INTO logins VALUES ("admin_login", x\'{hash_}\', {True})')
connection.commit()
cursor.execute(f'SELECT * FROM logins;')
for i in cursor.fetchall():
    print(i)
user = User("admin_login")
print("\nAdmin adding user:")
user.admin_add_user("new user", "new password", "False")
cursor.execute(f'SELECT * FROM logins;')
for i in cursor.fetchall():
    print(i)

print("\nAdmin checking users list:")
print(user.admin_get_users_list())

print("\nAdmin deleting user:")
user.admin_delete_user("new user")
cursor.execute(f'SELECT * FROM logins;')
for i in cursor.fetchall():
    print(i)

user.admin_delete_user("admin_login")
connection.commit()
connection.close()
'''