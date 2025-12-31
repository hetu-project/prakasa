#!/usr/bin/env python3
"""
Parallax CLI - Command line interface for Parallax distributed LLM serving.

This module provides the main CLI entry point for Parallax, supporting
commands like 'run' and 'join' that mirror the functionality of the
bash scripts.
"""

import argparse
import base64
import json
import os
import signal
import subprocess
import sys

import requests
from pynostr.key import PrivateKey
try:
    import yaml
except Exception:
    yaml = None

from parallax_utils.file_util import get_project_root
from parallax_utils.logging_config import get_logger
from parallax_utils.version_check import get_current_version

logger = get_logger("parallax.cli")

PUBLIC_INITIAL_PEERS = [
    "/dns4/bootstrap-lattica.gradient.network/udp/18080/quic-v1/p2p/12D3KooWJHXvu8TWkFn6hmSwaxdCLy4ZzFwr4u5mvF9Fe2rMmFXb",
    "/dns4/bootstrap-lattica.gradient.network/tcp/18080/p2p/12D3KooWJHXvu8TWkFn6hmSwaxdCLy4ZzFwr4u5mvF9Fe2rMmFXb",
    "/dns4/bootstrap-lattica-us.gradient.network/udp/18080/quic-v1/p2p/12D3KooWFD8NoyHfmVxLVCocvXJBjwgE9RZ2bgm2p5WAWQax4FoQ",
    "/dns4/bootstrap-lattica-us.gradient.network/tcp/18080/p2p/12D3KooWFD8NoyHfmVxLVCocvXJBjwgE9RZ2bgm2p5WAWQax4FoQ",
    "/dns4/bootstrap-lattica-eu.gradient.network/udp/18080/quic-v1/p2p/12D3KooWCNuEF4ro95VA4Lgq4NvjdWfJFoTcvWsBA7Z6VkBByPtN",
    "/dns4/bootstrap-lattica-eu.gradient.network/tcp/18080/p2p/12D3KooWCNuEF4ro95VA4Lgq4NvjdWfJFoTcvWsBA7Z6VkBByPtN",
]

PUBLIC_RELAY_SERVERS = [
    "/dns4/relay-lattica.gradient.network/udp/18080/quic-v1/p2p/12D3KooWDaqDAsFupYvffBDxjHHuWmEAJE4sMDCXiuZiB8aG8rjf",
    "/dns4/relay-lattica.gradient.network/tcp/18080/p2p/12D3KooWDaqDAsFupYvffBDxjHHuWmEAJE4sMDCXiuZiB8aG8rjf",
    "/dns4/relay-lattica-us.gradient.network/udp/18080/quic-v1/p2p/12D3KooWHMXi6SCfaQzLcFt6Th545EgRt4JNzxqmDeLs1PgGm3LU",
    "/dns4/relay-lattica-us.gradient.network/tcp/18080/p2p/12D3KooWHMXi6SCfaQzLcFt6Th545EgRt4JNzxqmDeLs1PgGm3LU",
    "/dns4/relay-lattica-eu.gradient.network/udp/18080/quic-v1/p2p/12D3KooWRAuR7rMNA7Yd4S1vgKS6akiJfQoRNNexTtzWxYPiWfG5",
    "/dns4/relay-lattica-eu.gradient.network/tcp/18080/p2p/12D3KooWRAuR7rMNA7Yd4S1vgKS6akiJfQoRNNexTtzWxYPiWfG5",
]


def check_python_version():
    """Check if Python version is 3.11 or higher."""
    if sys.version_info < (3, 11) or sys.version_info >= (3, 14):
        logger.info(
            f"Error: Python 3.11 or higher and less than 3.14 is required. Current version is {sys.version_info.major}.{sys.version_info.minor}."
        )
        sys.exit(1)


def _flag_present(args_list: list[str], flag_names: list[str]) -> bool:
    """Return True if any of the given flags is present in args_list.

    Supports forms: "--flag value", "--flag=value", "-f value", "-f=value".
    """
    if not args_list:
        return False
    flags_set = set(flag_names)
    for i, token in enumerate(args_list):
        if token in flags_set:
            return True
        for flag in flags_set:
            if token.startswith(flag + "="):
                return True
    return False


