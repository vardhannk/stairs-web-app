"""
NEW integration tests to append to test_integration.py.
Catches: signature drift between models_v2 calls and app.py functions,
         dead function references in app.py.

Uses pure AST analysis (no fragile regex).
"""
import ast
import os
import pytest


APP_DIR = os.environ.get("STAIRS_APP_DIR", "/opt/stairs-web-app")
APP_PY = os.path.join(APP_DIR, "app.py")
MODELS_V2_PY = os.path.join(APP_DIR, "models_v2.py")


# ─── Helpers (AST-based) ───────────────────────────────────────────

def _read(path):
    with open(path, "r") as f:
        return f.read()


def _module_function_signatures(source):
    """Return {function_name: arity_info} for top-level function definitions."""
    tree = ast.parse(source)
    sigs = {}
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef):
            sigs[node.name] = {
                "min_pos": len(node.args.args) - len(node.args.defaults),
                "max_pos": len(node.args.args),
                "varargs": node.args.vararg is not None,
                "lineno": node.lineno,
            }
    return sigs


def _find_attribute_calls(source, attr_name):
    """Find all calls like `OBJ.attr_name(...)` and return their positional arg counts.
    Returns list of dicts: {line, n_pos_args, n_keyword_args}."""
    tree = ast.parse(source)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == attr_name:
                # Star-args make positional count ambiguous; flag it
                has_starargs = any(isinstance(a, ast.Starred) for a in node.args)
                n_pos = sum(0 if isinstance(a, ast.Starred) else 1 for a in node.args)
                out.append({
                    "line": node.lineno,
                    "n_pos_args": n_pos,
                    "has_starargs": has_starargs,
                    "n_kwargs": len(node.keywords),
                })
    return out


def _collect_all_names_in_scope(source):
    """Walk full AST and collect every name 'made available' anywhere in the module —
    top-level defs, imports (incl. inside functions), assignments, class definitions.
    Used to identify which bare-name calls actually have a defined target."""
    names = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.AsyncFunctionDef):
            names.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    names.add(tgt.id)
                elif isinstance(tgt, ast.Tuple):
                    for elt in tgt.elts:
                        if isinstance(elt, ast.Name):
                            names.add(elt.id)
        elif isinstance(node, ast.AugAssign):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
        elif isinstance(node, ast.For):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
        elif isinstance(node, ast.With):
            for item in node.items:
                if item.optional_vars and isinstance(item.optional_vars, ast.Name):
                    names.add(item.optional_vars.id)
        elif isinstance(node, ast.arguments):
            for arg in (node.args + node.kwonlyargs):
                names.add(arg.arg)
    return names


def _find_bare_calls(source):
    """Find bare function calls like `foo(...)` — NOT `obj.foo(...)`."""
    tree = ast.parse(source)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            out.append({"line": node.lineno, "name": node.func.id})
    return out


# Python builtins that won't be in module-defined names
PY_BUILTINS = set(dir(__builtins__) if isinstance(__builtins__, dict) else dir(__builtins__))


# ═══════════════════════════════════════════════════════════════════
# 5. SIGNATURE COMPATIBILITY
# ═══════════════════════════════════════════════════════════════════

