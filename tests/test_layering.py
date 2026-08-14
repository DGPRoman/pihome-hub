"""The dependency rules docs/architecture.md claims the code follows.

Layering is the kind of property that holds until one convenient import breaks it,
and nothing else in a test suite notices. These read the import statements rather
than the runtime graph, so a violation fails here at the point it is written.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

import pytest

SOURCE_ROOT = Path(__file__).resolve().parent.parent / "src" / "pihome_hub"

#: Packages holding the logic and the state. None of them may know how requests
#: arrive. ``storage`` is here for the same reason the three domains are: what a
#: row means must not depend on the shape of the request that wrote it.
_DOMAINS: Final = ("relays", "sensors", "automation", "storage", "accounts")

#: Everything that would make a domain package depend on being served over HTTP.
_WEB_FRAMEWORKS: Final = ("fastapi", "starlette", "uvicorn", "pihome_hub.api")

#: Which domain may import which. Automation is a statement about a sensor and a
#: relay, so it reaches into both; neither of those needs a rule to exist.
_ALLOWED_DOMAIN_IMPORTS: Final = {
    "relays": frozenset(),
    "sensors": frozenset(),
    "automation": frozenset({"relays", "sensors"}),
    # Storage knows about rows, not about relays. Which table a domain keeps its
    # state in is that domain's business, and the dependency points that way.
    "storage": frozenset(),
    # Who may log in is not a statement about any particular relay or sensor. If
    # this ever needs one of them, the rule to add is a role, not an import.
    "accounts": frozenset(),
}


def _imported_modules(source: Path) -> set[str]:
    """Every module name the file imports, dotted and absolute."""
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    names: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            names.add(node.module)

    return names


def _python_files(package: str) -> list[Path]:
    found = sorted((SOURCE_ROOT / package).rglob("*.py"))
    assert found, f"no source files found under {package} — this test is out of date"
    return found


class TestTheDomainDoesNotKnowAboutHttp:
    @pytest.mark.parametrize("package", _DOMAINS)
    def test_no_web_framework_is_imported(self, package: str) -> None:
        offenders = {
            f"{source.relative_to(SOURCE_ROOT)} -> {module}"
            for source in _python_files(package)
            for module in _imported_modules(source)
            if module.startswith(_WEB_FRAMEWORKS)
        }
        assert not offenders, f"the domain reached for the web layer: {sorted(offenders)}"


class TestTheDomainsStayInOneDirection:
    @pytest.mark.parametrize("package", _DOMAINS)
    def test_only_the_allowed_domains_are_imported(self, package: str) -> None:
        reached = {
            module.split(".")[1]
            for source in _python_files(package)
            for module in _imported_modules(source)
            if module.startswith("pihome_hub.") and module.split(".")[1] in _DOMAINS
        } - {package}

        assert reached == _ALLOWED_DOMAIN_IMPORTS[package]


class TestOnlyTheApplicationFactoryPicksABackend:
    def test_no_route_module_imports_a_backend(self) -> None:
        """A route receives a service that already has one, and cannot choose another."""
        backends = ("pihome_hub.relays.mock", "pihome_hub.relays.gpio", "pihome_hub.relays.factory")
        offenders = {
            f"{source.relative_to(SOURCE_ROOT)} -> {module}"
            for source in _python_files("api")
            for module in _imported_modules(source)
            if module.startswith(backends)
        }
        assert not offenders, f"a route module chose a backend: {sorted(offenders)}"
