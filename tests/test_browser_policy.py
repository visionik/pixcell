"""Tests for pixcell.browser.policy: Policy dataclass, offline mode, and navigation gates."""

from __future__ import annotations

import tempfile
from pathlib import Path

from pixcell.browser import policy
from pixcell.browser.policy import Policy, URLPolicy, decide, file_path, host_of, navigation_gate


def test_default_policy_allows_pages_and_refuses_non_page_schemes():
    default = Policy()
    assert URLPolicy is Policy
    for allowed in (
        "https://example.com/path",
        "http://localhost:3000",
        "about:blank",
        "data:text/html,<h1>hi</h1>",
        Path("/tmp/report.html").resolve().as_uri(),
    ):
        assert default.allows(allowed) and default(allowed), allowed
    for refused in (
        "javascript:alert(1)",
        "mailto:a@b.c",
        "tel:123",
        "chrome://settings",
        "blob:null/1234",
        "ftp://example.com/file",
        "file://remote-host.example/share/doc.html",
        "",
    ):
        assert not default.allows(refused) and not default(refused), refused


def test_policy_allow_and_deny_hosts_with_wildcards():
    restricted = Policy(
        allow_hosts=frozenset({"example.com", "*.cdn.example.org"}),
        deny_hosts=frozenset({"blocked.cdn.example.org"}),
    )
    assert restricted("https://Example.COM:8443/a")
    assert restricted("https://assets.cdn.example.org/app.js")
    assert not restricted("https://blocked.cdn.example.org/app.js")  # deny wins
    assert not restricted("https://sub.example.com/")  # exact host, not wildcard
    assert not restricted("https://evil-cdn.example.org/")  # suffix boundary


def test_policy_offline_allows_only_file_and_data(tmp_path: Path):
    allowed_dir = tmp_path / "docs"
    allowed_dir.mkdir()
    local_page = (allowed_dir / "index.html").as_uri()
    sibling_page = (tmp_path / "docs-evil" / "index.html").as_uri()

    offline = Policy.offline(file_roots=[allowed_dir])
    assert offline.no_network is True
    assert offline(local_page)
    assert offline("data:text/html;charset=utf-8,<h1>Local</h1>")
    assert offline("about:blank")

    for blocked in (
        "https://example.com/",
        "http://localhost:8000/",
        "http://127.0.0.1:3000/",
        "http://[::1]:5000/",
        "about:version",
        "javascript:alert(1)",
        sibling_page,
        Path(Path.cwd().anchor, "etc", "hosts").as_uri(),
        "file://evil.example/share/a.html",
    ):
        assert not offline(blocked), blocked

    # Policy(no_network=True) even with default schemes still blocks network URLs.
    unscoped_offline = Policy(no_network=True)
    assert unscoped_offline(local_page)
    assert unscoped_offline("data:image/png;base64,AAAA")
    assert not unscoped_offline("https://example.com")
    assert not unscoped_offline("http://localhost:3000")


def test_policy_localhost_mode(tmp_path: Path):
    local = Policy.localhost(file_roots=[tmp_path])
    assert local("http://localhost:3000/app")
    assert local("http://127.0.0.1:8000")
    assert local("http://[::1]:5000")
    assert local((tmp_path / "a.html").as_uri())
    assert not local("https://example.com")
    assert not local("data:text/html,hi")
    assert not local(Path(Path.cwd().anchor, "etc", "hosts").as_uri())


def test_host_of_and_file_path(tmp_path: Path):
    assert host_of("https://Example.COM:8443/x") == "example.com"
    assert host_of("file:///tmp/a.html") == ""
    sample = tmp_path / "a b.html"
    sample.write_text("x", "utf-8")
    assert file_path(sample.as_uri()) == sample.resolve()
    assert file_path("https://example.com/a.html") is None
    assert file_path("file://evil.example/a.html") is None


# --- Ported Console policy & navigation_gate tests -----------------------------------------


