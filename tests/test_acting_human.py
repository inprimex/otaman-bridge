"""Tests for acting_human.py — team-mode 2.1 / B1 bridge writer primitive."""

from __future__ import annotations

import json
from pathlib import Path

from otaman_bridge.acting_human import (
    ACTING_HUMAN_FILENAME,
    SOURCE_ATTACH_JWT,
    clear_acting_human,
    default_acting_human_path,
    is_multi_human,
    resolve_acting_human,
    write_acting_human,
)

# Two-human roster = multi-human tenant.
_MULTI = [
    {"name": "Roman Starikov", "email": "roman@acme.com", "roles": ["cofounder", "cto"]},
    {"name": "Alice Dev", "email": "alice@acme.com", "roles": ["developer"]},
]
_SINGLE = [{"name": "Roman Starikov", "email": "roman@acme.com", "roles": ["cofounder"]}]


def _write_platform(root: Path, roster: list, *, nested: bool = False) -> None:
    import yaml

    base = root / "otaman-meta" if nested else root
    base.mkdir(parents=True, exist_ok=True)
    (base / "platform.yaml").write_text(
        yaml.safe_dump({"project": "acme", "human-roster": roster}), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# is_multi_human


class TestIsMultiHuman:
    def test_zero_or_one_is_single(self):
        assert is_multi_human([]) is False
        assert is_multi_human(_SINGLE) is False

    def test_two_or_more_is_multi(self):
        assert is_multi_human(_MULTI) is True

    def test_ignores_non_dict_entries(self):
        assert is_multi_human(["nope", {"email": "a@x"}]) is False  # only 1 valid
        assert is_multi_human([{"email": "a@x"}, {"email": "b@x"}, 3]) is True


# ---------------------------------------------------------------------------
# resolve_acting_human


class TestResolve:
    def test_single_human_is_inert(self):
        assert resolve_acting_human("roman@acme.com", _SINGLE) is None

    def test_empty_sub_is_inert(self):
        assert resolve_acting_human("", _MULTI) is None
        assert resolve_acting_human("   ", _MULTI) is None

    def test_multi_human_matched_carries_name_and_roles(self):
        ctx = resolve_acting_human("roman@acme.com", _MULTI)
        assert ctx == {
            "email": "roman@acme.com",
            "name": "Roman Starikov",
            "roles": ["cofounder", "cto"],
            "source": SOURCE_ATTACH_JWT,
        }

    def test_email_match_is_case_insensitive(self):
        ctx = resolve_acting_human("Roman@ACME.com", _MULTI)
        assert ctx is not None
        assert ctx["name"] == "Roman Starikov"
        assert ctx["email"] == "Roman@ACME.com"  # preserves the JWT sub as presented

    def test_multi_human_unrostered_is_attributed_with_empty_roles(self):
        ctx = resolve_acting_human("ghost@acme.com", _MULTI)
        assert ctx == {
            "email": "ghost@acme.com",
            "name": "",
            "roles": [],
            "source": SOURCE_ATTACH_JWT,
        }

    def test_non_list_roles_coerced_to_empty(self):
        roster = [
            {"email": "a@x.com", "roles": "oops"},
            {"email": "b@x.com", "roles": ["dev"]},
        ]
        ctx = resolve_acting_human("a@x.com", roster)
        assert ctx is not None
        assert ctx["roles"] == []


# ---------------------------------------------------------------------------
# write_acting_human / clear_acting_human


class TestWrite:
    def test_multi_human_writes_file(self, tmp_path):
        _write_platform(tmp_path, _MULTI)
        path = write_acting_human(tmp_path, "roman@acme.com")
        assert path == default_acting_human_path(tmp_path)
        assert path.is_file()
        data = json.loads(path.read_text())
        assert data["email"] == "roman@acme.com"
        assert data["roles"] == ["cofounder", "cto"]
        assert data["source"] == SOURCE_ATTACH_JWT

    def test_single_human_writes_nothing(self, tmp_path):
        _write_platform(tmp_path, _SINGLE)
        assert write_acting_human(tmp_path, "roman@acme.com") is None
        assert not default_acting_human_path(tmp_path).exists()

    def test_inert_removes_stale_file(self, tmp_path):
        # A prior multi-human session left a file; tenant is now single-human.
        _write_platform(tmp_path, _SINGLE)
        stale = default_acting_human_path(tmp_path)
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text('{"email":"old@acme.com"}', encoding="utf-8")
        assert write_acting_human(tmp_path, "roman@acme.com") is None
        assert not stale.exists()  # stale attribution cleared

    def test_no_leftover_tmp_file(self, tmp_path):
        _write_platform(tmp_path, _MULTI)
        write_acting_human(tmp_path, "alice@acme.com")
        tmps = list((tmp_path / ".otaman").glob("*.tmp"))
        assert tmps == []

    def test_reads_roster_from_nested_platform_yaml(self, tmp_path):
        _write_platform(tmp_path, _MULTI, nested=True)  # project_root/otaman-meta/platform.yaml
        path = write_acting_human(tmp_path, "alice@acme.com")
        assert path is not None
        assert json.loads(path.read_text())["roles"] == ["developer"]

    def test_no_platform_yaml_is_inert(self, tmp_path):
        assert write_acting_human(tmp_path, "roman@acme.com") is None
        assert not default_acting_human_path(tmp_path).exists()

    def test_clear_removes_file_and_is_idempotent(self, tmp_path):
        _write_platform(tmp_path, _MULTI)
        path = write_acting_human(tmp_path, "roman@acme.com")
        assert path.is_file()
        clear_acting_human(tmp_path)
        assert not path.exists()
        clear_acting_human(tmp_path)  # no-op, must not raise

    def test_filename_constant(self, tmp_path):
        assert default_acting_human_path(tmp_path).name == ACTING_HUMAN_FILENAME