def _find_flag_value(args_list: list[str], flag_names: list[str]) -> str | None:
    """Find the value for the first matching flag in args_list, if present.

    Returns the associated value for forms: "--flag value" or "--flag=value" or
    "-f value" or "-f=value". Returns None if not found or value is missing.
    """
    if not args_list:
        return None
    flags_set = set(flag_names)
    for i, token in enumerate(args_list):
        if token in flags_set:
            # expect value in next token if exists and is not another flag
            if i + 1 < len(args_list) and not args_list[i + 1].startswith("-"):
                return args_list[i + 1]
            return None
        for flag in flags_set:
            prefix = flag + "="
            if token.startswith(prefix):
                return token[len(prefix) :]
    return None


def _execute_with_graceful_shutdown(cmd: list[str], env: dict[str, str] | None = None) -> None:
    """Execute a command in a subprocess and handle graceful shutdown on Ctrl-C.

    This centralizes the common Popen + signal handling logic shared by
    run_command and join_command.
    """
    logger.info(f"Running command: {' '.join(cmd)}")

    sub_process = None
    try:
        # Start in a new session so we can signal the entire process group
        sub_process = subprocess.Popen(cmd, env=env, start_new_session=True)
        # Wait for the subprocess to finish
        return_code = sub_process.wait()
        if return_code != 0:
            logger.error(f"Command failed with exit code {return_code}")
            sys.exit(return_code)
    except KeyboardInterrupt:
        logger.info("Received interrupt signal, shutting down...")

        # If another Ctrl-C arrives during cleanup, force-kill the whole group immediately
        def _force_kill_handler(signum, frame):
            try:
                os.killpg(sub_process.pid, signal.SIGKILL)
            except Exception:
                try:
                    sub_process.kill()
                except Exception:
                    pass
            os._exit(130)

        try:
            signal.signal(signal.SIGINT, _force_kill_handler)
        except Exception:
            pass

        if sub_process is not None:
            try:
                logger.info("Terminating subprocess group...")
                # Gracefully terminate the entire process group
                try:
                    os.killpg(sub_process.pid, signal.SIGINT)
                except Exception:
                    # Fall back to signaling just the child process
                    sub_process.send_signal(signal.SIGINT)

                logger.info("Waiting for subprocess to exit...")
                # Wait for the subprocess to exit gracefully
                try:
                    sub_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.info("SIGINT timeout; sending SIGTERM to process group...")
                    try:
                        os.killpg(sub_process.pid, signal.SIGTERM)
                    except Exception:
                        sub_process.terminate()
                    try:
                        sub_process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        logger.info("SIGTERM timeout; forcing SIGKILL on process group...")
                        try:
                            os.killpg(sub_process.pid, signal.SIGKILL)
                        except Exception:
                            sub_process.kill()
                        sub_process.wait()
                logger.info("Subprocess exited.")
            except Exception as e:
                logger.error(f"Failed to terminate subprocess: {e}")
        else:
            logger.info("Subprocess not found, skipping shutdown...")
        sys.exit(0)


def _get_relay_params():
    return [
        "--relay-servers",
        *PUBLIC_RELAY_SERVERS,
        "--initial-peers",
        *PUBLIC_INITIAL_PEERS,
    ]


