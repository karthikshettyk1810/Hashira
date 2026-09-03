class CurrentUser:
    def __init__(self, name):
        self.name = name


def get_current_user():
    return CurrentUser(name="anonymous")
