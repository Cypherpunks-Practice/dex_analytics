from taipy.gui import builder as tgb
from taipy.gui import navigate

username = ""
password = ""
logged_in = False

def login(state):
    print("LOGIN CLICKED")
    state.logged_in = True
    navigate(state,"/dashboard")

with tgb.Page() as login_page:

    with tgb.part(class_name="login-page"):

        with tgb.part(class_name="login-card"):

            tgb.text("# ChainBI", mode="md")

            tgb.input(value="{username}", label="Логин")

            tgb.input(
                value="{password}",
                label="Пароль",
                password=True
            )

            tgb.button(
                "Войти",
                on_action=login
            )

def login(state):
    state.logged_in = True
    state.navigate("dashboard")