class TestSignatureCompatibility:
    """Catch arity drift between models_v2 calls and app.py function definitions.
    Today's OB skip was exactly this: models_v2 called app.quote_option with 3
    args while app.py defines it with 2 args. The TypeError was swallowed by
    safe_quote_directional, leaving OB silently no-op."""

    def test_app_quote_option_arity_matches_models_v2_calls(self):
        """For every app.quote_option(...) call in models_v2.py, verify the
        number of positional args is within the range that app.quote_option accepts."""
        app_sigs = _module_function_signatures(_read(APP_PY))
        if "quote_option" not in app_sigs:
            pytest.skip("app.py has no top-level quote_option function")

        max_args = app_sigs["quote_option"]["max_pos"]
        accepts_varargs = app_sigs["quote_option"]["varargs"]

        calls = _find_attribute_calls(_read(MODELS_V2_PY), "quote_option")
        violations = []
        for c in calls:
            if c["has_starargs"]:
                continue
            if not accepts_varargs and c["n_pos_args"] > max_args:
                violations.append(
                    f"  models_v2.py line {c['line']}: app.quote_option called with "
                    f"{c['n_pos_args']} positional args, but app.quote_option accepts only {max_args}"
                )

        if violations:
            msg = ["app.quote_option signature mismatch — will raise TypeError at runtime:"]
            msg.extend(violations)
            msg.append(f"  app.py defines: quote_option(...{max_args} positional args)")
            pytest.fail("\n".join(msg))

    def test_app_get_nifty_spread_quote_arity_matches_models_v2(self):
        """Same check for the AIT quote function (currently working, but guard against drift)."""
        app_sigs = _module_function_signatures(_read(APP_PY))
        if "get_nifty_spread_quote" not in app_sigs:
            pytest.skip("app.py has no get_nifty_spread_quote function")

        max_args = app_sigs["get_nifty_spread_quote"]["max_pos"]
        accepts_varargs = app_sigs["get_nifty_spread_quote"]["varargs"]

        calls = _find_attribute_calls(_read(MODELS_V2_PY), "get_nifty_spread_quote")
        violations = []
        for c in calls:
            if c["has_starargs"]:
                continue
            if not accepts_varargs and c["n_pos_args"] > max_args:
                violations.append(
                    f"  models_v2.py line {c['line']}: app.get_nifty_spread_quote called "
                    f"with {c['n_pos_args']} positional args, accepts only {max_args}"
                )
        if violations:
            pytest.fail("\n".join(["app.get_nifty_spread_quote signature mismatch:"] + violations))


# ═══════════════════════════════════════════════════════════════════
# 6. DEAD FUNCTION REFERENCES
# ═══════════════════════════════════════════════════════════════════

class TestDeadFunctionReferences:
    """Catch calls to functions that aren't defined anywhere in app.py.
    Today's workstation rollover error spam (every minute, 12+ hours) was
    exactly this pattern: app.py calls handle_obw_expiry_rollover() but
    no such function is defined → NameError at runtime."""

    def test_critical_handlers_referenced_only_if_defined(self):
        """Specifically check known-risky handler names. If app.py calls them,
        they MUST be defined."""
        src = _read(APP_PY)
        defined = _collect_all_names_in_scope(src)

        # Known risky names — handlers that we've refactored. If any of these
        # are CALLED in app.py but not DEFINED, we have a dead reference.
        risky = [
            "handle_obw_expiry_rollover",
            "handle_aitw_expiry_rollover",
            "handle_ob_expiry_rollover",
            "handle_ait_expiry_rollover",
            "handle_nifty_exp_tuesday_exit",
            "handle_nifty_exp_monday_entry",
            "handle_ob_on_signal",
            "handle_ait_on_signal",
            "handle_nifty_exp_on_signal",
            "handle_obw_on_signal",
            "handle_aitw_on_signal",
        ]

        # Find all bare calls in app.py
        bare_calls = _find_bare_calls(src)
        called_names = {c["name"] for c in bare_calls}

        violations = []
        for name in risky:
            if name in called_names and name not in defined:
                # Find lines where it's called
                lines = sorted({c["line"] for c in bare_calls if c["name"] == name})
                violations.append(f"  '{name}' called at line(s) {lines} but NOT defined in app.py")

        if violations:
            msg = ["Dead function references in app.py — will raise NameError at runtime:"]
            msg.extend(violations)
            pytest.fail("\n".join(msg))


class TestClosePathHelpersExist:
    """Ensure the contract-by-symbol helpers that _close_trade_inline depends on
    actually exist in app.py. Without these, the fixed close path crashes."""

    def test_quote_by_symbol_helpers_exist_in_app(self):
        app_src = _read(APP_PY)
        app_sigs = _module_function_signatures(app_src)
        # If models_v2 references these, app.py must define them
        mv2_src = _read(MODELS_V2_PY)
        if "quote_option_by_symbol" in mv2_src:
            assert "quote_option_by_symbol" in app_sigs, (
                "models_v2 calls app.quote_option_by_symbol but app.py doesn't define it"
            )
        if "get_spread_quote_by_symbols" in mv2_src:
            assert "get_spread_quote_by_symbols" in app_sigs, (
                "models_v2 calls app.get_spread_quote_by_symbols but app.py doesn't define it"
            )
