def test_put_is_idempotent_and_content_addressed(vault):
    ref1 = vault.put(b"hello world", ".txt")
    ref2 = vault.put(b"hello world", ".txt")
    assert ref1 == ref2
    assert vault.has(ref1)
    assert vault.path(ref1).read_bytes() == b"hello world"
    # layout: ab/cd/<sha256>.txt
    parts = ref1.split("/")
    assert len(parts) == 3
    assert parts[2].startswith(parts[0] + parts[1])
    assert ref1.endswith(".txt")


def test_different_content_different_ref(vault):
    assert vault.put(b"a", ".txt") != vault.put(b"b", ".txt")


def test_ext_normalized(vault):
    assert vault.put(b"x", "md").endswith(".md")