def load_and_merge_config(args, passthrough_args: list[str] | None = None):
    """Load YAML config (if provided) and merge values into parsed args.

    Command-line arguments take precedence over config file values.
    Returns (args, passthrough_args).
    """
    # Helper: check whether a flag was provided on the raw CLI (sys.argv)
    def _cli_flag_provided(flag_names: list[str]) -> bool:
        raw = sys.argv[1:]
        if not raw:
            return False
        flags_set = set(flag_names)
        for token in raw:
            if token in flags_set:
                return True
            for flag in flags_set:
                if token.startswith(flag + "="):
                    return True
        return False

    project_root = get_project_root()

    # Load YAML config (if provided either before or after subcommand).
    # If not explicitly provided, fall back to the default config.template.yaml.
    config_path = None
    if getattr(args, "config", None):
        config_path = args.config
    else:
        cfg = _find_flag_value(passthrough_args, ["--config"]) or _find_flag_value(sys.argv[1:], ["--config"]) if sys.argv else None
        if cfg:
            config_path = cfg
    if config_path is None:
        default_cfg = project_root / "config.template.yaml"
        if default_cfg.exists():
            config_path = str(default_cfg)

    config_data = None
    if config_path:
        if yaml is None:
            logger.error("PyYAML is required to load config files. Please install pyyaml.")
            sys.exit(1)
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config_data = yaml.safe_load(f) or {}
        except Exception as e:
            logger.error(f"Failed to load config file {config_path}: {e}")
            sys.exit(1)

    # Merge config values for the chosen subcommand when CLI did not provide them.
    if config_data and args.command:
        cmd_conf = config_data.get(args.command, {}) if isinstance(config_data, dict) else {}
        if not isinstance(cmd_conf, dict):
            cmd_conf = {}

        # Helper to pull nested Nostr config
        nostr_conf = cmd_conf.get("nostr") if isinstance(cmd_conf, dict) else None
        if nostr_conf is None:
            nostr_conf = {}

        # run command options
        if args.command == "run":
            if not _cli_flag_provided(["-m", "--model-name"]) and "model_name" in cmd_conf:
                args.model_name = cmd_conf.get("model_name")
            if not _cli_flag_provided(["--log-level"]) and "log_level" in cmd_conf:
                args.log_level = cmd_conf.get("log_level")
            if not _cli_flag_provided(["-n", "--init-nodes-num"]) and "init_nodes_num" in cmd_conf:
                try:
                    args.init_nodes_num = int(cmd_conf.get("init_nodes_num"))
                except Exception:
                    args.init_nodes_num = cmd_conf.get("init_nodes_num")
            if not _cli_flag_provided(["-r", "--use-relay"]) and "use_relay" in cmd_conf:
                args.use_relay = bool(cmd_conf.get("use_relay"))

            # Nostr options for scheduler
            if not _cli_flag_provided(["--nostr-privkey"]) and "privkey" in nostr_conf:
                setattr(args, "nostr_privkey", nostr_conf.get("privkey"))
            if not _cli_flag_provided(["--nostr-relay"]) and "relays" in nostr_conf:
                setattr(args, "nostr_relays", nostr_conf.get("relays") or [])

            # passthrough: e.g., --port
            if "port" in cmd_conf and not _flag_present(passthrough_args, ["--port"]):
                passthrough_args = passthrough_args or []
                passthrough_args.extend(["--port", str(cmd_conf.get("port"))])
            # passthrough: host (e.g., 0.0.0.0)
            if (
                "host" in cmd_conf
                and not _flag_present(passthrough_args, ["--host"])
                and not _cli_flag_provided(["--host"])
            ):
                passthrough_args = passthrough_args or []
                passthrough_args.extend(["--host", str(cmd_conf.get("host"))])

        # join command options
        if args.command == "join":
            if not _cli_flag_provided(["-s", "--scheduler-addr"]) and "scheduler_addr" in cmd_conf:
                args.scheduler_addr = cmd_conf.get("scheduler_addr")
            if not _cli_flag_provided(["--log-level"]) and "log_level" in cmd_conf:
                args.log_level = cmd_conf.get("log_level")
            if not _cli_flag_provided(["-r", "--use-relay"]) and "use_relay" in cmd_conf:
                args.use_relay = bool(cmd_conf.get("use_relay"))
            if not _cli_flag_provided(["--account"]) and "account" in cmd_conf:
                args.account = cmd_conf.get("account")

            # Nostr options for worker
            if not _cli_flag_provided(["--nostr-privkey"]) and "privkey" in nostr_conf:
                setattr(args, "nostr_privkey", nostr_conf.get("privkey"))
            if not _cli_flag_provided(["--nostr-relay"]) and "relays" in nostr_conf:
                setattr(args, "nostr_relays", nostr_conf.get("relays") or [])

            # join passthrough defaults (max-num-tokens-per-batch, etc.)
            join_passthrough_flags = [
                ("--max-num-tokens-per-batch", "max_num_tokens_per_batch"),
                ("--max-sequence-length", "max_sequence_length"),
                ("--max-batch-size", "max_batch_size"),
                ("--kv-block-size", "kv_block_size"),
            ]
            for flag, key in join_passthrough_flags:
                if key in cmd_conf and not _flag_present(passthrough_args, [flag]):
                    passthrough_args = passthrough_args or []
                    passthrough_args.extend([flag, str(cmd_conf.get(key))])

        # chat command options
        if args.command == "chat":
            if not _cli_flag_provided(["-s", "--scheduler-addr"]) and "scheduler_addr" in cmd_conf:
                args.scheduler_addr = cmd_conf.get("scheduler_addr")
            if not _cli_flag_provided(["-r", "--use-relay"]) and "use_relay" in cmd_conf:
                args.use_relay = bool(cmd_conf.get("use_relay"))
            if not _cli_flag_provided(["--log-level"]) and "log_level" in cmd_conf:
                args.log_level = cmd_conf.get("log_level")

            # Nostr options for chat client
            if not _cli_flag_provided(["--nostr-privkey"]) and "privkey" in nostr_conf:
                setattr(args, "nostr_privkey", nostr_conf.get("privkey"))
            if not _cli_flag_provided(["--nostr-relay"]) and "relays" in nostr_conf:
                setattr(args, "nostr_relays", nostr_conf.get("relays") or [])

    # If no Nostr private key was provided anywhere, lazily generate one and
    # persist it back into the current config file so future runs are stable.
    default_cfg_path = project_root / "config.template.yaml"
    using_default_cfg = config_path is not None and str(default_cfg_path) == str(config_path)

    if not using_default_cfg and getattr(args, "nostr_privkey", None) is None:
        if config_data is None:
            config_data = {}
        cmd_section = config_data.setdefault(args.command, {})
        nostr_section = cmd_section.setdefault("nostr", {})
        if not nostr_section.get("privkey"):
            pk = PrivateKey()
            nostr_section["privkey"] = pk.bech32()
            nostr_section["pubkey"] = pk.public_key.bech32()
            setattr(args, "nostr_privkey", nostr_section["privkey"])
            # Ensure relays array exists
            nostr_section.setdefault("relays", ["wss://nostr.parallel.hetu.org:8443"])

            try:
                if yaml is not None:
                    with open(config_path, "w", encoding="utf-8") as f:
                        yaml.safe_dump(config_data, f, sort_keys=False, allow_unicode=True)
            except Exception as e:
                logger.warning(f"Failed to persist generated Nostr key to {config_path}: {e}")

    return args, passthrough_args


