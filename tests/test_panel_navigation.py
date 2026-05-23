from telepath.manager_bot import PanelNavigation


def test_panel_navigation_back_returns_previous_action():
    navigation = PanelNavigation()

    navigation.reset(user_id=10)
    navigation.visit(user_id=10, action="transcription")
    navigation.visit(user_id=10, action="status")

    assert navigation.back(user_id=10) == "transcription"
    assert navigation.back(user_id=10) == "main"
    assert navigation.back(user_id=10) == "main"


def test_panel_navigation_does_not_stack_same_screen():
    navigation = PanelNavigation()

    navigation.reset(user_id=10)
    navigation.visit(user_id=10, action="transcription")
    navigation.visit(user_id=10, action="transcription")

    assert navigation.back(user_id=10) == "main"
