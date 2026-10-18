"""The owner's citation setting, read on paths that must not fail for it."""

from varyz.agent_style import (
    DEFAULT,
    STYLES,
    instruction,
    normalise,
    read_style,
    skill_paragraph,
)


def _config(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_short_answers_are_what_an_owner_who_set_nothing_gets():
    assert DEFAULT == "minimal"
    assert "short" in instruction("minimal")
    assert "as few sentences" in skill_paragraph("minimal")


def test_every_style_says_something_different_in_both_places():
    paragraphs = {skill_paragraph(style) for style in STYLES}
    sentences = {instruction(style) for style in STYLES}

    assert len(paragraphs) == len(STYLES)
    assert len(sentences) == len(STYLES)


def test_hiding_references_does_not_license_invention():
    # The one reading of `off` that would make it a privacy and honesty
    # problem rather than a display preference.
    unwrapped = " ".join(skill_paragraph("off").split())
    assert "still rests only on what varyz returned" in unwrapped
    assert "still refused" in unwrapped
    assert "still refused" in instruction("off")


def test_the_setting_is_read_from_the_config_file(tmp_path):
    path = _config(tmp_path, '[agent]\ncitations = "full"\n')

    assert read_style(path) == "full"


def test_anything_unreadable_is_the_default_rather_than_an_error(tmp_path):
    # Each of these is read during an MCP handshake, where raising leaves the
    # agent with no varyz at all rather than with a verbose one.
    assert read_style(None) == DEFAULT
    assert read_style(tmp_path / "absent.toml") == DEFAULT
    assert read_style(_config(tmp_path, "[agent\n")) == DEFAULT
    assert read_style(_config(tmp_path, '[storage]\ndata_dir = "data"\n')) == DEFAULT
    assert read_style(_config(tmp_path, '[agent]\ncitations = "loud"\n')) == DEFAULT
    assert read_style(_config(tmp_path, "[agent]\ncitations = 3\n")) == DEFAULT
    assert read_style(_config(tmp_path, "agent = 3\n")) == DEFAULT


def test_normalise_accepts_only_the_three_styles():
    assert [normalise(style) for style in STYLES] == list(STYLES)
    assert normalise("verbose") == DEFAULT
    assert normalise(None) == DEFAULT
