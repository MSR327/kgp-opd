import pytest

from bcroy_booker.cli import build_parser, main


def test_book_needs_a_doctor():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["book"])


def test_trial_modes_are_exclusive():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["book", "--doctor", "x", "--dry-run", "--observe"])


def test_forget_without_saved_login(capsys):
    assert main(["forget"]) == 0
    assert "No saved login" in capsys.readouterr().out
