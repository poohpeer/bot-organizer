from bot.formatting import to_plain_text


def test_double_asterisk_bold_removed():
    assert to_plain_text("**Куда:** Ben Shemen") == "Куда: Ben Shemen"


def test_single_asterisk_emphasis_removed():
    assert to_plain_text("*Куда:* Ben Shemen") == "Куда: Ben Shemen"


def test_double_underscore_emphasis_removed():
    assert to_plain_text("__Куда:__ x") == "Куда: x"


def test_single_underscore_emphasis_removed():
    assert to_plain_text("_x_") == "x"


def test_asterisk_list_marker_becomes_dash():
    assert to_plain_text("* хлеб") == "— хлеб"


def test_dash_list_marker_becomes_dash():
    assert to_plain_text("- хлеб") == "— хлеб"


def test_plus_list_marker_becomes_dash():
    assert to_plain_text("+ хлеб") == "— хлеб"


def test_indented_list_marker_keeps_indentation():
    assert to_plain_text("  * хлеб") == "  — хлеб"


def test_numbered_list_is_untouched():
    assert to_plain_text("1. хлеб") == "1. хлеб"


def test_heading_markers_removed():
    assert to_plain_text("### Основное") == "Основное"


def test_backticks_removed():
    assert to_plain_text("`code`") == "code"


def test_lone_asterisk_with_spaces_is_arithmetic():
    assert to_plain_text("2 * 3 = 6") == "2 * 3 = 6"


def test_unpaired_asterisk_in_content_survives():
    assert to_plain_text("звёздочка*") == "звёздочка*"


def test_empty_string_does_not_crash():
    assert to_plain_text("") == ""


def test_whitespace_only_string_passes_through():
    whitespace = "  \n\t \n  "
    assert to_plain_text(whitespace) == whitespace


def test_plain_text_with_no_markdown_passes_through_byte_identical():
    text = "Привет! Как дела? Едем в 15:00, встречаемся у метро."
    assert to_plain_text(text) == text


def test_the_bugs_file_example_end_to_end():
    """The exact reported symptom: a real multi-line reply mixing headings,
    bold key/value pairs and bullet lists, from the `bugs` file at the repo
    root."""
    raw = (
        "Вот вся информация по поездке:\n"
        "\n"
        "**Основное:**\n"
        "* **Куда:** Ben Shemen\n"
        "* **Когда:** 4 июля\n"
        "* **Организатор:** Sarah\n"
        "\n"
        "**Список покупок:**\n"
        "* хлеб\n"
        "* вода\n"
        "* молоко\n"
        "* sparklers\n"
        "* paper plates\n"
        "\n"
        "**Участники:**\n"
        "* Alex — подтвердил\n"
        "* Света — не определилось"
    )
    expected = (
        "Вот вся информация по поездке:\n"
        "\n"
        "Основное:\n"
        "— Куда: Ben Shemen\n"
        "— Когда: 4 июля\n"
        "— Организатор: Sarah\n"
        "\n"
        "Список покупок:\n"
        "— хлеб\n"
        "— вода\n"
        "— молоко\n"
        "— sparklers\n"
        "— paper plates\n"
        "\n"
        "Участники:\n"
        "— Alex — подтвердил\n"
        "— Света — не определилось"
    )
    assert to_plain_text(raw) == expected


def test_an_underscore_inside_a_word_is_not_emphasis():
    """The bot writes identifiers constantly — tool names in its reasoning,
    file names, URLs. Treating a paired `_` as emphasis ate them:
    "list_check_off" came out as "listcheckoff"."""
    assert to_plain_text("вызови list_check_off") == "вызови list_check_off"
    assert to_plain_text("snake_case_имя") == "snake_case_имя"
    assert to_plain_text("файл my_file_name.txt") == "файл my_file_name.txt"


def test_a_url_with_underscores_survives():
    """web_search hands back real URLs, and a mangled one is a broken link."""
    assert to_plain_text("ссылка https://ex.com/a_b_c") == "ссылка https://ex.com/a_b_c"


def test_asterisks_between_digits_are_arithmetic():
    """"2 * 3" was already handled by the whitespace rule, but "5*4" was not —
    it paired with the next one and turned "5*4 и 3*2" into "54 и 32"."""
    assert to_plain_text("5*4 и 3*2") == "5*4 и 3*2"
    assert to_plain_text("2 * 3 = 6") == "2 * 3 = 6"


def test_emphasis_still_works_next_to_punctuation():
    """The word-boundary rule must not be so strict that real emphasis stops
    being recognised."""
    assert to_plain_text("(**жирный**)") == "(жирный)"
    assert to_plain_text("**Куда:** Ben Shemen") == "Куда: Ben Shemen"
    assert to_plain_text("_курсив_, дальше текст") == "курсив, дальше текст"
