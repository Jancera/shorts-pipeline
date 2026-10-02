import hashlib

from shorts.project import Manifest, sha256_file, sha256_text, utcnow_iso


def test_sha256_helpers(tmp_path):
    assert sha256_text("hello") == hashlib.sha256(b"hello").hexdigest()
    f = tmp_path / "a.bin"
    f.write_bytes(b"hello")
    assert sha256_file(f) == hashlib.sha256(b"hello").hexdigest()


def test_utcnow_iso_format():
    value = utcnow_iso()
    assert value.endswith("Z")
    assert "T" in value
    assert "." not in value


def test_new_save_load_roundtrip(tmp_path):
    m = Manifest.new("demo")
    m.source = {"url": "u", "video_id": "v", "title": "t"}
    m.stage_done("fetch", outputs=["source/video.mp4"])
    path = tmp_path / "manifest.json"
    m.save(path)

    assert not (tmp_path / "manifest.json.tmp").exists()
    loaded = Manifest.load(path)
    assert loaded.name == "demo"
    assert loaded.source["title"] == "t"
    assert loaded.stages["fetch"]["status"] == "done"
    assert loaded.stages["fetch"]["outputs"] == ["source/video.mp4"]
    assert loaded.stages["fetch"]["at"].endswith("Z")


def test_stage_and_idea_merge():
    m = Manifest.new("demo")
    assert m.get_stage("ideate") is None
    assert m.is_stage_done("ideate") is False

    m.stage_done("ideate", model="gpt-4.1")
    m.stage_done("ideate", transcript_sha256="abc")
    assert m.is_stage_done("ideate") is True
    assert m.stages["ideate"]["model"] == "gpt-4.1"
    assert m.stages["ideate"]["transcript_sha256"] == "abc"

    assert m.get_idea("01-x") == {}
    m.set_idea("01-x", approved=False)
    m.set_idea("01-x", script_sha256="h1")
    m.set_idea("01-x", approved=True)
    assert m.get_idea("01-x") == {"approved": True, "script_sha256": "h1"}


def test_publish_roundtrips(tmp_path):
    from shorts.project import Manifest
    m = Manifest.new("demo")
    assert m.get_publish() == {}
    m.set_publish(start="2026-09-10T09:00:00Z", interval_hours=24)
    m.set_publish(weekdays=[1, 2, 3, 4, 5])
    m.set_idea("01-x", publish_at="2026-09-11T09:00:00Z")
    m.set_idea("01-x", youtube={"video_id": "abc", "url": "https://youtu.be/abc"})
    path = tmp_path / "manifest.json"
    m.save(path)
    back = Manifest.load(path)
    assert back.get_publish() == {
        "start": "2026-09-10T09:00:00Z", "interval_hours": 24,
        "weekdays": [1, 2, 3, 4, 5],
    }
    assert back.get_idea("01-x")["publish_at"] == "2026-09-11T09:00:00Z"
    assert back.get_idea("01-x")["youtube"]["video_id"] == "abc"


def test_publish_absent_loads_empty(tmp_path):
    from shorts.project import Manifest
    path = tmp_path / "manifest.json"
    path.write_text('{"name": "demo"}')
    assert Manifest.load(path).get_publish() == {}


# accents + an em dash: not representable the same way in cp1252 as in UTF-8
ACCENTED = "incríveis — ação"


def test_save_writes_utf8_regardless_of_locale(tmp_path):
    # ensure_ascii=False emits raw accents; the platform default encoding
    # (cp1252 on Windows) must not decide how they hit the disk.
    m = Manifest.new("demo")
    m.source = {"title": ACCENTED}
    path = tmp_path / "manifest.json"
    m.save(path)

    assert ACCENTED in path.read_bytes().decode("utf-8")


def test_load_reads_utf8_regardless_of_locale(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_bytes(
        ('{"name": "demo", "source": {"title": "%s"}}' % ACCENTED).encode("utf-8")
    )

    assert Manifest.load(path).source["title"] == ACCENTED


def test_stage_skipped(tmp_path):
    m = Manifest.new("demo")
    assert m.is_stage_skipped("fetch") is False
    assert m.is_stage_skipped("transcribe") is False

    m.stage_skipped("fetch", reason="text-input")
    assert m.is_stage_skipped("fetch") is True
    assert m.is_stage_done("fetch") is False
    stage = m.get_stage("fetch")
    assert stage is not None
    assert stage["status"] == "skipped"
    assert stage["reason"] == "text-input"
    assert stage["at"].endswith("Z")

    m.stage_skipped("transcribe")
    assert m.is_stage_skipped("transcribe") is True
    assert m.is_stage_done("transcribe") is False
    assert m.stages["transcribe"]["status"] == "skipped"

    path = tmp_path / "manifest.json"
    m.save(path)
    loaded = Manifest.load(path)
    assert loaded.is_stage_skipped("fetch") is True
    assert loaded.is_stage_skipped("transcribe") is True
    assert loaded.stages["fetch"]["status"] == "skipped"
    assert loaded.stages["transcribe"]["status"] == "skipped"


def test_settings_default_empty():
    m = Manifest.new("demo")
    assert m.settings == {}
    assert m.get_setting("nonexistent") is None
    assert m.get_setting("nonexistent", default=6) == 6


def test_settings_get_set():
    m = Manifest.new("demo")
    m.set_setting("count", 4)
    assert m.get_setting("count") == 4
    assert m.settings == {"count": 4}
    assert m.get_setting("nonexistent", default=6) == 6


def test_settings_save_load_roundtrip(tmp_path):
    m = Manifest.new("demo")
    m.set_setting("count", 4)
    path = tmp_path / "manifest.json"
    m.save(path)

    loaded = Manifest.load(path)
    assert loaded.settings == {"count": 4}
    assert loaded.get_setting("count") == 4


def test_settings_absent_loads_empty(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text('{"name": "demo"}')
    loaded = Manifest.load(path)
    assert loaded.settings == {}
    assert loaded.get_setting("count", default=6) == 6



def test_concurrent_saves_do_not_collide(tmp_path):
    # The UI fires two publish GETs at once and each one saves the manifest; a
    # temp file shared between saves made one os.replace find its source gone.
    import threading

    path = tmp_path / "manifest.json"
    Manifest.new("demo").save(path)
    errors = []
    barrier = threading.Barrier(8)

    def worker():
        m = Manifest.load(path)
        barrier.wait()
        try:
            for _ in range(60):
                m.save(path)
        except Exception as exc:  # noqa: BLE001 - any failure is the bug
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert errors == []
    assert Manifest.load(path).name == "demo"
    assert not list(tmp_path.glob("*.tmp"))