def run_command(args, passthrough_args: list[str] | None = None):
    """Run the scheduler (equivalent to scripts/start.sh)."""
    # if not args.skip_upload:
    #     update_package_info()
    check_python_version()

    project_root = get_project_root()
    backend_main = project_root / "src" / "backend" / "main.py"

    if not backend_main.exists():
        logger.info(f"Error: Backend main.py not found at {backend_main}")
        sys.exit(1)

    # Build the command to run the backend main.py
    passthrough_args = passthrough_args or []
    cmd = [sys.executable, str(backend_main)]
    if not _flag_present(passthrough_args, ["--port"]):
        cmd.extend(["--port", "3001"])

    # Add optional arguments if provided
    if args.model_name:
        cmd.extend(["--model-name", args.model_name])
    if args.log_level:
        cmd.extend(["--log-level", args.log_level])
    if args.init_nodes_num:
        cmd.extend(["--init-nodes-num", str(args.init_nodes_num)])
    if args.use_relay:
        cmd.extend(_get_relay_params())
        logger.info(
            "Using public relay server to help nodes and the scheduler establish a connection (remote mode). Your IP address will be reported to the relay server to help establish the connection."
        )

    # Nostr options for scheduler backend
    nostr_privkey = getattr(args, "nostr_privkey", None)
    nostr_relays = getattr(args, "nostr_relays", None) or []
    if nostr_privkey:
        cmd.extend(["--nostr-privkey", nostr_privkey])
    for r in nostr_relays:
        cmd.extend(["--nostr-relay", r])

    # Append any passthrough args (unrecognized by this CLI) directly to the command
    if passthrough_args:
        cmd.extend(passthrough_args)

    _execute_with_graceful_shutdown(cmd)


