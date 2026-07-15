"""cxswap — CLI entry point."""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Optional

from . import __version__
from .auth import AuthError
from .paths import auth_path, codex_home, store_path, swap_home
from .store import Account, Store, load_store, locked, save_store, now
from .switcher import (
    SwitchError,
    add_account,
    codex_running,
    do_switch,
    live_refresh_usage,
    resolve_target,
    rotation_target,
    best_target,
    safe_login,
    snapshot_usage,
    sync_back,
)
from . import usage as usage_mod
from .autoswitch import run_auto
from .session import run_codex


def _err(msg: str) -> int:
    print(f"cxswap: {msg}", file=sys.stderr)
    return 1


def _account_row(store: Store, a: Account, current_id: Optional[str]) -> dict:
    age = (now() - a.usage_at) if a.usage_at else None
    return {
        "slot": a.slot,
        "email": a.email,
        "alias": a.alias,
        "plan": a.plan,
        "active": a.account_id == current_id,
        "disabled": a.disabled,
        "status": a.status,
        "api_key_only": a.api_key_only,
        "usage": a.usage,
        "usage_at": a.usage_at,
        "usage_text": usage_mod.describe(a.usage, age),
    }


def cmd_add(args) -> int:
    if args.login:
        # Safe login: auth.json is moved aside first so `codex login` has no
        # managed credentials to revoke — the current session survives.
        with locked():
            store = load_store()
            sync_back(store)
            save_store(store)
        try:
            identity = safe_login(load_store())
        except SwitchError as e:
            return _err(str(e))
        print(f"logged in as {identity.label}")
    with locked():
        store = load_store()
        sync_back(store)
        acc = add_account(store, alias=args.alias)
        live_refresh_usage(store, acc)
        save_store(store)
    print(f"saved {acc.display()} in slot {acc.slot} ({acc.plan or 'unknown plan'})")
    if acc.disabled:
        print(f"note: slot {acc.slot} is disabled — `cxswap enable {acc.slot}` to rotate onto it")
    return 0


def cmd_list(args) -> int:
    with locked():
        store = load_store()
        current = sync_back(store)
        if current is not None:
            snapshot_usage(store, store.by_account_id(current.account_id))
        if not args.offline:
            for a in store.sorted_accounts():
                live_refresh_usage(store, a)
        save_store(store)

    current_id = current.account_id if current else None
    rows = [_account_row(store, a, current_id) for a in store.sorted_accounts()]
    if args.json:
        print(json.dumps({"accounts": rows, "current": current.label if current else None}, indent=2))
        return 0
    if not rows:
        print("no accounts stored — log in with `codex login`, then run `cxswap add`")
        return 0
    for r in rows:
        marker = "→" if r["active"] else " "
        name = r["email"] or "(api key)"
        alias = f"  [{r['alias']}]" if r["alias"] else ""
        flags = ""
        if r["disabled"]:
            flags += "  (disabled)"
        if r["status"] == "needs_relogin":
            flags += "  ⚠ needs `codex login` + `cxswap add`"
        if r["api_key_only"]:
            flags += "  (api-key)"
        plan = f"  {r['plan']}" if r["plan"] else ""
        print(f"{marker} {r['slot']}. {name}{alias}{plan}{flags}")
        print(f"     {r['usage_text']}")
    if current is None:
        print("\n(no active login in auth.json)")
    elif current_id and not any(r["active"] for r in rows):
        print(f"\ncurrent login {current.label} is not managed — run `cxswap add`")
    return 0


def cmd_status(args) -> int:
    with locked():
        store = load_store()
        current = sync_back(store)
        if current is not None:
            acc = store.by_account_id(current.account_id)
            snapshot_usage(store, acc)
            if not args.offline:
                live_refresh_usage(store, acc)
        save_store(store)
    if current is None:
        print("not logged in (no auth.json)")
        return 1
    acc = store.by_account_id(current.account_id)
    if args.json:
        row = _account_row(store, acc, current.account_id) if acc else {"email": current.label, "managed": False}
        print(json.dumps(row, indent=2))
        return 0
    managed = f"slot {acc.slot}" if acc else "NOT managed (run `cxswap add`)"
    print(f"active: {current.label} ({current.plan or 'unknown plan'}) — {managed}")
    if acc:
        age = (now() - acc.usage_at) if acc.usage_at else None
        print(f"usage:  {usage_mod.describe(acc.usage, age)}")
    return 0


