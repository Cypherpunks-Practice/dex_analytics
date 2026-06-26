import sqlite3

def init_db():
    connection = sqlite3.connect('logins.db')
    cursor = connection.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS logins
        (login VARCHAR(16), password VARBINARY(20), is_admin BOOL)
                ''')

    connection.commit()
    connection.close()