def test_local_pages_always_open():
    for url in ("about:blank", "http://localhost:3000", "http://127.0.0.1:8000/x", "http://[::1]:5000"):
        for setting in ("ask", "local", "any"):
            assert decide(url, setting, set()) == "open", (url, setting)


def test_other_sites_follow_the_setting_unless_the_user_opened_them():
    assert decide("https://example.com", "ask", set()) == "ask"
    assert decide("https://example.com", "local", set()) == "refuse"
    assert decide("https://example.com", "any", set()) == "open"
    assert decide("https://example.com/a", "ask", {"example.com"}) == "open"
    assert decide("https://evil.example.com", "ask", {"example.com"}) == "ask"


def test_non_pages_and_data_pages():
    for url in ("mailto:a@b.c", "tel:123", "javascript:alert(1)"):
        assert decide(url, "any", set()) == "refuse"
    page = "data:text/html,<h1>Sign in</h1>"
    assert decide(page, "ask", set()) == "ask"
    assert decide(page, "local", set()) == "refuse"
    assert decide(page, "any", set()) == "open"


def test_files_outside_the_project_and_temp_are_asked_about(tmp_path: Path, monkeypatch):
    project, temp, elsewhere = tmp_path / "project", tmp_path / "temp", tmp_path / "home"
    for folder in (project, temp, elsewhere):
        folder.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp))
    monkeypatch.setattr(policy, "_SYSTEM_TEMP", temp)
    inside = (project / "report.html").as_uri()
    in_temp = (temp / "agent-report.html").as_uri()
    home = (elsewhere / ".ssh" / "config").as_uri()
    assert decide(inside, "ask", set(), project) == "open"
    assert decide(inside, "ask", set(), None) == "ask"
    assert decide(in_temp, "ask", set(), project) == "open"
    assert decide(home, "ask", set(), project) == "ask"
    assert decide(home, "local", set(), project) == "refuse"
    assert decide(home, "any", set(), project) == "open"
    assert decide((tmp_path / "temp-evil" / "x.html").as_uri(), "ask", set(), project) == "ask"


def test_a_page_an_agent_opened_may_go_only_where_the_policy_allows(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    report = (project / "report.html").as_uri()
    setting = ["ask"]
    hosts: set[str] = set()
    allow = navigation_gate(report, lambda: setting[0], hosts, project)
    assert allow(report)
    assert allow((project / "other.html").as_uri())
    assert allow("http://localhost:3000/") and allow("http://127.0.0.1:8000/x") and allow("about:blank")
    for elsewhere in (
        "file:///etc/hosts",
        "https://example.com/login",
        "http://192.168.1.1/",
        "http://10.0.0.1:8080/",
        "data:text/html,<h1>fake</h1>",
        "javascript:alert(1)",
        "chrome://settings",
        "blob:null/1234",
        "ftp://example.com/",
    ):
        assert not allow(elsewhere), elsewhere
    hosts.add("example.com")
    assert allow("https://example.com/login")
    setting[0] = "any"
    assert allow("https://other.example/") and not allow("javascript:alert(1)")
    setting[0] = "local"
    assert not allow("https://other.example/") and not allow("file:///etc/hosts")


def test_a_site_the_user_agreed_to_may_go_on_within_itself(tmp_path: Path):
    allow = navigation_gate("https://docs.example.com/a", lambda: "ask", set(), tmp_path)
    assert allow("https://docs.example.com/b?c")
    assert not allow("https://example.com/") and not allow("https://evil.test/")
    outside = Path.home() / "pixcell-policy-not-here" / "page.html"
    allow = navigation_gate(outside.as_uri(), lambda: "ask", set(), tmp_path)
    assert allow(outside.as_uri())
    assert not allow(outside.with_name("other.html").as_uri())
    allow = navigation_gate("data:text/html,hi", lambda: "ask", set(), None)
    assert allow("data:text/html,hi") and not allow("data:text/html,other")