def cmd_switch(args) -> int:
    with locked():
        store = load_store()
        current = sync_back(store)
        current_id = current.account_id if current else None
        try:
            if args.target:
                target = resolve_target(store, args.target)
            elif args.strategy == "best":
                target = best_target(store, current_id)
                if target is None:
                    save_store(store)
                    return _err("no viable account (all disabled or exhausted)")
            else:
                target = rotation_target(store, current_id)
        except SwitchError as e:
            save_store(store)
            return _err(str(e))

        if target.disabled and not args.target:
            save_store(store)
            return _err(f"{target.display()} is disabled")
        if target.status == "needs_relogin":
            save_store(store)
            return _err(
                f"{target.display()} session was revoked — run `codex login` "
                f"with it, then `cxswap add`"
            )
        if current_id and target.account_id == current_id:
            save_store(store)
            print(f"already on {target.display()}")
            return 0

        identity = do_switch(store, target, None)
        save_store(store)

    print(f"switched to {identity.label} (slot {target.slot})")
    if codex_running():
        print("note: running codex sessions keep the old account until restarted")
    return 0


def cmd_remove(args) -> int:
    with locked():
        store = load_store()
        current = sync_back(store)
        try:
            acc = resolve_target(store, args.target)
        except SwitchError as e:
            return _err(str(e))
        if current and acc.account_id == current.account_id and not args.force:
            return _err(f"{acc.display()} is the active account — pass --force to remove anyway")
        store.accounts.remove(acc)
        save_store(store)
    print(f"removed {acc.display()} (slot {acc.slot})")
    return 0


def cmd_alias(args) -> int:
    with locked():
        store = load_store()
        try:
            acc = resolve_target(store, args.target)
        except SwitchError as e:
            return _err(str(e))
        acc.alias = args.name or None
        save_store(store)
    print(f"slot {acc.slot} alias: {acc.alias or '(cleared)'}")
    return 0


def _set_disabled(target: str, disabled: bool) -> int:
    with locked():
        store = load_store()
        try:
            acc = resolve_target(store, target)
        except SwitchError as e:
            return _err(str(e))
        acc.disabled = disabled
        save_store(store)
    print(f"{acc.display()}: {'disabled' if disabled else 'enabled'}")
    return 0


def cmd_run(args) -> int:
    with locked():
        store = load_store()
        sync_back(store)
        try:
            acc = resolve_target(store, args.target)
        except SwitchError as e:
            return _err(str(e))
        save_store(store)
    try:
        return run_codex(acc, args.codex_args)
    except SwitchError as e:
        return _err(str(e))


def cmd_auto(args) -> int:
    store = load_store()
    threshold = args.threshold if args.threshold is not None else float(store.setting("autoswitch.threshold"))
    interval = args.interval if args.interval is not None else float(store.setting("autoswitch.interval"))
    cooldown = args.cooldown if args.cooldown is not None else float(store.setting("autoswitch.cooldown"))
    return run_auto(threshold, interval, cooldown, args.once, args.dry_run, args.json)


def cmd_export(args) -> int:
    import os

    with locked():
        store = load_store()
        sync_back(store)
        save_store(store)
    payload = {
        "codex_swap_export": 1,
        "accounts": [
            {
                "account_id": a.account_id,
                "email": a.email,
                "plan": a.plan,
                "alias": a.alias,
                "api_key_only": a.api_key_only,
                "auth": a.auth,
            }
            for a in store.sorted_accounts()
        ],
    }
    out = args.file
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.chmod(out, 0o600)
    print(f"exported {len(payload['accounts'])} account(s) to {out} (contains secrets — handle like a password)")
    return 0


