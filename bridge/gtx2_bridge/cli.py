"""gtx2-bridge command line: manifest / scan / send / render / serve.

Offline-first: every action supports ``--dry-run`` (build + print the frames, no BLE). Live
sends connect to the target watch, run the action, and disconnect. The generic ``send`` reaches
the whole command catalog; ``buzz``/``notify``/``set-time``/``weather`` are thin conveniences.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Dict, List

from . import catalog
from .config import BridgeConfig
from .dispatch import Dispatcher


def _parse_params(pairs: List[str], params_json: str) -> Dict[str, object]:
    """``["title=Hi", "duration=10"]`` (+ optional ``--params-json``) -> a params dict.

    Each value is JSON-decoded when possible (so ``10``/``true``/``[1,2]`` keep their type),
    else kept as a string. ``--params-json`` is merged first, then overridden by ``--param``s.
    """
    out: Dict[str, object] = {}
    if params_json:
        obj = json.loads(params_json)
        if isinstance(obj, dict):
            out.update(obj)
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"--param must be key=value, got {pair!r}")
        k, v = pair.split("=", 1)
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _dispatch(config: BridgeConfig, command: str, args) -> int:
    disp = Dispatcher(config)
    params = _parse_params(getattr(args, "param", None), getattr(args, "params_json", "") or "")
    result = asyncio.run(disp.handle(command, mac=getattr(args, "mac", None) or None,
                                     params=params, dry_run=args.dry_run,
                                     confirm=getattr(args, "confirm", False)))
    _print(result)
    return 0 if result.get("ok") else 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m gtx2_bridge",
                                description="Home Assistant ⇄ Starmax GTX2 bridge.")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--mac", help="target watch MAC (default: $GTX2_MAC)")
    sub = p.add_subparsers(dest="cmd", required=True)

    mp = sub.add_parser("manifest", help="print the command+sensor manifest (JSON) for the dashboard")
    mp.set_defaults(func=_cmd_manifest)

    cp = sub.add_parser("commands", help="list the catalog by safety tier")
    cp.set_defaults(func=_cmd_commands)

    sp = sub.add_parser("scan", help="scan for watches and print the registry")
    sp.add_argument("--timeout", type=float, default=8.0)
    sp.set_defaults(func=_cmd_scan)

    dp = sub.add_parser("send", help="dispatch ANY catalog command")
    dp.add_argument("command", help="catalog command name (see `manifest`)")
    dp.add_argument("--mac", default=argparse.SUPPRESS,
                    help="target watch MAC (also accepted before the subcommand)")
    dp.add_argument("--param", action="append", metavar="K=V", help="a command parameter (repeatable)")
    dp.add_argument("--params-json", help="params as a JSON object")
    dp.add_argument("--dry-run", action="store_true", help="build + print the frames, don't send")
    dp.add_argument("--confirm", action="store_true", help="required for DANGER (red) commands")
    dp.set_defaults(func=_cmd_send)

    # conveniences -> `send <name>`
    for name, extra in (("buzz", "find"), ("notify", "notify"),
                        ("set-time", "set-time"), ("weather", "weather")):
        c = sub.add_parser(name, help=f"convenience for `send {extra}`")
        c.add_argument("--mac", default=argparse.SUPPRESS, help="target watch MAC")
        c.add_argument("--param", action="append", metavar="K=V")
        c.add_argument("--params-json")
        c.add_argument("--dry-run", action="store_true")
        c.add_argument("--confirm", action="store_true")
        c.set_defaults(func=_cmd_send, _map_to=extra)

    rp = sub.add_parser("render", help="render a notification face to PNG and/or a dial blob (offline)")
    rp.add_argument("title")
    rp.add_argument("--body", default="")
    rp.add_argument("--footer", default="")
    rp.add_argument("--icon")
    rp.add_argument("--bg", default="#000000")
    rp.add_argument("--fg", default="#FFFFFF")
    rp.add_argument("--accent", default="#00E5FF")
    rp.add_argument("--png", help="write the rendered face PNG here")
    rp.add_argument("--blob", help="write the native dial blob here")
    rp.set_defaults(func=_cmd_render)

    vp = sub.add_parser("serve", help="run the MQTT bridge service")
    vp.set_defaults(func=_cmd_serve)

    bp = sub.add_parser("serve-blobs",
                        help="plain-HTTP render+serve blob endpoint for nodes (render role, no TLS)")
    bp.set_defaults(func=_cmd_serve_blobs)
    return p


# --------------------------------------------------------------------------- subcommands
def _cmd_manifest(config, args) -> int:
    from . import metrics
    m = catalog.manifest(topics=config.topics)
    m["sensors"] = metrics.sensor_manifest()
    m["topic_root"] = config.topic_root
    _print(m)
    return 0


def _cmd_commands(config, args) -> int:
    for tier in (catalog.GREEN, catalog.YELLOW, catalog.RED):
        names = catalog.by_tier(tier)
        print(f"\n{tier.upper()} ({len(names)}):")
        for n in names:
            c = catalog.CATALOG[n]
            flags = []
            if not c.ha_expose:
                flags.append("no-dashboard")
            if c.needs_confirm:
                flags.append("confirm")
            print(f"  {n:<16} {c.summary}" + (f"   [{', '.join(flags)}]" if flags else ""))
    return 0


def _cmd_scan(config, args) -> int:
    from .registry import Registry, scan_watches
    reg = Registry()
    found = asyncio.run(scan_watches(timeout=args.timeout))
    reg.merge_scan(found)
    _print(reg.as_payload())
    return 0


def _cmd_send(config, args) -> int:
    command = getattr(args, "_map_to", None) or args.command
    return _dispatch(config, command, args)


def _cmd_render(config, args) -> int:
    from . import faces
    face = faces.NotificationFace(title=args.title, body=args.body, footer=args.footer,
                                  icon=args.icon, bg=args.bg, fg=args.fg, accent=args.accent)
    img = faces.render_image(face)
    if args.png:
        img.save(args.png)
        print(f"wrote face PNG -> {args.png}")
    blob = faces.image_to_blob(img)
    if args.blob:
        with open(args.blob, "wb") as fh:
            fh.write(blob)
        print(f"wrote dial blob -> {args.blob}")
    print(f"blob: {len(blob)} bytes (push with: send dial-push --param file={args.blob or '<blob>'})")
    return 0


def _cmd_serve(config, args) -> int:
    from .mqtt_bridge import MqttBridge
    if config.uses_placeholder_mac():
        print("warning: GTX2_MAC is the placeholder; commands without an explicit mac will refuse "
              "live sends. Set GTX2_MAC to your watch.", file=sys.stderr)
    asyncio.run(MqttBridge(config).run())
    return 0


def _cmd_serve_blobs(config, args) -> int:
    """Run the plain-HTTP blob server (render role): nodes fetch face blobs over http:// so the
    ESP32-C3 avoids the ~40 KB TLS spike that crashes it on >~15 KB blobs. No command plane here."""
    from . import blobserver
    blobserver.serve(host=config.blob_host, port=config.blob_port,
                     blob_dir=config.blob_dir or None)
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    config = BridgeConfig.from_env()
    if getattr(args, "mac", None):
        config.mac = args.mac
    try:
        return args.func(config, args)
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001
        if args.verbose:
            raise
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
