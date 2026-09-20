"""连接断开标记单测：纯内存标记，无需数据库/GUI。"""
from storage import db


def test_manual_offline_set_clear():
    db.clear_manual_offline("write")
    db.clear_manual_offline("read")
    assert not db.is_manual_offline("write")
    assert not db.is_manual_offline("read")
    db.set_manual_offline("write")
    assert db.is_manual_offline("write")
    assert not db.is_manual_offline("read")
    db.set_manual_offline("read")
    assert db.is_manual_offline("read")
    db.clear_manual_offline("write")
    assert not db.is_manual_offline("write")
    assert db.is_manual_offline("read")
    db.clear_manual_offline("read")
    assert not db.is_manual_offline("read")


def test_manual_offline_role_alias():
    db.clear_manual_offline("write")
    db.clear_manual_offline("read")
    db.set_manual_offline("d")
    assert db.is_manual_offline("write")
    db.set_manual_offline("archive")
    assert db.is_manual_offline("read")
    assert db.is_manual_offline("query")
    db.clear_manual_offline("buffer")
    db.clear_manual_offline("query")
    assert not db.is_manual_offline("write")
    assert not db.is_manual_offline("read")