def cmd_import(args) -> int:
    try:
        with open(args.file, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return _err(f"cannot read {args.file}: {e}")
    if not payload.get("codex_swap_export"):
        return _err(f"{args.file} is not a cxswap export")
    added = updated = 0
    with locked():
        store = load_store()
        for item in payload.get("accounts", []):
            acc = store.by_account_id(item["account_id"])
            if acc is None:
                store.accounts.append(
                    Account(
                        slot=store.next_slot(),
                        account_id=item["account_id"],
                        email=item.get("email"),
                        plan=item.get("plan"),
                        alias=item.get("alias"),
                        api_key_only=item.get("api_key_only", False),
                        auth=item.get("auth") or {},
                        added_at=now(),
                        updated_at=now(),
                    )
                )
                added += 1
            else:
                incoming = item.get("auth") or {}
                if (incoming.get("last_refresh") or "") > (acc.auth.get("last_refresh") or ""):
                    acc.auth = incoming
                    acc.updated_at = now()
                    updated += 1
        save_store(store)
    print(f"imported: {added} new, {updated} refreshed")
    return 0


def cmd_config(args) -> int:
    with locked():
        store = load_store()
        if args.set:
            key, _, value = args.set.partition("=")
            if not value:
                return _err("use --set key=value")
            try:
                store.settings[key] = float(value) if "." in value else int(value)
            except ValueError:
                store.settings[key] = value
            save_store(store)
            print(f"{key} = {store.settings[key]}")
            return 0
    from .store import DEFAULT_SETTINGS

    for key in sorted(set(DEFAULT_SETTINGS) | set(store.settings)):
        print(f"{key} = {store.setting(key)}")
    return 0


def cmd_path(args) -> int:
    print(f"store:      {store_path()}")
    print(f"swap home:  {swap_home()}")
    print(f"codex home: {codex_home()}")
    print(f"auth.json:  {auth_path()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cxswap",
        description="Multi-account switcher for the OpenAI Codex CLI.",
    )
    p.add_argument("--version", action="version", version=f"cxswap {__version__}")
    sub = p.add_subparsers(dest="command")

    sp = sub.add_parser("add", help="Save the currently logged-in codex account")
    sp.add_argument("--alias", help="Optional short name for the account")
    sp.add_argument(
        "--login",
        action="store_true",
        help="Run `codex login` first, WITHOUT revoking the current session "
        "(auth.json is moved aside during the login)",
    )
    sp.set_defaults(func=cmd_add)

    for name in ("list", "ls"):
        sp = sub.add_parser(name, help="Dashboard: all accounts with live usage")
        sp.add_argument("--json", action="store_true")
        sp.add_argument("--offline", action="store_true",
                        help="Skip live usage fetch (rollout data only)")
        sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("status", help="Show the active account")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--offline", action="store_true")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("switch", help="Rotate to the next account, or jump to NUM|EMAIL|ALIAS")
    sp.add_argument("target", nargs="?", metavar="NUM|EMAIL|ALIAS")
    sp.add_argument("--strategy", choices=["next", "best"], default="next",
                    help="'best' picks the account with most quota left")
    sp.set_defaults(func=cmd_switch)

    for name in ("remove", "rm"):
        sp = sub.add_parser(name, help="Remove an account from the store")
        sp.add_argument("target", metavar="NUM|EMAIL|ALIAS")
        sp.add_argument("--force", action="store_true")
        sp.set_defaults(func=cmd_remove)

    sp = sub.add_parser("alias", help="Set (or clear) an account alias")
    sp.add_argument("target", metavar="NUM|EMAIL")
    sp.add_argument("name", nargs="?", help="Omit to clear")
    sp.set_defaults(func=cmd_alias)

    sp = sub.add_parser("disable", help="Hold an account out of rotation")
    sp.add_argument("target", metavar="NUM|EMAIL|ALIAS")
    sp.set_defaults(func=lambda a: _set_disabled(a.target, True))

    sp = sub.add_parser("enable", help="Put an account back into rotation")
    sp.add_argument("target", metavar="NUM|EMAIL|ALIAS")
    sp.set_defaults(func=lambda a: _set_disabled(a.target, False))

    sp = sub.add_parser("run", help="Launch codex as an account in this terminal only")
    sp.add_argument("target", metavar="NUM|EMAIL|ALIAS")
    sp.add_argument("codex_args", nargs=argparse.REMAINDER,
                    help="Args after -- go to codex verbatim")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("auto", help="Watch usage and switch before the limit hits")
    sp.add_argument("--threshold", type=float, help="Switch at this used %% (default 90)")
    sp.add_argument("--interval", type=float, help="Poll seconds (default 60)")
    sp.add_argument("--cooldown", type=float, help="Min seconds between switches (default 300)")
    sp.add_argument("--once", action="store_true", help="Single check (exit: 0 switched, 2 nothing, 3 blocked)")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_auto)

    sp = sub.add_parser("export", help="Export all accounts (with secrets) to a file")
    sp.add_argument("file")
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("import", help="Import accounts from an export file")
    sp.add_argument("file")
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser("config", help="Show or set settings")
    sp.add_argument("--set", metavar="KEY=VALUE")
    sp.set_defaults(func=cmd_config)

    sp = sub.add_parser("path", help="Print file locations")
    sp.set_defaults(func=cmd_path)

    return p


def main(argv: Optional[list] = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `run TARGET -- args…`: strip the separator argparse.REMAINDER keeps.
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        sys.exit(0)
    if args.command == "run" and args.codex_args and args.codex_args[0] == "--":
        args.codex_args = args.codex_args[1:]
    try:
        sys.exit(args.func(args))
    except (AuthError, SwitchError) as e:
        sys.exit(_err(str(e)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