def join_command(args, passthrough_args: list[str] | None = None):
    """Join a distributed cluster (equivalent to scripts/join.sh)."""
    # if not args.skip_upload:
    #     update_package_info()

    check_python_version()

    project_root = get_project_root()
    launch_script = project_root / "src" / "parallax" / "launch.py"

    if not launch_script.exists():
        logger.info(f"Error: Launch script not found at {launch_script}")
        sys.exit(1)

    # Set environment variable for the subprocess
    env = os.environ.copy()
    env["SGLANG_ENABLE_JIT_DEEPGEMM"] = "0"

    # Build the command to run the launch.py script
    passthrough_args = passthrough_args or []

    cmd = [sys.executable, str(launch_script)]
    if not _flag_present(passthrough_args, ["--max-num-tokens-per-batch"]):
        cmd.extend(["--max-num-tokens-per-batch", "4096"])
    if not _flag_present(passthrough_args, ["--max-sequence-length"]):
        cmd.extend(["--max-sequence-length", "2048"])
    if not _flag_present(passthrough_args, ["--max-batch-size"]):
        cmd.extend(["--max-batch-size", "8"])
    if not _flag_present(passthrough_args, ["--kv-block-size"]):
        cmd.extend(["--kv-block-size", "32"])

    # The scheduler address is now taken directly from the parsed arguments.
    cmd.extend(["--scheduler-addr", args.scheduler_addr])


    if args.account is not None:
        cmd.extend(["--account", args.account])

    if args.log_level:
        cmd.extend(["--log-level", args.log_level])

    # Nostr options for worker node
    nostr_privkey = getattr(args, "nostr_privkey", None)
    nostr_relays = getattr(args, "nostr_relays", None) or []
    if nostr_privkey:
        cmd.extend(["--nostr-privkey", nostr_privkey])
    for r in nostr_relays:
        cmd.extend(["--nostr-relay", r])

    # Relay logic based on effective scheduler address
    if args.use_relay or (
        args.scheduler_addr != "auto" and not str(args.scheduler_addr).startswith("/")
    ):
        cmd.extend(_get_relay_params())
        logger.info(
            "Using public relay server to help nodes and the scheduler establish a connection (remote mode). Your IP address will be reported to the relay server to help establish the connection."
        )

    # Append any passthrough args (unrecognized by this CLI) directly to the command
    if passthrough_args:
        cmd.extend(passthrough_args)

    logger.info(f"Scheduler address: {args.scheduler_addr}")
    _execute_with_graceful_shutdown(cmd, env=env)


def chat_command(args, passthrough_args: list[str] | None = None):
    """Start the Prakasa chat server (equivalent to scripts/chat.sh)."""
    check_python_version()

    project_root = get_project_root()
    launch_script = project_root / "src" / "parallax" / "launch_chat.py"

    if not launch_script.exists():
        logger.info(f"Error: Launch chat script not found at {launch_script}")
        sys.exit(1)

    # Build the command to run the launch_chat.py script
    passthrough_args = passthrough_args or []
    cmd = [sys.executable, str(launch_script)]

    cmd.extend(["--scheduler-addr", args.scheduler_addr])

    if args.log_level:
        cmd.extend(["--log-level", args.log_level])

    # Relay logic based on effective scheduler address
    if args.use_relay or (
        args.scheduler_addr != "auto" and not str(args.scheduler_addr).startswith("/")
    ):
        cmd.extend(_get_relay_params())
        logger.info(
            "Using public relay server to help chat client and the scheduler establish a connection (remote mode). Your IP address will be reported to the relay server to help establish the connection."
        )

    # Append any passthrough args (unrecognized by this CLI) directly to the command
    if passthrough_args:
        cmd.extend(passthrough_args)

    logger.info(f"Scheduler address: {args.scheduler_addr}")
    _execute_with_graceful_shutdown(cmd)


def update_package_info():
    """Update package information."""
    version = get_current_version()

    try:
        package_info = load_package_info()
        if package_info is not None and package_info["version"] == version:
            return

        save_package_info({"version": version})
    except Exception:
        pass


def load_package_info():
    """Load package information."""
    try:
        project_root = get_project_root()
        if not (project_root / ".cache" / "tmp_key.txt").exists():
            return None
        with open(project_root / ".cache" / "tmp_key.txt", "r") as f:
            return json.loads(reversible_decode_string(f.read()))
    except Exception:
        return None


def save_package_info(usage_info: dict):
    """Save package information."""
    project_root = get_project_root()
    os.makedirs(project_root / ".cache", exist_ok=True)
    with open(project_root / ".cache" / "tmp_key.txt", "w") as f:
        f.write(reversible_encode_string(json.dumps(usage_info)))

    upload_package_info(usage_info)


def upload_package_info(usage_info: dict):
    post_url = "https://chatbe-dev.gradient.network/api/v1/parallax/upload"
    headers = {
        "Content-Type": "application/json",
    }
    try:
        requests.post(post_url, headers=headers, json=usage_info, timeout=5)
        return
    except Exception:
        return


def reversible_encode_string(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("utf-8")


def reversible_decode_string(encoded: str) -> str:
    return base64.urlsafe_b64decode(encoded.encode("utf-8")).decode("utf-8")


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Prakasa - A decentralized, privacy-preserving P2P GPU inference network built on Parallax",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  prakasa run                                                          # Start scheduler with frontend
  prakasa run -m {model-name} -n {number-of-worker-nodes}              # Start scheduler without frontend
  prakasa run -m Qwen/Qwen3-0.6B -n 2                                  # example
  prakasa join                                                         # Join cluster in local network
  prakasa join -s {scheduler-address}                                  # Join cluster in public network
  prakasa join -s 12D3KooWLX7MWuzi1Txa5LyZS4eTQ2tPaJijheH8faHggB9SxnBu # example
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Global config file (YAML). Also added to subparsers below so it can appear
    # after the subcommand on the CLI as well.
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to YAML config file (values used when CLI doesn't provide them)",
    )

    # Add 'run' command parser
    run_parser = subparsers.add_parser(
        "run", help="Start the Prakasa scheduler (equivalent to scripts/start.sh)"
    )
    run_parser.add_argument("-n", "--init-nodes-num", type=int, help="Number of initial nodes")
    run_parser.add_argument("-m", "--model-name", type=str, help="Model name")
    run_parser.add_argument(
        "-r", "--use-relay", action="store_true", help="Use public relay servers"
    )
    run_parser.add_argument(
        "-u", "--skip-upload", action="store_true", help="Skip upload package info"
    )
    run_parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to YAML config file (values used when CLI doesn't provide them)",
    )
    run_parser.add_argument(
        "--nostr-privkey",
        type=str,
        default=None,
        help="Hex-encoded Nostr private key for the scheduler node",
    )
    run_parser.add_argument(
        "--nostr-relay",
        dest="nostr_relays",
        action="append",
        default=None,
        help="Nostr relay URL (can be specified multiple times)",
    )

    # Add 'join' command parser
    join_parser = subparsers.add_parser(
        "join", help="Join a distributed cluster (equivalent to scripts/join.sh)"
    )
    join_parser.add_argument(
        "-s",
        "--scheduler-addr",
        default="auto",
        type=str,
        help="Scheduler address (required)",
    )
    join_parser.add_argument(
        "-r", "--use-relay", action="store_true", help="Use public relay servers"
    )
    join_parser.add_argument(
        "-u", "--skip-upload", action="store_true", help="Skip upload package info"
    )
    join_parser.add_argument(
        "--account",
        type=str,
        default=None,
        help="EVM address for the worker node (e.g., 0x789...)",
    )
    join_parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to YAML config file (values used when CLI doesn't provide them)",
    )
    join_parser.add_argument(
        "--nostr-privkey",
        type=str,
        default=None,
        help="Hex-encoded Nostr private key for the worker node",
    )
    join_parser.add_argument(
        "--nostr-relay",
        dest="nostr_relays",
        action="append",
        default=None,
        help="Nostr relay URL (can be specified multiple times)",
    )

    # Add 'chat' command parser
    chat_parser = subparsers.add_parser(
        "chat", help="Start the Prakasa chat server (equivalent to scripts/chat.sh)"
    )
    chat_parser.add_argument(
        "-s",
        "--scheduler-addr",
        default="auto",
        type=str,
        help="Scheduler address (required)",
    )
    chat_parser.add_argument(
        "-r", "--use-relay", action="store_true", help="Use public relay servers"
    )
    chat_parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to YAML config file (values used when CLI doesn't provide them)",
    )
    chat_parser.add_argument(
        "--nostr-privkey",
        type=str,
        default=None,
        help="Hex-encoded Nostr private key for the chat client",
    )
    chat_parser.add_argument(
        "--nostr-relay",
        dest="nostr_relays",
        action="append",
        default=None,
        help="Nostr relay URL (can be specified multiple times)",
    )

    # Accept unknown args and pass them through to the underlying python command
    args, passthrough_args = parser.parse_known_args()

    # Load and merge configuration (from YAML) into parsed args and passthrough_args
    args, passthrough_args = load_and_merge_config(args, passthrough_args)


    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "run":
        run_command(args, passthrough_args)
    elif args.command == "join":
        join_command(args, passthrough_args)
    elif args.command == "chat":
        chat_command(args, passthrough_args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
